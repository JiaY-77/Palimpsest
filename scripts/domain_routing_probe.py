"""查询侧自动域路由的离线评测探针（域过滤 / 软加权，不改生产链路）。

背景：域内过滤（把候选限制在目标域）曾是跨层竞争的缓解手段，旧排序下 hermes 层
R@5 0.579 → 0.632（+5.3pp，见 eval/docs/05）。但那是元数据硬加权时代的数字，
当前是干净语义序，需重测；且真实场景里**域要自动推断**（用户不会手填 domain），
推断错误会直接变成 miss，所以必须同时量「oracle 天花板」与「自动推断的实际效果」。

变体（同库副本 / 同题集 / 同 embedding 模型，严格单变量）：
  base          全库排序，不过滤
  oracle_hard   用真实域硬过滤候选（天花板，不可上线，只用来量收益空间）
  llm_hard      用本地生成模型推断域后硬过滤
  maj_hard      用「全库 top-10 候选的多数域」推断后硬过滤
  maj_soft      用多数域给同域候选加小分（不过滤，同 ε 级软加权思路）
  llm_soft      用模型推断域给同域候选加小分

域分组：payload.domain 归并成 5 类（rule→hermes、由佳→novel、tms/work→task、其余原样）。

用法：
  venv/Scripts/python.exe scripts/domain_routing_probe.py [--limit N] [--model qwen2.5:7b-instruct]
                                                          [--boost 0.02] [--rebuild]
输出：eval/.tmp/domain_routing_probe.json
缓存：eval/.tmp/domain_routing_cache.json（LLM 分类结果）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

EVAL_DIR = ROOT / "eval"
TMP = EVAL_DIR / ".tmp"
TMP.mkdir(parents=True, exist_ok=True)

_env_db = os.getenv("DB_PATH", "")
ORIG_DB = (Path(_env_db) if os.path.isabs(_env_db) else ROOT / _env_db) if _env_db \
    else ROOT / "data" / "mh_memory.db"
ORIG_FTS = ORIG_DB.parent / "fts.db"
CACHE = TMP / "domain_routing_cache.json"

GROUPS = ("hermes", "kb", "novel", "task", "general")
GROUP_MAP = {"hermes": "hermes", "rule": "hermes", "kb": "kb",
             "novel": "novel", "由佳": "novel",
             "task": "task", "tms": "task", "work": "task", "general": "general"}

PROMPT = (
    "你是检索域分类器。把用户查询归入下面 5 个域之一：\n"
    "- hermes: 助手自身的记忆、规则、配置、模型与工具设置、运维记录\n"
    "- kb: 知识库中的文档、笔记、技术资料、学习记录\n"
    "- novel: 小说创作、人物设定、剧情\n"
    "- task: 任务记录、项目进展、待办事项\n"
    "- general: 以上都不是\n"
    "只输出一个标签（hermes/kb/novel/task/general），不要任何解释。\n"
    "用户查询：{q}\n标签："
)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


pre_sha = sha256(ORIG_DB)
for p in sorted(ORIG_DB.parent.iterdir()):
    if p.name.startswith(ORIG_DB.name) and p.is_file():
        try:
            shutil.copy2(p, TMP / p.name)
        except Exception as e:  # noqa: BLE001 —— 库副本复制失败仅告警 SHA 校验兜底
            print(f"  [warn] 复制 {p.name} 失败（忽略）: {e}")
if ORIG_FTS.exists():
    try:
        shutil.copy2(ORIG_FTS, TMP / ORIG_FTS.name)
    except Exception as e:  # noqa: BLE001 —— FTS 副本复制失败仅告警后续校验兜底
        print(f"  [warn] 复制 fts.db 失败（忽略）: {e}")
os.environ["DB_PATH"] = str(TMP / ORIG_DB.name)

from metrics import mrr_at_k, recall_at_k  # noqa: E402

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

EMB_MODEL = Config.OLLAMA_EMBEDDING_MODEL


def unit(m):
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def group_of(payload: dict) -> str:
    raw = (payload.get("domain", "") or payload.get("character_name", "")
           or "general").strip().lower()
    return GROUP_MAP.get(raw, "general")


def embed_batch(texts, batch=32):
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/")
    out = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        try:
            r = requests.post(f"{url}/api/embed",
                              json={"model": EMB_MODEL, "input": part}, timeout=600)
            r.raise_for_status()
            vecs = r.json().get("embeddings")
            if not vecs or len(vecs) != len(part):
                raise RuntimeError("embed 数量不符")
            out.extend(vecs)
        except Exception:  # noqa: BLE001 —— 批量嵌入失败逐条回退保证部分结果
            for t in part:
                rr = requests.post(f"{url}/api/embeddings",
                                   json={"model": EMB_MODEL, "prompt": t[:2500]}, timeout=180)
                rr.raise_for_status()
                out.append(rr.json()["embedding"])
    return np.asarray(out, dtype=np.float32)


def classify(gen_model: str, query: str) -> str:
    url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/")
    r = requests.post(f"{url}/api/generate", json={
        "model": gen_model, "prompt": PROMPT.format(q=query), "stream": False,
        "options": {"temperature": 0, "num_predict": 12, "top_p": 1.0}}, timeout=120)
    r.raise_for_status()
    txt = (r.json().get("response") or "").strip().lower()
    for g in GROUPS:
        if g in txt:
            return g
    return "general"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", type=str, default="qwen2.5:7b-instruct")
    ap.add_argument("--boost", type=float, default=0.02, help="软加权的域内加分")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    store = TriviumStore()
    all_nodes = list(store.iter_nodes())
    active = [(nid, n) for nid, n in all_nodes
              if (n["payload"].get("status") or "active") != "outdated"
              and (n["payload"].get("content") or "").strip()]
    nids = [nid for nid, _ in active]
    texts = [(n["payload"].get("content") or "") for _, n in active]
    grp = [group_of(n["payload"]) for _, n in active]
    print(f"active 节点 {len(active)}；域分布 {dict(Counter(grp).most_common())}")

    node_vecs = unit(embed_batch([t[:4000] for t in texts]))
    print(f"节点向量 {node_vecs.shape}")

    items = json.loads((EVAL_DIR / "eval_set.json").read_text(encoding="utf-8"))["items"]
    pos = [it for it in items if it.get("kind") != "negative" and it.get("gold_ids")]
    if args.limit:
        pos = pos[: args.limit]
    id2idx = {nid: i for i, nid in enumerate(nids)}
    gold_grp = {}
    for it in pos:
        pid = id2idx.get(it["gold_ids"][0])
        gold_grp[it["qid"]] = grp[pid] if pid is not None else "general"
    print(f"题集真实域分布 {dict(Counter(gold_grp.values()).most_common())}")

    # ---- LLM 分类（缓存）----
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if (CACHE.exists() and not args.rebuild) else {}
    bucket = cache.setdefault(args.model, {})
    todo = [it for it in pos if it["qid"] not in bucket]
    if todo:
        print(f"LLM 分类 {len(todo)} 题…")
        t0 = time.time()
        for n, it in enumerate(todo, 1):
            t1 = time.time()
            try:
                lab = classify(args.model, it["query"])
            except Exception as e:  # noqa: BLE001 —— 分类失败兜底 general 并记录错误继续评估
                lab = "general"
                bucket[it["qid"]] = {"q": it["query"], "label": lab, "err": str(e)[:100]}
                continue
            bucket[it["qid"]] = {"q": it["query"], "label": lab, "s": round(time.time() - t1, 3)}
            if n % 20 == 0:
                print(f"  {n}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    llm_lab = {q: v["label"] for q, v in bucket.items()}
    acc = sum(1 for it in pos if llm_lab.get(it["qid"]) == gold_grp[it["qid"]]) / len(pos)
    lat = [v.get("s", 0) for v in bucket.values() if v.get("s")]
    print(f"LLM 域分类准确率 {acc:.4f}（{sum(1 for it in pos if llm_lab.get(it['qid']) == gold_grp[it['qid']])}/{len(pos)}）"
          f"；延迟均值 {sum(lat) / max(len(lat), 1):.3f}s")
    print(f"  混淆（真实→推断）: "
          f"{dict(Counter((gold_grp[it['qid']], llm_lab.get(it['qid'])) for it in pos).most_common(8))}")

    # ---- 评估 ----
    rows = []
    t0 = time.time()
    for k, it in enumerate(pos, 1):
        qv = unit(np.asarray(store.embed_text(it["query"]), dtype=np.float32)[None, :])[0]
        sc = node_vecs @ qv
        order = np.argsort(-sc)
        rk_base = [nids[i] for i in order]
        maj = Counter(grp[i] for i in order[:10]).most_common(1)[0][0]
        lab = llm_lab.get(it["qid"], "general")
        truth = gold_grp[it["qid"]]

        def hard(target, order=order):
            return [nids[i] for i in order if grp[i] == target]

        def soft(target, boost=None, sc=sc):
            b = args.boost if boost is None else boost
            s2 = sc + np.asarray([b if g == target else 0.0 for g in grp])
            return [nids[i] for i in np.argsort(-s2)]

        rows.append({"qid": it["qid"], "query": it["query"], "gold": it["gold_ids"],
                     "gold_grp": truth, "llm_label": lab, "maj_label": maj,
                     "base": rk_base, "oracle_hard": hard(truth), "llm_hard": hard(lab),
                     "maj_hard": hard(maj), "maj_soft": soft(maj), "llm_soft": soft(lab),
                     "oracle_soft": soft(truth),
                     # 域已知（oracle）时的 boost 扫描：软加权能否既拿收益又不丢跨域
                     "oracle_soft_b05": soft(truth, 0.05), "oracle_soft_b10": soft(truth, 0.10),
                     "oracle_soft_b20": soft(truth, 0.20),
                     # 最坏情况：boost 加在错误的域上（风险上界）
                     "wrong_soft_b10": soft(next(g for g in GROUPS if g != truth), 0.10)})
        if k % 20 == 0:
            print(f"  {k}/{len(pos)}  {time.time() - t0:.0f}s", flush=True)

    keys = ("base", "oracle_hard", "oracle_soft", "oracle_soft_b05", "oracle_soft_b10",
            "oracle_soft_b20", "wrong_soft_b10", "llm_hard", "maj_hard", "maj_soft", "llm_soft")

    def met(key):
        agg = defaultdict(list)
        dom = defaultdict(list)
        for r in rows:
            g = set(r["gold"])
            for k in (1, 3, 5, 10):
                agg[f"R@{k}"].append(recall_at_k(r[key], g, k))
            agg["MRR"].append(mrr_at_k(r[key], g, 10))
            dom[r["gold_grp"]].append(recall_at_k(r[key], g, 5))
        out = {m: sum(v) / len(v) for m, v in agg.items()}
        out["dom"] = {d: sum(v) / len(v) for d, v in sorted(dom.items(), key=lambda x: -len(x[1]))}
        return out

    res = {k: met(k) for k in keys}
    print("\n" + "=" * 88)
    print(f"域路由评测（{len(rows)} 题；分类模型 {args.model}；软加权 boost={args.boost}）")
    doms = list(res["base"]["dom"].keys())
    hdr = f"{'variant':<14} {'R@1':>7} {'R@3':>7} {'R@5':>7} {'R@10':>7} {'MRR':>7}  "
    for d in doms:
        hdr += f"{d[:6]:>8}"
    print(hdr)
    for k in keys:
        m = res[k]
        line = (f"{k:<14} {m['R@1']:7.4f} {m['R@3']:7.4f} {m['R@5']:7.4f} {m['R@10']:7.4f} "
                f"{m['MRR']:7.4f}  ")
        for d in doms:
            line += f"{m['dom'].get(d, 0):8.3f}"
        print(line)
    print("=" * 88)

    b = res["base"]
    print("\nΔ 相对 base（pp）与逐题 gain/loss（R@5）：")
    for k in keys[1:]:
        m = res[k]
        g = loss = 0
        for r in rows:
            gold = set(r["gold"])
            a, b2 = recall_at_k(r["base"], gold, 5), recall_at_k(r[k], gold, 5)
            if b2 > a:
                g += 1
            elif b2 < a:
                loss += 1
        print(f"  {k:<14} ΔR@1 {100 * (m['R@1'] - b['R@1']):+6.2f} | ΔR@5 {100 * (m['R@5'] - b['R@5']):+6.2f} "
              f"| ΔR@10 {100 * (m['R@10'] - b['R@10']):+6.2f} | ΔMRR {100 * (m['MRR'] - b['MRR']):+6.2f} "
              f"| 逐题 +{g}/−{loss}")

    post_sha = sha256(ORIG_DB)
    print(f"\n库完整性: 前 {pre_sha[:16]} 后 {post_sha[:16]} → "
          f"{'一致' if pre_sha == post_sha else '不一致'}")

    out = {"meta": {"model": args.model, "boost": args.boost, "n_questions": len(rows),
                    "llm_accuracy": round(acc, 4),
                    "llm_latency_s": round(sum(lat) / max(len(lat), 1), 3),
                    "embed_model": EMB_MODEL, "limit": args.limit},
           "sha256": {"pre": pre_sha, "post": post_sha, "consistent": pre_sha == post_sha},
           "results": res, "rows": rows}
    (TMP / "domain_routing_probe.json").write_text(json.dumps(out, ensure_ascii=False),
                                                   encoding="utf-8")
    print(f"结果已写入 {TMP / 'domain_routing_probe.json'}")


if __name__ == "__main__":
    main()
