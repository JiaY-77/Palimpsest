"""Freshness weighting sweep — issue #175 的数据优先评测协议。

在库副本上对 eval query set 走真实检索入口（``_mem_search_impl``，
与 ab_snapshot_eval.py 同口径），扫 (weight × decay × scale_days) 组合，
输出每组 recall@k / MRR@10 / nDCG@5，并对每个非零组对 w=0 基线做
配对 bootstrap 显著性检验（复用 eval/significance.py，stdlib only）。

数据优先纪律：权重默认 0（中性）。任何非零取值都必须以本脚本的输出为
依据选型——先在 tune 子集上选，再在 held-out 子集上报告（--split-manifest，
见 docs/RETRIEVAL_QUALITY.md 的 held-out 协议），不许拍脑袋定常数。

用法（库副本自动从 DB_PATH 拷贝，隔离模式与 run_eval.py 相同）：
    DB_PATH=data/mh_memory.db python eval/freshness_sweep.py \
        --weights 0,0.05,0.1,0.2 --decays exp,linear --scales 7,30,90 \
        [--eval-set eval/eval_set.json] [--split-manifest eval/split_manifest.json]

输出：eval/.tmp/freshness_sweep_<ts>.json（全量 per-query 结果 + 汇总）+
stdout 摘要表。副本只读不写（结束打印 sha256 校验）。
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


def _locate_original() -> Path:
    if _DB_PATH_ENV and os.path.isabs(_DB_PATH_ENV):
        return Path(_DB_PATH_ENV)
    if _DB_PATH_ENV:
        return Path(_PROJECT_ROOT) / _DB_PATH_ENV
    return Path(_PROJECT_ROOT) / "data" / "mh_memory.db"


_ORIG_DB_PATH = _locate_original()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_db_to_tmp() -> Path:
    """Copy DB + sidecars into .tmp/（与 run_eval.py 同一隔离模式）。

    拷完必须把 ``DB_PATH`` 环境变量指到副本——否则后续 import mcp_tools 时
    单例 ``TriviumStore()`` 会按原始 DB_PATH 直接开生产库（:8090 独占锁会
    拒绝并抛 DatabaseBusyError，但它挡住的是事故、浪费的是一轮评测）。
    """
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _TMP_DIR.resolve()
    for p in _ORIG_DB_PATH.parent.iterdir():
        if p.name.startswith(_ORIG_DB_PATH.name) and p.is_file():
            dst = tmp / p.name
            try:
                if dst.exists() and os.path.samefile(p.resolve(), dst):
                    continue
            except OSError:
                pass
            shutil.copy2(p, dst)
    tmp_db = tmp / _ORIG_DB_PATH.name
    os.environ["DB_PATH"] = str(tmp_db)
    return tmp_db


_copy_db_to_tmp()

# Now safe to import project modules
from config import Config  # noqa: E402
from mcp_tools.memory import _mem_search_impl  # noqa: E402

sys.path.insert(0, str(_EVAL_DIR))
from metrics import mrr_at_k, ndcg_at_k, recall_at_k  # noqa: E402
from significance import paired_bootstrap, per_query_scores  # noqa: E402

METRICS = ("recall@1", "recall@3", "recall@5", "recall@10", "mrr@10", "ndcg@5")


def _parse_floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def _tag(w: float, decay: str, scale: float) -> str:
    return f"w{w:g}/{decay}/d{scale:g}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Freshness weighting sweep over the eval query set")
    parser.add_argument(
        "--weights",
        type=str,
        default="0,0.05,0.1,0.2",
        help="Comma-separated weight candidates (0 must be included — it is the neutral baseline)",
    )
    parser.add_argument("--decays", type=str, default="exp,linear", help="Comma-separated decay forms (exp|linear)")
    parser.add_argument("--scales", type=str, default="7,30,90", help="Comma-separated scale_days candidates")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--eval-set", type=str, default=None)
    parser.add_argument(
        "--split-manifest",
        type=str,
        default=None,
        help="Optional split manifest (eval/split_manifest.json) to additionally report tune/held_out subsets",
    )
    args = parser.parse_args()

    weights = sorted(set(_parse_floats(args.weights)))
    decays = [d.strip().lower() for d in args.decays.split(",") if d.strip()]
    scales = sorted(set(_parse_floats(args.scales)))
    if 0.0 not in weights:
        print("[sweep] ERROR: --weights must include 0 (the neutral baseline)")
        return 1

    eval_set_path = Path(args.eval_set) if args.eval_set else _EVAL_DIR / "eval_set.json"
    items = json.loads(eval_set_path.read_text(encoding="utf-8"))["items"]
    pos = [it for it in items if it.get("kind") != "negative" and it.get("gold_ids")]
    items_by_qid = {it["qid"]: it for it in items}
    print(f"[sweep] {len(pos)} positive queries x {len(weights) * len(decays) * len(scales)} combos")

    split_map: dict[str, str] = {}
    if args.split_manifest:
        manifest = json.loads(Path(args.split_manifest).read_text(encoding="utf-8"))
        # 兼容两种形态：{"qid_split": {qid: side}}（dict）或 split_eval_set.py 的
        # {"tune_qids": [...], "held_out_qids": [...]}（列表）
        split_map = dict(manifest.get("qid_split") or {})
        for qid in manifest.get("tune_qids", []):
            split_map[qid] = "tune"
        for qid in manifest.get("held_out_qids", []):
            split_map[qid] = "held_out"
        if split_map:
            print(f"[sweep] split manifest loaded: {len(split_map)} qids tagged")

    results_data: dict[str, dict] = defaultdict(dict)
    baseline_tag = _tag(0.0, decays[0], scales[0])

    for w in weights:
        for decay in decays:
            for scale in scales:
                tag = _tag(w, decay, scale)
                Config.MEM_SEARCH_FRESHNESS_WEIGHT = w
                Config.MEM_SEARCH_FRESHNESS_DECAY = decay
                Config.MEM_SEARCH_FRESHNESS_SCALE_DAYS = scale
                for it in pos:
                    out = _mem_search_impl(
                        it["query"],
                        scope="all",
                        domain="",
                        domain_bias="",
                        top_k=args.top_k,
                        include_neighbors=False,
                        block="",
                        include_outdated=False,
                    )
                    results_data[it["qid"]][tag] = {
                        "ids": [r["id"] for r in out.get("results", [])],
                        "scores": [r.get("score") for r in out.get("results", [])],
                    }
                print(f"[sweep] done {tag}")

    # ── Aggregate ────────────────────────────────────────────────────────────
    def _subset_scores(tag: str, metric: str, qids: set[str] | None = None) -> list[float]:
        fn = {
            "recall@1": lambda r, g: recall_at_k(r, g, 1),
            "recall@3": lambda r, g: recall_at_k(r, g, 3),
            "recall@5": lambda r, g: recall_at_k(r, g, 5),
            "recall@10": lambda r, g: recall_at_k(r, g, 10),
            "mrr@10": lambda r, g: mrr_at_k(r, g, 10),
            "ndcg@5": lambda r, g: ndcg_at_k(r, g, 5),
        }[metric]
        data = results_data if qids is None else {q: v for q, v in results_data.items() if q in qids}
        return list(per_query_scores(data, items_by_qid, tag, fn).values())

    summary: list[dict] = []
    for w in weights:
        for decay in decays:
            for scale in scales:
                tag = _tag(w, decay, scale)
                row: dict = {"tag": tag, "weight": w, "decay": decay, "scale_days": scale}
                for m in METRICS:
                    vals = _subset_scores(tag, m)
                    row[m] = sum(vals) / len(vals) if vals else 0.0
                if w != 0.0:
                    for m in ("recall@5", "mrr@10"):
                        base = _subset_scores(baseline_tag, m)
                        curr = _subset_scores(tag, m)
                        bt = paired_bootstrap(curr, base)
                        row[f"bt_{m}"] = bt
                summary.append(row)

    # ── Report ───────────────────────────────────────────────────────────────
    print("\n=== Sweep summary (all positive queries) ===")
    header = f"{'tag':>18} | " + " | ".join(f"{m:>9}" for m in METRICS)
    print(header)
    for row in summary:
        cells = " | ".join(f"{row[m]:>9.4f}" for m in METRICS)
        print(f"{row['tag']:>18} | {cells}")

    if split_map:
        tune_qids = {q for q, s in split_map.items() if s == "tune"}
        hold_qids = {q for q, s in split_map.items() if s == "held_out"}
        for name, qids in (("tune", tune_qids), ("held_out", hold_qids)):
            if not qids:
                continue
            print(f"\n=== {name} subset ({len(qids)} queries) ===")
            print(header)
            for row in summary:
                cells = " | ".join(
                    f"{(lambda v: sum(v) / len(v) if v else 0.0)(_subset_scores(row['tag'], m, qids)):>9.4f}"
                    for m in METRICS
                )
                print(f"{row['tag']:>18} | {cells}")

    print("\n=== Significance vs baseline (w=0, paired bootstrap on per-query diffs) ===")
    for row in summary:
        if "bt_mrr@10" not in row:
            continue
        for m in ("recall@5", "mrr@10"):
            bt = row[f"bt_{m}"]
            if bt is None:
                print(f"{row['tag']:>18} {m}: n/a (too few pairs)")
            else:
                print(
                    f"{row['tag']:>18} {m}: diff {bt['mean_diff']:+.4f} "
                    f"[{bt['ci_low']:+.4f}, {bt['ci_high']:+.4f}] p={bt['p_value']:.4f} n={bt['n']}"
                )

    # ── Write results ────────────────────────────────────────────────────────
    sha_ok = _sha256(_ORIG_DB_PATH) == _sha256(_TMP_DIR / _ORIG_DB_PATH.name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _EVAL_DIR / ".tmp" / f"freshness_sweep_{ts}.json"
    payload = {
        "summary": summary,
        "metrics": METRICS,
        "baseline_tag": baseline_tag,
        "db": str(_ORIG_DB_PATH),
        "n_queries": len(pos),
        "results": {qid: dict(tags) for qid, tags in results_data.items()},
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[sweep] written {out_path}")
    print(f"[sweep] source db sha256 unchanged: {sha_ok} (read-only check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
