# -*- coding: utf-8 -*-
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

    total = 0
    active = 0
    outdated = 0
    by_type: dict = {}
    by_domain: dict = {}
    kind_counter: dict = {}
    imp_buckets = {name: 0 for name, _func in _IMP_BUCKETS}
    month_counter: dict = {}
    nodes_with_edges = 0
    total_edges = 0
    label_dist: dict = {}
    hit_count_total = 0
    hit_nodes: list = []

    db = None
    try:
        db = store._acquire()
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node:
                continue
            payload = node.payload or {}
            total += 1
            status = payload.get("status", "")
            if status == "outdated":
                outdated += 1
            else:
                active += 1

            t = payload.get("type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
            from core.trivium_store import node_domain
            d = node_domain(payload)
            by_domain[d] = by_domain.get(d, 0) + 1

            # kinds：仅当存在含 kind 字段的 novel_chunk 时统计
            if t == "novel_chunk" and payload.get("kind"):
                k = str(payload.get("kind"))
                kind_counter[k] = kind_counter.get(k, 0) + 1

            # importance 分布区间
            imp = _to_float(payload.get("importance"), 0.5)
            for name, pred in _IMP_BUCKETS:
                if pred(imp):
                    imp_buckets[name] += 1
                    break

            # time：按 created_at 月份分布（null/0/非法跳过）
            m = _month_label(payload.get("created_at"))
            if m:
                month_counter[m] = month_counter.get(m, 0) + 1

            # graph：边信息 + hit 信息（同一连接内读边）
            edges = list(db.get_edges(nid) or [])
            if edges:
                nodes_with_edges += 1
            total_edges += len(edges)
            for e in edges:
                lab = getattr(e, "label", "") or getattr(e, "relation", "") or "unknown"
                label_dist[lab] = label_dist.get(lab, 0) + 1

            hc = _to_float(payload.get("hit_count"), 0)
            if hc:
                hit_count_total += int(hc)
                hit_nodes.append({
                    "id": nid,
                    "hit_count": int(hc),
                    "content": (payload.get("content") or "")[:60],
                })
    except Exception as e:  # noqa: BLE001 — 盘点容错：统计失败不阻断，返回已收集数据
        logger.warning("mem_stats 遍历失败（返回已收集数据）: %s", e)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    # label 分布 top10（按 count 降序）
    top_labels = sorted(label_dist.items(), key=lambda kv: kv[1], reverse=True)[:10]
    avg_outdegree = round(total_edges / total, 2) if total else 0.0
    hit_nodes.sort(key=lambda x: x["hit_count"], reverse=True)

    elapsed_ms = round((time.time() - start) * 1000, 1)
    return {
        "totals": {
            "total_nodes": total,
            "active": active,
            "outdated": outdated,
            "by_type": dict(sorted(by_type.items(), key=lambda kv: kv[0])),
            "by_domain": dict(sorted(by_domain.items(), key=lambda kv: kv[0])),
        },
        "kinds": dict(sorted(kind_counter.items(), key=lambda kv: kv[0])),
        "importance": imp_buckets,
        "time": dict(sorted(month_counter.items(), key=lambda kv: kv[0])),
        "graph": {
            "nodes_with_edges": nodes_with_edges,
            "total_edges": total_edges,
            "label_dist_top10": dict(top_labels),
            "avg_outdegree": avg_outdegree,
            "hit_count_total": hit_count_total,
            "top_hit_nodes": hit_nodes[:10],
        },
        "elapsed_ms": elapsed_ms,
    }
