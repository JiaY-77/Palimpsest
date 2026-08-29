# -*- coding: utf-8 -*-
"""
mcp_tools.kb —— 知识库相关工具
=============================
kb_index（扫描知识库根目录 .md）/ kb_search（只查 type=kb_chunk 的语义检索）。
"""

import os  # noqa: E402

from mcp_tools._common import (  # noqa: E402
    KNOWLEDGE_DIR, _kb_md_files, _shorten, _to_json, mcp, store,
)


@mcp.tool()
def kb_index() -> str:
    """知识库索引：扫描知识库根目录下所有 .md 文件，返回相对路径 + 文件名"""
    if not os.path.isdir(KNOWLEDGE_DIR):
        return _to_json({
            "results": [],
            "hint": f"知识库目录不存在，请设置 KNOWLEDGE_DIR 或创建 {KNOWLEDGE_DIR}",
        })
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
