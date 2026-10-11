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
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from config import Config

app = FastAPI(title="Palimpsest Dashboard")

REST_BASE_URL = (os.getenv("PALIMPSEST_BASE_URL") or f"http://127.0.0.1:{Config.REST_PORT}").rstrip("/")

DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.html")

# 面板 2/3 需要全量 (id, payload) 遍历，但 dashboard 是纯客户端、不直接开库，
# 只能经 REST /export 分页拉取。全库 ~1500 节点 4 页，实测 <1s；加个极短 TTL
# 缓存避免同一页面渲染时重复扫描。只读、内存内，不写任何库。
_EXPORT_CACHE = {"at": 0.0, "nodes": []}
_EXPORT_TTL = 30.0
# 知识库切片豁免遗忘曲线（与检索侧一致：kb_chunk 是知识、不参与衰减）
_KB_EXEMPT_TYPES = {"kb_chunk", "skill_chunk", "novel_chunk"}


def _to_float(value, default: float = 0.0) -> float:
    """payload 里的数字多为字符串（如 "0.95"），统一兜底转换，脏值回退默认。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scan_nodes() -> list:
    """分页遍历 REST ``GET /export?include_payload=true``，返回带 payload 的节点列表。

    只读。短 TTL 缓存（30s）：面板刷新不重复扫库。
    """
    now = time.time()
    if _EXPORT_CACHE["nodes"] and (now - _EXPORT_CACHE["at"]) < _EXPORT_TTL:
        return _EXPORT_CACHE["nodes"]

    nodes = []
    page = 1
    while True:
        data = _rest("GET", "/export", params={"page": page, "page_size": 500, "include_payload": "true"})
        if not isinstance(data, dict):
            break
        nodes.extend(data.get("memories") or [])
        total_pages = data.get("total_pages") or 1
        if page >= total_pages:
            break
        page += 1
        if page > 200:  # 防御：异常情况下不死循环
            break

    _EXPORT_CACHE["nodes"] = nodes
    _EXPORT_CACHE["at"] = now
    return nodes


def _node_view(item: dict) -> dict:
    """把 /export 的一条记录压成面板用的精简视图。"""
    payload = item.get("payload")
    if not isinstance(payload, dict):
        payload = item if isinstance(item, dict) else {}
    return {
        "id": item.get("id"),
        "type": payload.get("type", ""),
        "domain": payload.get("domain", ""),
        "importance": _to_float(payload.get("importance"), 0.0),
        "created_at": _to_float(payload.get("created_at"), 0.0),
        "status": payload.get("status", ""),
        "content": (payload.get("content") or "")[:140],
    }


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
        raise HTTPException(status_code=502, detail=f"REST 返回 {resp.status_code}：{resp.text[:300]}")
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
    total = totals.get("total_nodes") or totals.get("total") or totals.get("nodes") or 0
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
        nodes.append(
            {
                "id": item.get("id"),
                "type": payload.get("type", ""),
                "importance": payload.get("importance"),
                "content": (payload.get("content") or "")[:120],
                "status": payload.get("status", ""),
            }
        )
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
        detail=("合并功能暂不可用：REST 尚未提供 consolidate 端点。见 /api/consolidate/preview 的说明。"),
    )


@app.get("/api/mem/conflicts")
async def mem_conflicts(limit: int = 20):
    """冲突消解链：近期被取代（status=outdated）的记忆列表。

    只读：扫 ``/export`` 取全部 outdated 节点，按 created_at 倒序。
    """
    nodes = [_node_view(n) for n in _scan_nodes()]
    outdated = [n for n in nodes if n["status"] == "outdated"]
    outdated.sort(key=lambda n: n["created_at"], reverse=True)
    return {"total": len(outdated), "nodes": outdated[:limit]}


@app.get("/api/mem/conflict-chain")
async def mem_conflict_chain(node_id: int):
    """某条记忆的版本链（代理 REST ``POST /mem/fact-history``）：谁取代了它 / 它取代了谁。"""
    data = _rest("POST", "/mem/fact-history", json={"node_id": node_id})
    return data if isinstance(data, dict) else {"found": False, "node_id": node_id}


@app.get("/api/mem/decay")
async def mem_decay(limit: int = 20):
    """遗忘曲线：按 ``0.95^(days/30) × importance`` 算有效分，列出衰减最严重的活跃记忆。

    只读。知识库切片（kb_chunk/skill_chunk/novel_chunk）豁免，不参与衰减。
    """
    now = time.time()
    nodes = [_node_view(n) for n in _scan_nodes()]
    scored = []
    for n in nodes:
        if n["status"] != "active":
            continue
        if n["type"] in _KB_EXEMPT_TYPES:
            continue
        if n["created_at"] <= 0:
            continue
        days = (now - n["created_at"]) / 86400.0
        effective = (0.95 ** (days / 30.0)) * n["importance"]
        scored.append(
            {
                "id": n["id"],
                "type": n["type"],
                "domain": n["domain"],
                "importance": round(n["importance"], 3),
                "age_days": round(days, 1),
                "effective": round(effective, 4),
                "content": n["content"],
            }
        )
    scored.sort(key=lambda n: n["effective"])
    return {"total": len(scored), "nodes": scored[:limit]}


@app.get("/api/mem/graph")
async def mem_graph():
    """图谱规模：节点数 / 有边节点占比 / 孤立率 / 各边类型计数（代理 REST ``POST /mem/stats``）。

    只读。``/mem/stats`` 已带 ``graph`` 分节；孤立率由总节点数与有边节点数推出。
    """
    data = _rest("POST", "/mem/stats")
    if not isinstance(data, dict):
        data = {}
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    graph = data.get("graph") if isinstance(data.get("graph"), dict) else {}

    total_nodes = totals.get("total_nodes", 0) or 0
    nodes_with_edges = graph.get("nodes_with_edges", 0) or 0
    isolated = max(total_nodes - nodes_with_edges, 0)
    return {
        "total_nodes": total_nodes,
        "nodes_with_edges": nodes_with_edges,
        "isolated_nodes": isolated,
        "isolated_rate": round(isolated / total_nodes, 4) if total_nodes else 0.0,
        "edge_coverage": round(nodes_with_edges / total_nodes, 4) if total_nodes else 0.0,
        "total_edges": graph.get("total_edges", 0),
        "avg_outdegree": graph.get("avg_outdegree", 0),
        "edge_labels": graph.get("label_dist_top10", {}),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=Config.DASHBOARD_PORT)
