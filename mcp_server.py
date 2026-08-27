# -*- coding: utf-8 -*-
"""
Palimpsest 本地 MCP Server
=========================
把小七（Hermes）的记忆层能力暴露为标准 MCP 工具，供 Hermes 通过 MCP 协议调用。
底层使用 Palimpsest 的 TriviumStore（向量 + 图 + 文档存储），embedding 由本地
Ollama 的 qwen3-embedding:0.6b 生成（1024 维，已验证可用）。

工具列表：
  1. mem_retrieve - 语义检索记忆（只返回 150 字摘要 + meta，绝不返回全文，省 token）
  2. mem_get_full - 按 id 取完整记忆内容（全文由本工具单独取）
  3. mem_ingest   - 写入新记忆（带冲突检测：相似旧记忆标记 outdated + REVISED_BY 链）
  4. mem_recent   - 最近记忆列表（按 created_at 倒序）
  5. kb_index     - 知识库文件索引（扫描知识库根目录下所有 .md）
  6. kb_search    - 知识库语义检索（向量检索，只查 build_kb_index.py 建的 kb_chunk 节点，
                   含 domain=rule 规则类切片）
  7. mem_search   - 统一检索入口：scope=memory/kb/all 混合检索记忆与知识库
                   （v2.0：domain=rule 规则切片内置 ×1.3 加权；
                    v3.0：include_neighbors=True 时返回图关联区（分区返回），
                    语义区原样 + neighbors 区展示已命中节点的一跳邻居）
  8. router_query - 任务路由查询（v2.0）：查规则类知识切片，提取推荐模型/配置
  9. mem_version_history - 版本历史查询：沿 REVISED_BY 修订链返回版本演进摘要
                 （如 SOUL 版本日志；domain/full_content/offset/limit 参数）
  10. graph_neighbors  - 图谱邻居查询：从任意节点沿出边 BFS 遍历
                 （relation 过滤 / depth 1-3 / limit 截断，去重）
  11. mem_link         - 手动建边（RELATED_TO / CAUSES / REFERS_TO 等）

边类型约定：
  - REVISED_BY : 版本修订链（mem_ingest 自动建，新 → 旧；单向语义）
  - RELATED_TO : 关联（mem_link 手动建；无向语义，双向建边协议自动补反向）
  - CAUSES     : 因果（预留，未来 ingest 提取；无向语义，双向补反向）
  - REFERS_TO  : 引用（预留；无向语义，双向补反向）
双向建边协议：RELATED_TO / CAUSES / REFERS_TO 在 mem_link(bidirectional=True)
  时自动补反向边（先查存在则跳过），绕开 get_edges 只返回出边、入边不可查的
  API 限制；REVISED_BY 保持单向（版本链方向语义）。

运行方式（由 Hermes 以 stdio 方式拉起）：
    python mcp_server.py
"""

import json
import os
import re
import sys
import time

# 确保能 import 项目 core 模块（以脚本所在目录为项目根）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
# 切换到项目根目录，保证 config 里的相对路径（如 data/mh_memory.db）解析正确
os.chdir(_SCRIPT_DIR)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore, domain_in_block, node_domain  # noqa: E402
from core.secret_scan import SecretScanError  # noqa: E402

# 知识库根目录（环境变量 KNOWLEDGE_DIR 优先；默认相对项目根的通用路径，不硬编码个人路径）
KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "") or os.path.normpath(
    os.path.join(_SCRIPT_DIR, "../../../Knowledge")
)

# 全局存储实例（TriviumDB：向量 + 图 + 文档）
store = TriviumStore()

mcp = FastMCP("palimpsest")


def _to_json(data) -> str:
    """JSON 序列化（保留中文，不转义）"""
    return json.dumps(data, ensure_ascii=False)


def _shorten(text: str, length: int) -> str:
    """截取文本前 length 个字符"""
    text = text or ""
    return text[:length] if len(text) > length else text


def _kb_md_files() -> list:
    """遍历知识库目录，返回所有 .md 文件（绝对路径，排序稳定）"""
    files = []
    for root, _dirs, names in os.walk(KNOWLEDGE_DIR):
        for name in names:
            if name.lower().endswith(".md"):
                files.append(os.path.join(root, name))
    return sorted(files)


