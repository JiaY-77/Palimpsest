# -*- coding: utf-8 -*-
"""
mcp_tools.memory —— 记忆读写与查询工具
=====================================
mem_retrieve / mem_get_full / mem_ingest / mem_recent / mem_review /
mem_version_history / mem_search（及核心 _mem_search_impl）/ mem_hybrid_search、
L1 嗅探。
"""

import os  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402

from core.fts_index import index_node, search_fts  # noqa: E402
from core.secret_scan import SecretScanError  # noqa: E402
from core.trivium_store import domain_in_block, node_domain  # noqa: E402

from mcp_tools._common import _shorten, _to_json, mcp, store  # noqa: E402
from mcp_tools.graph import _collect_neighbors  # noqa: E402


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

    # FTS 全文索引同步（T056 混合检索依赖；失败不阻塞主写入，可手动 fts-rebuild 兜底）
    try:
        index_node(node_id, content)
    except Exception:
        pass

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


# ---- 混合检索（T056：FTS5 精确 + 语义向量 的 RRF 融合 / 级联策略）----
# 只新增，不改动 mem_search / _mem_search_impl 现有行为。
# RRF 标准 k=60：单侧命中也算贡献；每项 meta 标出 fts_hit / sem_hit 来源。
_RRF_K = 60.0


def _sem_candidate_items(query: str, scope: str, domain: str,
                         domain_bias: str, top_k: int, block: str) -> list:
    """语义候选：复用 _mem_search_impl 宽松召回（top_k*3），按 score 降序排名。"""
    sem = _mem_search_impl(query, scope, domain, domain_bias,
                           top_k=max(top_k * 3, 30), include_neighbors=False,
                           block=block)
    items = sem.get("results", [])
    return sorted(items, key=lambda it: it.get("score", 0.0), reverse=True)


def _fts_only_item(node_id: int, scope: str, domain: str, block: str):
    """FTS 命中但语义未命中的节点：按 payload 补全 mem_search 同构条目。

    复用 _mem_search_impl 的 scope/domain/block 过滤语义，
    避免不同 scope 下 FTS 侧混入越界结果。
    """
    node = store.get_node(node_id)
    if not node:
        return None
    payload = node.get("payload", {}) or {}
    ptype = payload.get("type", "")
    is_kb = ptype == "kb_chunk"
    if scope == "memory" and is_kb:
        return None
    if scope == "kb" and not is_kb:
        return None
    if domain and not is_kb and payload.get("character_name") != domain:
        return None
    if block and not domain_in_block(node_domain(payload), block):
        return None
    meta = {
        "type": ptype,
        "importance": payload.get("importance", 0.5),
        "status": payload.get("status", ""),
        "domain": payload.get("character_name", "") or payload.get("domain", ""),
    }
    if is_kb:
        meta["source_path"] = payload.get("source_path", "")
        meta["title"] = payload.get("title", "")
    return {
        "id": node_id,
        "type": ptype,
        "score": 0.0,
        "summary": _shorten(payload.get("content", ""), 150),
        "meta": meta,
    }


def _hybrid_rrf(query: str, scope: str, domain: str, domain_bias: str,
                top_k: int, fts_limit: int, block: str) -> list:
    """RRF 融合：语义排名 + FTS 排名的 reciprocal rank 求和（k=60）。

    两个排名都是 0-based；单侧命中也计入 rrf；按 rrf 降序取 top_k。
    """
    sem_items = _sem_candidate_items(query, scope, domain, domain_bias, top_k, block)
    fts = search_fts(query, limit=fts_limit)

    rrf: dict = {}
    sem_hit: set = set()
    fts_hit: set = set()
    for rank, item in enumerate(sem_items):
        nid = item.get("id")
        if nid is None:
            continue
        rrf[nid] = rrf.get(nid, 0.0) + 1.0 / (_RRF_K + rank)
        sem_hit.add(nid)
    for rank, r in enumerate(fts):
        nid = r.get("node_id")
        if nid is None:
            continue
        rrf[nid] = rrf.get(nid, 0.0) + 1.0 / (_RRF_K + rank)
        fts_hit.add(nid)

    # 语义条目按 id 建表，便于合并命中来源标记；FTS-only 节点按 payload 补全
    by_id = {item.get("id"): item for item in sem_items if item.get("id") is not None}
    merged = []
    for nid, score in sorted(rrf.items(), key=lambda kv: kv[1], reverse=True):
        item = by_id.get(nid)
        if item is None:
            item = _fts_only_item(nid, scope, domain, block)
            if item is None:
                continue
        item = dict(item)
        meta = dict(item.get("meta", {}) or {})
        meta["fts_hit"] = nid in fts_hit
        meta["sem_hit"] = nid in sem_hit
        item["meta"] = meta
        item["score"] = round(score, 4)
        merged.append(item)
        if len(merged) >= top_k:
            break
    return merged


