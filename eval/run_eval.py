"""Run evaluation on the eval query set.

Usage:
    python eval/run_eval.py [--modes fts,vec,rrf,cascade] [--limit N] [--top-k 10]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict
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

    for p in _ORIG_DB_PATH.parent.iterdir():
        if p.name.startswith(_ORIG_DB_PATH.name) and p.is_file():
            shutil.copy2(p, tmp / p.name)

    if _ORIG_FTS_DB.exists():
        shutil.copy2(_ORIG_FTS_DB, tmp / _ORIG_FTS_DB.name)

    tmp_db = tmp / _ORIG_DB_PATH.name
    os.environ["DB_PATH"] = str(tmp_db)
    return tmp


_copy_db_to_tmp()

# Now safe to import project modules
from core.fts_index import search_fts  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402
from mcp_tools.memory import _hybrid_cascade, _hybrid_rrf  # noqa: E402

# Metrics (from eval/ directory)
sys.path.insert(0, str(_EVAL_DIR))
from metrics import mrr_at_k, ndcg_at_k, recall_at_k  # noqa: E402

ALL_MODES = ("fts", "vec", "rrf", "cascade")


def _run_fts(query: str, top_k: int, _store: TriviumStore) -> tuple[list[int], list[float | None]]:
    results = search_fts(query, limit=top_k)
    ids = [r["node_id"] for r in results if "node_id" in r][:top_k]
    scores = [None] * len(ids)
    return ids, scores


def _run_vec(query: str, top_k: int, store: TriviumStore) -> tuple[list[int], list[float]]:
    emb = store.embed_text(query)
    results = store.search_similar(emb, top_k=top_k, expand_depth=0,
                                   apply_decay=True, block="")
    ids = [r["id"] for r in results if "id" in r][:top_k]
    scores = [r.get("score") for r in results[:top_k]]
    return ids, scores


def _run_rrf(query: str, top_k: int, store: TriviumStore) -> tuple[list[int], list[float]]:
    results = _hybrid_rrf(query, scope="all", domain="", domain_bias="",
                          top_k=top_k, fts_limit=top_k * 3, block="",
                          include_outdated=False)
    ids = [r.get("id") for r in results if r.get("id") is not None][:top_k]
    scores = [r.get("score") for r in results[:top_k]]
    return ids, scores


def _run_cascade(query: str, top_k: int, store: TriviumStore) -> tuple[list[int], list[float]]:
    results = _hybrid_cascade(query, scope="all", domain="", domain_bias="",
                              top_k=top_k, fts_limit=top_k * 3, block="",
                              include_outdated=False)
    ids = [r.get("id") for r in results if r.get("id") is not None][:top_k]
    scores = [r.get("score") for r in results[:top_k]]
    return ids, scores


_MODE_FNS = {
    "fts": _run_fts,
    "vec": _run_vec,
    "rrf": _run_rrf,
    "cascade": _run_cascade,
}


def _collect_source_paths(store: TriviumStore) -> dict[int, str]:
    """Build node_id -> source_path map for doc-level recall."""
    mapping: dict[int, str] = {}
    for nid, payload in store.iter_payloads():
        if payload:
            mapping[nid] = payload.get("source_path", "")
    return mapping


def _doc_recall(
    ranked_ids: list[int],
    gold_id: int,
    source_path: str,
    source_map: dict[int, str],
    k: int,
) -> float:
    """Doc-level recall: binary 0/1 — any node with same source_path in top-k."""
    if not source_path:
        return 0.0
    top = ranked_ids[:k]
    for rid in top:
        if rid == gold_id or source_map.get(rid, "") == source_path:
            return 1.0
    return 0.0


def _generate_report(
    results_data: dict,
    eval_set: dict,
    modes: list[str],
    sha256_before: str,
    sha256_after: str,
    source_map: dict[int, str],
) -> str:
    """Generate markdown report."""
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append("# 检索质量评测报告")
    lines.append("")
    lines.append(f"生成时间: {now}")
    lines.append(f"题目数: {len(eval_set['items'])}")
    lines.append(f"评测模式: {', '.join(modes)}")
    lines.append("")

    # SHA256 check
    sha_ok = sha256_before == sha256_after
    lines.append("## 数据库完整性检查")
    lines.append("")
    lines.append(f"- 跑前 SHA256: `{sha256_before}`")
    lines.append(f"- 跑后 SHA256: `{sha256_after}`")
    lines.append(f"- 一致性: {'✅ 一致' if sha_ok else '❌ 不一致！'}")
    lines.append("")

    def _get_layer(item: dict) -> str:
        # 值域归一化：生成器写 "kb_chunk"，报告分组键是 "kb"——同一层，必须对齐
        if "layer" in item:
            return {"kb_chunk": "kb"}.get(item["layer"], item["layer"])
        gt = item.get("gold_type", "")
        gd = item.get("gold_domain", "")
        if gt == "kb_chunk":
            return "kb"
        if gd == "hermes":
            return "hermes"
        if gd == "novel":
            return "novel"
        return "other"

    # ── Aggregate metrics per mode ───────────────────────────────────────
    agg: dict[str, dict[str, list[float]]] = {}
    for mode in modes:
        agg[mode] = defaultdict(list)

    items_by_qid = {it["qid"]: it for it in eval_set["items"]}

    for qid, mode_results in results_data.items():
        item = items_by_qid.get(qid)
        if not item or item.get("kind") == "negative":
            continue
        gold_ids = set(item.get("gold_ids", []))
        if not gold_ids:
            continue
        for mode in modes:
            ranked = mode_results.get(mode, {}).get("ids", [])
            agg[mode]["recall@1"].append(recall_at_k(ranked, gold_ids, 1))
            agg[mode]["recall@3"].append(recall_at_k(ranked, gold_ids, 3))
            agg[mode]["recall@5"].append(recall_at_k(ranked, gold_ids, 5))
            agg[mode]["recall@10"].append(recall_at_k(ranked, gold_ids, 10))
            agg[mode]["mrr@10"].append(mrr_at_k(ranked, gold_ids, 10))
            agg[mode]["ndcg@5"].append(ndcg_at_k(ranked, gold_ids, 5))

    def _avg(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    # ── Mode comparison table ────────────────────────────────────────────
    lines.append("## 模式对比（正样本）")
    lines.append("")
    header = "| 指标 | " + " | ".join(modes) + " |"
    sep = "|---|" + "|".join(["---"] * len(modes)) + "|"
    lines.append(header)
    lines.append(sep)
    for metric in ["recall@1", "recall@3", "recall@5", "recall@10",
                    "mrr@10", "ndcg@5"]:
        row = f"| {metric} | "
        row += " | ".join(f"{_avg(agg[m][metric]):.4f}" for m in modes)
        row += " |"
        lines.append(row)
    lines.append("")

    # ── Per-layer breakdown (Problem 3: use 'layer' field) ──────────────
    layer_agg: dict[str, dict[str, dict[str, list[float]]]] = {}
    for layer_name in ["hermes", "kb", "novel", "other"]:
        layer_agg[layer_name] = {m: defaultdict(list) for m in modes}

    for qid, mode_results in results_data.items():
        item = items_by_qid.get(qid)
        if not item or item.get("kind") == "negative":
            continue
        gold_ids = set(item.get("gold_ids", []))
        if not gold_ids:
            continue
        layer = _get_layer(item)
        if layer not in layer_agg:
            layer = "other"
        for mode in modes:
            ranked = mode_results.get(mode, {}).get("ids", [])
            layer_agg[layer][mode]["recall@5"].append(
                recall_at_k(ranked, gold_ids, 5))
            layer_agg[layer][mode]["mrr@10"].append(
                mrr_at_k(ranked, gold_ids, 10))

    lines.append("## 分层细分（Recall@5 / MRR@10）")
    lines.append("")
    for layer_name in ["hermes", "kb", "novel", "other"]:
        has_data = any(
            layer_agg[layer_name][m]["recall@5"] for m in modes)
        if not has_data:
            continue
        lines.append(f"### {layer_name}")
        lines.append("")
        header = "| 指标 | " + " | ".join(modes) + " |"
        sep = "|---|" + "|".join(["---"] * len(modes)) + "|"
        lines.append(header)
        lines.append(sep)
        for metric in ["recall@5", "mrr@10"]:
            row = f"| {metric} | "
            row += " | ".join(
                f"{_avg(layer_agg[layer_name][m][metric]):.4f}" for m in modes)
            row += " |"
            lines.append(row)
        lines.append("")

    # ── KB doc-level recall (Problem 2) ─────────────────────────────────
    kb_doc_agg: dict[str, dict[str, list[float]]] = {}
    for mode in modes:
        kb_doc_agg[mode] = defaultdict(list)

    for qid, mode_results in results_data.items():
        item = items_by_qid.get(qid)
        if not item or item.get("kind") == "negative":
            continue
        if item.get("gold_type") != "kb_chunk":
            continue
        gold_ids = set(item.get("gold_ids", []))
        if not gold_ids:
            continue
        gold_id = next(iter(gold_ids))
        gold_sp = source_map.get(gold_id, "")
        if not gold_sp:
            continue
        partial = {nid for nid, sp in source_map.items()
                   if sp == gold_sp and nid not in gold_ids}
        for mode in modes:
            ranked = mode_results.get(mode, {}).get("ids", [])
            doc_hit = _doc_recall(ranked, gold_id, gold_sp, source_map, 5)
            kb_doc_agg[mode]["doc_recall@5"].append(doc_hit)
            kb_doc_agg[mode]["ndcg@5_partial"].append(
                ndcg_at_k(ranked, gold_ids, 5, partial=partial))

    has_kb_doc = any(
        kb_doc_agg[m]["doc_recall@5"] for m in modes)
    if has_kb_doc:
        lines.append("## KB 文档级指标（kb_chunk 题）")
        lines.append("")
        header = "| 指标 | " + " | ".join(modes) + " |"
        sep = "|---|" + "|".join(["---"] * len(modes)) + "|"
        lines.append(header)
        lines.append(sep)
        for metric in ["doc_recall@5", "ndcg@5_partial"]:
            row = f"| {metric} | "
            row += " | ".join(
                f"{_avg(kb_doc_agg[m][metric]):.4f}" for m in modes)
            row += " |"
            lines.append(row)
        lines.append("*doc_recall@5: top-5 中命中同一 source_path 的任一节点（二值 0/1，非比例）*")
        lines.append("*ndcg@5(partial): 同一 source_path 的其他 chunk 作为 partial（相关性 0.5）*")
        lines.append("")

    # ── Negative sample analysis (Problem 6: FTS shows proportion) ──────
    neg_top1_scores: dict[str, list[float | None]] = {m: [] for m in modes}
    pos_top1_scores: dict[str, list[float | None]] = {m: [] for m in modes}
    neg_has_result: dict[str, list[bool]] = {m: [] for m in modes}

    for qid, mode_results in results_data.items():
        item = items_by_qid.get(qid)
        if not item:
            continue
        for mode in modes:
            mode_data = mode_results.get(mode, {})
            ranked_ids = mode_data.get("ids", [])
            ranked_scores = mode_data.get("scores", [])
            if item.get("kind") == "negative":
                score = ranked_scores[0] if ranked_scores else None
                neg_top1_scores[mode].append(score)
                neg_has_result[mode].append(len(ranked_ids) > 0)
            elif item.get("kind") != "negative" and item.get("gold_ids"):
                score = ranked_scores[0] if ranked_scores else None
                pos_top1_scores[mode].append(score)

    lines.append("## 负样本分析")
    lines.append("")
    header = "| 指标 | " + " | ".join(modes) + " |"
    sep = "|---|" + "|".join(["---"] * len(modes)) + "|"
    lines.append(header)
    lines.append(sep)

    def _median(vals: list[float | None]) -> str:
        scored = [v for v in vals if v is not None]
        if not scored:
            return "N/A"
        scored.sort()
        mid = len(scored) // 2
        if len(scored) % 2 == 0:
            return f"{(scored[mid-1] + scored[mid]) / 2:.4f}"
        return f"{scored[mid]:.4f}"

    row = "| 负样本 top1 分数中位数 | "
    row += " | ".join(_median(neg_top1_scores[m]) for m in modes)
    row += " |"
    lines.append(row)
    row = "| 正样本 top1 分数中位数 | "
    row += " | ".join(_median(pos_top1_scores[m]) for m in modes)
    row += " |"
    lines.append(row)

    def _neg_result_ratio(has_res: list[bool]) -> str:
        if not has_res:
            return "N/A"
        return f"{sum(has_res) / len(has_res):.2%}"

    row = "| 负样本有返回结果比例 | "
    row += " | ".join(_neg_result_ratio(neg_has_result[m]) for m in modes)
    row += " |"
    lines.append(row)
    lines.append("*负样本有返回结果比例: 无正确答案的查询中，该模式仍返回了检索结果的比例（越高说明误召回风险越大）*")
    lines.append("")

    # ── Error cases (top 10) ─────────────────────────────────────────────
    errors: list[dict] = []
    for qid, mode_results in results_data.items():
        item = items_by_qid.get(qid)
        if not item or item.get("kind") == "negative":
            continue
        gold_ids = set(item.get("gold_ids", []))
        if not gold_ids:
            continue
        ranks = {}
        for mode in modes:
            ranked = mode_results.get(mode, {}).get("ids", [])
            found_rank = None
            for i, rid in enumerate(ranked):
                if rid in gold_ids:
                    found_rank = i + 1
                    break
            ranks[mode] = found_rank
        worst_avg = sum(r if r is not None else 999 for r in ranks.values())
        errors.append({
            "qid": qid,
            "query": item["query"],
            "gold_ids": item["gold_ids"],
            "ranks": ranks,
            "worst_avg": worst_avg,
        })

    errors.sort(key=lambda e: e["worst_avg"], reverse=True)
    top_errors = errors[:10]

    lines.append("## 错例 Top 10")
    lines.append("")
    if top_errors:
        header = "| QID | Query | Gold ID | " + " | ".join(modes) + " |"
        sep = "|---|---|---|" + "|".join(["---"] * len(modes)) + "|"
        lines.append(header)
        lines.append(sep)
        for err in top_errors:
            rank_strs = []
            for m in modes:
                r = err["ranks"][m]
                rank_strs.append(str(r) if r is not None else "miss")
            row = (f"| {err['qid']} | {err['query'][:30]} | "
                   f"{err['gold_ids']} | " + " | ".join(rank_strs) + " |")
            lines.append(row)
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run retrieval evaluation")
    parser.add_argument("--modes", type=str, default="fts,vec,rrf,cascade",
                        help="Comma-separated modes to evaluate")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only evaluate first N questions (0=all)")
    parser.add_argument("--top-k", type=int, default=10,
                        help="Number of results to retrieve per query")
    parser.add_argument("--eval-set", type=str, default=None,
                        help="Path to eval set JSON (default: eval/eval_set.json)")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for m in modes:
        if m not in _MODE_FNS:
            print(f"[run_eval] Unknown mode: {m}")
            return 1

    print(f"[run_eval] DB副本路径: {os.environ.get('DB_PATH', 'N/A')}")

    # ── SHA256 before ────────────────────────────────────────────────────
    sha256_before = _sha256(_ORIG_DB_PATH)
    print(f"[run_eval] DB SHA256 (before): {sha256_before}")

    # ── Load eval set ────────────────────────────────────────────────────
    eval_set_path = Path(args.eval_set) if args.eval_set else _EVAL_DIR / "eval_set.json"
    if not eval_set_path.exists():
        print(f"[run_eval] ERROR: {eval_set_path} not found")
        return 1

    with open(eval_set_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    items = eval_set["items"]
    if args.limit > 0:
        items = items[:args.limit]
    print(f"[run_eval] Evaluating {len(items)} items with modes: {modes}")

    # ── Initialize store ─────────────────────────────────────────────────
    store = TriviumStore()
    source_map = _collect_source_paths(store)

    # ── Run evaluation ───────────────────────────────────────────────────
    results_data: dict[str, dict] = {}
    fail_count = 0

    for idx, item in enumerate(items):
        qid = item["qid"]
        query = item["query"]
        kind = item.get("kind", "semantic")

        if kind == "negative":
            print(f"  [{idx+1}/{len(items)}] {qid} (negative)")

        mode_results: dict[str, dict] = {}

        for mode in modes:
            try:
                ranked_ids, ranked_scores = _MODE_FNS[mode](query, args.top_k, store)
                mode_results[mode] = {"ids": ranked_ids, "scores": ranked_scores}
            except Exception as e:  # noqa: BLE001 —— 单模式跑分失败记空结果继续评测其余模式
                print(f"  [ERROR] {qid}/{mode}: {e}")
                fail_count += 1
                mode_results[mode] = {"ids": [], "scores": []}

        results_data[qid] = mode_results

    # ── SHA256 after ─────────────────────────────────────────────────────
    sha256_after = _sha256(_ORIG_DB_PATH)
    print(f"[run_eval] DB SHA256 (after): {sha256_after}")
    sha_ok = sha256_before == sha256_after
    print(f"[run_eval] DB integrity: {'✅ PASS' if sha_ok else '❌ FAIL'}")

    # ── Write results JSON ───────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_path = _EVAL_DIR / f"results_{ts}.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"[run_eval] Results written to: {results_path}")

    # ── Write report ─────────────────────────────────────────────────────
    report = _generate_report(results_data, eval_set, modes,
                              sha256_before, sha256_after, source_map)
    report_path = _EVAL_DIR / f"report_{ts}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[run_eval] Report written to: {report_path}")

    if fail_count > 0:
        print(f"[run_eval] {fail_count} mode(s) had errors")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
