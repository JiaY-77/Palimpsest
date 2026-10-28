"""dashboard 客户端化测试
========================

验证 dashboard 已成为**纯客户端**：

1. 模块**不再直接打开数据库**（无 ``TriviumStore()`` 实例化）
2. ``/api/*`` 代理到 REST（用 monkeypatch 替换 httpx，不真连网）
3. 对外响应结构与前端 ``dashboard.html`` 期望一致
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

# ---- 静态检查：不得自己开库 ----


def test_dashboard_does_not_open_database_directly():
    """dashboard 源码**代码部分**不得实例化 TriviumStore/TriviumDB。

    用 AST 剥离 docstring 与注释后再检查——文档里完全可以（也应该）提到
    「历史上自己 TriviumStore() 开库」这个事实，那不是违规。
    """
    import ast

    import scripts.dashboard as dash_mod

    tree = ast.parse(inspect.getsource(dash_mod))
    # 抹掉所有 docstring
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    code_only = ast.unparse(tree)

    assert "TriviumStore" not in code_only, "dashboard 代码不得引用 TriviumStore——它是客户端，必须走 REST"
    assert "TriviumDB" not in code_only, "dashboard 代码不得引用 TriviumDB"

    # 也不得 import core 里的存储/索引模块
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = [a.name for a in node.names]
            assert "core.trivium_store" not in mod, "dashboard 不得 import core.trivium_store"
            assert "core" not in [n.split(".")[0] for n in names], f"dashboard 不得直接 import core 模块：{names}"


def test_dashboard_has_no_module_level_store_instance():
    """dashboard 模块级不得残留常驻 store 对象。"""
    import scripts.dashboard as dash_mod

    # 模块里不应有 TriviumStore 类型的东西
    names = [n for n in dir(dash_mod) if not n.startswith("__")]
    leaked = [n for n in names if n.lower() in ("store", "_store")]
    assert not leaked, f"dashboard 模块级残留 store 对象：{leaked}"


# ---- 行为检查：代理到 REST ----


@pytest.fixture()
def client():
    import scripts.dashboard as dash_mod

    return TestClient(dash_mod.app)


def _fake_response(payload, status=200):
    class _Resp:
        status_code = status
        content = b"x" if payload is not None else b""

        def json(self):
            return payload

        @property
        def text(self):
            return str(payload)

    return _Resp()


def test_mem_stats_proxies_rest(client, monkeypatch):
    """统计接口代理 /mem/stats 并映射真实响应结构。"""
    import scripts.dashboard as dash_mod

    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        return _fake_response(
            {
                "totals": {
                    "total_nodes": 1169,
                    "outdated": 3,
                    "by_type": {"memory": 118, "record": 287},
                }
            }
        )

    monkeypatch.setattr(dash_mod.httpx, "request", fake_request)

    r = client.get("/api/mem/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1169
    assert body["by_type"] == {"memory": 118, "record": 287}
    assert body["outdated"] == 3
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/mem/stats")


def test_mem_recent_proxies_export(client, monkeypatch):
    """最近节点接口代理 /export 并倒序取前 limit 个。"""
    import scripts.dashboard as dash_mod

    def fake_request(method, url, **kwargs):
        assert kwargs.get("params", {}).get("page") == 1
        return _fake_response(
            {
                "memories": [
                    {"id": 10, "payload": {"type": "memory", "content": "a", "status": "active"}},
                    {"id": 30, "payload": {"type": "plan", "content": "b", "status": "active"}},
                    {"id": 20, "payload": {"type": "record", "content": "c", "status": "active"}},
                ]
            }
        )

    monkeypatch.setattr(dash_mod.httpx, "request", fake_request)

    r = client.get("/api/mem/recent?limit=2")
    assert r.status_code == 200
    nodes = r.json()["nodes"]
    assert [n["id"] for n in nodes] == [30, 20], "应按 id 倒序取前 limit 个"


def test_mem_search_proxies_rest(client, monkeypatch):
    """搜索接口代理 /mem/search。"""
    import scripts.dashboard as dash_mod

    def fake_request(method, url, **kwargs):
        assert method == "POST"
        assert kwargs["json"]["query"] == "测试"
        return _fake_response({"results": [{"id": 1, "score": 0.9}]})

    monkeypatch.setattr(dash_mod.httpx, "request", fake_request)

    r = client.get("/api/mem/search?q=测试")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_mem_search_empty_query_short_circuits(client):
    """空查询不发请求，直接返回空。"""
    r = client.get("/api/mem/search?q=")
    assert r.status_code == 200
    assert r.json() == {"query": "", "results": [], "total": 0}


def test_consolidate_preview_works_apply_is_501(monkeypatch):
    """合并**预览**走 REST 的 dry-run 端点（真实候选）；**写操作**恒 501。

    2026-10-28 起：REST 早有 `POST /mem/consolidate`（写死 dry_run=True），原先
    连预览都返回 501 是过时的保守写法；面板定位仍是只读观察窗，所以 apply 保持
    501，需要真实合并请用 CLI（可审计、可先看候选）。

    ⚠️ 这里必须 mock `_rest`：不 mock 的话本机恰好起着 8090 就绿、CI 上必然红
    （`Connection refused` → 502），测试就成了环境的镜子而不是契约。
    """
    import scripts.dashboard as dash_mod

    seen: list[tuple[str, str]] = []

    def fake_rest(method: str, path: str, **kwargs):
        seen.append((method, path))
        return {"candidates": [], "pairs_considered": 0}

    monkeypatch.setattr(dash_mod, "_rest", fake_rest)
    c = TestClient(dash_mod.app)

    preview = c.get("/api/consolidate/preview")
    assert preview.status_code == 200, preview.text
    assert ("POST", "/mem/consolidate") in seen

    apply_resp = c.post("/api/consolidate/apply")
    assert apply_resp.status_code == 501
    assert "CLI" in apply_resp.json()["detail"]


def test_rest_unreachable_returns_502(client, monkeypatch):
    """REST 不可达时返回 502 并说明——不假装成功。"""
    import scripts.dashboard as dash_mod

    def boom(*a, **k):
        raise dash_mod.httpx.ConnectError("connection refused")

    monkeypatch.setattr(dash_mod.httpx, "request", boom)

    r = client.get("/api/mem/stats")
    assert r.status_code == 502
    assert "不可达" in r.json()["detail"]
