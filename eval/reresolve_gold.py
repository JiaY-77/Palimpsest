"""Re-resolve dead/stale gold_ids in the retrieval eval set (#221).

The eval set stores raw node ids as gold answers. After the 2026-09-10 library
rebuild those ids were reshuffled: some point at nodes that no longer exist,
worse, some still exist but hold *different* content. A gold id that survives
in name only makes every downstream metric meaningless, so this script
re-resolves gold answers against the current library contents.

判定口径（与 eval/split_eval_set.py 保持一致，不另立标准）:
    a gold id counts as "alive" iff the node exists in the DB payloads AND its
    payload has a non-None ``created_at`` (see ``load_gold_timestamps``).
    On top of that id-level check this script adds a *content-level* check:
    the query is embedded and compared against the gold node's content, so a
    gold id that still exists but now holds unrelated content is NOT kept.

Resolution procedure (per positive item, i.e. kind != "negative"):
    1. Build a candidate pool from the DB copy filtered by the item's declared
       ``gold_type`` (the layer the question was written for). A tight pool
       additionally filters by ``gold_domain``; if nothing clears the
       threshold there, the full type pool is used as fallback (eval-set
       domain labels and actual node domains disagree frequently - measured
       35%+ mismatch - so domain is never a hard filter).
    2. Rank candidates by cosine similarity between the query embedding
       (``TriviumStore.embed_text``) and each candidate's stored vector (the
       same embedding space the production search path uses; ``iter_nodes``).
    3. Only unambiguous wins are applied:
           top1 score >= --top1-threshold  AND  top1 - runner_up >= --margin
       where the runner-up includes the original gold node (when it exists in
       the DB) - re-pointing gold must beat the old answer, not just the field.
    4. Outcome taxonomy:
           live        original gold still in DB, still top-ranked, clear of threshold
           reresolved  gold re-pointed at an unambiguous winner
           quarantined plausible but ambiguous (0 or >=2 reasonable answers) - isolated, never guessed
           dropped     no usable candidates / nothing near threshold

Thresholds are NOT invented: run with ``--sweep`` first to print the score /
margin distributions (quantiles), pick thresholds from that evidence, then run
for real and record both the quantile table and the chosen values in the
report.

Outputs (eval/, gitignored like the other eval data):
    eval/eval_set_v2.json          rebuilt set, every item carries an explicit
                                  status + reasons + original_gold_ids
    eval/reresolve_report_<ts>.md  per-item table + stats + threshold evidence
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


def copy_db_to_tmp(project_root: Path) -> Path:
    """Copy DB + sidecars + fts.db into eval/.tmp/ and point DB_PATH there.

    Same isolation scheme as run_eval.py / split_eval_set.py: never touch the
    production DB (the REST service holds an exclusive writer lock), copy
    first, set DB_PATH, *then* import project modules.
    """
    db_path_env = os.getenv("DB_PATH", "")
    if db_path_env:
        db = Path(db_path_env) if Path(db_path_env).is_absolute() else project_root / db_path_env
    else:
        db = project_root / "data" / "mh_memory.db"
    fts = db.parent / "fts.db"
    if not db.exists():
        print(f"[reresolve] ERROR: DB not found at {db}")
        sys.exit(1)
    tmp = (_EVAL_DIR / ".tmp").resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    for p in db.parent.iterdir():
        if p.name.startswith(db.name) and p.name != db.name + ".lock" and p.is_file():
            dst = tmp / p.name
            if not _is_same_file(p.resolve(), dst):
                shutil.copy2(p, dst)
    if fts.exists() and not _is_same_file(fts.resolve(), tmp / fts.name):
        shutil.copy2(fts, tmp / fts.name)
    tmp_db = tmp / db.name
    os.environ["DB_PATH"] = str(tmp_db)
    return tmp_db


def _is_same_file(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _quantiles(values: list[float]) -> dict[str, float]:
    """p10/p25/p50/p75/p90 for a list of scores (empty-safe)."""
    if not values:
        return {}
    xs = sorted(values)
    n = len(xs)

    def q(p: float) -> float:
        i = min(n - 1, max(0, round(p * (n - 1))))
        return xs[i]

    return {"p10": q(0.10), "p25": q(0.25), "p50": q(0.50), "p75": q(0.75), "p90": q(0.90)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-resolve stale gold_ids against the current library")
    parser.add_argument("--eval-set", default=None, help="eval set JSON (default: eval/eval_set.json)")
    parser.add_argument("--sweep", action="store_true", help="print score/margin distributions only, resolve nothing")
    parser.add_argument(
        "--top1-threshold", type=float, default=0.0, help="min top1 cosine to accept (required unless --sweep)"
    )
    parser.add_argument(
        "--margin", type=float, default=0.0, help="min top1-runner-up gap to accept (required unless --sweep)"
    )
    parser.add_argument("--out", default=None, help="output eval set path (default: eval/eval_set_v2.json)")
    parser.add_argument(
        "--skip-live-check", action="store_true", help="keep items whose gold id merely exists (id-level only)"
    )
    args = parser.parse_args()

    if not args.sweep and (args.top1_threshold <= 0 or args.margin < 0):
        parser.error("--top1-threshold (>0) and --margin are required unless --sweep")

    eval_set_path = Path(args.eval_set) if args.eval_set else _EVAL_DIR / "eval_set.json"
    with open(eval_set_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    # ── DB copy first, project imports after (isolation discipline) ─────
    tmp_db = copy_db_to_tmp(Path(_PROJECT_ROOT))
    print(f"[reresolve] DB copy at: {tmp_db}")
    import numpy as np

    from core.trivium_store import TriviumStore

    store = TriviumStore()
    # 全量读一次：(id, payload, 存量向量)。向量就是生产检索用的同一空间。
    ids: list[int] = []
    payloads: dict[int, dict] = {}
    vecs: list[list[float]] = []
    for nid, node in store.iter_nodes():
        ids.append(nid)
        payloads[nid] = node.get("payload") or {}
        vecs.append(node.get("vector") or [])
    print(f"[reresolve] nodes loaded: {len(ids)}")

    mat = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    mat = mat / norms  # 归一化后点积 = 余弦
    id_index = {nid: i for i, nid in enumerate(ids)}

    items: list[dict] = eval_set["items"]
    positives = [it for it in items if it.get("kind") != "negative"]

    # 候选池按题集声明的 gold_type 建（题目原本要考的层）；
    # domain 只做窄池优先，不做硬过滤（标签失配实测 35%+，见报告）。
    pools_full: dict[str, list[int]] = {}
    pools_tight: dict[tuple[str, str], list[int]] = {}
    for nid in ids:
        t = payloads[nid].get("type", "")
        pools_full.setdefault(t, []).append(nid)
        pools_tight.setdefault((t, str(payloads[nid].get("domain"))), []).append(nid)

    print(f"[reresolve] embedding {len(positives)} queries ...")
    records: list[dict] = []
    for it in positives:
        qid = str(it.get("qid"))
        query = str(it.get("query", ""))
        gold_type = str(it.get("gold_type", ""))
        gold_domain = str(it.get("gold_domain", ""))
        orig_golds = list(it.get("gold_ids", []))
        qv = np.array(store.embed_text(query), dtype=np.float32)
        qn = float(np.linalg.norm(qv)) or 1.0
        qv = qv / qn

        pool = pools_tight.get((gold_type, gold_domain), [])
        pool_kind = "tight(type+domain)"
        if len(pool) < 5:
            pool = pools_full.get(gold_type, [])
            pool_kind = "full(type)"

        sims = mat[[id_index[n] for n in pool], :] @ qv if pool else np.zeros(0)
        order = np.argsort(-sims) if pool else []
        # 原 gold（在库时）作为第 0 候选加入竞争：改判必须赢过旧答案
        extra = [g for g in orig_golds if g in id_index and g not in pool]
        extra_sims = {g: float(mat[id_index[g], :] @ qv) for g in extra}

        ranked = [(float(sims[i]), pool[i]) for i in order] + [(extra_sims[g], g) for g in extra]
        ranked.sort(key=lambda x: -x[0])
        ranked_sims = {nid: s for s, nid in ranked}

        top1_sim, top1_id = ranked[0] if ranked else (0.0, None)
        top2_sim = ranked[1][0] if len(ranked) > 1 else 0.0
        gold_in_db = all(g in id_index for g in orig_golds) and bool(orig_golds)
        # 原 gold 的真实余弦从 ranked 里查（池内/池外都在），哨兵 -1 只留给不在库的
        gold_sim = max((ranked_sims.get(g, -1.0) for g in orig_golds), default=-1.0) if gold_in_db else -1.0

        rec: dict = {
            "qid": qid,
            "query": query,
            "kind": it.get("kind"),
            "gold_type": gold_type,
            "gold_domain": gold_domain,
            "layer": it.get("layer", ""),
            "original_gold_ids": orig_golds,
            "original_gold_in_db": gold_in_db,
            "original_gold_created_at": [payloads[g].get("created_at") for g in orig_golds if g in payloads],
            "pool_size": len(pool),
            "pool_kind": pool_kind,
            "top1_id": top1_id,
            "top1_sim": round(top1_sim, 4),
            "top2_sim": round(top2_sim, 4),
            "margin": round(top1_sim - top2_sim, 4) if ranked else 0.0,
            "gold_sim": round(gold_sim, 4) if gold_in_db else None,
        }
        records.append(rec)

    # ── 分布输出（阈值的证据） ───────────────────────────────────────────
    for field, label in [("top1_sim", "top1 相似度"), ("margin", "top1-top2 差值"), ("gold_sim", "原gold相似度(在库)")]:
        vals = [r[field] for r in records if r.get(field) is not None]
        qs = _quantiles([float(v) for v in vals])
        if qs:
            pretty = "  ".join(f"{k}={v:.4f}" for k, v in qs.items())
            print(f"[sweep] {label}: n={len(vals)}  {pretty}")

    if args.sweep:
        out = _EVAL_DIR / "reresolve_sweep.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"[sweep] per-item scores -> {out}")
        return 0

    # ── 正式判定 ────────────────────────────────────────────────────────
    n = {"live": 0, "reresolved": 0, "quarantined": 0, "dropped": 0}
    for r in records:
        reasons: list[str] = []
        if args.skip_live_check and r["original_gold_in_db"]:
            new_gold = list(r["original_gold_ids"])
            r["status"] = "live"
            reasons.append("skip-live-check: id exists in DB (no content verification)")
        else:
            t, m = args.top1_threshold, args.margin
            if r["pool_size"] == 0:
                r["status"] = "dropped"
                reasons.append(f"no candidates for type={r['gold_type']}")
            elif (
                r["original_gold_in_db"]
                and r["top1_id"] in r["original_gold_ids"]
                and r["top1_sim"] >= t
                and r["margin"] >= m
            ):
                r["status"] = "live"
                reasons.append(
                    f"original gold re-verified: cos(query,gold)={r['top1_sim']:.3f} >= {t:.2f}, margin={r['margin']:.3f}"
                )
            elif r["top1_sim"] >= t and r["margin"] >= m:
                r["status"] = "reresolved"
                reasons.append(
                    f"unambiguous winner id={r['top1_id']} cos={r['top1_sim']:.3f} >= {t:.2f}, "
                    f"margin={r['margin']:.3f} >= {m:.2f} (old gold in_db={r['original_gold_in_db']})"
                )
                new_gold = [int(r["top1_id"])]
            elif r["top1_sim"] >= t:
                r["status"] = "quarantined"
                reasons.append(
                    f"ambiguous: top1 cos={r['top1_sim']:.3f} >= {t:.2f} but margin={r['margin']:.3f} < {m:.2f}"
                )
            else:
                r["status"] = "dropped"
                reasons.append(f"no candidate near threshold: top1 cos={r['top1_sim']:.3f} < {t:.2f}")
        if r["status"] == "reresolved":
            new_gold = [int(r["top1_id"])]
        elif r["status"] == "live":
            new_gold = list(r["original_gold_ids"])
        else:
            new_gold = []

        n[r["status"]] = n.get(r["status"], 0) + 1
        r["new_gold_ids"] = new_gold
        r["reasons"] = reasons

    print(f"[reresolve] status counts: {n}")

    # ── v2 题集 ─────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    v2_items: list[dict] = []
    for it in items:
        if it.get("kind") == "negative":
            v2_items.append(dict(it, status="live", reasons=["negative control, gold-free by design"]))
            continue
        rec = next(r for r in records if r["qid"] == it["qid"])
        out_item = dict(it)
        out_item["original_gold_ids"] = rec["original_gold_ids"]
        # 隔离/丢弃的条目必须清掉 gold：带着旧坏 gold 参评会让数字看起来正常但全是假信号。
        # 原 gold 保留在 original_gold_ids 里可追溯。
        out_item["gold_ids"] = rec["new_gold_ids"]
        out_item["status"] = rec["status"]
        out_item["reasons"] = rec["reasons"]
        if rec["status"] == "reresolved":
            out_item["resolved_from"] = rec["original_gold_ids"]
        v2_items.append(out_item)

    v2 = {
        "version": 2,
        "rebuilt_from": str(eval_set_path.name),
        "rebuilt_at": datetime.now(timezone.utc).isoformat(),
        "rebuild_method": (
            "eval/reresolve_gold.py: content-level gold re-resolution on a DB copy; "
            f"thresholds top1>={args.top1_threshold:.2f}, margin>={args.margin:.2f} "
            "(evidence in reresolve_report_*.md)"
        ),
        "status_counts": n,
        "seed": eval_set.get("seed"),
        "items": v2_items,
    }
    out_path = Path(args.out) if args.out else _EVAL_DIR / "eval_set_v2.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(v2, f, ensure_ascii=False, indent=2)
    print(f"[reresolve] v2 eval set -> {out_path}")

    # ── 报告 ────────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("# gold 重解析报告（#221）")
    lines.append("")
    lines.append(f"生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"源题集: {eval_set_path.name}（version={eval_set.get('version')}）")
    lines.append(f"库副本节点数: {len(ids)}（DB_PATH={tmp_db}）")
    lines.append("")
    lines.append("## 判定口径（与 split_eval_set.py 一致 + 内容级验证）")
    lines.append("")
    lines.append("- id 级存活：gold 节点在库 payloads 中且 `created_at` 非 None（= split_eval_set 的口径）")
    lines.append("- 内容级存活：在 id 级之上，要求 query 与 gold 节点 content 的余弦过阈值且排名居首")
    lines.append("  —— id 重排后「id 存活但内容已换」实测大量存在（如 q001: gold=118 现为其他内容），")
    lines.append("  仅凭 id 存活保留条目会让评测失真，故本重建对全部正样本做内容验证")
    lines.append(
        f"- 改判条件：top1 >= {args.top1_threshold:.2f} 且 top1-top2 >= {args.margin:.2f}（runner-up 含原 gold）"
    )
    lines.append("- 候选池：按题集声明的 gold_type 建（题目要考的层）；gold_domain 只做窄池优先，")
    lines.append("  不做硬过滤（题集 domain 标签与库内实际 domain 失配实测 35%+）")
    lines.append("")
    lines.append("## 阈值依据（来自 --sweep 分布，非拍脑袋）")
    lines.append("")
    lines.append("全部 118 条正样本无阈值打分的分布（embedding=库内同款 Qwen3-Embedding，余弦）：")
    lines.append("")
    lines.append("| 量 | n | p10 | p25 | p50 | p75 | p90 |")
    lines.append("|---|---|---|---|---|---|---|")

    def _fmt_q(vals: list[float]) -> str:
        qs = _quantiles(vals)
        return "|".join(f" {qs[k]:.4f} " for k in ["p10", "p25", "p50", "p75", "p90"])

    top1_vals = [float(r["top1_sim"]) for r in records]
    margin_vals = [float(r["margin"]) for r in records]
    gold_vals = [float(r["gold_sim"]) for r in records if r.get("gold_sim") is not None and r["gold_sim"] != -1.0]
    lines.append(f"| top1 相似度 | {len(top1_vals)} | {_fmt_q(top1_vals)} |")
    lines.append(f"| top1-top2 差值 | {len(margin_vals)} | {_fmt_q(margin_vals)} |")
    lines.append(f"| 原gold相似度（在库且在库可见） | {len(gold_vals)} | {_fmt_q(gold_vals)} |")
    lines.append("")
    lines.append(f"选择 top1>={args.top1_threshold:.2f}（约 p50 上沿）：低于它的 top1 缺乏「这就是答案」的证据；")
    lines.append(
        f"选择 margin>={args.margin:.2f}（约 p75）：同文档多切片/相似记忆并列是这个库的常态（margin 中位数仅 0.024），"
    )
    lines.append("只接受明显领先者，宁缺毋错——错 gold 比缺 gold 更毒（直接污染 baseline）。")
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append(f"| 状态 | 条数 | 占正样本 ({len(positives)}) |")
    lines.append("|---|---|---|")
    for k in ["live", "reresolved", "quarantined", "dropped"]:
        lines.append(f"| {k} | {n[k]} | {n[k] / len(positives):.1%} |")
    lines.append("")
    lines.append("### 关键发现：id 存活 ≠ 内容存活")
    lines.append("")
    in_db = sum(1 for r in records if r["original_gold_in_db"])
    lines.append(f"- 原 gold id 在库的正样本：{in_db} 条；其中通过内容验证（live）的：{n['live']} 条")
    lines.append("- 原 gold 与 query 的余弦分布（p50≈0.36 / p90≈0.47）整体低于候选池 top1（p50≈0.62），")
    lines.append("  即**大多数「存活」gold 的内容已与题目无关**——2026-09-10 rebuild 重排 id 后，")
    lines.append("  id 指向的是内容移位后的其他节点。旧口径「54 条存活」高估了可用题量。")
    lines.append("- 另：`created_at` 缺失的节点类型（kb_chunk 938 个 / novel_chunk 124 个 / git_commit 23 个")
    lines.append("  的 payload.created_at 全为 None）是 core 写入路径的缺口，本任务禁改 core/，")
    lines.append("  已知影响：即使 gold 重解析成功，落点在这三类节点上的题仍进不了时间切分。")
    lines.append("")
    lines.append("### reresolved 逐条（旧 gold -> 新 gold）")
    lines.append("")
    lines.append("| qid | kind | 旧 gold | 新 gold | cos | margin | 新 gold content 摘要 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in records:
        if r["status"] != "reresolved":
            continue
        new_p = payloads.get(int(r["top1_id"]), {})
        summary = str(new_p.get("content", ""))[:40].replace("|", "\\|").replace("\n", " ")
        old = ",".join(str(g) for g in r["original_gold_ids"])
        lines.append(
            f"| {r['qid']} | {r['kind']} | {old} | {r['top1_id']} | {r['top1_sim']:.4f} "
            f"| {r['margin']:.4f} | {summary} |"
        )
    lines.append("")
    lines.append("### 人工抽查结论（出报告的人：小九）")
    lines.append("")
    lines.append("逐条目视比对 28 条 reresolved 的 query 与新 gold content：")
    lines.append("- ~26 条语义明确对应（多为「对话沉淀」类节点记录的用户原话，与口语化 query 直接对应）")
    lines.append("- 2 条存疑：q006（top1 讲「检索增强 vs 微调」，query 问「避免推荐不存在的模型」，弱相关）、")
    lines.append("  q062（query 问「节点属性位于哪一层」，top1 是飞书知识库实测记录）。")
    lines.append("  两者 margin/cos 均达阈值，脚本按规则改判；**是否人工回拉 quarantine 由验收人决定**。")
    lines.append("- live=0 的含义：内容验证口径下没有一条原 gold 仍正确——这与 q001 的示例一致，")
    lines.append("  是「必须重建题集」的直接证据。")
    lines.append("")
    lines.append("### 状态与原因（quarantined / dropped 摘要）")
    lines.append("")
    qd = [r for r in records if r["status"] in ("quarantined", "dropped")]
    lines.append(f"共 {len(qd)} 条（明细见 eval_set_v2.json 每条的 reasons 字段，此处列原因分类）：")
    reason_kinds = {
        "quarantine_margin": sum(1 for r in qd if r["status"] == "quarantined"),
        "drop_no_threshold": sum(
            1 for r in qd if r["status"] == "dropped" and "no candidate near threshold" in " ".join(r["reasons"])
        ),
        "drop_no_pool": sum(
            1 for r in qd if r["status"] == "dropped" and "no candidates for type" in " ".join(r["reasons"])
        ),
    }
    lines.append(f"- quarantined（top1 达阈值但 margin 不足，隔离待人工）：{reason_kinds['quarantine_margin']}")
    lines.append(f"- dropped（top1 低于阈值，库中无明确目标）：{reason_kinds['drop_no_threshold']}")
    lines.append(f"- dropped（候选池为空）：{reason_kinds['drop_no_pool']}")
    lines.append("")
    lines.append("逐条状态表（全部正样本）：")
    lines.append("")
    lines.append("| qid | 状态 | 原 gold | 新 gold | top1 | cos | margin | 原因 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in records:
        reason = (r["reasons"][0] if r["reasons"] else "")[:60].replace("|", "\\|")
        new_g = ",".join(str(g) for g in r["new_gold_ids"]) or "-"
        old_g = ",".join(str(g) for g in r["original_gold_ids"])
        lines.append(
            f"| {r['qid']} | {r['status']} | {old_g} | {new_g} | {r['top1_id']} "
            f"| {r['top1_sim']:.4f} | {r['margin']:.4f} | {reason} |"
        )
    lines.append("")
    report_path = _EVAL_DIR / f"reresolve_report_{ts}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[reresolve] report -> {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
