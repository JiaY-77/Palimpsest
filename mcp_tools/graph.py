# -*- coding: utf-8 -*-
"""
mcp_tools.graph —— 图谱相关工具
=============================
graph_neighbors（通用邻居遍历）/ mem_link（手动建边）+ 图关联区收集 _collect_neighbors
与辅助函数 _edge_exists。无向语义关系双向建边协议定义见 _BIDIRECTIONAL_RELATIONS。
"""

from core.trivium_store import domain_in_block, node_domain  # noqa: E402

from mcp_tools._common import _shorten, _to_json, mcp, store  # noqa: E402

# 无向语义关系：mem_link 双向建边协议自动补反向边；REVISED_BY 保持单向
_BIDIRECTIONAL_RELATIONS = {"RELATED_TO", "CAUSES", "REFERS_TO"}


def _edge_exists(src: int, dst: int, label: str) -> bool:
    """检查 src → dst 且 label 匹配的出边是否已存在（防重复建边）"""
    label = label.upper()
    for edge in store.get_edges(src):
        if edge.target_id == dst and (getattr(edge, "label", "") or "").upper() == label:
            return True
    return False


def _collect_neighbors(items: list, neighbor_limit: int = 5) -> list:
    """
    从语义命中节点出发，沿出边取一跳邻居，生成图关联区（方案 B 分区返回）。

    - 过滤：已在语义区 / 自环 / 节点缺失无 payload
    - 去重：同一邻居被多个命中节点到达时，保留 score 最高一条（via 取最高分来源）
    - score = via_score × weight（关联强度分，仅图关联区内部排序用，不参与语义区排序）
    - relation 统一大写展示；weight round 6 位；title = content 前 80 字
    """
    if not items:
        return []
    sem_ids = {item.get("id") for item in items}
    best = {}  # neighbor_id -> 条目（去重后取最高分）
    for item in items:
        nid = item.get("id")
        if nid is None:
            continue
        via_score = float(item.get("score", 0.0))
        for edge in store.get_edges(nid):
            nb = edge.target_id
            if nb is None or nb == nid or nb in sem_ids:
                continue
            node = store.get_node(nb)
            if not node:
                continue
            payload = node.get("payload", {}) or {}
            label = (getattr(edge, "label", "") or "").upper() or "LINKED"
            weight = round(float(getattr(edge, "weight", 1.0) or 1.0), 6)
            strength = round(via_score * weight, 4)
            prev = best.get(nb)
            if prev is None or strength > prev["score"]:
                best[nb] = {
                    "id": nb,
                    "relation": label,
                    "via_id": nid,
                    "via_score": round(via_score, 4),
                    "weight": weight,
                    "target_type": payload.get("type", ""),
                    "title": _shorten(payload.get("content", ""), 80),
                    "score": strength,
                }
    result = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return result[:neighbor_limit]


def _clamp_params(depth, limit, min_weight, relation, block) -> tuple:
    """钳制 graph_neighbors 参数：depth 1-3，limit >= 1，min_weight >= 0，rel/blk 小写化。

    返回 (depth, limit, min_w, rel, blk)。
    """
    depth = max(1, min(int(depth), 3))
    limit = max(1, int(limit))
    min_w = max(0.0, float(min_weight))
    rel = (relation or "").strip().lower()
    blk = (block or "").strip().lower()
    return depth, limit, min_w, rel, blk


def _bfs_neighbors(node_id: int, depth: int, min_w: float, rel: str,
                   blk: str) -> list:
    """BFS 收集出边邻居（min_weight 过滤弱边；block 分区块；去重）。

    返回原始 relations 列表（仅 target_id/relation/weight，target_type/title 留空）。
    """
    relations = []
    seen = {node_id}
    frontier = [(node_id, 0)]
    while frontier:
        cur, hop = frontier.pop(0)
        if hop >= depth:
            continue
        for edge in store.get_edges(cur):
            label = getattr(edge, "label", "") or ""
            if rel and label.lower() != rel:
                continue
            w = float(getattr(edge, "weight", 1.0) or 1.0)
            if w < min_w:
                continue
            tid = edge.target_id
            if tid is None or tid in seen:
                continue
            # 区块过滤：target 节点 domain 匹配 block 才进入（不扩散跨区块边）
            if blk:
                tnode = store.get_node(tid)
                if not tnode:
                    continue
                tpayload = tnode.get("payload", {}) or {}
                tdomain = node_domain(tpayload)
                if not domain_in_block(tdomain, blk):
                    continue
            seen.add(tid)
            relations.append({
                "target_id": tid,
                "relation": label,
                "weight": w,
                "target_type": "",
                "target_title": "",
            })
            frontier.append((tid, hop + 1))
    return relations


