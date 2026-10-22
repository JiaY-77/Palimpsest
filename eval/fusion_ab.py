"""融合权重 A/B 评测：同一批通道排名，用不同融合方案重排，比较检索质量。

设计要点（为什么这样做）
------------------------
融合方案的差异只体现在「怎么把两路排名合成一个顺序」。如果每个方案各自走一遍
线上检索，语义通道的 embedding、FTS 的降级、库的瞬时状态都会引入噪声，A/B 的
差值就不再只归因于融合逻辑。因此本脚本：

1. 每题**只采集一次**两路原始排名（语义 top-N 带分数、FTS top-N 带 bm25 rank）；
2. 四套融合方案在内存里对同一份排名重排；
3. 指标在重排后的顺序上计算（Recall@k / MRR@k / nDCG@k）。

生产代码零改动：方案 A/B/D 直接复用 ``mcp_tools.memory._rrf_fuse``（纯函数），
方案 C 在本文件内实现归一化融合。库走 eval/.tmp_fusion 副本，跑前跑后 SHA256 自证。

四套方案
--------
A. 基线        全局 w_sem=1.0 / w_fts=0.1（改动前的线上行为）
B. 分档        精确标识符 query 用 w_fts=1.0，其余仍 0.1（已上线的 RRF_FTS_EXACT_WEIGHT）
C. 归一化      通道内分数 min-max 归一化后加权求和（替代纯 rank 位置）
D. √n 补偿     w_fts_eff = w_fts * sqrt(n_sem / n_fts)，FTS 候选越少相对抬得越高

用法
----
    venv/Scripts/python.exe eval/fusion_ab.py --eval-set eval/fusion_set_v1.json
    venv/Scripts/python.exe eval/fusion_ab.py --tier "" --top-k 10      # 不过滤分层（推荐）
    venv/Scripts/python.exe eval/fusion_ab.py --limit 5                 # 冒烟
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 路径与库副本（必须在 import 项目模块之前完成，config.py 在 import 时读 env）──
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_EVAL_DIR = Path(__file__).resolve().parent
_TMP_DIR = _EVAL_DIR / ".tmp_fusion"
sys.path.insert(0, _PROJECT_ROOT)


def _locate_originals() -> tuple[Path, Path]:
    """定位原始库与 FTS 索引（支持 DB_PATH 覆盖，与 run_eval.py 同口径）。"""
    db_env = os.getenv("DB_PATH", "")
    if db_env and os.path.isabs(db_env):
        db = Path(db_env)
    elif db_env:
        db = Path(_PROJECT_ROOT) / db_env
    else:
        db = Path(_PROJECT_ROOT) / "data" / "mh_memory.db"
    return db, db.parent / "fts.db"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_db_to_tmp() -> Path:
    """复制主库 + 全部 sidecar + FTS 索引到 .tmp_fusion/，并把 DB_PATH 指过去。

    主库是一组文件（.db/.vec/.pld/.gidx/.pidx/.flush_ok/.wal），少拷任何一个
    都会让 store 打开时重建索引或读到半截状态——所以按前缀整体复制。
    """
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    orig_db, orig_fts = _locate_originals()
    tmp = _TMP_DIR.resolve()

    for p in orig_db.parent.iterdir():
        if p.name.startswith(orig_db.name) and p.is_file():
            dst = tmp / p.name
            if p.resolve() == dst:
                continue
            shutil.copy2(p, dst)

    if orig_fts.exists():
        dst = tmp / orig_fts.name
        if orig_fts.resolve() != dst:
            shutil.copy2(orig_fts, dst)

    os.environ["DB_PATH"] = str(tmp / orig_db.name)
    return tmp


_COPY_DIR = _copy_db_to_tmp()

# 现在可以安全 import 项目模块了
from core.fts_index import build_fts_query  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402
from mcp_tools.memory import (  # noqa: E402
    _mem_search_impl,
    _rrf_fuse,
    _tier_matches,
)

sys.path.insert(0, str(_EVAL_DIR))
from metrics import mrr_at_k, ndcg_at_k, recall_at_k  # noqa: E402

# 生产常量（对照基准必须与线上一致，改动时只改这里）
PROD_K = 60.0
PROD_W_SEM = 1.0
PROD_W_FTS = 0.1
EXACT_W_FTS = 1.0

# 采集深度：要足够深，否则排名靠后的 gold 在融合前就被截断，
# A/B 差值会被「采集深度」而不是融合逻辑决定。
COLLECT = 30


# ══════════════════════════════════════════════════════════════════════
# 通道采集
# ══════════════════════════════════════════════════════════════════════
def collect_semantic(query: str, top_k: int, tier: str) -> list[tuple[int, float]]:
    """语义通道排名：复用 _mem_search_impl（含 scope/tier/outdated 过滤链）。"""
    res = _mem_search_impl(
        query,
        scope="all",
        domain="",
        domain_bias="",
        top_k=top_k,
        include_neighbors=False,
        include_outdated=False,
        tier=tier,
    )
    items = res.get("results", [])
    return [(it["id"], float(it.get("score") or 0.0)) for it in items if it.get("id") is not None]


def collect_fts(query: str, top_k: int, store: TriviumStore, tier: str) -> list[tuple[int, float]]:
    """FTS 通道排名：返回 [(node_id, bm25_score)]，score 已取负号（越大越相关）。

    刻意不复用 search_fts()：它只回顺序、丢掉 bm25 分数，而方案 C 需要通道内
    原始分数做归一化。匹配表达式与过滤口径抄自 core.fts_index.search_fts_status
    与 run_eval._run_fts，保证与线上同一条路径。
    """
    q = (query or "").strip()
    if not q:
        return []
    fts_path = Path(os.environ["DB_PATH"]).parent / "fts.db"
    if not fts_path.exists():
        return []

    conn = None
    try:
        conn = sqlite3.connect(fts_path.as_uri() + "?mode=ro", uri=True)
        limit = max(top_k * 3, 30)
        if len(q) > 8 and '"' not in q:
            fts_query = build_fts_query(q)
            if fts_query:
                rows = conn.execute(
                    "SELECT node_id, rank FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT node_id, rank FROM mem_fts WHERE content LIKE ? LIMIT ?",
                    (f"%{q}%", int(limit)),
                ).fetchall()
        elif len(q) >= 3 and '"' not in q:
            rows = conn.execute(
                "SELECT node_id, rank FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank LIMIT ?",
                (f'"{q}"', int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT node_id, rank FROM mem_fts WHERE content LIKE ? LIMIT ?",
                (f"%{q}%", int(limit)),
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    out: list[tuple[int, float]] = []
    for node_id, rank in rows:
        if node_id is None:
            continue
        payload = (store.get_node(int(node_id)) or {}).get("payload", {}) or {}
        # 与线上 FTS-only 侧同口径：outdated 剔除 + tier 过滤
        if payload.get("status") == "outdated":
            continue
        if tier and not _tier_matches(payload.get("type", ""), tier):
            continue
        out.append((int(node_id), -float(rank or 0.0)))  # bm25 越小越相关 → 取负变越大越相关
        if len(out) >= top_k:
            break
    return out


# ══════════════════════════════════════════════════════════════════════
# 四套融合方案
# ══════════════════════════════════════════════════════════════════════
def _ids(ranked: list[tuple[int, float]]) -> list[int]:
    return [nid for nid, _ in ranked]


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 if hi > 0 else 0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def fuse_baseline(sem: list[tuple[int, float]], fts: list[tuple[int, float]], top_k: int, exact: bool) -> list[int]:
    """A：全局 w_fts=0.1，不分档。"""
    fused = _rrf_fuse(_ids(sem), _ids(fts), top_k, PROD_K, PROD_W_SEM, PROD_W_FTS)
    return [nid for nid, _, _, _ in fused]


def fuse_tiered(sem: list[tuple[int, float]], fts: list[tuple[int, float]], top_k: int, exact: bool) -> list[int]:
    """B：精确标识符 query 抬到 w_fts=1.0（已上线行为）。"""
    w_fts = EXACT_W_FTS if exact else PROD_W_FTS
    fused = _rrf_fuse(_ids(sem), _ids(fts), top_k, PROD_K, PROD_W_SEM, w_fts)
    return [nid for nid, _, _, _ in fused]


def fuse_normalized(sem: list[tuple[int, float]], fts: list[tuple[int, float]], top_k: int, exact: bool) -> list[int]:
    """C：通道内 min-max 归一化后加权求和（用原始分数，不只看排名位置）。

    权重与 A 对齐（1.0 / 0.1），只把「rank 位置」换成「归一化分数」，
    这样 A vs C 的差异纯粹来自融合信号的选择。
    """
    w_fts = EXACT_W_FTS if exact else PROD_W_FTS
    scores: dict[int, float] = {}
    for (nid, _), norm in zip(sem, _minmax([s for _, s in sem]), strict=True):
        scores[nid] = scores.get(nid, 0.0) + PROD_W_SEM * norm
    for (nid, _), norm in zip(fts, _minmax([s for _, s in fts]), strict=True):
        scores[nid] = scores.get(nid, 0.0) + w_fts * norm
    return [nid for nid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)][:top_k]


def fuse_comp_pure(sem: list[tuple[int, float]], fts: list[tuple[int, float]], top_k: int, exact: bool) -> list[int]:
    """D：**纯**候选数补偿，不分档——w_fts = 0.1·√(n_sem/n_fts)。

    刻意不叠加上线方案的分档，用来单独度量「补偿」这一项自身的贡献。
    """
    n_sem, n_fts = len(sem), len(fts)
    w_fts = PROD_W_FTS * min(math.sqrt(max(n_sem, 1) / max(n_fts, 1)), 10.0)
    fused = _rrf_fuse(_ids(sem), _ids(fts), top_k, PROD_K, PROD_W_SEM, w_fts)
    return [nid for nid, _, _, _ in fused]


def fuse_tiered_comp(sem: list[tuple[int, float]], fts: list[tuple[int, float]], top_k: int, exact: bool) -> list[int]:
    """E：分档 **+** 候选数补偿——两者叠加，度量联合效果。"""
    base = EXACT_W_FTS if exact else PROD_W_FTS
    n_sem, n_fts = len(sem), len(fts)
    w_fts = base * min(math.sqrt(max(n_sem, 1) / max(n_fts, 1)), 10.0)
    fused = _rrf_fuse(_ids(sem), _ids(fts), top_k, PROD_K, PROD_W_SEM, w_fts)
    return [nid for nid, _, _, _ in fused]


SCHEMES = {
    "A_baseline": fuse_baseline,
    "B_tiered": fuse_tiered,
    "C_normalized": fuse_normalized,
    "D_comp_pure": fuse_comp_pure,
    "E_tiered_comp": fuse_tiered_comp,
}

SCHEME_LABELS = {
    "A_baseline": "A 基线 w_fts=0.1",
    "B_tiered": "B 分档 exact→1.0",
    "C_normalized": "C 归一化分数",
    "D_comp_pure": "D 纯√n补偿",
    "E_tiered_comp": "E 分档+√n补偿",
}


# ══════════════════════════════════════════════════════════════════════
# 精确标识符判定（与 core.search 同规则，此处独立实现一份便于报告标注）
# ══════════════════════════════════════════════════════════════════════
def _is_exact(query: str) -> bool:
    from core.search import is_exact_identifier_query

    return is_exact_identifier_query(query)


# ══════════════════════════════════════════════════════════════════════
# 指标与报告
# ══════════════════════════════════════════════════════════════════════
METRICS = ("hit@5", "hit@10", "recall@5", "recall@10", "mrr@10", "ndcg@10")


def score_item(ranked: list[int], gold: set[int]) -> dict[str, float]:
    """hit@k 与 recall@k 一起报，是因为 multi-gold 题的 recall 有数学上限。

    例：某标识符的可见 gold 有 36 条，top-5 最多也只能覆盖 5/36≈0.14——这个数字
    反映的是 gold 规模，不是检索好坏。hit@k（前 k 条里至少有一条 gold）则不受
    gold 规模影响，更接近「用户问一句，能不能找到东西」的真实体验。
    """
    top5, top10 = ranked[:5], ranked[:10]
    return {
        "hit@5": 1.0 if any(g in top5 for g in gold) else 0.0,
        "hit@10": 1.0 if any(g in top10 for g in gold) else 0.0,
        "recall@5": recall_at_k(ranked, gold, 5),
        "recall@10": recall_at_k(ranked, gold, 10),
        "mrr@10": mrr_at_k(ranked, gold, 10),
        "ndcg@10": ndcg_at_k(ranked, gold, 10),
    }


def avg(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def build_report(
    rows: list[dict],
    sha_before: str,
    sha_after: str,
    eval_set_name: str,
    top_k: int,
    tier: str,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    kinds = sorted({r["kind"] for r in rows})
    lines: list[str] = []
    lines.append("# 融合权重 A/B 评测报告")
    lines.append("")
    lines.append(f"- 生成时间: {now}")
    lines.append(f"- 题集: `{eval_set_name}`（{len(rows)} 题）")
    lines.append(f"- 采集深度: 每路 top-{COLLECT}，评估 top-{top_k}")
    lines.append(f"- tier 口径: `{tier or '(不过滤)'}`")
    lines.append(
        f"- 库完整性: 跑前 `{sha_before[:16]}…` / 跑后 `{sha_after[:16]}…` → {'✅ 一致' if sha_before == sha_after else '❌ 不一致！'}"
    )
    lines.append("")

    # ── 按 kind 分组 × 方案 ──
    for kind in [*kinds, "ALL"]:
        subset = rows if kind == "ALL" else [r for r in rows if r["kind"] == kind]
        if not subset:
            continue
        title = "全部题目" if kind == "ALL" else f"kind = `{kind}`"
        lines.append(f"## {title}（{len(subset)} 题）")
        lines.append("")
        header = "| 方案 | " + " | ".join(METRICS) + " |"
        lines.append(header)
        lines.append("|---|" + "|".join(["---"] * len(METRICS)) + "|")
        for scheme, label in SCHEME_LABELS.items():
            cells = []
            for m in METRICS:
                cells.append(f"{avg([r['scores'][scheme][m] for r in subset]):.3f}")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")

    # ── 判据检查 ──
    lines.append("## 判据检查（相对 A 基线）")
    lines.append("")
    lines.append(
        "主判据用 **hit@5**（前 5 条里能否命中至少一条 gold）：multi-gold 题的 recall@5 有数学上限"
        "（gold 36 条时上限 5/36≈0.14），直接比 recall 会把「gold 规模」误读成「检索质量」。"
        "recall@5 一并给出，用来看覆盖面变化。"
    )
    lines.append("")
    lines.append("判据：三类 query 的 hit@5 都不低于基线，且至少一类有提升。")
    lines.append("")
    lines.append(
        "| 方案 | "
        + " | ".join(f"{k} Δhit@5" for k in kinds)
        + " | "
        + " | ".join(f"{k} ΔR@5" for k in kinds)
        + " | 结论 |"
    )
    ncol = len(kinds) * 2 + 1
    lines.append("|---|" + "|".join(["---"] * ncol) + "|")
    for scheme, label in SCHEME_LABELS.items():
        if scheme == "A_baseline":
            continue
        hit_deltas: list[float] = []
        hit_cells: list[str] = []
        rec_cells: list[str] = []
        for kind in kinds:
            subset = [r for r in rows if r["kind"] == kind]
            if not subset:
                hit_cells.append("—")
                rec_cells.append("—")
                continue
            base_hit = avg([r["scores"]["A_baseline"]["hit@5"] for r in subset])
            cur_hit = avg([r["scores"][scheme]["hit@5"] for r in subset])
            d_hit = cur_hit - base_hit
            hit_deltas.append(d_hit)
            hit_cells.append(f"{d_hit:+.3f}")
            base_rec = avg([r["scores"]["A_baseline"]["recall@5"] for r in subset])
            cur_rec = avg([r["scores"][scheme]["recall@5"] for r in subset])
            rec_cells.append(f"{cur_rec - base_rec:+.3f}")
        verdict = (
            "✅ 全不劣且有提升"
            if hit_deltas and all(d >= 0 for d in hit_deltas) and any(d > 0 for d in hit_deltas)
            else ("❌ 有类别下降" if any(d < 0 for d in hit_deltas) else "➖ 无提升")
        )
        lines.append(f"| {label} | " + " | ".join(hit_cells) + " | " + " | ".join(rec_cells) + f" | {verdict} |")
    lines.append("")

    # ── 逐题对照（gold 首次命中排名）──
    lines.append("## 逐题对照（gold 首次命中排名，miss = 未进采集深度）")
    lines.append("")
    header = "| QID | kind | query | " + " | ".join(SCHEME_LABELS.values()) + " |"
    lines.append(header)
    lines.append("|---|---|---|" + "|".join(["---"] * len(SCHEME_LABELS)) + "|")
    for r in rows:
        cells = []
        for scheme in SCHEME_LABELS:
            ranks = r["ranks"][scheme]
            hit = next((i + 1 for i, nid in enumerate(ranks) if nid in r["gold"]), None)
            cells.append(str(hit) if hit else "miss")
        q = r["query"][:40].replace("|", "\\|")
        lines.append(f"| {r['qid']} | {r['kind']} | {q} | " + " | ".join(cells) + " |")
    lines.append("")

    # ── 通道覆盖 ──
    lines.append("## 通道覆盖（金标是否进入各通道候选）")
    lines.append("")
    lines.append("| QID | gold 数 | 语义路命中 | FTS 路命中 | 两路皆无 |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        in_sem = len(r["gold"] & set(r["sem_ids"]))
        in_fts = len(r["gold"] & set(r["fts_ids"]))
        both_miss = len(r["gold"]) - len(r["gold"] & (set(r["sem_ids"]) | set(r["fts_ids"])))
        lines.append(f"| {r['qid']} | {len(r['gold'])} | {in_sem} | {in_fts} | {both_miss} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="融合权重 A/B 评测")
    ap.add_argument("--eval-set", default=None, help="题集 JSON（默认 eval/fusion_set_v1.json）")
    ap.add_argument("--top-k", type=int, default=10, help="评估截断（默认 10）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    ap.add_argument(
        "--tier",
        default="",
        help='记忆分层：""=不过滤（默认，排除 tier 混淆变量）| facts | logs',
    )
    ap.add_argument("--out", default=None, help="报告输出路径（默认 eval/report_fusion_<ts>.md）")
    args = ap.parse_args()

    eval_path = Path(args.eval_set) if args.eval_set else _EVAL_DIR / "fusion_set_v1.json"
    if not eval_path.exists():
        print(f"[fusion_ab] 题集不存在: {eval_path}")
        return 1
    with open(eval_path, encoding="utf-8") as f:
        eval_set = json.load(f)
    items = eval_set["items"]
    if args.limit:
        items = items[: args.limit]

    orig_db, _ = _locate_originals()
    sha_before = _sha256(orig_db)

    store = TriviumStore()
    print(f"[fusion_ab] 题集 {eval_path.name}，{len(items)} 题；副本 {os.environ['DB_PATH']}")

    rows: list[dict] = []
    for idx, item in enumerate(items, 1):
        qid, query = item["qid"], item["query"]
        gold = set(item.get("gold_ids") or [])
        if not gold:
            print(f"  [{idx}/{len(items)}] {qid} 跳过（无 gold）")
            continue
        sem = collect_semantic(query, COLLECT, args.tier)
        fts = collect_fts(query, COLLECT, store, args.tier)
        exact = _is_exact(query)
        ranks = {name: fn(sem, fts, args.top_k, exact) for name, fn in SCHEMES.items()}
        scores = {name: score_item(ranked, gold) for name, ranked in ranks.items()}
        rows.append(
            {
                "qid": qid,
                "kind": item.get("kind", "semantic"),
                "query": query,
                "gold": gold,
                "exact": exact,
                "sem_ids": [n for n, _ in sem],
                "fts_ids": [n for n, _ in fts],
                "ranks": ranks,
                "scores": scores,
            }
        )
        best = max(SCHEME_LABELS, key=lambda s: scores[s]["recall@5"])
        print(
            f"  [{idx}/{len(items)}] {qid} exact={exact} "
            f"sem={len(sem)} fts={len(fts)} best={best} R@5={scores[best]['recall@5']:.2f}"
        )

    sha_after = _sha256(orig_db)
    if sha_before != sha_after:
        print("[fusion_ab] ⚠️ 原始库 SHA256 变化！评测可能写到了真实库，立即检查")

    report = build_report(rows, sha_before, sha_after, eval_path.name, args.top_k, args.tier)
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out) if args.out else _EVAL_DIR / f"report_fusion_{ts}.md"
    out_path.write_text(report, encoding="utf-8")
    results_path = _EVAL_DIR / f"results_fusion_{ts}.json"
    results_path.write_text(
        json.dumps(
            {
                "eval_set": eval_path.name,
                "top_k": args.top_k,
                "tier": args.tier,
                "collect": COLLECT,
                "sha256_before": sha_before,
                "sha256_after": sha_after,
                "rows": [
                    {
                        "qid": r["qid"],
                        "kind": r["kind"],
                        "query": r["query"],
                        "gold": sorted(r["gold"]),
                        "exact": r["exact"],
                        "sem_ids": r["sem_ids"],
                        "fts_ids": r["fts_ids"],
                        "ranks": r["ranks"],
                        "scores": r["scores"],
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n[fusion_ab] 报告 → {out_path}")
    print(f"[fusion_ab] 原始结果 → {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