@mcp.tool()
def mem_retrieve(query: str, domain: str = "", top_k: int = 5) -> str:
    """
    语义检索记忆：返回 150 字摘要 + meta（绝不返回全文，省 token 的关键设计）。
    全文请用 mem_get_full 按 id 单独取。
    """
    emb = store.embed_text(query)
    # v1.1 拉宽召回：与 mem_search 一致，top_k*3 召回再过滤，避免 kb_chunk 挤占名额导致记忆条数凑不满
    results = store.search_similar(emb, top_k=max(top_k * 3, 30), expand_depth=1)
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        # domain 过滤：只保留指定角色/域的记忆
        if domain and payload.get("character_name") != domain:
            continue
        # 排除知识库块：kb_chunk 只供 kb_search / mem_search(scope=kb) 检索，不混入记忆
        if payload.get("type") == "kb_chunk":
            continue
        items.append({
            "id": r.get("id"),
            "score": round(float(r.get("score", 0.0)), 4),
            "summary": _shorten(payload.get("content", ""), 150),
            "meta": {
                "type": payload.get("type", ""),
                "importance": payload.get("importance", 0.5),
                "status": payload.get("status", ""),
                "domain": payload.get("character_name", ""),
            },
        })
        if len(items) >= top_k:
            break
    if not items:
        return _to_json({"results": []})
    return _to_json({"results": items})


@mcp.tool()
def mem_get_full(node_id: int) -> str:
    """按节点 id 取完整记忆（含全部 payload 字段；不含向量，避免返回 1024 维浮点数组）"""
    node = store.get_node(node_id)
    if not node:
        return _to_json({"found": False, "node_id": node_id})
    return _to_json({
        "found": True,
        "id": node.get("id"),
        "payload": node.get("payload", {}),
        "num_edges": node.get("num_edges", 0),
    })


@mcp.tool()
def mem_ingest(content: str, type: str = "memory", importance: float = 0.5,
               domain: str = "", source: str = "") -> str:
    """
    写入新记忆；自动冲突检测：与库中 score > 0.4 的相似旧记忆标记为 outdated，
    并建立 新记忆 --REVISED_BY--> 旧记忆 的修订链。
    v1.1：新增知识关联检测——score > 0.35 的 kb_chunk 记入 payload.linked_from，
    返回 linked_kb_ids 便于追溯关联的知识库块。
    """
    now = time.time()
    emb = store.embed_text(content)

    # ---- v1.1 知识关联检测：找出 score > 0.35 的 kb_chunk 节点，写入 payload.linked_from ----
    linked_kb_ids = []
    kb_similar = store.search_similar(emb, top_k=3, expand_depth=0, apply_decay=False)
    for r in kb_similar:
        r_payload = r.get("payload", {}) or {}
        if r_payload.get("type") == "kb_chunk" and float(r.get("score", 0.0)) > 0.35:
            rid = r.get("id")
            if rid is not None:
                linked_kb_ids.append(rid)

    node_data = {
        "type": type,
        "content": content,
        "importance": importance,
        "character_name": domain,
        "source": source,
        "created_at": now,
        "linked_from": linked_kb_ids,
    }
    try:
        node_id = store.insert_node(node_data, emb)
    except SecretScanError as e:
        return _to_json({"stored": False, "error": str(e), "rules": e.rules})

    # 注意：core.insert_node 的基础 payload 固定 created_at=None 且不接收外部传入，
    # 这里补写一次真实时间戳，保证 mem_recent 排序有意义
    node = store.get_node(node_id)
    payload = node.get("payload", {}) if node else {}
    if payload.get("created_at") is None:
        payload["created_at"] = now
        store.update_payload(node_id, payload)

    # ---- 冲突检测：查找与本次写入相似的旧记忆 ----
    outdated_ids = []
    similar = store.search_similar(emb, top_k=3, expand_depth=0, apply_decay=False)
    for r in similar:
        old_id = r.get("id")
        score = float(r.get("score", 0.0))
        if old_id is None or old_id == node_id or score <= 0.4:
            continue
        old_node = store.get_node(old_id)
        old_payload = old_node.get("payload", {}) if old_node else {}
        if not old_payload:
            continue
        # 排除知识库块：kb_chunk 只供 kb_search 检索，不参与记忆冲突检测（不标 outdated、不建 REVISED_BY 边）
        if old_payload.get("type") == "kb_chunk":
            continue
        # 保留原字段，仅把 status 标记为 outdated
        old_payload["status"] = "outdated"
        store.update_payload(old_id, old_payload)
        # 新记忆 -> 旧记忆 的修订关系
        store.create_edge(node_id, old_id, "REVISED_BY")
        outdated_ids.append(old_id)

    suggestion = ""
    if outdated_ids:
        ids = ", ".join(str(i) for i in outdated_ids)
        suggestion = (f"旧记忆 id={ids} 已标记 outdated（REVISED_BY 链），"
                      "若涉及固定记忆（MEMORY.md）请同步更新")
    return _to_json({
        "stored": True,
        "node_id": node_id,
        "conflict_found": bool(outdated_ids),
        "outdated_ids": outdated_ids,
        "linked_kb_ids": linked_kb_ids,
        "suggestion": suggestion,
    })


