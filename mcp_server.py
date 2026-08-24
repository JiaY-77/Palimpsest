# -*- coding: utf-8 -*-
"""
MemoryHub 本地 MCP Server
=========================
把小七（Hermes）的记忆层能力暴露为标准 MCP 工具，供 Hermes 通过 MCP 协议调用。
底层使用 MemoryHub 的 TriviumStore（向量 + 图 + 文档存储），embedding 由本地
Ollama 的 qwen3-embedding:0.6b 生成（1024 维，已验证可用）。

工具列表：
  1. mem_retrieve - 语义检索记忆（只返回 150 字摘要 + meta，绝不返回全文，省 token）
  2. mem_get_full - 按 id 取完整记忆内容（全文由本工具单独取）
  3. mem_ingest   - 写入新记忆（带冲突检测：相似旧记忆标记 outdated + REVISED_BY 链）
  4. mem_recent   - 最近记忆列表（按 created_at 倒序）
  5. kb_index     - 知识库文件索引（扫描 D:/HeJiaQi/Documents/Knowledge 下所有 .md）
  6. kb_search    - 知识库语义检索（向量检索，只查 build_kb_index.py 建的 kb_chunk 节点，
                   含 domain=rule 规则类切片）
  7. mem_search   - 统一检索入口：scope=memory/kb/all 混合检索记忆与知识库
                   （v2.0：domain=rule 规则切片内置 ×1.3 加权）
  8. router_query - 任务路由查询（v2.0）：查规则类知识切片，提取推荐模型/配置
  9. mem_version_history - 版本历史查询：沿 REVISED_BY 修订链返回版本演进摘要
                  （如 SOUL 版本日志；domain/full_content/offset/limit 参数）

运行方式（由 Hermes 以 stdio 方式拉起）：
    D:/HeJiaQi/Documents/Code/Python/Memory_Hub/venv/Scripts/python.exe mcp_server.py
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
from core.trivium_store import TriviumStore  # noqa: E402

# 知识库根目录（小七的知识库）
KNOWLEDGE_DIR = r"D:/HeJiaQi/Documents/Knowledge"

# 全局存储实例（TriviumDB：向量 + 图 + 文档）
store = TriviumStore()

mcp = FastMCP("memoryhub")


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
    node_id = store.insert_node(node_data, emb)

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
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        if not node:
            continue
        payload = node.get("payload", {}) or {}
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


# ---- 版本历史查询（REVISED_BY 修订链）----
_VERSION_RE = re.compile(r"\[SOUL变更日志\]\s*(\d{4}-\d{2}-\d{2})\s*版本\s*([\d.]+)")
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
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        if not node:
            continue
        payload = node.get("payload", {}) or {}
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


@mcp.tool()
def kb_index() -> str:
    """知识库索引：扫描 D:/HeJiaQi/Documents/Knowledge 下所有 .md 文件，返回相对路径 + 文件名"""
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


def _mem_search_impl(query: str, scope: str = "all", domain: str = "",
                     domain_bias: str = "", top_k: int = 5) -> dict:
    """
    mem_search 的核心实现（返回 dict，供 mem_search 工具与 router_query 复用）。
    v2.0 统一语义层：
      - 内置 rule 加权：domain="rule" 的 kb_chunk（规则类知识）恒 ×1.3，
        无论是否显式传 domain_bias（rule 是知识子集，理应排在普通知识前）。
      - domain_bias："" 不额外 bias；"memory" 非 kb_chunk ×1.15；
        "kb" 对 kb_chunk（含 rule）×1.15；"rule" 对 rule 节点 ×1.15（叠加内置 ×1.3）。
      - 最终权重 = base_score × (rule?1.3:1) × (bias 系数)，在过滤之后、排序之前应用。
    """
    if scope not in ("memory", "kb", "all"):
        scope = "all"
    if domain_bias not in ("memory", "kb", "rule"):
        domain_bias = ""
    query = (query or "").strip()
    if not query:
        return {"results": [], "scope": scope, "hint": "查询内容不能为空"}
    emb = store.embed_text(query)
    # 一次向量检索，拉宽召回再按 scope 过滤截断，保证过滤后仍有足够结果
    results = store.search_similar(emb, top_k=max(top_k * 3, 30), expand_depth=1)
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
    result = {"results": items[:top_k], "scope": scope}
    if domain_bias:
        result["bias"] = domain_bias
    return result


@mcp.tool()
def mem_search(query: str, scope: str = "all", domain: str = "",
               domain_bias: str = "", top_k: int = 5) -> str:
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
    """
    return _to_json(_mem_search_impl(query, scope, domain, domain_bias, top_k))


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
