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
    # 候选池隔离：技能节点在库里是少数派（记忆/知识块远多于技能），
    # 同池竞争时会被挤出候选窗口，过滤后返回空。这里把过滤条件下推到检索层
    # （payload_filter），只在 skill_chunk 子集内排序，噪声再多也不影响召回。
    results = store.search_similar(
        emb,
        top_k=max(top_k, 1),
        expand_depth=getattr(Config, "RETRIEVAL_EXPAND_DEPTH", 0),
        payload_filter={"type": "skill_chunk"},
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
        if len(items) >= max(top_k, 1):
            break
    if not items:
        return _to_json({
            "results": [],
            "hint": "未命中技能，可先运行 scripts/build_skill_index.py 建立索引",
        })
    return _to_json({"results": items})
