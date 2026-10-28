"""Dashboard 融合进 REST（`/dashboard` + `/api/*`）的契约测试。

三件事必须钉住：

1. REST 进程自己就能提供面板（不用另起 8010）；
2. 九个只读端点全部走**注入的数据源**，不再 HTTP 调自己；
3. 合并预览真的能出候选（REST 已有 dry-run 端点），而**写操作永远是 501**
   （面板是只读观察窗，破坏性操作走 CLI）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.dashboard_api import DashboardBackend, build_router


def _client():
    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. REST 自带面板
# ---------------------------------------------------------------------------


def test_dashboard_page_served_by_rest(db_path):
    """`/dashboard` 直接返回面板 HTML——这是「一个进程搞定」的核心。"""
    r = _client().get("/dashboard")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers.get("content-type", "")
    assert "Palimpsest Dashboard" in r.text[:2000]


def test_stats_and_recent_work_in_rest(db_path):
    client = _client()
    stats = client.get("/api/mem/stats")
    assert stats.status_code == 200, stats.text
    assert "total" in stats.json()

    recent = client.get("/api/mem/recent?limit=3")
    assert recent.status_code == 200, recent.text
    assert "nodes" in recent.json()


def test_graph_and_conflicts_and_decay_work_in_rest(db_path):
    client = _client()
    graph = client.get("/api/mem/graph")
    assert graph.status_code == 200, graph.text
    assert "isolated_rate" in graph.json()

    assert client.get("/api/mem/conflicts?limit=2").status_code == 200
    assert client.get("/api/mem/decay?limit=2").status_code == 200


def test_search_returns_empty_for_blank_query(db_path):
    """空 query 不触发检索（省一次 embedding），返回空结果而不是报错。"""
    r = _client().get("/api/mem/search?q=")
    assert r.status_code == 200
    assert r.json()["results"] == []


# ---------------------------------------------------------------------------
# 2. 合并治理：预览可用，写操作恒 501
# ---------------------------------------------------------------------------


def test_consolidate_preview_works_in_rest(db_path):
    """REST 已有 `POST /mem/consolidate`（dry-run），面板预览不该再是 501。"""
    r = _client().get("/api/consolidate/preview")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict)


def test_consolidate_apply_is_always_501(db_path):
    """面板不发起写操作——这是架构边界，不是缺陷。"""
    r = _client().post("/api/consolidate/apply")
    assert r.status_code == 501
    assert "CLI" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 独立挂载模式（scripts/dashboard.py 那条路）用注入的数据源
# ---------------------------------------------------------------------------


class _FakeBackend(DashboardBackend):
    """构造型数据源：验证「同一份路由换个 backend 就能挂到别处」。"""


def _fake_app(**overrides) -> FastAPI:
    nodes = [
        {
            "id": 3,
            "payload": {
                "type": "memory",
                "domain": "d",
                "importance": 0.95,
                "created_at": 1.0,
                "status": "active",
                "content": "活跃",
            },
        },
        {
            "id": 2,
            "payload": {
                "type": "memory",
                "domain": "d",
                "importance": 0.5,
                "created_at": 1.0,
                "status": "outdated",
                "content": "被取代",
            },
        },
        {
            "id": 1,
            "payload": {
                "type": "kb_chunk",
                "domain": "kb",
                "importance": 0.9,
                "created_at": 1.0,
                "status": "active",
                "content": "知识",
            },
        },
    ]
    defaults: dict = {
        "scan_nodes": lambda: nodes,
        "stats": lambda: {
            "totals": {"total_nodes": 3, "outdated": 1, "by_type": {"memory": 2, "kb_chunk": 1}},
            "graph": {
                "nodes_with_edges": 2,
                "total_edges": 5,
                "avg_outdegree": 1.2,
                "label_dist_top10": {"RELATED_TO": 5},
            },
        },
        "search": lambda q, limit: {"results": [{"id": 3, "score": 0.9, "summary": q}]},
        "fact_history": lambda node_id: {"found": True, "node_id": node_id},
        "consolidate_preview": lambda: {"candidates": []},
    }
    defaults.update(overrides)
    app = FastAPI()
    app.include_router(
        build_router(
            _FakeBackend(**defaults),  # type: ignore[arg-type]
            page_route="/",
            page_path=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "dashboard.html"),
        )
    )
    return TestClient(app)


def test_backend_injection_drives_all_endpoints():
    """换成构造数据源后，统计/冲突/衰减/搜索全部反映注入的数据。"""
    c = _fake_app()
    assert c.get("/api/mem/stats").json()["total"] == 3
    assert c.get("/api/mem/stats").json()["outdated"] == 1

    conflicts = c.get("/api/mem/conflicts").json()
    assert conflicts["total"] == 1 and conflicts["nodes"][0]["id"] == 2

    # 衰减：kb_chunk 豁免 → 只剩两条 memory 参与
    decay = c.get("/api/mem/decay").json()
    assert decay["total"] == 1 or decay["total"] == 2
    assert all(n["type"] != "kb_chunk" for n in decay["nodes"])

    graph = c.get("/api/mem/graph").json()
    assert graph["total_nodes"] == 3 and graph["nodes_with_edges"] == 2
    assert graph["isolated_nodes"] == 1

    assert c.get("/api/mem/search?q=x").json()["total"] == 1
    assert c.get("/api/mem/conflict-chain?node_id=7").json()["node_id"] == 7


def test_preview_returns_501_without_backend():
    """挂载方没提供合并数据源时如实 501——不静默返回空。"""
    c = _fake_app(consolidate_preview=None)
    assert c.get("/api/consolidate/preview").status_code == 501


@pytest.mark.parametrize(
    "path", ["/api/mem/stats", "/api/mem/recent", "/api/mem/conflicts", "/api/mem/decay", "/api/mem/graph"]
)
def test_all_readonly_endpoints_survive_empty_store(path):
    """空库不该让任何只读端点 500（面板首次打开就是这种状态）。"""
    c = _fake_app(
        scan_nodes=list,
        stats=lambda: {"totals": {"total_nodes": 0}, "graph": {}},
    )
    assert c.get(path).status_code == 200
