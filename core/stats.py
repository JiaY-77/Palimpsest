"""
core.stats —— 库级盘点统计（记忆生命周期：mem_stats）
====================================================
回答「库里有什么 / 领域密度 / 图谱状态」：基于全部节点一次全遍历，
汇总 totals / kinds / importance / time / graph 五类分节数据，供
CLI（palimpsest_cli stats）、REST（POST /mem/stats）、MCP（mem_stats）三入口共用。

设计：
  - 单连接单次全遍历收集所有分节数据（不做「每节各遍历一遍」），
    与 iter_payloads 同一连接模式，避免 N+1 次开/关连接。
  - 只读、不修改任何节点数据（可安全作用于含 domain=novel 的正式库）。
  - graph 分节需要边信息，故在同一个 _acquire 连接内逐节点读边。
"""

import contextlib
import logging
import time

from core.utils import _to_float

logger = logging.getLogger(__name__)

# 可选分节清单（CLI --section 可独立开关，缺省全给）：
#   domains = totals 里的按 domain 分布（CLI 别名，compute_stats 仍归并在 totals.by_domain）
SECTIONS = ("totals", "kinds", "importance", "time", "graph", "domains")

# importance 分布区间（左闭右开，末段含右端点）
_IMP_BUCKETS = (
    ("lt_0_4", lambda v: v < 0.4),
    ("0_4_to_0_6", lambda v: 0.4 <= v < 0.6),
    ("0_6_to_0_8", lambda v: 0.6 <= v < 0.8),
    ("ge_0_8", lambda v: v >= 0.8),
)


def _month_label(ts):
    """时间戳 → 'YYYY-MM'；null/0/非法返回 None（time 分节跳过）。"""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return time.strftime("%Y-%m", time.localtime(ts))


def _new_accumulator() -> dict:
    """盘点累加器初值（单次遍历收集的全部计数）。"""
    return {
        "total": 0,
        "active": 0,
        "outdated": 0,
        "by_type": {},
        "by_domain": {},
        "kind_counter": {},
        "imp_buckets": {name: 0 for name, _func in _IMP_BUCKETS},
        "month_counter": {},
        "nodes_with_edges": 0,
        "total_edges": 0,
        "label_dist": {},
        "hit_count_total": 0,
        "hit_nodes": [],
    }


def _accumulate_node(acc: dict, nid, node, db) -> None:
    """把一个节点的计数累加进累加器（status/type/domain/kind/importance/time/graph/hit）。"""
    from core.trivium_store import node_domain

    payload = node.payload or {}
    acc["total"] += 1
    status = payload.get("status", "")
    if status == "outdated":
        acc["outdated"] += 1
    else:
        acc["active"] += 1

    t = payload.get("type") or "unknown"
    by_type = acc["by_type"]
    by_type[t] = by_type.get(t, 0) + 1
    d = node_domain(payload)
    by_domain = acc["by_domain"]
    by_domain[d] = by_domain.get(d, 0) + 1

    # kinds：仅当存在含 kind 字段的 novel_chunk 时统计
    if t == "novel_chunk" and payload.get("kind"):
        k = str(payload.get("kind"))
        kind_counter = acc["kind_counter"]
        kind_counter[k] = kind_counter.get(k, 0) + 1

    # importance 分布区间
    imp = _to_float(payload.get("importance"), 0.5)
    for name, pred in _IMP_BUCKETS:
        if pred(imp):
            acc["imp_buckets"][name] += 1
            break

    # time：按 created_at 月份分布（null/0/非法跳过）
    m = _month_label(payload.get("created_at"))
    if m:
        month_counter = acc["month_counter"]
        month_counter[m] = month_counter.get(m, 0) + 1

    # graph：边信息 + hit 信息（同一连接内读边）
    edges = list(db.get_edges(nid) or [])
    if edges:
        acc["nodes_with_edges"] += 1
    acc["total_edges"] += len(edges)
    label_dist = acc["label_dist"]
    for e in edges:
        lab = getattr(e, "label", "") or getattr(e, "relation", "") or "unknown"
        label_dist[lab] = label_dist.get(lab, 0) + 1

    hc = _to_float(payload.get("hit_count"), 0)
    if hc:
        acc["hit_count_total"] += int(hc)
        acc["hit_nodes"].append({
            "id": nid,
            "hit_count": int(hc),
            "content": (payload.get("content") or "")[:60],
        })


def _build_stats_result(acc: dict, start: float) -> dict:
    """把累加器汇总成 compute_stats 的返回结构（label top10 / 平均出度 / 耗时）。"""
    total = acc["total"]
    top_labels = sorted(acc["label_dist"].items(), key=lambda kv: kv[1], reverse=True)[:10]
    avg_outdegree = round(acc["total_edges"] / total, 2) if total else 0.0
    hit_nodes = sorted(acc["hit_nodes"], key=lambda x: x["hit_count"], reverse=True)

    return {
        "totals": {
            "total_nodes": total,
            "active": acc["active"],
            "outdated": acc["outdated"],
            "by_type": dict(sorted(acc["by_type"].items(), key=lambda kv: kv[0])),
            "by_domain": dict(sorted(acc["by_domain"].items(), key=lambda kv: kv[0])),
        },
        "kinds": dict(sorted(acc["kind_counter"].items(), key=lambda kv: kv[0])),
        "importance": acc["imp_buckets"],
        "time": dict(sorted(acc["month_counter"].items(), key=lambda kv: kv[0])),
        "graph": {
            "nodes_with_edges": acc["nodes_with_edges"],
            "total_edges": acc["total_edges"],
            "label_dist_top10": dict(top_labels),
            "avg_outdegree": avg_outdegree,
            "hit_count_total": acc["hit_count_total"],
            "top_hit_nodes": hit_nodes[:10],
        },
        "elapsed_ms": round((time.time() - start) * 1000, 1),
    }


def compute_stats(store) -> dict:
    """单连接单次全遍历，收集全库盘点统计，返回分节 dict。

    返回结构：
      {
        "totals": {total_nodes, active, outdated, by_type, by_domain},
        "kinds":  {kind: count}（仅当存在含 kind 字段的 novel_chunk 时非空，否则空 dict），
        "importance": {小于0.4 / 0.4到0.6 / 0.6到0.8 / 大于等于0.8},
        "time":   {"2026-08": n, ...}（created_at 为 null/0 的跳过），
        "graph":  {nodes_with_edges, total_edges, label_dist(top10),
                   avg_outdegree, hit_count_total, top_hit_nodes},
        "elapsed_ms": 统计耗时
      }
    """
    start = time.time()
    acc = _new_accumulator()

    db = None
    try:
        db = store._acquire()
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node:
                continue
            _accumulate_node(acc, nid, node, db)
    except Exception as e:  # noqa: BLE001 — 盘点容错：统计失败不阻断，返回已收集数据
        logger.warning("mem_stats 遍历失败（返回已收集数据）: %s", e)
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()

    return _build_stats_result(acc, start)
