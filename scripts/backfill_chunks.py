"""node_chunks 存量回填脚本（chunk 级向量索引第 3 步配套，issue #295）。

背景
----
chunk 表（独立 node_chunks.db）自第 2 步起只有**新写入**的节点才同步 chunk 行：
存量长文档节点没有 chunk 行，chunk 召回对它们不可见。本脚本把存量补齐。

「待回填」判定（纯文本比对，不耗 embedding）——对每个有 content 字段的节点::

    predicted = split_chunks(content)          # 与检索侧同一套切分器
    expected_model = embedding_model_id(store) # 与写入侧同一套模型标识

    +--------------------+-------------------------------------------+--------------+
    | 现状               | 判定                                      | 动作         |
    +--------------------+-------------------------------------------+--------------+
    | 无 chunk 行且      | missing（历史缺口）                       | 补建         |
    | len(predicted)>1  |                                           |              |
    +--------------------+-------------------------------------------+--------------+
    | 行 model 不匹配    | stale_model（换 embedding 模型空间变了）  | 重建         |
    +--------------------+-------------------------------------------+--------------+
    | 行 chunk_text 序列 | stale_content（内容或切分规则变了，含标题 | 重建         |
    | != predicted       | 块规则上线前的旧行）                      |              |
    +--------------------+-------------------------------------------+--------------+
    | len(predicted)<=1  | short_orphan（短内容不该有行）            | 清除行       |
    | 但有 chunk 行      |                                           |              |
    +--------------------+-------------------------------------------+--------------+

幂等：写回走 ``index_node_chunks``（同一节点先删后插、同内容重跑产出同一组行），
重复执行不产生重复行。

执行顺序（⚠ 生产库主库独占，必须停服窗口）::

    1. 停服务（Windows: taskkill /F /PID <服务进程> 或运行 start_rest.vbs 对应的
       停止方式；确认 :8090 无监听）
    2. 冷备：backup_db.py 或整文件组拷贝（.db/.vec/.gidx/.pidx/.flush_ok/.pld/.wal，
       含 node_chunks.db 全套），必须在停服后、任何动作前做
    3. ./venv/Scripts/python.exe startup-check.py   # 库健康 + embedding 可用
    4. ./venv/Scripts/python.exe scripts/backfill_chunks.py --apply
       （建议先 --dry-run 看统计与预计耗时，或 DB_PATH=<整组副本> 预演）
    5. 复核：--dry-run 再跑一遍应报 0 待回填；>30 字长文档抽 3 个节点
       mem_search 语义检索末段命中

失败语义：单节点失败只记录不中断（报告 failed 列表），最后以非零退出码退出；
绝不半途改判据。

用法（在仓库根目录）::

    python scripts/backfill_chunks.py              # dry-run：统计 + 预计耗时，不写
    python scripts/backfill_chunks.py --apply      # 执行回填（须停服窗口）
    python scripts/backfill_chunks.py --json       # 机器可读输出
    python scripts/backfill_chunks.py --limit 50   # 只回填前 N 个待回填节点

**本轮交付不执行回填**——--apply 由维护者在停服窗口另行批准。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config  # noqa: E402
from core import chunk_index as ci  # noqa: E402
from mcp_tools import store  # noqa: E402  # noqa: F401 —— 接线到 .env 的正式库

# 预计耗时采样：抽这么多条样本文本测 embed 单条耗时（0=全量实测，太慢）
_TIMING_SAMPLE = 6


def _load_chunk_rows(node_ids: list[int]) -> dict[int, list[tuple]]:
    """读这些节点现有的 chunk 行 (model, chunk_text)。库缺失/不可读按空处理。"""
    path = ci._db_path()
    if not os.path.isfile(path):
        return {}
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:  # —— 损坏 / 被锁时按空降级，待回填统计偏保守
        return {}
    try:
        rows: dict[int, list[tuple]] = {}
        for nid in node_ids:
            rows[nid] = [
                (m, t)
                for m, t in conn.execute(
                    "SELECT model, chunk_text FROM node_chunks WHERE node_id = ? ORDER BY chunk_idx", (nid,)
                ).fetchall()
            ]
        return rows
    finally:
        conn.close()


def plan_backfill(store) -> dict:
    """只读扫描：返回 {items, stats}。items 每项含待回填原因与预计 chunk 数。"""
    items: list[dict] = []
    counts = {"missing": 0, "stale_model": 0, "stale_content": 0, "short_orphan": 0}
    expected_model = ci.embedding_model_id(store)

    all_ids: list[int] = []
    contents: dict[int, str] = {}
    for nid, payload in store.iter_payloads():
        content = payload.get("content") or ""
        if not content.strip():
            continue
        all_ids.append(nid)
        contents[nid] = content

    existing = _load_chunk_rows(all_ids)
    for nid in all_ids:
        content = contents[nid]
        predicted = ci.split_chunks(content)
        rows = existing.get(nid, [])
        if len(predicted) <= 1:
            if rows:
                items.append({"node_id": nid, "reason": "short_orphan", "expected_rows": 0})
                counts["short_orphan"] += 1
            continue
        row_models = {m for m, _ in rows}
        if not rows:
            reason = "missing"
        elif row_models != {expected_model}:
            reason = "stale_model"
        else:
            row_texts = [t for _, t in rows]
            reason = "stale_content" if row_texts != predicted else ""
        if reason:
            items.append({"node_id": nid, "reason": reason, "expected_rows": len(predicted)})
            counts[reason] += 1

    return {
        "items": items,
        "stats": {"total_nodes": len(all_ids), "counts": counts, "expected_model": expected_model},
    }


def _estimate_seconds(store, items: list[dict]) -> tuple[float, int]:
    """预计耗时：抽样 embed 若干段文本求单段均值 × 预计 embed 总段数。"""
    total_chunks = sum(it["expected_rows"] for it in items)
    # 只统计真正要 embed 的（short_orphan 是删除，0 段）
    embed_chunks = total_chunks
    sample_texts: list[str] = []
    predicted_cache: dict[int, list[str]] = {}
    for it in items:
        node = store.get_node(it["node_id"])
        if not node:
            continue
        predicted_cache[it["node_id"]] = ci.split_chunks(node.get("payload", {}).get("content") or "")
    for _nid, segs in predicted_cache.items():
        if segs:
            sample_texts.extend(segs[: _TIMING_SAMPLE // max(1, len(items))] or segs[:1])
        if len(sample_texts) >= _TIMING_SAMPLE:
            break
    if not sample_texts or embed_chunks == 0:
        return 0.0, embed_chunks
    t0 = time.perf_counter()
    for t_ in sample_texts[:_TIMING_SAMPLE]:
        store.embed_text(t_)
    per_chunk = (time.perf_counter() - t0) / len(sample_texts[:_TIMING_SAMPLE])
    return per_chunk * embed_chunks, embed_chunks


def apply_backfill(store, items: list[dict]) -> dict:
    """执行回填。单节点失败不中断，返回 {ok, failed, removed, elapsed}。"""
    ok = 0
    failed: list[dict] = []
    removed = 0
    t0 = time.perf_counter()
    for i, it in enumerate(items, 1):
        nid = it["node_id"]
        try:
            node = store.get_node(nid)
            if not node:
                failed.append({"node_id": nid, "reason": it["reason"], "error": "node 不存在"})
                continue
            content = node.get("payload", {}).get("content") or ""
            rows = ci.index_node_chunks(store, nid, content)
            if it["expected_rows"] == 0 and rows == 0:
                removed += 1
            elif rows == it["expected_rows"]:
                ok += 1
            else:
                # index_node_chunks 失败只告警静默返回 0——这里逐位复核行数
                failed.append(
                    {
                        "node_id": nid,
                        "reason": it["reason"],
                        "error": f"回填后行数 {rows} != 预期 {it['expected_rows']}",
                    }
                )
            if i % 50 == 0:
                done = (time.perf_counter() - t0) / i
                eta = done * (len(items) - i)
                print(f"[backfill] {i}/{len(items)} 已完成，剩余预计 {eta:.0f}s", flush=True)
        except Exception as e:  # noqa: BLE001 —— 单节点失败记录继续，绝不中止整批
            failed.append({"node_id": nid, "reason": it["reason"], "error": str(e)[:200]})
    return {"ok": ok, "failed": failed, "removed": removed, "elapsed": round(time.perf_counter() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description="node_chunks 存量回填（生产库须停服窗口）")
    ap.add_argument("--apply", action="store_true", help="执行回填（默认 dry-run 只统计不写）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个待回填节点（0=全部）")
    args = ap.parse_args()

    if not getattr(Config, "CHUNK_INDEX_ENABLED", True):
        print("[backfill] CHUNK_INDEX_ENABLED=false，chunk 索引写入整体关闭——先开开关再回填", file=sys.stderr)
        return 2

    plan = plan_backfill(store)
    items = plan["items"]
    if args.limit > 0:
        items = items[: args.limit]
    stats = dict(plan["stats"])
    stats["to_fix"] = len(items)
    seconds, embed_chunks = _estimate_seconds(store, items)
    stats["estimated_embed_chunks"] = embed_chunks
    stats["estimated_seconds"] = round(seconds, 1)

    if args.json:
        print(json.dumps({**stats, "items": items[:200]}, ensure_ascii=False, indent=2))
    else:
        print(f"节点总数: {stats['total_nodes']}")
        print(f"待回填: {len(items)}  {stats['counts']}")
        print(
            f"预计 embed 段数: {embed_chunks}，预计耗时 ~{stats['estimated_seconds']}s（抽样 {min(_TIMING_SAMPLE, 6)} 段外推）"
        )
        hint = "python scripts/backfill_chunks.py --apply（停服窗口，顺序见脚本头注释）"
        print(f"执行: {hint}" if items and not args.apply else "")
        for it in items[:20]:
            print(f"  node={it['node_id']} reason={it['reason']} expected_rows={it['expected_rows']}")
        if len(items) > 20:
            print(f"  … 共 {len(items)} 项（--json 看全量前 200 条）")

    if not args.apply:
        return 0
    res = apply_backfill(store, items)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(
            f"回填完成: ok={res['ok']} removed={res['removed']} failed={len(res['failed'])} elapsed={res['elapsed']}s"
        )
        for f_ in res["failed"][:20]:
            print(f"  FAILED node={f_['node_id']} reason={f_['reason']}: {f_['error']}")
    # 复核：重扫应为 0 待回填（失败除外）
    after = plan_backfill(store)
    remaining = len(after["items"])
    print(f"复核: 剩余待回填 {remaining} 个（应为失败项数 {len(res['failed'])}）")
    return 1 if res["failed"] or remaining != len(res["failed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
