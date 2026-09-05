# -*- coding: utf-8 -*-
"""
安全加固回归测试（v1.1.0 发布后）
=============================
覆盖：
  A. 可选 API Key 鉴权（未配置放行 / 配置后限流 / 错误 key 401 / 根路径始终放行）
  B1. /export 分页（page/page_size/total_pages，page_size 上限 500）
  B2. GET /memory/{node_id} 剥掉内部字段（secret_hint 等）
  B3. 删除/更新/向量错误固定提示（不泄漏 str(exc)）

隔离保证：conftest 已把 DB_PATH 指向临时库，全部测试不触碰正式库。
注意：main 在 import 时读取 PALIMPSEST_API_KEY，因此「未启用」态须在
commit 前确保未设置该环境变量（本测试通过 monkeypatch 覆盖 Config.API_KEY）。
"""

import json

from fastapi.testclient import TestClient


def _get(result: str) -> dict:
    return json.loads(result)


# ---------------------------------------------------------------------------
# A. 可选 API Key 鉴权 —— 未设置时放行
# ---------------------------------------------------------------------------
def test_auth_disabled_allows_all(monkeypatch):
    """不设 PALIMPSEST_API_KEY：所有请求放行（含 / /memory /export 等）。"""
    monkeypatch.setenv("PALIMPSEST_API_KEY", "")

    import main as main_mod
    from config import Config

    monkeypatch.setattr(main_mod, "API_KEY", "")
    monkeypatch.setattr(Config, "API_KEY", "")

    from main import app

    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.post("/mem/search", json={"query": "x"}).status_code in (200, 422)
    assert client.get("/export").status_code == 200