@mcp.tool()
def mem_recent(domain: str = "", limit: int = 10) -> str:
    """最近记忆列表：按 created_at 倒序（时间戳缺失时退化为按 id 倒序，即插入顺序）"""
    items = []
    for nid, payload in store.iter_payloads():
        if domain and payload.get("character_name") != domain:
            continue
        items.append({
            "id": nid,
            "type": payload.get("type", ""),
            "content": _shorten(payload.get("content", ""), 100),
            "importance": payload.get("importance", 0.5),
            "status": payload.get("status", ""),
            "domain": payload.get("character_name", ""),
            "created_at": payload.get("created_at"),
        })
    # (created_at, id) 双键倒序：时间戳缺失（None→0）时按 id 倒序兜底
    items.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)
    return _to_json({"results": items[:limit], "total": len(items)})


@mcp.tool()
def mem_review(days: int = 7, domain: str = "") -> str:
    """复盘盘点（2026-08-25 主人批准：复盘机制融入小帕，复盘=记忆治理）。

    统计全库节点 + 盘点最近 days 天 ingest 的记忆，输出复盘草稿：
      - recent_ingests：窗口内新增记忆（type=memory，按 created_at 倒序）
      - high_value_candidates：importance>=0.6 且 active（升级知识库候选）
      - stale_outdated：outdated 节点（版本链历史，可清理候选）
      - low_value_candidates：importance<=0.4 且 active 的非知识节点（清理候选）
    供每日复盘使用：指挥官裁决后升级/清理，再存 type=review 节点。
    """
    import time
    now = time.time()
    window = max(1, int(days)) * 86400.0

    items = []
    for nid, payload in store.iter_payloads():
        if domain and payload.get("character_name") != domain:
            continue
        items.append({
            "id": nid,
            "type": payload.get("type", ""),
            "content": _shorten(payload.get("content", ""), 100),
            "importance": payload.get("importance", 0.5),
            "status": payload.get("status", ""),
            "domain": payload.get("character_name", ""),
            "created_at": payload.get("created_at"),
        })

    total = len(items)
    active = sum(1 for x in items if x["status"] != "outdated")
    outdated = sum(1 for x in items if x["status"] == "outdated")
    kb_chunks = sum(1 for x in items if x["type"] == "kb_chunk")
    memory_nodes = sum(1 for x in items if x["type"] == "memory")

    # 窗口内新增记忆（只算 type=memory，kb_chunk 是文档切片不算 ingest）
    recent = [x for x in items
              if x["type"] == "memory" and isinstance(x["created_at"], (int, float))
              and x["created_at"] and (now - x["created_at"]) <= window]
    recent.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)

    # 高价值候选（importance>=0.6 且 active → 升级知识库候选）
    high_value = [x for x in items
                  if x["status"] != "outdated" and float(x["importance"] or 0) >= 0.6]
    high_value.sort(key=lambda x: x["importance"], reverse=True)

    # 待治理：outdated 节点 + 低价值旧记忆（清理候选）
    stale = [x for x in items if x["status"] == "outdated"]
    low_value = [x for x in items
                 if x["status"] != "outdated" and x["type"] != "kb_chunk"
                 and float(x["importance"] or 0) <= 0.4]

    return _to_json({
        "review_window_days": days,
        "stats": {
            "total": total, "active": active, "outdated": outdated,
            "memory": memory_nodes, "kb_chunk": kb_chunks,
        },
        "recent_ingests": recent[:30],
        "high_value_candidates": high_value[:20],
        "stale_outdated": stale[:20],
        "low_value_candidates": low_value[:20],
    })


