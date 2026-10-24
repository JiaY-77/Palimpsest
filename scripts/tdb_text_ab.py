#!/usr/bin/env python
"""
TDB BM25 文本通道 A/B 对比脚本（feat/tdb-text-channel 交付物）。

产出三组可复现的对比数据：
  FTS-only   —— 现行 FTS5 trigram 通道单独检索
  TDB-only   —— 新增 TDB BM25 文本通道单独检索
  融合(权重x) —— 语义 + FTS + TDB 的 RRF 融合（TDB 权重可参数化）

输出：每个查询下各路的命中数、FTS/TDB 重合数、top3 列表；末尾汇总。

用法：
  venv/Scripts/python.exe scripts/tdb_text_ab.py --db <副本库路径>
      [--queries "兜底" "接续点" ...] [--limit 10] [--weight 0.5]

--db 传生产库副本路径，勿用生产库本体。副本默认没有 .text 文本侧车，
传 --rebuild 让脚本在副本上全量重建文本索引（不影响 fts.db，主库只读）。

安全：主库只读（read_only）；--rebuild 只在副本库上执行（它会写副本的
.text 侧车，这是重建语义的一部分）；绝不写主库。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_QUERIES = [
    "兜底",  # 2 字短词：FTS 大弱点场景
    "降级",  # 2 字短词
    "接续点",  # 3 字
    "混合检索降级",  # 6 字长短语：FTS 整串 MATCH 0 命中场景
    "记忆写入冲突检测",  # 8 字长短语
]


def _ensure_fts_for_copy(copy_db: str) -> str:
    """给副本目录备一份 fts.db（FTS 索引不随 trivium 备份组走，副本缺它时
    FTS-only 会全员 degraded、对比白跑）。

    * copy 目录已有 → 直接用。
    * 原库（.env 指向的生产 data/）目录有 fts.db → 用 **sqlite backup API**
      在线快照到副本目录（绝不直接 cp 运行中的文件——上轮拷运行库混合
      generation 的事故教训；backup API 保证事务一致性，且只读源库）。
    * 两处都没有 → 返回 ""，调用方如实打出 degraded 并继续跑 TDB-only。
    """
    copy_fts = os.path.join(os.path.dirname(copy_db), "fts.db")
    if os.path.exists(copy_fts):
        return copy_fts
    src = os.path.join(os.path.dirname(os.environ.get("DB_PATH_ORIGINAL", "")), "fts.db")
    if not os.path.exists(src):
        return ""
    import sqlite3

    os.makedirs(os.path.dirname(copy_db) or ".", exist_ok=True)
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(copy_fts)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return copy_fts


def main() -> int:
    from config import Config
    from core import tdb_text_index
    from core.fts_index import search_fts_status

    ap = argparse.ArgumentParser(description="TDB BM25 vs FTS5 文本召回 A/B")
    ap.add_argument("--db", default=Config.DB_PATH, help="主库路径（建议传副本路径）")
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES, help="查询集（参数化）")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--weight", type=float, default=float(os.getenv("TDB_TEXT_WEIGHT", "1.0")))
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="副本库缺 .text 侧车时，先在副本上全量重建文本索引（写副本、不写主库）",
    )
    ap.add_argument("--output", default="", help="另写 JSON 到该路径（可选）")
    args = ap.parse_args()

    # 保留原始库目录供 _ensure_fts_for_copy 找 fts.db 源（backup API 快照）
    os.environ["DB_PATH_ORIGINAL"] = Config.DB_PATH
    os.environ.setdefault("DB_PATH", args.db)
    Config.DB_PATH = args.db  # 本进程内生效（不写回磁盘）
    _ensure_fts_for_copy(args.db)

    # 文本侧车不存在：--rebuild 才在副本上重建；否则显式告知后退出
    sidecar = args.db + ".text"
    if not os.path.exists(sidecar):
        if args.rebuild:
            print("[rebuild] 副本缺 .text 侧车，全量重建文本索引（只写副本）…")
            import time as _t

            from core.trivium_store import TriviumStore as _TS

            t0 = _t.perf_counter()
            count = tdb_text_index.rebuild(_TS(read_only=False))
            print(f"[rebuild] 完成 entries={count} 耗时={((_t.perf_counter() - t0) * 1000):.0f}ms")
        else:
            print(
                f"[提示] .text 侧车不存在（{sidecar}）。\n"
                "       副本库加 --rebuild 让本脚本在副本上重建（不碰生产库）。\n"
            )
            return 2

    if not tdb_text_index.warmup():
        print("[错误] TDB 文本索引预热失败，A/B 无法进行。", file=sys.stderr)
        return 2

    rows_summary = []
    for query in args.queries:
        fts_rows, fts_status = search_fts_status(query, limit=args.limit)
        tdb_rows, tdb_status = tdb_text_index.search(query, limit=args.limit)
        fts_ids = [r.get("node_id") for r in fts_rows]
        tdb_ids = [r.get("node_id") for r in tdb_rows]
        overlap = sorted(n for n in set(fts_ids) & set(tdb_ids) if n is not None)
        # 融合（RRF，双重加权：语义路缺省不计（只比文本两路）——这里模拟
        # _hybrid_rrf 的纯文本对比视角：FTS 与 TDB 各自独立 top）
        rrf: dict = {}
        for rank, nid in enumerate(fts_ids):
            if nid is None:
                continue
            rrf[nid] = rrf.get(nid, 0.0) + (Config.RRF_FTS_WEIGHT or 1.0) / (60 + rank)
        for rank, nid in enumerate(tdb_ids):
            if nid is None:
                continue
            rrf[nid] = rrf.get(nid, 0.0) + args.weight / (60 + rank)
        fused = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)[: args.limit]
        rows_summary.append(
            {
                "query": query,
                "fts": {"status": fts_status, "count": len(fts_ids), "top3": fts_ids[:3]},
                "tdb": {"status": tdb_status, "count": len(tdb_ids), "top3": tdb_ids[:3]},
                "overlap_ids": overlap,
                "overlap": len(overlap),
                "fused_top3": [nid for nid, _ in fused[:3]],
            }
        )
        print(f"query={query!r}")
        print(f"  FTS-only : status={fts_status:8s} 命中 {len(fts_ids):3d}  top3={fts_ids[:3]}")
        print(f"  TDB-only : status={tdb_status:8s} 命中 {len(tdb_ids):3d}  top3={tdb_ids[:3]}")
        print(f"  重合     : {len(overlap):3d}  ids={overlap}")
        print(f"  融合w{args.weight}: top3={[nid for nid, _ in fused[:3]]}")
        print()

    tdb_ok = sum(1 for r in rows_summary if r["tdb"]["status"] == "ok")
    fts_ok = sum(1 for r in rows_summary if r["fts"]["status"] == "ok")
    print(f"[汇总] queries={len(rows_summary)}  FTS-ok={fts_ok}  TDB-ok={tdb_ok}")

    if args.output:
        import json

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(
                {"db": args.db, "limit": args.limit, "weight": args.weight, "rows": rows_summary},
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"[输出] JSON 已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