def _fill_neighbor_summaries(relations: list) -> None:
    """补邻居节点摘要（target_type / target_title=content 前 80 字），原位修改。"""
    for item in relations:
        node = store.get_node(item["target_id"])
        if not node:
            continue
        payload = node.get("payload", {}) or {}
        item["target_type"] = payload.get("type", "")
        item["target_title"] = _shorten(payload.get("content", ""), 80)


@mcp.tool()
def graph_neighbors(node_id: int, relation: str = "", depth: int = 1,
                    limit: int = 20, min_weight: float = 0.0,
                    block: str = "") -> str:
    """
    图谱邻居查询：从 node_id 沿出边 BFS 遍历到 depth 层（1-3）。
    relation 非空时只保留 label 匹配的边（忽略大小写）；min_weight 过滤弱边；
    block 非空时只沿 target 节点 domain 匹配区块的边扩散（图谱分区块，防跨域污染）；
    每节点去重（只出现一次，取最先到达的跳数），结果按 weight 降序截断
    （精馏：强关联优先，防高节点「先到先得」占满 limit，2026-08-25 主人挑战 #2）。
    每条邻居返回 {target_id, relation, weight, target_type, target_title}。
    返回 JSON：{"node_id", "depth", "count", "relations": [...]}。
    （结构已拆分：_clamp_params / _bfs_neighbors / _fill_neighbor_summaries，行为不变。）
    """
    # 参数钳制：depth 1-3，limit >= 1，min_weight >= 0
    depth, limit, min_w, rel, blk = _clamp_params(depth, limit, min_weight,
                                                  relation, block)

    if not store.get_node(node_id):
        return _to_json({"node_id": node_id, "depth": depth, "count": 0,
                         "relations": [], "hint": f"节点不存在: {node_id}"})

    # 起点自检（主人 2026-08-25 审查补丁）：带 block 时起点必须属于该区块，
    # 否则直接拦截——堵死跨域起点污染（起点自身不能违规闯入别的区块）
    if blk:
        start_payload = (store.get_node(node_id) or {}).get("payload", {}) or {}
        if not domain_in_block(node_domain(start_payload), blk):
            return _to_json({
                "node_id": node_id, "block": blk, "depth": depth, "count": 0,
                "relations": [],
                "error": f"起点节点 {node_id} 不属于 {blk} 区块（domain={node_domain(start_payload)}），已拦截",
            })

    # BFS 收集出边（min_weight 过滤弱边；block 分区块；先收集后按 weight 排序截断）
    relations = _bfs_neighbors(node_id, depth, min_w, rel, blk)

    # 精馏：按 weight 降序截断（强关联优先）
    relations.sort(key=lambda x: x.get("weight", 0.0), reverse=True)
    relations = relations[:limit]

    # 补邻居节点摘要（target_type / target_title=content 前 80 字）
    _fill_neighbor_summaries(relations)

    return _to_json({
        "node_id": node_id,
        "depth": depth,
        "count": len(relations),
        "relations": relations,
    })


@mcp.tool()
def mem_link(source_id: int, target_id: int, relation: str = "RELATED_TO",
             weight: float = 0.9, bidirectional: bool = True) -> str:
    """
    手动建边：在 source_id → target_id 之间建立 relation 类型的关联边
    （如 RELATED_TO / CAUSES / REFERS_TO），供 graph_neighbors 图谱查询使用。
    校验两端节点存在后建边。自环（source_id == target_id）禁止。
    weight 统一 round 6 位小数。
    bidirectional=True 且 relation 大写后属于双向集合（RELATED_TO / CAUSES / REFERS_TO）
    时，自动补反向边 target → source（先查已存在则跳过，不重复建）；REVISED_BY 保持单向。
    返回 JSON：{"linked", "source_id", "target_id", "relation", "weight",
               "reverse_added"}。
    """
    if source_id == target_id:
        return _to_json({"linked": False,
                         "error": "自环禁止：source_id 与 target_id 不能相同"})
    relation = (relation or "").strip() or "related"
    try:
        weight = round(float(weight), 6)
    except (TypeError, ValueError):
        weight = 0.9
    if not store.get_node(source_id):
        return _to_json({"linked": False, "error": f"源节点不存在: {source_id}"})
    if not store.get_node(target_id):
        return _to_json({"linked": False, "error": f"目标节点不存在: {target_id}"})
    rel_upper = relation.upper()
    # 主边防重：已存在则不重复建（反向边协议见下）
    main_edge_needed = not _edge_exists(source_id, target_id, rel_upper)
    # 双向建边协议：无向语义关系自动补反向边（先查存在则跳过）
    reverse_added = False
    if bidirectional and rel_upper in _BIDIRECTIONAL_RELATIONS:
        if not _edge_exists(target_id, source_id, rel_upper):
            store.create_edge(target_id, source_id, rel_upper, weight=weight)
            reverse_added = True
    if main_edge_needed:
        store.create_edge(source_id, target_id, relation, weight=weight)
    return _to_json({
        "linked": True,
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "weight": weight,
        "reverse_added": reverse_added,
    })