# ---- 版本历史查询（REVISED_BY 修订链）----
_VERSION_RE = re.compile(r"\[SOUL变更日志\]\s*(\d{4}-\d{2}-\d{2})\s*版本\s*v?([\d.]+)")
_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")


def _parse_version_content(content: str) -> tuple:
    """解析版本日志 content → (date, version, title)"""
    content = content or ""
    m = _VERSION_RE.search(content)
    date = m.group(1) if m else ""
    version = m.group(2) if m else ""
    t = _TITLE_RE.search(content)
    if t:
        title = t.group(1).strip()
    else:
        # 无 **标题** 时取「：」后的前 50 字
        title = content.split("：", 1)[-1].strip()[:50]
    return date, version, title


@mcp.tool()
def mem_version_history(domain: str = "hermes", full_content: bool = False,
                        offset: int = 0, limit: int = 20) -> str:
    """
    版本历史查询：沿 REVISED_BY 修订链（新版本 → 旧版本）返回版本演进摘要，
    用于查 SOUL 版本日志等历史事件链。
    参数：domain 按 character_name 过滤（默认 hermes）；full_content=True 时
    每条含完整 content 原文；offset/limit 分页（链长 > limit 时分页）。
    返回 JSON：{"found", "start_id", "chain_length", "versions": [...]}，
    每条版本为 {id, version, date, title, importance}。
    """
    # 1. 找 domain 下最新的事件节点（created_at 最大，缺失时按 id 最大兜底）
    candidates = []
    for nid, payload in store.iter_payloads():
        if payload.get("character_name") != domain or payload.get("type") != "event":
            continue
        try:
            ts = float(payload.get("created_at") or 0)
        except (TypeError, ValueError):
            ts = 0
        candidates.append((ts, nid))
    if not candidates:
        return _to_json({"found": False, "start_id": None,
                         "chain_length": 0, "versions": []})
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    start_id = candidates[0][1]

    # 2. 沿 REVISED_BY 出边往回走（新 → 旧），收集版本链（防环）
    chain = []
    seen = set()
    cur = start_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        node = store.get_node(cur)
        if not node:
            break
        chain.append(node)
        nxt = None
        for edge in store.get_edges(cur):
            if edge.label == "REVISED_BY":
                nxt = edge.target_id
                break
        cur = nxt

    # 3. 组装版本摘要 + 分页
    versions = []
    for node in chain[offset:offset + limit]:
        payload = node.get("payload", {}) or {}
        content = payload.get("content", "")
        date, version, title = _parse_version_content(content)
        item = {
            "id": node.get("id"),
            "version": version,
            "date": date,
            "title": title,
            "importance": payload.get("importance", 0.5),
        }
        if full_content:
            item["content"] = content
        versions.append(item)

    return _to_json({
        "found": True,
        "start_id": start_id,
        "chain_length": len(chain),
        "versions": versions,
    })


