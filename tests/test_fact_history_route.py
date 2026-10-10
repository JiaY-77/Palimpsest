"""REST 路由 /mem/fact-history 契约测试。

新增的 mem_fact_history 此前只有 MCP 工具，没有 REST 路由（违背三接口一核心）。
本测试锁定：路由存在、返回结构与 MCP 工具同构、节点不存在时 found=false。

隔离：用 conftest 的 TestClient 与临时库；只读查询，不写库。
建节点走 store 直插（独立于本测试关注的路由）。
"""

import json

from fastapi.testclient import TestClient


def _client():
    from main import app

    return TestClient(app)


def _mk_node(store, content, domain="facthist_test"):
    return store.insert_node(
        {"type": "memory", "content": content, "importance": 0.5, "domain": domain},
        store.embed_text(content),
    )


def test_fact_history_route_registered():
    """路由已注册：不在 404 之列（此前缺路由返回 404）。"""
    c = _client()
    from mcp_tools import store

    nid = _mk_node(store, "REST 路由契约：一条普通事实")
    resp = c.post("/mem/fact-history", json={"node_id": nid})
    assert resp.status_code == 200, resp.text


def test_fact_history_route_shape_matches_tool():
    """REST 返回结构与 MCP 工具同构（复用同一实现，不复制逻辑）。"""
    c = _client()
    from mcp_tools import store
    from mcp_tools.memory import mem_fact_history as tool_fn

    nid = _mk_node(store, "REST 与 MCP 同构：另一条事实")
    via_rest = c.post("/mem/fact-history", json={"node_id": nid}).json()
    via_tool = json.loads(tool_fn(nid))
    assert via_rest == via_tool


def test_fact_history_route_missing_node():
    """节点不存在：found=false（与工具一致，不抛 500）。"""
    c = _client()
    resp = c.post("/mem/fact-history", json={"node_id": 999999999})
    assert resp.status_code == 200
    assert resp.json()["found"] is False


def test_fact_history_in_root_endpoint_index():
    """/ 的端点索引应列出新路由（readme_check 的「端点索引」一节据此核对）。"""
    c = _client()
    eps = c.get("/").json()["endpoints"]
    assert "/mem/fact-history" in eps