# ---------------------------------------------------------------------------
# A. 可选 API Key 鉴权 —— 设置后校验
# ---------------------------------------------------------------------------
def test_auth_enabled_requires_key(monkeypatch):
    """设 PALIMPSEST_API_KEY=secret123：
    无 header → 401；Bearer secret123 → 200；错 key → 401；/ 始终 200。"""
    monkeypatch.setenv("PALIMPSEST_API_KEY", "secret123")

    import main as main_mod
    from config import Config

    monkeypatch.setattr(main_mod, "API_KEY", "secret123")
    monkeypatch.setattr(Config, "API_KEY", "secret123")

    from main import app

    client = TestClient(app)

    # 根健康检查始终放行
    assert client.get("/").status_code == 200

    # 无 header → 401
    resp = client.get("/export")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "未授权：缺少或无效的 API Key"

    # Authorization: Bearer 正确 → 200
    assert client.get(
        "/export", headers={"Authorization": "Bearer secret123"}
    ).status_code == 200

    # X-API-Key 正确 → 200
    assert client.get(
        "/export", headers={"X-API-Key": "secret123"}
    ).status_code == 200

    # 错误 key → 401
    resp = client.get("/export", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401, resp.text


def test_auth_enabled_bearer_wrong_scheme(monkeypatch):
    """Authorization 非 Bearer（如 Basic）应视为缺失 key → 401。"""
    monkeypatch.setenv("PALIMPSEST_API_KEY", "secret123")

    import main as main_mod
    from config import Config

    monkeypatch.setattr(main_mod, "API_KEY", "secret123")
    monkeypatch.setattr(Config, "API_KEY", "secret123")

    from main import app

    client = TestClient(app)
    resp = client.get("/export", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# B1. /export 分页
# ---------------------------------------------------------------------------
def test_export_pagination(db_path):
    """造 5 条数据，page_size=2 → page1 回 2 条 + total_pages 正确；越界页空列表。"""
    from core.fts_index import sync_node
    from main import app
    from mcp_tools import store

    ids = []
    for i in range(5):
        content = f"分页护栏记忆编号{i}：用于验证导出分页"
        emb = store.embed_text(content)
        nid = store.insert_node(
            {"type": "memory", "content": content, "importance": 0.5},
            emb,
        )
        sync_node(nid, content)
        ids.append(nid)

    client = TestClient(app)

    # 不传参数 → 默认 page=1, page_size=100
    r1 = client.get("/export")
    assert r1.status_code == 200, r1.text
    data1 = r1.json()
    assert data1["page"] == 1
    assert data1["page_size"] == 100
    assert data1["total_pages"] == 1
    assert len(data1["memories"]) == data1["total_nodes"]

    # page_size=2 → page1 回 2 条，total_pages = ceil(total_nodes/2)
    r2 = client.get("/export", params={"page": 1, "page_size": 2})
    data2 = r2.json()
    total = data2["total_nodes"]
    expected_pages = (total + 1) // 2
    assert data2["page"] == 1
    assert data2["page_size"] == 2
    assert data2["total_pages"] == expected_pages
    assert len(data2["memories"]) == 2

    # 最后一页剩余条数正确（total 可能含其他测试节点，按公式计算）
    r3 = client.get("/export", params={"page": expected_pages, "page_size": 2})
    data3 = r3.json()
    expect_last_page = total - (expected_pages - 1) * 2
    assert len(data3["memories"]) == expect_last_page
    assert data3["total_pages"] == expected_pages

    # 越界页 → 空列表但 total_pages 不变
    r4 = client.get("/export", params={"page": expected_pages + 99, "page_size": 2})
    data4 = r4.json()
    assert data4["memories"] == []
    assert data4["total_pages"] == expected_pages


def test_export_page_size_capped(db_path):
    """page_size 超上限 500 → 被截断为 500。"""
    from main import app

    client = TestClient(app)
    r = client.get("/export", params={"page_size": 5000})
    assert r.status_code == 200, r.text
    assert r.json()["page_size"] == 500


# ---------------------------------------------------------------------------
# B2. GET /memory/{node_id} 剥内部字段
# ---------------------------------------------------------------------------
def test_get_memory_strips_internal_fields(db_path):
    """GET /memory/{nid} 返回 payload 不含 secret_hint / linked_from / linked_kb_ids / superseded，
    但保留 content/type/importance/domain 等正常字段。"""
    from core.fts_index import sync_node
    from main import app
    from mcp_tools import store

    content = "剥内字段护栏：含内部标记的记忆内容"
    emb = store.embed_text(content)
    nid = store.insert_node(
        {"type": "memory", "content": content,
         "importance": 0.6, "domain": "hero"},
        emb,
    )
    sync_node(nid, content)
    store.update_payload(nid, {
        "secret_hint": ["phone"],
        "linked_from": [1, 2],
        "linked_kb_ids": [3],
        "superseded": True,
    })

    client = TestClient(app)
    resp = client.get(f"/memory/{nid}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["payload"]

    # 内部字段被剥掉
    for k in ("secret_hint", "linked_from", "linked_kb_ids", "superseded"):
        assert k not in payload, f"内部字段 {k} 不应出现在响应: {payload}"

    # 正常字段保留
    assert payload["content"] == "剥内字段护栏：含内部标记的记忆内容"
    assert payload["type"] == "memory"
    assert payload["importance"] == 0.6
    assert payload["domain"] == "hero"


# ---------------------------------------------------------------------------
# B3. 错误固定提示（不泄漏 str(exc)）
# ---------------------------------------------------------------------------
def test_delete_memory_fixed_error(db_path):
    """删除不存在的节点 → 404 固定提示，不泄漏内部异常。"""
    from main import app

    client = TestClient(app)
    resp = client.delete("/memory/999_999_999")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "删除失败：节点不存在或已被删除"


def test_update_memory_fixed_error(db_path):
    """更新不存在的节点 → 404 固定提示，不泄漏内部异常。"""
    from main import app

    client = TestClient(app)
    for method in ("put", "patch"):
        resp = getattr(client, method)("/memory/999_999_999", json={"content": "x"})
        assert resp.status_code == 404, method
        assert resp.json()["detail"] == "更新失败：节点不存在或数据格式错误"


def test_vector_update_fixed_error(db_path):
    """更新不存在节点的向量 → 404 固定提示（不泄漏异常）。"""
    from main import app
    from mcp_tools import store

    dummy_vec = [0.0] * store.dim
    client = TestClient(app)
    resp = client.patch("/memory/999_999_999/vector", json=dummy_vec)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "向量更新失败：节点不存在或维度不匹配"
