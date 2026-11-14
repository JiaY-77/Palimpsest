"""自建题集判分器：对 eval/datasets/ 下每套题跑检索并判分。

用法：
    venv/Scripts/python.exe eval/check_datasets.py            # 全部
    venv/Scripts/python.exe eval/check_datasets.py --only temporal
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent / "datasets"
API = "http://127.0.0.1:8090"
TOP_K = 5


def search(query: str, top_k: int = TOP_K) -> list[dict]:
    body = json.dumps({"query": query, "top_k": top_k, "scope": "memory"}).encode()
    req = urllib.request.Request(f"{API}/mem/search", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return (json.loads(r.read()) or {}).get("results") or []


def judge_temporal(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        hits = search(it["query"])
        ids = [h["id"] for h in hits]
        gold = set(it.get("gold_ids") or [])
        stale = set(it.get("stale_ids") or [])
        gold_ranks = [i for i, x in enumerate(ids) if x in gold]
        stale_ranks = [i for i, x in enumerate(ids) if x in stale]
        passed = bool(gold_ranks) and (not stale_ranks or min(gold_ranks) < min(stale_ranks))
        out.append(
            {
                "qid": it["qid"],
                "pass": passed,
                "gold_rank": (gold_ranks[0] + 1) if gold_ranks else None,
                "stale_rank": (stale_ranks[0] + 1) if stale_ranks else None,
                "top3": ids[:3],
                "why": it.get("why", "")[:60],
            }
        )
    return out


def judge_negative(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        hits = search(it["query"])
        top = hits[0] if hits else None
        top_score = float(top.get("score") or 0) if top else 0.0
        danger = [h for h in hits if float(h.get("score") or 0) >= 0.65]
        passed = top_score < 0.55 and not danger
        out.append(
            {
                "qid": it["qid"],
                "pass": passed,
                "top1_score": round(top_score, 3),
                "danger_hits": len(danger),
                "note": it.get("note", ""),
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="temporal / negative")
    args = ap.parse_args()

    total_pass = total = 0
    for path in sorted(BASE.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        kind = data.get("kind")
        if args.only and args.only not in (kind or "") and args.only not in path.name:
            continue
        items = data.get("items") or []
        if not items:
            continue
        print(f"\n=== {path.relative_to(BASE)} | kind={kind} | {len(items)} 题 ===")
        rows = judge_temporal(items) if kind == "temporal_twin" else judge_negative(items)
        for r in rows:
            mark = "PASS" if r["pass"] else "FAIL"
            total += 1
            total_pass += int(r["pass"])
            if kind == "temporal_twin":
                print(f"  [{mark}] {r['qid']} gold_rank={r['gold_rank']} stale_rank={r['stale_rank']} top3={r['top3']}")
            else:
                print(f"  [{mark}] {r['qid']} top1={r['top1_score']} danger={r['danger_hits']} ({r['note']})")
    print(f"\n通过率: {total_pass}/{total}" + (f" = {total_pass / total:.1%}" if total else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
