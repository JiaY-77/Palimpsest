"""治理 REST 端点测试
====================

锁定两个只读治理端点的契约：

1. ``POST /mem/consolidate`` —— 只读 dry-run 预览：返回 200 且响应体带
   ``dry_run: true``；调用前后节点数不变（端点不接受 apply，不写库）。
2. ``GET /doctor`` —— 只读部署体检：返回 200，响应体为 dict 且含 ``checks``
   键（体检结果依赖运行环境，不断言 ``ok``）。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库；
直接用 ``fastapi.testclient.TestClient``，不起服务、不连网。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _total_nodes(client: TestClient) -> int:
    """经 POST /mem/stats 取当前节点总数（是 POST 不是 GET）。

    用下标取值而非 .get()——键缺失时应立刻暴露，不静默吞成 None。
    """
    resp = client.post("/mem/stats")
    assert resp.status_code == 200, resp.text
    return resp.json()["totals"]["total_nodes"]


def test_mem_consolidate_returns_dry_run(client):
    """POST /mem/consolidate 返回 200，响应体含 dry_run: true（只读预览）。"""
    resp = client.post("/mem/consolidate", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, dict), data
    assert data["dry_run"] is True, data


def test_mem_consolidate_does_not_change_node_count(client):
    """调用前后库的节点数不变——dry-run 不写库。"""
    before = _total_nodes(client)
    resp = client.post("/mem/consolidate", json={})
    assert resp.status_code == 200, resp.text
    after = _total_nodes(client)
    assert after == before, f"节点数变化：{before} -> {after}"


def test_doctor_returns_checks(client):
    """GET /doctor 返回 200，响应体是 dict 且含 checks 键。

    不断言 ok is True——体检结果依赖运行环境，只锁结构。
    """
    resp = client.get("/doctor")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, dict), data
    assert "checks" in data, data
