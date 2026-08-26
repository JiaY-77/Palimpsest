"""
Palimpsest Dashboard — 独立 FastAPI 服务
端口 8010，与主 REST 服务 (8000) 分离
"""

import sys
import os

# 确保项目根目录在 sys.path 中，以便导入 core.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI
from fastapi.responses import FileResponse
from core.trivium_store import TriviumStore
from core import fts_index, consolidator

app = FastAPI(title="Palimpsest Dashboard")

store = TriviumStore()

DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.html"
)


@app.get("/")
async def dashboard():
    """返回 Dashboard HTML 页面"""
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/api/mem/stats")
async def mem_stats():
    """记忆库统计：总数 / 按类型分布 / 过期数"""
    all_ids = store._get_all_node_ids()
    total = len(all_ids)
    by_type: dict[str, int] = {}
    outdated = 0
    for nid in all_ids:
        node = store.get_node(nid)
        if not node:
            continue
        payload = node.get("payload", {}) or {}
        t = payload.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        if payload.get("status") == "outdated":
            outdated += 1
    return {"total": total, "by_type": by_type, "outdated": outdated}


@app.get("/api/mem/recent")
async def mem_recent(limit: int = 20):
    """最近记忆节点（按 id 倒序取 limit 个）"""
    all_ids = store._get_all_node_ids()
    all_ids.sort(reverse=True)
    recent_ids = all_ids[:limit]
    nodes = []
    for nid in recent_ids:
        node = store.get_node(nid)
        if not node:
            continue
        payload = node.get("payload", {}) or {}
        nodes.append({
            "id": nid,
            "type": payload.get("type", ""),
            "importance": payload.get("importance"),
            "content": (payload.get("content") or "")[:120],
            "status": payload.get("status", ""),
        })
    return {"nodes": nodes, "total": len(all_ids)}


@app.get("/api/mem/search")
async def mem_search(q: str = "", limit: int = 10):
    """FTS 全文搜索"""
    results = fts_index.search_fts(q, limit=limit)
    return {"query": q, "results": results, "total": len(results)}


@app.get("/api/consolidate/preview")
async def consolidate_preview():
    """预览合并候选（dry-run）"""
    return consolidator.consolidate(store, dry_run=True)


@app.post("/api/consolidate/apply")
async def consolidate_apply():
    """执行合并（不可逆）"""
    return consolidator.consolidate(store, dry_run=False)


if __name__ == "__main__":
    import uvicorn
    from config import Config
    uvicorn.run(app, host="127.0.0.1", port=Config.DASHBOARD_PORT)
