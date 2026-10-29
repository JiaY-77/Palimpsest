"""GET /health 的契约测试（docs/API.md 路由表 + Docker/compose HEALTHCHECK 的对端）。

覆盖四件事：
1. 恒 200 + 字段结构：status=ok / version 非空 / db ∈ {ok, degraded} /
   db_initialized 是布尔——存活探针不许因库忙被 5xx 判死，这是契约不是实现细节。
2. 免鉴权白名单：PALIMPSEST_API_KEY 设置时 /health 无 key 仍 200，
   业务端点（/tasks/active）无 key 必须 401，且 / 的白名单行为不变。
3. **未初始化 ≠ 异常**：全新实例（数据目录空）返回 db="ok" + db_initialized=false。
   早先的实现把「主库文件不存在」一律报 degraded，导致 `docker compose up` 之后
   健康检查长期显示 degraded（数据其实在 WAL 里、读写正常），把人引向不存在的故障。
4. 真异常才降级：数据目录不可写 / 不存在 → degraded。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import main


def test_health_returns_200_and_shape():
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"status", "version", "db", "db_initialized"}
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]
    assert body["db"] in ("ok", "degraded")
    assert isinstance(body["db_initialized"], bool)


def test_health_whitelisted_with_api_key(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "test-secret")
    client = TestClient(main.app)
    # 免鉴权白名单：/health 与 /
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    # 白名单之外：无 key 必须 401（中间件在端点之前拦截，/tasks/active 不会真开库）
    resp = client.get("/tasks/active")
    assert resp.status_code == 401


def test_health_fresh_instance_is_ok_not_degraded(monkeypatch, tmp_path):
    """空数据目录 = 全新实例，不是故障：活着且可写就报 ok。"""
    monkeypatch.setattr(main.Config, "DB_PATH", str(tmp_path / "not-created.db"))
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    assert body["db_initialized"] is False


def test_health_wal_only_store_counts_as_initialized(monkeypatch, tmp_path):
    """TriviumDB 在 flush 前写只在 WAL 里，主库文件尚不存在——这是正常状态。"""
    db = tmp_path / "mh_memory.db"
    (tmp_path / "mh_memory.db.wal").write_bytes(b"\x00" * 32)
    monkeypatch.setattr(main.Config, "DB_PATH", str(db))
    client = TestClient(main.app)
    body = client.get("/health").json()
    assert body["db"] == "ok"
    assert body["db_initialized"] is True


def test_health_reports_degraded_when_data_dir_missing(monkeypatch, tmp_path):
    """真异常（数据目录不存在、挂卷位置不对）才报 degraded。"""
    monkeypatch.setattr(main.Config, "DB_PATH", str(tmp_path / "missing-dir" / "mh_memory.db"))
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["db"] == "degraded"
