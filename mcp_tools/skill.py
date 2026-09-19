"""Hermes 技能语义检索工具。"""

from config import Config
from core.utils import _to_float
from mcp_tools._common import _to_json, mcp, store


@mcp.tool()
def skill_search(query: str, top_k: int = 5) -> str:
    """
    技能语义检索（向量检索）：只查 type=skill_chunk 节点（由
    scripts/build_skill_index.py 建立索引）。返回 name、description、category、
    source_path 和 score。
    """
    query = (query or "").strip()
    if not query:
        return _to_json({"results": [], "hint": "查询内容不能为空"})
    emb = store.embed_text(query)
    results = store.search_similar(
        emb,
        top_k=top_k,
        expand_depth=getattr(Config, "RETRIEVAL_EXPAND_DEPTH", 0),
    )
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        if payload.get("type") != "skill_chunk":
            continue
        items.append({
            "name": payload.get("name", ""),
            "description": payload.get("description", ""),
            "category": payload.get("category", ""),
            "source_path": payload.get("source_path", ""),
            "score": round(_to_float(r.get("score"), 0.0), 4),
        })
    if not items:
        return _to_json({
            "results": [],
            "hint": "未命中技能，可先运行 scripts/build_skill_index.py 建立索引",
        })
    return _to_json({"results": items})
