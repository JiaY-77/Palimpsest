"""Dashboard 的 `/api/*` 路由 —— **一份实现，两种挂载**。

## 为什么抽出来

Dashboard 原先只以独立进程存在（`scripts/dashboard.py`，端口 8010，纯客户端代理
8090）。这让「大众部署」多了一道门槛：只跑 REST 的人根本没有面板，而文档里那条
启动命令还曾经因为漏注入 `sys.path` 而直接崩（2026-10-28 修）。

融合方式是：把所有端点搬进这里，REST（`main.py`）和独立进程（`scripts/dashboard.py`）
各自 `include_router`，前端 `dashboard.html` 一行都不用改。

## 数据源可注入（关键设计）

| 挂载方式 | backend | 为什么 |
|---|---|---|
| REST 进程内（`8090/dashboard`） | `main.py` 注入的**直读实现**（store / 内部函数） | REST 不能自己 HTTP 调自己：那会占用两个线程、平白多一跳，还可能在线程池紧张时自锁 |
| 独立进程（`8010/`） | `client.remote_store` 风格的 HTTP 代理 | 独立进程没有库访问权（单写者），必须经 REST |

四个数据源函数就够了——九个端点全部由它们组合出来，逻辑在这里只写一份。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

# 知识库切片豁免衰减（知识不老化）——与检索侧的豁免口径一致。
_KB_EXEMPT_TYPES = frozenset({"kb_chunk", "skill_chunk", "novel_chunk"})

# 全量扫描的短 TTL 缓存：面板刷新（人手点）不需要每次重扫全库。
_EXPORT_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class DashboardBackend:
    """Dashboard 需要的四个数据源（挂载方提供实现）。

    - ``scan_nodes``：返回**带 payload** 的全库节点列表（面板算冲突/衰减/图谱要用）
    - ``stats``：库级统计（总数 / 按类型 / 过期数 / 图规模）
    - ``search``：语义检索 ``(query, limit) -> {"results": [...]}``
    - ``fact_history``：单节点的版本链 ``(node_id) -> dict``
    """

    scan_nodes: Callable[[], list[dict[str, Any]]]
    stats: Callable[[], dict[str, Any]]
    search: Callable[[str, int], dict[str, Any]]
    fact_history: Callable[[int], dict[str, Any]]
    # 可选：合并候选预览（dry-run）。REST 的 `POST /mem/consolidate` 只做只读预览，
    # 所以这里能给出真实候选；**apply 仍然不暴露**（写操作不通过面板发起）。
    consolidate_preview: Callable[[], dict[str, Any]] | None = None


def _to_float(value: Any, default: float = 0.0) -> float:
    """payload 里的数字多为字符串（如 ``"0.95"``），统一兜底转换，脏值回退默认。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _node_view(item: dict[str, Any]) -> dict[str, Any]:
    """把一条导出记录压成面板用的精简视图。"""
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


