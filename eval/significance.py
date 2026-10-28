"""Paired significance testing for retrieval evaluation results.

Compares two configurations (two result files, or two modes within one run)
on the same query set: per-query paired scores -> paired bootstrap p-value and
percentile confidence interval for the mean difference (A - B).

Inputs are the per-run result files written by ``eval/run_eval.py`` plus the
eval set they were produced from. Every number in the report is tagged with
the query subset it was computed on (all / tune / held_out), per the held-out
protocol in ``docs/RETRIEVAL_QUALITY.md``.

Usage:
    python eval/significance.py --results-a eval/results_X.json --mode-a vec \
        --results-b eval/results_X.json --mode-b rrf --metric recall@5 \
        [--split-manifest eval/split_manifest.json]

No DB access: this module only reads JSON files written by ``run_eval.py``.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from eval.metrics import mrr_at_k, ndcg_at_k, recall_at_k  # noqa: E402


def metric_fn(name: str) -> Callable[[list[int], set[int]], float]:
    """Resolve a metric name like 'recall@5' into (ranked_ids, gold_ids) -> float."""
    prefix, _, k_str = name.strip().lower().partition("@")
    if not k_str.isdigit():
        raise ValueError(f"metric must look like 'recall@5', got: {name!r}")
    k = int(k_str)
    if prefix == "recall":
        return lambda ranked, gold: recall_at_k(ranked, gold, k)
    if prefix == "mrr":
        return lambda ranked, gold: mrr_at_k(ranked, gold, k)
    if prefix == "ndcg":
        return lambda ranked, gold: ndcg_at_k(ranked, gold, k)
    raise ValueError(f"unsupported metric prefix: {prefix!r} (supported: recall / mrr / ndcg)")


def per_query_scores(
    results_data: dict,
    items_by_qid: dict[str, dict],
    mode: str,
    fn: Callable[[list[int], set[int]], float],
) -> dict[str, float]:
    """Per-query metric scores for one mode.

    Skips negative samples and items without gold ids — the same inclusion
    rule as the aggregate report in ``run_eval.py``, so the numbers here are
    comparable with it.
    """
    scores: dict[str, float] = {}
    for qid, mode_results in results_data.items():
        item = items_by_qid.get(qid)
        if item is None or item.get("kind") == "negative":
            continue
        gold = set(item.get("gold_ids", []))
        if not gold:
            continue
        ranked = (mode_results.get(mode) or {}).get("ids", [])
        scores[qid] = fn(ranked, gold)
    return scores


def paired_bootstrap(
    scores_a: list[float],
    scores_b: list[float],
    n_resamples: int = 1000,
    seed: int = 42,
    confidence: float = 0.95,
) -> dict | None:
    """Paired bootstrap test on the mean difference (A - B), two-sided.

    - Test statistic: mean of per-query differences d_i = a_i - b_i.
    - p-value: centered bootstrap under H0 (mean diff = 0). The differences are
      shifted to zero mean, resampled with replacement, and p is the fraction of
      resampled means at least as extreme (in absolute value) as the observed
      one. Add-one correction keeps p > 0.
    - CI: percentile interval on the resampled (uncentered) mean differences.
    - stdlib only (no numpy), deterministic for a fixed seed.

    Returns None when there are fewer than 2 pairs (no meaningful resampling),
    so a 1-query subset reports N/A instead of crashing.
    """
    if len(scores_a) != len(scores_b):
        raise ValueError(f"length mismatch: {len(scores_a)} != {len(scores_b)}")
    n = len(scores_a)
    if n < 2:
        return None

    diffs = [a - b for a, b in zip(scores_a, scores_b, strict=True)]
    mean_diff = sum(diffs) / n
    centered = [d - mean_diff for d in diffs]

    rng = random.Random(seed)
    centered_means: list[float] = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += centered[rng.randrange(n)]
        centered_means.append(total / n)

    # The uncentered resampled mean is just the centered one shifted back by mean_diff.
    raw_means = sorted(m + mean_diff for m in centered_means)
    tail = (1.0 - confidence) / 2.0
    ci_low = _percentile(raw_means, tail * 100.0)
    ci_high = _percentile(raw_means, (1.0 - tail) * 100.0)

    observed = abs(mean_diff)
    exceed = sum(1 for m in centered_means if abs(m) >= observed)
    p_value = (exceed + 1) / (n_resamples + 1)

    return {
        "n": n,
        "mean_a": sum(scores_a) / n,
        "mean_b": sum(scores_b) / n,
        "mean_diff": mean_diff,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "confidence": confidence,
        "p_value": p_value,
        "n_resamples": n_resamples,
        "seed": seed,
    }


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list, q in [0, 100]."""
    if not sorted_vals:
        raise ValueError("percentile of empty list")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q / 100.0 * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _fmt_summary_line(set_name: str, s: dict | None) -> str:
    if s is None:
        return f"  {set_name:<9}  n/a  (fewer than 2 paired queries — test not defined)"
    star = "*" if s["p_value"] < 0.05 else " "
    return (
        f"  {set_name:<9} {s['n']:>4}  {s['mean_a']:>7.4f}  {s['mean_b']:>7.4f}  {s['mean_diff']:+.4f} "
        f"[{s['ci_low']:.4f}, {s['ci_high']:.4f}]  {s['p_value']:.4f}{star}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Paired significance test between two eval configurations")
    parser.add_argument("--results-a", required=True, help="results JSON for config A (from run_eval.py)")
    parser.add_argument("--mode-a", required=True, help="mode name for config A, e.g. vec")
    parser.add_argument("--results-b", default=None, help="results JSON for config B (default: same file as A)")
    parser.add_argument("--mode-b", default=None, help="mode name for config B (default: same as --mode-a)")
    parser.add_argument("--metric", default="recall@5", help="metric to compare: recall@K / mrr@K / ndcg@K")
    parser.add_argument(
        "--eval-set", default=None, help="eval set JSON used for the runs (default: eval/eval_set.json)"
    )
    parser.add_argument(
        "--split-manifest",
        default=None,
        help="split_manifest.json from split_eval_set.py; "
        "when given, the comparison is also reported per subset (tune / held_out)",
    )
    parser.add_argument("--n-resamples", type=int, default=1000, help="bootstrap resamples (default: 1000)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    parser.add_argument("--confidence", type=float, default=0.95, help="CI level (default: 0.95)")
    parser.add_argument(
        "--out", default=None, help="output path for per-query scores (default: eval/comparison_<ts>.json)"
    )
    args = parser.parse_args()

    mode_b = args.mode_b or args.mode_a
    results_b_path = args.results_b or args.results_a
    eval_set_path = args.eval_set or str(Path(__file__).parent / "eval_set.json")

    fn = metric_fn(args.metric)
    with open(args.results_a, encoding="utf-8") as f:
        results_a = json.load(f)
    with open(results_b_path, encoding="utf-8") as f:
        results_b = json.load(f)
    with open(eval_set_path, encoding="utf-8") as f:
        eval_set = json.load(f)
    items_by_qid = {it["qid"]: it for it in eval_set["items"]}

    scores_a = per_query_scores(results_a, items_by_qid, args.mode_a, fn)
    scores_b = per_query_scores(results_b, items_by_qid, mode_b, fn)
    common = sorted(set(scores_a) & set(scores_b))
    if not common:
        print("[significance] ERROR: no common queries between the two result sets")
        return 1

    subsets: dict[str, list[str]] = {"all": list(common)}
    manifest_data = None
    if args.split_manifest:
        with open(args.split_manifest, encoding="utf-8") as f:
            manifest_data = json.load(f)
        tune_qids = [q for q in manifest_data.get("tune_qids", []) if q in set(common)]
        held_qids = [q for q in manifest_data.get("held_out_qids", []) if q in set(common)]
        subsets["tune"] = tune_qids
        subsets["held_out"] = held_qids

    summaries: dict[str, dict | None] = {}
    for name, qids in subsets.items():
        summaries[name] = paired_bootstrap(
            [scores_a[q] for q in qids],
            [scores_b[q] for q in qids],
            n_resamples=args.n_resamples,
            seed=args.seed,
            confidence=args.confidence,
        )

    # ── Per-query scores to disk (gitignored, like other eval/*.json) ────
    set_of_qid = {qid: name for name, qids in subsets.items() for qid in qids}
    out_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metric": args.metric,
        "config_a": {"results_file": str(args.results_a), "mode": args.mode_a},
        "config_b": {"results_file": str(results_b_path), "mode": mode_b},
        "eval_set": str(eval_set_path),
        "split_manifest": str(args.split_manifest) if args.split_manifest else None,
        "bootstrap": {
            "n_resamples": args.n_resamples,
            "seed": args.seed,
            "confidence": args.confidence,
            "test": "paired centered bootstrap, two-sided",
            "ci": "percentile",
        },
        "summaries": summaries,
        "per_query": [
            {"qid": qid, "set": set_of_qid.get(qid, "unplaced"), "score_a": scores_a[qid], "score_b": scores_b[qid]}
            for qid in common
        ],
    }
    out_path = (
        Path(args.out)
        if args.out
        else Path(__file__).parent / (f"comparison_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_payload, f, ensure_ascii=False, indent=2)

    # ── Report ────────────────────────────────────────────────────────────
    print("== Paired comparison ==")
    print(f"  A: mode '{args.mode_a}' in {args.results_a}")
    print(f"  B: mode '{mode_b}' in {results_b_path}")
    print(
        f"  metric: {args.metric} | bootstrap: {args.n_resamples} resamples, seed {args.seed}, "
        f"{args.confidence:.0%} percentile CI"
    )
    print("  diff = mean(A) - mean(B); p-value: two-sided centered bootstrap, H0: mean diff = 0")
    print()
    print("  set         n   mean(A)  mean(B)    diff    95% CI               p-value")
    for name, qids in subsets.items():
        summary = summaries[name]
        if summary is not None and summary["n"] != len(qids):
            print(f"  ({name}: {len(qids)} common qids, but summary n mismatch — unexpected)")
        print(_fmt_summary_line(name, summary))
    if args.split_manifest:
        placed = set(manifest_data.get("tune_qids", [])) | set(manifest_data.get("held_out_qids", []))
        unplaced = len(common) - len([q for q in common if q in placed])
        print(f"  (unplaced  {unplaced:>4}  excluded from tune / held_out rows; see split manifest)")
    print()
    print(f"  per-query scores written to: {out_path}")
    print(
        "  each row above is computed on exactly the queries of that subset (all = tune + held_out + "
        "other positive queries when no manifest is given)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