def _hybrid_cascade(query: str, scope: str, domain: str, domain_bias: str,
                    top_k: int, fts_limit: int, block: str) -> list:
    """级联：FTS 粗筛候选集 → 向量精排（只留交集）→ 不足 top_k 从剩余语义补足。

    候选集为空时退化为纯语义结果；兜底条目 fts_hit=False 如实标记未过 FTS 粗筛。
    """
    fts = search_fts(query, limit=fts_limit)
    fts_ids = {r.get("node_id") for r in fts if r.get("node_id") is not None}
    sem_items = _sem_candidate_items(query, scope, domain, domain_bias, top_k, block)

    in_candidate = [it for it in sem_items if it.get("id") in fts_ids]
    rest = [it for it in sem_items if it.get("id") not in fts_ids]
    picked = in_candidate[:top_k]
    if len(picked) < top_k:
        picked = picked + rest[:top_k - len(picked)]

    items = []
    for it in picked:
        it = dict(it)
        meta = dict(it.get("meta", {}) or {})
        meta["fts_hit"] = it.get("id") in fts_ids
        meta["sem_hit"] = True
        it["meta"] = meta
        it["score"] = round(float(it.get("score", 0.0)), 4)
        items.append(it)
    return items


def _hybrid_search_impl(query: str, scope: str = "all", domain: str = "",
                        domain_bias: str = "", top_k: int = 5, mode: str = "rrf",
                        fts_limit: int = 50, include_neighbors: bool = False,
                        neighbor_limit: int = 5, block: str = "") -> dict:
    """
    mem_hybrid_search 的核心实现（返回 dict，供 mem_hybrid_search 工具复用）。
    T056 混合检索增强：
      - mode="rrf"（默认）：Reciprocal Rank Fusion（k=60），FTS5 精确 + 语义向量
        双路排名求和，单侧命中也算贡献；score = RRF 分。
      - mode="cascade"：FTS 粗筛候选集 → 向量精排只保留交集 → 交集不足 top_k
        时从剩余语义结果按 score 补足（候选集为空退化为纯语义）；score = 语义分。
    返回结构与 mem_search 一致：{"results", "scope", "bias"(可选), "mode",
    "neighbors"/"neighbor_count"(include_neighbors=True 时)}。
    空 query 返回 hint；任何异常吞掉返回 hint，不抛出。
    """
    if scope not in ("memory", "kb", "all"):
        scope = "all"
    if domain_bias not in ("memory", "kb", "rule"):
        domain_bias = ""
    if mode not in ("rrf", "cascade"):
        mode = "rrf"
    query = (query or "").strip()
    if not query:
        return {"results": [], "scope": scope, "hint": "查询内容不能为空"}
    top_k = max(1, int(top_k or 0))
    fts_limit = max(1, int(fts_limit or 0))
    try:
        if mode == "cascade":
            items = _hybrid_cascade(query, scope, domain, domain_bias,
                                    top_k, fts_limit, block)
        else:
            items = _hybrid_rrf(query, scope, domain, domain_bias,
                                top_k, fts_limit, block)
        result = {"results": items, "scope": scope, "mode": mode}
        if domain_bias:
            result["bias"] = domain_bias
        if include_neighbors:
            neighbors = _collect_neighbors(items, max(1, min(int(neighbor_limit), 20)))
            result["neighbors"] = neighbors
            result["neighbor_count"] = len(neighbors)
        return result
    except Exception as e:
        return {"results": [], "scope": scope, "hint": f"混合检索失败: {e}"}


@mcp.tool()
def mem_hybrid_search(query: str, scope: str = "all", domain: str = "",
                      domain_bias: str = "", top_k: int = 5, mode: str = "rrf",
                      fts_limit: int = 50, include_neighbors: bool = False,
                      neighbor_limit: int = 5, block: str = "") -> str:
    """
    混合检索（T056）：FTS5 精确检索 + 语义向量检索的融合排序。
    mode 取值："rrf"（默认，Reciprocal Rank Fusion，k=60，单侧命中也算）/
              "cascade"（FTS 粗筛候选集 → 向量精排交集，交集不足从剩余语义补足）。
    scope 同 mem_search（memory/kb/all）；fts_limit 控制 FTS 侧候选量；
    每项返回 {id, type, score, summary, meta}，meta 含 fts_hit/sem_hit 命中来源标记；
    include_neighbors=True 时附 neighbors 图关联区（复用 _collect_neighbors）。
    其余参数（domain/domain_bias/neighbor_limit/block）语义同 mem_search。
    """
    return _to_json(_hybrid_search_impl(
        query, scope, domain, domain_bias, top_k, mode, fts_limit,
        include_neighbors, neighbor_limit, block,
    ))
