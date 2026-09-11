"""Generate evaluation query set from the real database.

Usage:
    python eval/gen_eval_set.py [--limit N] [--seed S] [--resume] [--dry-run]

Must set DB_PATH env var BEFORE this script runs (it's set at the top).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Project root on sys.path ────────────────────────────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

# ── Copy DB to safe location BEFORE importing project modules ───────────────
_EVAL_DIR = Path(__file__).resolve().parent
_TMP_DIR = _EVAL_DIR / ".tmp"
_DB_PATH_ENV = os.getenv("DB_PATH", "")

_ORIG_DB_PATH: Path
_ORIG_FTS_DB: Path


def _locate_originals() -> tuple[Path, Path]:
    if _DB_PATH_ENV and os.path.isabs(_DB_PATH_ENV):
        db = Path(_DB_PATH_ENV)
    elif _DB_PATH_ENV:
        db = Path(_PROJECT_ROOT) / _DB_PATH_ENV
    else:
        db = Path(_PROJECT_ROOT) / "data" / "mh_memory.db"
    fts = db.parent / "fts.db"
    return db, fts


_ORIG_DB_PATH, _ORIG_FTS_DB = _locate_originals()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_db_to_tmp() -> Path:
    """Copy DB + sidecars + FTS into .tmp/.  Set DB_PATH and return tmp dir."""
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _TMP_DIR

    # Copy main DB + all sidecar files
    str(_ORIG_DB_PATH)
    for p in _ORIG_DB_PATH.parent.iterdir():
        if p.name.startswith(_ORIG_DB_PATH.name) and p.is_file():
            dst = tmp / p.name
            shutil.copy2(p, dst)

    # Copy FTS db
    if _ORIG_FTS_DB.exists():
        shutil.copy2(_ORIG_FTS_DB, tmp / _ORIG_FTS_DB.name)

    # Point DB_PATH to copy
    tmp_db = tmp / _ORIG_DB_PATH.name
    os.environ["DB_PATH"] = str(tmp_db)
    return tmp


_copy_db_to_tmp()

# Now safe to import project modules
sys.path.insert(0, str(_EVAL_DIR))
from pool_filter import filter_pool, should_write_output  # noqa: E402

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

# ── Constants ───────────────────────────────────────────────────────────────

KIND_SEMANTIC = "semantic"
KIND_ENTITY = "entity"
KIND_NEGATIVE = "negative"

_LAYER_HERMES = "hermes"
_LAYER_KB = "kb"  # 与评测报告分组键一致（防分层表错位）
_LAYER_NOVEL = "novel"
_LAYER_OTHER = "other"

_HERMES_TYPES = {"memory", "record", "correction", "plan", "decision"}
_NOVEL_TYPES = {"novel_chunk", "character_state", "plot_plan"}
_TASK_RULE_TYPES = {"task", "rule"}

# 会话末自动提炼的转录片段，不适合作为评测基准答案
_DEFAULT_EXCLUDED_SOURCES = {"hermes-session_end"}

_NEGATIVE_QUERIES = [
    "特斯拉2025年最新的全自动驾驶版本号是什么",
    "今天北京的空气质量指数是多少",
    "特朗普在2026年签署了什么新的行政令",
    "Palimpsest项目的部署服务器密码是什么",
    "你昨天晚上吃了什么晚餐",
    "你的银行账户余额有多少",
    "今年诺贝尔物理学奖得主是谁",
    "你计划什么时候去南极旅行",
    "项目中使用了哪个版本的Redis",
    "你养的猫叫什么名字",
    "2026年世界杯决赛的比分是多少",
    "你最近一次出国去了哪个国家",
    "项目CI部署的SSH密钥是什么",
    "你的身高体重分别是多少",
    "明天上海的天气预报怎么样",
    "你的社保卡号是多少",
    "项目使用的云服务商月账单金额",
    "你上周和谁开了会",
    "你手机里最近安装了什么App",
    "项目数据库的root密码是什么",
]


def _categorize_node(payload: dict) -> str:
    ntype = payload.get("type", "unknown")
    domain = payload.get("domain", "general")
    if domain == "hermes" and ntype in _HERMES_TYPES:
        return _LAYER_HERMES
    if ntype == "kb_chunk":
        return _LAYER_KB
    if domain == "novel" or ntype in _NOVEL_TYPES:
        return _LAYER_NOVEL
    return _LAYER_OTHER


def _longest_common_substring(a: str, b: str) -> int:
    """Return length of the longest common contiguous substring."""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    # Optimised O(min(m,n)) space
    if m < n:
        a, b = b, a
        m, n = n, m
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best:
                    best = curr[j]
        prev = curr
    return best


def _check_longest_substring(query: str, content: str, limit: int = 6) -> bool:
    """Return True if query shares >= limit consecutive chars with content."""
    return _longest_common_substring(query, content) >= limit


# 本次运行的 LLM 用量累计（成本可观测：出题真实消耗多少 token）
_USAGE = {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0}


def _call_deepseek(prompt: str, max_retries: int = 1) -> str | None:
    """Call DeepSeek chat completions API. Returns response content or None."""
    url = f"{Config.DEEPSEEK_BASE_URL}/chat/completions"
    body = json.dumps({
        "model": Config.DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 8192,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
    }

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            u = data.get("usage") or {}
            _USAGE["calls"] += 1
            _USAGE["prompt"] += int(u.get("prompt_tokens") or 0)
            _USAGE["completion"] += int(u.get("completion_tokens") or 0)
            det = u.get("completion_tokens_details") or {}
            _USAGE["reasoning"] += int(det.get("reasoning_tokens") or 0)
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001 —— DeepSeek 调用失败打印告警按策略重试
            print(f"  [WARN] DeepSeek call failed (attempt {attempt+1}): {e}")
            if attempt < max_retries:
                time.sleep(2)
    return None


def _generate_batch(
    batch: list[dict],
    rng: random.Random,
) -> list[dict] | None:
    """Generate queries for a batch of 5 nodes. Returns list of items or None."""
    lines = []
    for idx, item in enumerate(batch):
        content = item["payload"].get("content", "")
        # Truncate to first 300 chars for API
        snippet = content[:300].replace("\n", " ")
        lines.append(f"idx={idx}: {snippet}")

    prompt = (
        "你是一个检索质量评测助手。下面给你若干条从数据库中抽取的真实记忆节点，"
        "请为每条生成一条中文查询（模拟真实用户向助手提问的口吻，8–25字）。\n\n"
        "要求：\n"
        "- 交替产出两类查询：\n"
        "  奇数条(idx 0,2,4,...) kind=\"semantic\"：口语化改写，不带专业术语黑话\n"
        "  偶数条(idx 1,3,...) kind=\"entity\"：包含关键实体/术语\n"
        "- **严禁复制原文中连续≥6个字的短语**——用自己的话重新表述\n"
        "- 返回严格JSON：{\"items\":[{\"idx\":0,\"query\":\"...\"},...]}\n"
        "- 只输出JSON，不要任何其他文字\n\n"
        "节点内容：\n" + "\n".join(lines)
    )

    raw = _call_deepseek(prompt)
    if raw is None:
        return None

    # Extract JSON from response (may be wrapped in ```json ... ```)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(raw)
        items = parsed.get("items", [])
        if len(items) != len(batch):
            print(f"  [WARN] Expected {len(batch)} items, got {len(items)}")
            return None
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [WARN] JSON parse failed: {e}")
        return None

    # Validate and enforce substring constraint
    result = []
    skipped = 0
    for pos, (it, node) in enumerate(zip(items, batch, strict=False)):
        query = it["query"]
        content = node["payload"].get("content", "")
        # kind 由脚本按批内位置强制分配（模型不返回该字段，依赖它会导致全 semantic）
        kind = KIND_SEMANTIC if pos % 2 == 0 else KIND_ENTITY

        if _check_longest_substring(query, content):
            # Retry once with a more explicit prompt
            retry_prompt = (
                f"换一种说法，不要与以下原文有任何连续6字以上的重复：\n"
                f"原文：{content[:200]}\n"
                f"你之前的回答：{query}\n"
                f"请直接给出新的查询（8-25字中文），只输出查询本身。"
            )
            retry_raw = _call_deepseek(retry_prompt, max_retries=0)
            if retry_raw:
                query = retry_raw.strip().strip('"').strip("'")
                if _check_longest_substring(query, content):
                    # 仍超限 → 丢弃该题（绝不用原文拼题：那会造成最严重的词面泄露）
                    print(f"  [SKIP] node {node['node_id']} 生成题仍与原文重合 >=6 字，丢弃")
                    skipped += 1
                    continue

        result.append({
            "query": query,
            "kind": kind,
            "gold_ids": [node["node_id"]],
            "gold_type": node["payload"].get("type", "unknown"),
            "gold_domain": node["payload"].get("domain", "general"),
            "layer": _categorize_node(node["payload"]),
            "source_content": content[:200],
        })

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate eval query set")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only generate first N questions (0=all)")
    parser.add_argument("--seed", type=int, default=20260910,
                        help="Random seed for reproducibility")
    parser.add_argument("--resume", action="store_true",
                        help="Skip existing qids and continue")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only sample, print distribution, no API calls")
    parser.add_argument("--exclude-sources", type=str,
                        default="hermes-session_end",
                        help="逗号分隔的 source 值，命中的节点不入池；"
                             "显式传空字符串表示不过滤")
    parser.add_argument("--keep-duplicates", action="store_true",
                        help="开启后不做内容去重（默认做去重）")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    print(f"[gen_eval_set] Seed: {args.seed}")
    print(f"[gen_eval_set] DB副本路径: {os.environ.get('DB_PATH', 'N/A')}")

    # ── Collect active nodes ─────────────────────────────────────────────
    store = TriviumStore()
    nodes: list[dict] = []
    for node_id, payload in store.iter_payloads():
        if not payload:
            continue
        if payload.get("status") == "outdated":
            continue
        nodes.append({"node_id": node_id, "payload": payload})
    print(f"[gen_eval_set] Active nodes: {len(nodes)}")

    # ── Pool filter ──────────────────────────────────────────────────────
    exclude_src_arg = args.exclude_sources
    if exclude_src_arg == "":
        exclude_sources: tuple[str, ...] = ()
    else:
        exclude_sources = tuple(s.strip() for s in exclude_src_arg.split(",") if s.strip())
    nodes, pool_stats = filter_pool(
        nodes,
        exclude_sources=exclude_sources,
        drop_duplicates=not args.keep_duplicates,
    )
    print(f"[gen_eval_set] Pool filter: input={pool_stats['input']} "
          f"excluded_source={pool_stats['excluded_source']} "
          f"excluded_duplicate={pool_stats['excluded_duplicate']} "
          f"kept={pool_stats['kept']}")

    # ── Stratified sampling ──────────────────────────────────────────────
    layers: dict[str, list[dict]] = {
        _LAYER_HERMES: [],
        _LAYER_KB: [],
        _LAYER_NOVEL: [],
        _LAYER_OTHER: [],
    }
    for n in nodes:
        layer = _categorize_node(n["payload"])
        layers[layer].append(n)

    targets_per_layer = {
        _LAYER_HERMES: 40,
        _LAYER_KB: 40,
        _LAYER_NOVEL: 20,
        _LAYER_OTHER: 20,
    }

    sampled: list[dict] = []
    shortfall_detail: dict[str, int] = {}

    for layer, target in targets_per_layer.items():
        pool = layers[layer]
        if len(pool) >= target:
            chosen = rng.sample(pool, target)
        else:
            chosen = list(pool)
            shortfall_detail[layer] = target - len(pool)
        sampled.extend(chosen)

    # Fill shortfall from other layers
    if shortfall_detail:
        remaining = [n for n in nodes if n not in sampled]
        rng.shuffle(remaining)
        for _, deficit in shortfall_detail.items():
            fill = remaining[:deficit]
            sampled.extend(fill)
            remaining = remaining[deficit:]

    rng.shuffle(sampled)
    print(f"[gen_eval_set] Sampled positive targets: {len(sampled)}")

    # ── Print distribution ───────────────────────────────────────────────
    dist: dict[str, int] = {}
    for n in sampled:
        layer = _categorize_node(n["payload"])
        dist[layer] = dist.get(layer, 0) + 1
    print("[gen_eval_set] Layer distribution:")
    for layer, count in sorted(dist.items()):
        print(f"  {layer}: {count}")

    # ── Dry-run exit ─────────────────────────────────────────────────────
    if args.dry_run:
        print("[gen_eval_set] Dry-run complete. No API calls made.")
        print(f"[gen_eval_set] Negative samples would be: {len(_NEGATIVE_QUERIES)}")
        return 0

    # ── Load existing if resuming ────────────────────────────────────────
    output_path = _EVAL_DIR / "eval_set.json"
    existing_items: list[dict] = []
    existing_qids: set[str] = set()
    if args.resume and output_path.exists():
        with open(output_path, encoding="utf-8") as f:
            data = json.load(f)
        existing_items = data.get("items", [])
        existing_qids = {it["qid"] for it in existing_items}
        print(f"[gen_eval_set] Resuming: {len(existing_qids)} existing items")

    # ── Generate queries via API ─────────────────────────────────────────
    BATCH_SIZE = 5
    all_items = list(existing_items)
    qid_counter = len(existing_items) + 1
    fail_count = 0
    success_count = 0
    limit = args.limit

    # resume：已被出过题的 target 节点跳过，避免重复生成
    done_targets: set[int] = set()
    for it in existing_items:
        done_targets.update(it.get("gold_ids") or [])
    if done_targets:
        before = len(sampled)
        sampled = [n for n in sampled if n["node_id"] not in done_targets]
        print(f"[gen_eval_set] Resume: 跳过 {before - len(sampled)} 个已覆盖 target，"
              f"剩余 {len(sampled)} 个")

    batches = [sampled[i:i + BATCH_SIZE] for i in range(0, len(sampled), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        # Check limit
        if limit and success_count >= limit:
            break

        print(f"[gen_eval_set] Batch {batch_idx+1}/{len(batches)} "
              f"(nodes {[n['node_id'] for n in batch]})")

        results = _generate_batch(batch, rng)
        if results is None:
            fail_count += 1
            print(f"  [FAIL] Batch {batch_idx+1} failed entirely")
            continue

        for res in results:
            qid = f"q{qid_counter:03d}"
            qid_counter += 1

            item = {
                "qid": qid,
                "query": res["query"],
                "kind": res["kind"],
                "gold_ids": res["gold_ids"],
                "gold_type": res["gold_type"],
                "gold_domain": res["gold_domain"],
                "layer": res["layer"],
            }
            all_items.append(item)
            success_count += 1

        time.sleep(0.5)  # rate-limit

    # ── Add negative samples (Problem 4: dedup on resume) ──────────────
    existing_neg_queries = {it["query"] for it in all_items
                            if it.get("kind") == KIND_NEGATIVE}
    neg_count = sum(1 for it in all_items if it.get("kind") == KIND_NEGATIVE)
    target_neg = 20
    for neg_query in _NEGATIVE_QUERIES:
        if neg_count >= target_neg:
            break
        if neg_query in existing_neg_queries:
            continue
        qid = f"q{qid_counter:03d}"
        qid_counter += 1
        all_items.append({
            "qid": qid,
            "query": neg_query,
            "kind": KIND_NEGATIVE,
            "gold_ids": [],
            "gold_type": "negative",
            "gold_domain": "negative",
            "layer": "negative",
        })
        existing_neg_queries.add(neg_query)
        neg_count += 1

    # ── Guard: refuse to overwrite when nothing was generated ───────────
    allow, reason = should_write_output(success_count, existing_items, all_items)
    if not allow:
        print(f"\n  ⚠  BLOCKED: {reason}")
        print(f"     target file: {output_path}")
        print("     已有题集未被修改。")
        return 2

    # ── Write output (atomic) ───────────────────────────────────────────
    output = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "items": all_items,
    }
    tmp_path = output_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, output_path)

    total = _USAGE["prompt"] + _USAGE["completion"]
    print(f"[gen_eval_set] Token 消耗: calls={_USAGE['calls']} "
          f"prompt={_USAGE['prompt']} completion={_USAGE['completion']} "
          f"(reasoning={_USAGE['reasoning']}) total={total}")
    print(f"[gen_eval_set] Done. Success: {success_count}, Failed batches: {fail_count}")
    print(f"[gen_eval_set] Output: {output_path}")

    # Final distribution
    final_dist: dict[str, int] = {}
    for it in all_items:
        final_dist[it["kind"]] = final_dist.get(it["kind"], 0) + 1
    print("[gen_eval_set] Final distribution:")
    for kind, count in sorted(final_dist.items()):
        print(f"  {kind}: {count}")

    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