def build_router(
    backend: DashboardBackend,
    *,
    page_route: str = "/dashboard",
    page_path: str,
) -> APIRouter:
    """构造面板路由。

    - ``page_route``：页面自身的路径（REST 挂 ``/dashboard``，独立进程挂 ``/``）
    - ``page_path``：`dashboard.html` 的绝对路径
    """
    router = APIRouter(tags=["dashboard"])
    cache: dict[str, Any] = {"nodes": [], "at": 0.0}

    def _scan_cached() -> list[dict[str, Any]]:
        now = time.time()
        if cache["nodes"] and (now - cache["at"]) < _EXPORT_TTL_SECONDS:
            return cache["nodes"]
        nodes = backend.scan_nodes()
        cache["nodes"] = nodes
        cache["at"] = now
        return nodes

    @router.get(page_route, include_in_schema=True)
    def dashboard_page() -> FileResponse:
        """Dashboard 页面。"""
        return FileResponse(page_path, media_type="text/html")

    @router.get("/api/mem/stats")
    def mem_stats() -> dict[str, Any]:
        """记忆库统计：总数 / 按类型分布 / 过期数。"""
        data = backend.stats()
        if not isinstance(data, dict):
            return {"total": 0, "by_type": {}, "outdated": 0}
        raw_totals = data.get("totals")
        totals: dict[str, Any] = raw_totals if isinstance(raw_totals, dict) else data
        total = totals.get("total_nodes") or totals.get("total") or totals.get("nodes") or 0
        by_type = totals.get("by_type") or data.get("by_type") or {}
        outdated = totals.get("outdated") or 0
        return {"total": total, "by_type": by_type, "outdated": outdated}

    @router.get("/api/mem/recent")
    def mem_recent(limit: int = 20) -> dict[str, Any]:
        """最近记忆节点（按 id 倒序取前 ``limit`` 个）。"""
        nodes = [
            {
                "id": n["id"],
                "type": n["type"],
                "importance": n["importance"],
                "content": (n["content"] or "")[:120],
                "status": n["status"],
            }
            for n in (_node_view(item) for item in _scan_cached())
        ]
        nodes.sort(key=lambda n: (n["id"] is None, -(n["id"] or 0)))
        return {"nodes": nodes[:limit], "total": len(nodes)}

    @router.get("/api/mem/search")
    def mem_search(q: str = "", limit: int = 10) -> dict[str, Any]:
        """语义检索（与 REST ``/mem/search`` 同口径，不是旧的 FTS 全文检索）。"""
        if not q:
            return {"query": q, "results": [], "total": 0}
        data = backend.search(q, limit)
        results = data.get("results", []) if isinstance(data, dict) else (data or [])
        return {"query": q, "results": results, "total": len(results)}

    @router.get("/api/consolidate/preview")
    def consolidate_preview() -> dict[str, Any]:
        """预览合并候选（dry-run，只读）。

        挂载方提供了 ``consolidate_preview`` 就返回真实候选（REST 的
        ``POST /mem/consolidate`` 只做只读预览，正好对得上）；没有实现才 501，
        绝不静默返回空结果。
        """
        if backend.consolidate_preview is None:
            raise HTTPException(
                status_code=501,
                detail="合并预览暂不可用：当前挂载方式未提供 consolidate 数据源（只读预览需要 REST 的 POST /mem/consolidate）。",
            )
        return backend.consolidate_preview()

    @router.post("/api/consolidate/apply")
    def consolidate_apply() -> dict[str, Any]:
        """执行合并（不可逆）——**面板不发起写操作**，恒定 501。

        这是有意的架构边界：面板是只读观察窗，破坏性操作用 CLI（可审计、可 dry-run
        先看）。REST 的 ``POST /mem/consolidate`` 同样写死 ``dry_run=True``。
        """
        raise HTTPException(
            status_code=501,
            detail=(
                "面板不提供合并写操作（只读观察窗）。需要真实合并请用 CLI："
                "`python scripts/palimpsest_cli.py consolidate --apply`，"
                "先不带 --apply 看候选。"
            ),
        )

    @router.get("/api/mem/conflicts")
    def mem_conflicts(limit: int = 20) -> dict[str, Any]:
        """冲突消解链：近期被取代（``status=outdated``）的记忆，按时间倒序。"""
        outdated = [n for n in (_node_view(item) for item in _scan_cached()) if n["status"] == "outdated"]
        outdated.sort(key=lambda n: n["created_at"], reverse=True)
        return {"total": len(outdated), "nodes": outdated[:limit]}

    @router.get("/api/mem/conflict-chain")
    def mem_conflict_chain(node_id: int) -> dict[str, Any]:
        """某条记忆的版本链：谁取代了它 / 它取代了谁。"""
        data = backend.fact_history(node_id)
        return data if isinstance(data, dict) else {"found": False, "node_id": node_id}

    @router.get("/api/mem/decay")
    def mem_decay(limit: int = 20) -> dict[str, Any]:
        """遗忘曲线：``0.95^(days/30) × importance`` 最低的活跃记忆。知识切片豁免。"""
        now = time.time()
        scored: list[dict[str, Any]] = []
        for n in (_node_view(item) for item in _scan_cached()):
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

    @router.get("/api/mem/graph")
    def mem_graph() -> dict[str, Any]:
        """图谱规模：节点数 / 有边节点占比 / 孤立率 / 各边类型计数。"""
        data = backend.stats()
        if not isinstance(data, dict):
            data = {}
        raw_totals = data.get("totals")
        totals: dict[str, Any] = raw_totals if isinstance(raw_totals, dict) else {}
        raw_graph = data.get("graph")
        graph: dict[str, Any] = raw_graph if isinstance(raw_graph, dict) else {}

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

    return router
