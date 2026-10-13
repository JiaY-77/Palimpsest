"""
mcp_tools.kb —— 知识库相关工具
=============================
kb_index（扫描知识库根目录 .md）/ kb_search（只查 type=kb_chunk 的语义检索）。
"""

import os

from config import Config
from core.utils import _to_float
from mcp_tools._common import (
    KNOWLEDGE_DIR,
    _kb_md_files,
    _shorten,
    _to_json,
    mcp,
    store,
)


@mcp.tool()
def kb_index() -> str:
    """知识库索引：扫描知识库根目录下所有 .md 文件，返回相对路径 + 文件名"""
    if not os.path.isdir(KNOWLEDGE_DIR):
        return _to_json(
            {
                "results": [],
                "hint": f"知识库目录不存在，请设置 KNOWLEDGE_DIR 或创建 {KNOWLEDGE_DIR}",
            }
        )
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
    # 候选池隔离：知识库块在库里是少数派（记忆/技能节点远多于 kb_chunk），
    # 同池竞争时会被挤出候选窗口，过滤后返回空——即使知识库已建索引。
    # 把过滤条件下推到检索层（payload_filter），只在 kb_chunk 子集内排序，
    # 噪声再多也不影响召回。与 skill_search 同款修法（见 mcp_tools/skill.py）。
    results = store.search_similar(
        emb,
        top_k=max(int(top_k), 1),
        expand_depth=getattr(Config, "RETRIEVAL_EXPAND_DEPTH", 0),
        payload_filter={"type": "kb_chunk"},
    )
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        # 下推过滤已保证子集隔离，此处保留防御性校验（与检索契约一致）
        if payload.get("type") != "kb_chunk":
            continue
        items.append(
            {
                "path": payload.get("source_path", ""),
                "title": payload.get("title", ""),
                "score": round(_to_float(r.get("score"), 0.0), 4),
                "snippet": _shorten(payload.get("content", ""), 150),
            }
        )
    if not items:
        return _to_json({"results": [], "hint": "未命中，可试 kb_index 查看全部笔记"})
    return _to_json({"results": items})
