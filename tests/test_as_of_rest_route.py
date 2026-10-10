"""REST /mem/search、/mem/recent、/mem/hybrid-search 的 as_of 参数契约测试。

背景：一期（2.6.0）曾漏掉新工具的 REST 路由，CI 全绿因无测试碰 REST 层。
本测试锁定 as_of 参数经 REST 请求体 → 路由 → MCP 实现 的完整透传。

隔离：用 conftest 的 TestClient 与临时共享库；建节点走 store 直插。
"""

import json

from fastapi.testclient import TestClient


def _client():
    from main import app

    return TestClient(app)


def _mk(store, content, domain, valid_at=None):
    payload = {"type": "memory", "content": content, "importance": 0.5, "domain": domain}
    if valid_at is not None:
        payload["valid_at"] = valid_at
    return store.insert_node(payload, store.embed_text(content))


def test_search_route_accepts_as_of():
    """/mem/search 接受 as_of 参数（不透传则参数被忽略、结果不符）。"""
    c = _client()
    from mcp_tools import store

    domain = "rest_asof_search"
    _mk(store, "REST as_of 检索契约内容甲", domain, valid_at=1000.0)
    # T=500（早于 valid_at）→ 应不返回
    resp = c.post(
        "/mem/search", json={"query": "REST as_of 检索契约内容甲", "domain": domain, "tier": "", "as_of": 500.0}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
    # T=2000（晚于 valid_at）→ 应返回
    resp2 = c.post(
        "/mem/search", json={"query": "REST as_of 检索契约内容甲", "domain": domain, "tier": "", "as_of": 2000.0}
    )
    assert len(resp2.json()["results"]) >= 1


def test_recent_route_accepts_as_of():
    """/mem/recent 接受 as_of 参数。"""
    c = _client()
    from mcp_tools import store

    domain = "rest_asof_recent"
    _mk(store, "REST as_of recent 契约内容乙", domain, valid_at=1000.0)
    resp = c.post("/mem/recent", json={"domain": domain, "as_of": 500.0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
    resp2 = c.post("/mem/recent", json={"domain": domain, "as_of": 2000.0})
    assert len(resp2.json()["results"]) >= 1


def test_hybrid_search_route_accepts_as_of():
    """/mem/hybrid-search 接受 as_of 参数。"""
    c = _client()
    from mcp_tools import store

    domain = "rest_asof_hybrid"
    _mk(store, "REST as_of hybrid 契约内容丙", domain, valid_at=1000.0)
    resp = c.post(
        "/mem/hybrid-search",
        json={"query": "REST as_of hybrid 契约内容丙", "domain": domain, "tier": "", "as_of": 500.0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []


def test_search_as_of_matches_tool():
    """REST 与 MCP 工具对同一 as_of 返回同构结果（复用同一实现）。"""
    c = _client()
    from mcp_tools import store
    from mcp_tools.memory import mem_search as tool_fn

    domain = "rest_asof_parity"
    _mk(store, "REST 与 MCP as_of 同构内容丁", domain, valid_at=1000.0)
    via_rest = c.post(
        "/mem/search", json={"query": "REST 与 MCP as_of 同构内容丁", "domain": domain, "tier": "", "as_of": 3000.0}
    ).json()
    via_tool = json.loads(tool_fn("REST 与 MCP as_of 同构内容丁", domain=domain, tier="", as_of=3000.0))
    assert [it["id"] for it in via_rest["results"]] == [it["id"] for it in via_tool["results"]]
