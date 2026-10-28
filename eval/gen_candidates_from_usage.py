"""从真实使用挖候选评测题（#221 Phase 1）—— 只出清单，绝不自动入集。

issue 红线：item 入集前必须人工确认。本脚本只产出
eval/candidate_questions_<ts>.json（每条带 pending_human_confirm=true），
等人确认后才可手工并入题集。

「真实复现」的依据：库内节点的 hit_count（mem_search 命中时自动 +1，
见 core/trivium_store.py::_bump_hit_counts）——hit_count 越高代表该节点
被真实检索命中过越多次。query 文本只给草稿（取 content 首行），须人工润色
并过「禁抄原文 ≥6 字」校验后才可用。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_EVAL_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Dig candidate eval questions from real usage (hit_count)")
    parser.add_argument("--top-n", type=int, default=10, help="how many candidates to emit (default: 10)")
    parser.add_argument("--min-hits", type=int, default=1, help="minimum hit_count to qualify (default: 1)")
    args = parser.parse_args()

    # ── 拷库隔离（照 run_eval.py 模式：先拷，再指 DB_PATH，再 import） ──
    db = Path(_PROJECT_ROOT) / "data" / "mh_memory.db"
    fts = db.parent / "fts.db"
    tmp = (_EVAL_DIR / ".tmp").resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    for p in db.parent.iterdir():
        if p.name.startswith(db.name) and p.is_file():
            dst = tmp / p.name
            if dst.resolve() != p.resolve():
                shutil.copy2(p, dst)
    if fts.exists() and (tmp / fts.name).resolve() != fts.resolve():
        shutil.copy2(fts, tmp / fts.name)
    os.environ["DB_PATH"] = str(tmp / db.name)

    from core.trivium_store import TriviumStore

    store = TriviumStore()
    candidates: list[dict] = []
    n_hit_total = 0
    for nid, payload in store.iter_payloads():
        if not payload:
            continue
        hits = payload.get("hit_count") or 0
        if hits >= args.min_hits:
            n_hit_total += 1
        content = str(payload.get("content") or "")
        if hits <= 0 or payload.get("status") == "outdated":
            continue
        first_line = content.strip().splitlines()[0].strip() if content.strip() else ""
        if not first_line:
            continue
        candidates.append(
            {
                "query_draft": first_line[:60],
                "suggested_gold_id": nid,
                "hit_count": hits,
                "source_fields": {
                    "type": payload.get("type"),
                    "domain": payload.get("domain"),
                    "source_path": payload.get("source_path", ""),
                    "created_at": payload.get("created_at"),
                },
                "why_real": (
                    f"node {nid} was actually retrieved {hits}x (hit_count is bumped by every "
                    "mem_search hit; see core/trivium_store.py::_bump_hit_counts)"
                ),
                "pending_human_confirm": True,
            }
        )
    candidates.sort(key=lambda c: -c["hit_count"])
    candidates = candidates[: args.top_n]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "hit_count top-N from a DB copy; query_draft is a raw first-line draft",
        "note": (
            "HARD RULE: these are CANDIDATES ONLY and must NOT be merged into any eval set "
            "before human confirmation (issue #221). query_draft also fails the >=6-char "
            "anti-copy check on purpose - human must rewrite it."
        ),
        "nodes_with_any_hits": n_hit_total,
        "items": candidates,
    }
    out_path = _EVAL_DIR / f"candidate_questions_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[candidates] nodes with hit_count>0: {n_hit_total}")
    print(f"[candidates] wrote top {len(candidates)} -> {out_path}")
    for c in candidates[:3]:
        print(f"  e.g. gold={c['suggested_gold_id']} hits={c['hit_count']} draft={c['query_draft'][:40]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
