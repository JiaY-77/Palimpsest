"""
Palimpsest Dashboard — 独立 FastAPI 服务
端口 8010，与主 REST 服务 (8090) 分离

架构（2026-09-26 起）
---------------------
本服务是**纯客户端**：不再自己打开数据库，所有数据一律经主 REST 服务获取。

原因：TriviumDB 对库是**连接级排他**的——只要一个连接持着库，第二个连接
连打开都会失败。dashboard 原先自己 `TriviumStore()` 开库，等于在 REST 之外
多出一个「写者」，两者互相抢库，表现为间歇性写失败并最终导致文件组损坏。
历史上「库老是坏」的根因正在于此。

对外 API（/api/*，dashboard.html 使用）保持不变，仅内部实现改为代理 REST。
"""

import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from config import Config

app = FastAPI(title="Palimpsest Dashboard")

REST_BASE_URL = (
    os.getenv("PALIMPSEST_BASE_URL") or f"http://127.0.0.1:{Config.REST_PORT}"
).rstrip("/")

DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.html"
)


def _rest(method: str, path: str, **kwargs) -> object:
    """代理一次 REST 调用；失败转成 HTTPException，明确说明是后端问题。"""
    url = f"{REST_BASE_URL}{path}"
    try:
        resp = httpx.request(method, url, timeout=15.0, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"主 REST 服务不可达：{url}\n"
                f"底层错误：{exc}\n"
                f"处理：确认 Palimpsest REST 在运行（端口 {Config.REST_PORT}），"
                f"或设置 PALIMPSEST_BASE_URL 指向已运行的实例。"
            ),
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"REST 返回 {resp.status_code}：{resp.text[:300]}"
        )
    if not resp.content:
        return None
    return resp.json()


@app.get("/")
async def dashboard():
    """返回 Dashboard HTML 页面"""
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/api/mem/stats")
async def mem_stats():
    """记忆库统计：总数 / 按类型分布 / 过期数（代理 REST ``POST /mem/stats``）"""
    data = _rest("POST", "/mem/stats")
    if not isinstance(data, dict):
        return {"total": 0, "by_type": {}, "outdated": 0}

    totals = data.get("totals") if isinstance(data.get("totals"), dict) else data
    total = (
        totals.get("total_nodes")
        or totals.get("total")
        or totals.get("nodes")
        or 0
    )
    by_type = totals.get("by_type") or data.get("by_type") or {}
    outdated = totals.get("outdated") or 0
    return {"total": total, "by_type": by_type, "outdated": outdated}


@app.get("/api/mem/recent")
async def mem_recent(limit: int = 20):
    """最近记忆节点（代理 REST ``GET /export``，按 id 倒序取前 limit 个）。"""
    data = _rest("GET", "/export", params={"page": 1, "page_size": limit})
    items = []
    if isinstance(data, dict):
        items = data.get("memories") or data.get("items") or []
    elif isinstance(data, list):
        items = data

    nodes = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = item
        nodes.append({
            "id": item.get("id"),
            "type": payload.get("type", ""),
            "importance": payload.get("importance"),
            "content": (payload.get("content") or "")[:120],
            "status": payload.get("status", ""),
        })
    nodes.sort(key=lambda n: (n["id"] is None, -(n["id"] or 0)))
    return {"nodes": nodes[:limit], "total": len(nodes)}


@app.get("/api/mem/search")
async def mem_search(q: str = "", limit: int = 10):
    """搜索（代理 REST ``POST /mem/search``）。

    注意语义差异：原实现走本地 FTS 全文检索（``fts_index.search_fts``）；
    改走 REST 后使用 ``/mem/search``，它是**语义检索**。对用户而言是能力增强
    （能命中近义表述），但排序与 FTS 不同——如实记录，不当作「无差别替换」。
    """
    if not q:
        return {"query": q, "results": [], "total": 0}
    data = _rest("POST", "/mem/search", json={"query": q, "top_k": limit})
    results = data.get("results", []) if isinstance(data, dict) else (data or [])
    return {"query": q, "results": results, "total": len(results)}


@app.get("/api/consolidate/preview")
async def consolidate_preview():
    """预览合并候选（dry-run）。

    REST 尚未暴露 consolidate 业务端点。按架构决策，复合操作应作为**服务端
    业务级端点**（如 ``POST /mem/consolidate``）暴露，而不是让客户端直接开库。
    此处显式返回 501 并说明原因——不静默返回空结果，避免「看着正常、实际没干活」。
    """
    raise HTTPException(
        status_code=501,
        detail=(
            "合并功能暂不可用：REST 尚未提供 consolidate 端点。"
            "按架构决策，该操作应由服务端业务端点承担（POST /mem/consolidate），"
            "而不是由客户端直接打开数据库。待服务端补齐后本接口恢复。"
        ),
    )


@app.post("/api/consolidate/apply")
async def consolidate_apply():
    """执行合并（不可逆）——同 ``/api/consolidate/preview``，待服务端业务端点。"""
    raise HTTPException(
        status_code=501,
        detail=(
            "合并功能暂不可用：REST 尚未提供 consolidate 端点。"
            "见 /api/consolidate/preview 的说明。"
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=Config.DASHBOARD_PORT)