# ---- 图谱扩展：通用邻居遍历 + 手动建边 ----
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
    """
    # 参数钳制：depth 1-3，limit >= 1，min_weight >= 0
    depth = max(1, min(int(depth), 3))
    limit = max(1, int(limit))
    min_w = max(0.0, float(min_weight))
    rel = (relation or "").strip().lower()
    blk = (block or "").strip().lower()

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
                tdomain = (tpayload.get("character_name", "")
                           or tpayload.get("domain", "") or "general")
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

    # 精馏：按 weight 降序截断（强关联优先）
    relations.sort(key=lambda x: x.get("weight", 0.0), reverse=True)
    relations = relations[:limit]

    # 补邻居节点摘要（target_type / target_title=content 前 80 字）
    for item in relations:
        node = store.get_node(item["target_id"])
        if not node:
            continue
        payload = node.get("payload", {}) or {}
        item["target_type"] = payload.get("type", "")
        item["target_title"] = _shorten(payload.get("content", ""), 80)

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


@mcp.tool()
def kb_index() -> str:
    """知识库索引：扫描知识库根目录下所有 .md 文件，返回相对路径 + 文件名"""
    entries = []
    for fp in _kb_md_files():
        rel = os.path.relpath(fp, KNOWLEDGE_DIR).replace("\\", "/")
        entries.append({"path": rel, "name": os.path.basename(fp)})
    return _to_json({"results": entries, "total": len(entries)})


@mcp.tool()
def kb_search(query: str, top_k: int = 5) -> str:
    """
    知识库语义检索（向量检索）：只查 type=kb_chunk 节点（由 scripts/build_kb_index.py
    建立索引）。返回 {path, title, score, snippet(内容前 150 字)}。
    """
    query = (query or "").strip()
    if not query:
        return _to_json({"results": [], "hint": "查询内容不能为空"})
    emb = store.embed_text(query)
    results = store.search_similar(emb, top_k=top_k, expand_depth=1)
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        # 只保留知识库块，过滤记忆节点
        if payload.get("type") != "kb_chunk":
            continue
        items.append({
            "path": payload.get("source_path", ""),
            "title": payload.get("title", ""),
            "score": round(float(r.get("score", 0.0)), 4),
            "snippet": _shorten(payload.get("content", ""), 150),
        })
    if not items:
        return _to_json({"results": [], "hint": "未命中，可试 kb_index 查看全部笔记"})
    return _to_json({"results": items})


# ---- 阶段三：图关联区（分区返回）----
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
        # L1 虚拟节点（id=-1，MEMORY.md 命中）无图谱边，跳过——否则 get_edges(-1) 崩溃
        if nid is None or nid < 0:
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


# ---- L1 嗅探（2026-08-25 主人建议：mem_search 一体化检索 L1 MEMORY.md）----
# MEMORY.md（<5KB）读入内存缓存，命中查询词则置顶返回；底层仍物理隔离于 TriviumDB。
_L1_CACHE: dict = {"path": "", "mtime": 0.0, "content": ""}
_L1_MAX_SIZE = 5 * 1024  # <5KB 直接读进内存缓存


def _l1_sniff(query: str) -> list:
    """前置 L1 嗅探：MEMORY.md 命中查询词则返回置顶结果。

    路径来自环境变量 HERMES_MEMORY_FILE（开源不硬编码个人路径）；
    命中规则：查询词整体，或按空白/标点分词后 ≥2 字符的词，出现在 MEMORY.md 内容中。
    """
    path = os.getenv("HERMES_MEMORY_FILE", "")
    if not path or not os.path.isfile(path):
        return []
    try:
        mtime = os.path.getmtime(path)
        if _L1_CACHE["path"] != path or _L1_CACHE["mtime"] != mtime:
            if os.path.getsize(path) > _L1_MAX_SIZE:
                _L1_CACHE.update(path=path, mtime=mtime, content="")  # 过大不缓存
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    _L1_CACHE.update(path=path, mtime=mtime, content=f.read())
        content = _L1_CACHE["content"]
        if not content:
            return []
        q = (query or "").strip()
        hits = [q] if q and q in content else [
            t for t in re.split(r"[\s,，。；;、/（）()]+", q)
            if len(t) >= 2 and t in content
        ]
        if not hits:
            return []
        idx = content.find(hits[0])
        snippet = content[max(0, idx - 40):idx + 60].replace("\n", " ")
        return [{
            "id": -1,
            "type": "memory_l1",
            "score": 1.0,
            "summary": f"[L1 MEMORY.md 命中] …{snippet}…",
            "meta": {"type": "memory_l1", "importance": 1.0, "status": "active",
                     "domain": "l1", "source": "MEMORY.md", "hits": hits[:3],
                     "note": "L1 静态高频区（物理隔离于 TriviumDB）"},
        }]
    except Exception:
        return []


def _mem_search_impl(query: str, scope: str = "all", domain: str = "",
                     domain_bias: str = "", top_k: int = 5,
                     include_neighbors: bool = False,
                     neighbor_limit: int = 5, block: str = "") -> dict:
    """
    mem_search 的核心实现（返回 dict，供 mem_search 工具与 router_query 复用）。
    v2.0 统一语义层：
      - 内置 rule 加权：domain="rule" 的 kb_chunk（规则类知识）恒 ×1.3，
        无论是否显式传 domain_bias（rule 是知识子集，理应排在普通知识前）。
      - domain_bias："" 不额外 bias；"memory" 非 kb_chunk ×1.15；
        "kb" 对 kb_chunk（含 rule）×1.15；"rule" 对 rule 节点 ×1.15（叠加内置 ×1.3）。
      - 最终权重 = base_score × (rule?1.3:1) × (bias 系数)，在过滤之后、排序之前应用。
      - v3.7 L1 嗅探：scope!=kb 时先查 MEMORY.md（HERMES_MEMORY_FILE），命中置顶。
    """
    if scope not in ("memory", "kb", "all"):
        scope = "all"
    if domain_bias not in ("memory", "kb", "rule"):
        domain_bias = ""
    query = (query or "").strip()
    if not query:
        return {"results": [], "scope": scope, "hint": "查询内容不能为空"}
    # 前置 L1 嗅探（2026-08-25 主人建议）：MEMORY.md 一体化检索（scope=kb 跳过；
    # block 非空且不是 hermes 时跳过——L1 属于 hermes 区块，分区查询不跨区块）
    l1_hits = ([] if scope == "kb" or (block and block.strip().lower() != "hermes")
               else _l1_sniff(query))
    emb = store.embed_text(query)
    # 一次向量检索，拉宽召回再按 scope 过滤截断，保证过滤后仍有足够结果
    results = store.search_similar(emb, top_k=max(top_k * 3, 30), expand_depth=1,
                                   block=block)
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        ptype = payload.get("type", "")
        is_kb = ptype == "kb_chunk"
        if scope == "memory" and is_kb:
            continue
        if scope == "kb" and not is_kb:
            continue
        # domain 过滤仅作用于记忆节点（kb_chunk 无 character_name 概念）
        if domain and not is_kb and payload.get("character_name") != domain:
            continue
        # 分区块（主人 2026-08-25 审查补丁）：block 非空时主结果也按区块过滤——
        # 带 --block hermes 的搜索只返回 hermes 区块内容，语义命中的跨区块节点丢弃
        if block and not domain_in_block(node_domain(payload), block):
            continue
        # v2.0 统一语义层加权：过滤之后、排序之前应用（只乘系数，不改变过滤逻辑）
        score = float(r.get("score", 0.0))
        is_rule = is_kb and payload.get("domain") == "rule"
        if is_rule:
            score *= 1.3  # 内置 rule 加权：规则类知识恒优先
        if domain_bias == "memory" and not is_kb:
            score *= 1.15
        elif domain_bias == "kb" and is_kb:
            score *= 1.15
        elif domain_bias == "rule" and is_rule:
            score *= 1.15
        meta = {
            "type": ptype,
            "importance": payload.get("importance", 0.5),
            "status": payload.get("status", ""),
            "domain": payload.get("character_name", "") or payload.get("domain", ""),
        }
        if is_kb:
            meta["source_path"] = payload.get("source_path", "")
            meta["title"] = payload.get("title", "")
        items.append({
            "id": r.get("id"),
            "type": ptype,
            "score": round(score, 4),
            "summary": _shorten(payload.get("content", ""), 150),
            "meta": meta,
        })
    # bias 后按最终 score 降序排序再截断（bias 需影响排序，不能按原始顺序收集后 break）
    items.sort(key=lambda x: x["score"], reverse=True)
    top_items = items[:top_k]
    # L1 命中置顶（L1 静态高频区优先于 L2~L4 语义结果）
    if l1_hits:
        top_items = l1_hits + top_items
    result = {"results": top_items, "scope": scope}
    if domain_bias:
        result["bias"] = domain_bias
    # 阶段三：分区返回——图关联区（语义区原样，邻居独立展示，不参与语义排序）
    if include_neighbors:
        neighbors = _collect_neighbors(top_items, max(1, min(int(neighbor_limit), 20)))
        result["neighbors"] = neighbors
        result["neighbor_count"] = len(neighbors)
    return result


@mcp.tool()
def mem_search(query: str, scope: str = "all", domain: str = "",
               domain_bias: str = "", top_k: int = 5,
               include_neighbors: bool = False,
               neighbor_limit: int = 5, block: str = "") -> str:
    """
    统一检索入口：记忆 + 知识库混合检索。
    scope 取值：memory（只查记忆节点，排除 kb_chunk）/ kb（只查知识库块）/ all（都查）。
    domain 非空时按 character_name 过滤记忆（kb_chunk 的 domain 是 "kb" 或 "rule"，不受影响）。
    domain_bias 取值 "" / "memory" / "kb" / "rule"：为空不额外 bias；"memory" 记忆优先
        （非 kb_chunk 的 score *= 1.15）；"kb" 知识优先（kb_chunk 的 score *= 1.15）；
        "rule" 规则优先（rule 节点再 ×1.15）。v2.0 起 domain="rule" 的规则类知识切片
        内置恒 ×1.3 加权（排在普通知识前），rule 是知识的子集，kb bias 时同样叠加。
        bias 在过滤之后、排序之前应用（先 bias 再按最终 score 排序截断）。
    双层返回原则不变：只返回 150 字摘要 + meta，不返回全文（全文请用 mem_get_full）。
    v3.0 分区返回：include_neighbors=True 时，语义区（results）原样返回，
        额外附 neighbors 图关联区（语义命中节点的一跳邻居，score = via_score × weight，
        去重、relation 大写、按 score 降序）。include_neighbors=False 时输出与旧版完全一致。
        neighbor_limit 钳制 1-20（默认 5）。
    """
    return _to_json(_mem_search_impl(query, scope, domain, domain_bias, top_k,
                                     include_neighbors, neighbor_limit, block))


# ---- v2.0 统一语义层：任务路由查询 ----
# 模型/配置关键词（用于从规则切片中提取「推荐模型/配置」）
_MODEL_KEYWORDS = [
    "r1", "qwen9b", "qwen", "glm", "phi", "opencode", "deepseek", "kimi",
    "gpt", "claude", "gemini", "mistral", "llama", "minimax", "yi",
    "codestral", "qwen3",
]
_CONFIG_KEYWORDS = [
    "preset", "think", "token", "max_tokens", "temperature", "8k", "16k",
    "32k", "context", "stream", "tool",
]


def _extract_recommendation(text: str) -> tuple:
    """
    从规则片段文本中粗提取「推荐模型/配置」。
    返回 (recommended_dict|None, confidence, hit_model, hit_config)。
    confidence：命中模型关键词 → high；只命中部分（仅配置关键词）→ medium；无命中 → low。
    """
    text = text or ""
    low = text.lower()
    hit_models = [m for m in _MODEL_KEYWORDS if m in low]
    hit_configs = [c for c in _CONFIG_KEYWORDS if c in low]
    if hit_models:
        model = hit_models[0]
        config_hints = [c for c in hit_configs]
        recommended = {"model": model}
        if config_hints:
            recommended["config"] = ", ".join(config_hints[:4])
        return recommended, "high", bool(hit_models), bool(hit_configs)
    if hit_configs:
        return {"model": None, "config": ", ".join(hit_configs[:4])}, "medium", False, True
    return None, "low", False, False


@mcp.tool()
def router_query(task: str, top_k: int = 3) -> str:
    """
    任务路由查询（v2.0 统一语义层）：给定一个任务描述，查规则类知识切片
    （domain=rule，即 副官加班协议/宪法/模型军团管理办法/模型路由决策树 等），
    提取「推荐模型/配置」建议。
    返回 JSON：{"task", "recommended": {model, config}|null, "confidence": high|medium|low,
               "sources": [{path, score, snippet(前100字)}], "raw_hint": 规则原文摘要}
    """
    task = (task or "").strip()
    if not task:
        return _to_json({
            "task": task, "recommended": None, "confidence": "low",
            "sources": [], "raw_hint": "任务描述不能为空",
        })
    # 内部复用 mem_search 核心：只查规则切片（scope=kb + domain_bias=rule + 内置 rule ×1.3）
    raw = _mem_search_impl(task, scope="kb", domain="", domain_bias="rule", top_k=top_k)
    results = raw.get("results", [])
    sources = []
    for item in results:
        sources.append({
            "path": item["meta"].get("source_path", ""),
            "score": item["score"],
            "snippet": _shorten(item.get("summary", ""), 100),
        })
    # 拼接规则原文（前 800 字）做关键词提取
    raw_text = " ".join(item.get("summary", "") for item in results)[:800]
    recommended, confidence, _hit_model, _hit_cfg = _extract_recommendation(raw_text)
    return _to_json({
        "task": task,
        "recommended": recommended,
        "confidence": confidence,
        "sources": sources,
        "raw_hint": _shorten(raw_text, 300),
    })


if __name__ == "__main__":
    mcp.run()
