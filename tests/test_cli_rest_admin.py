"""CLI admin/batch 命令走 REST 的契约测试（issue #168）
===================================================

1. **静态**：九个迁移命令的源码只允许经 ``_rest_call``，不得出现
   ``TriviumStore(``；整个 CLI 模块代码（剥注释/docstring 后）也为零。
2. **代理**： Monkeypatch httpx 断言请求路径与 body（含 dry-run → apply=false）。
3. **端点**：TestClient 打九个新/扩展端点，验证 dry-run 不改库、
   apply 才写，返回形状正确。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json

import pytest

# ---- 1. 静态检查：九个命令不再开库 ----

_ADMIN_COMMANDS = [
    "cmd_tasks_backfill",
    "cmd_review",
    "cmd_consolidate",
    "cmd_promote",
    "cmd_ingest_git",
    "cmd_fts_rebuild",
    "cmd_fts_search",
    "cmd_task_archive",
    "cmd_reindex",
]


def _cli_funcs() -> dict:
    import scripts.palimpsest_cli as cli_mod

    source = inspect.getsource(cli_mod)
    tree = ast.parse(source)
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_admin_commands_use_rest_only():
    """九个命令必须经 _rest_call，且不得出现 TriviumStore(。"""
    funcs = _cli_funcs()
    for name in _ADMIN_COMMANDS:
        assert name in funcs, f"{name} 不存在"
        src = ast.unparse(funcs[name])
        assert "_rest_call" in src, f"{name} 未走 REST"
        assert "TriviumStore(" not in src, f"{name} 仍在直接开库"


def test_cli_module_code_has_zero_triviumstore():
    """全 CLI 模块（剥掉 docstring，含全部命令函数）TriviumStore( 计数为 0。"""
    import scripts.palimpsest_cli as cli_mod

    tree = ast.parse(inspect.getsource(cli_mod))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    code = ast.unparse(tree)
    assert "TriviumStore(" not in code, "CLI 代码仍出现直接开库构造"
    code_body = []
    for node in ast.parse(code).body:
        if isinstance(node, ast.FunctionDef):
            code_body.append(node.name)
            assert "TriviumStore(" not in ast.unparse(node), f"{node.name} 直接开库"


# ---- 2. 代理层：请求形状 ----


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode()

    def json(self):
        return json.loads(self.text)


class _FakeClient:
    calls: list = []  # noqa: RUF012

    def __init__(self, *a, **kw):
        self._kw = kw

    def request(self, method, path, json=None):
        _FakeClient.calls.append({"method": method, "path": path, "json": json, "kw": self._kw})
        return _FakeResponse('{"ok": true}')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.calls = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient


def test_cmd_tasks_backfill_dry_run_sends_apply_false(fake_httpx):
    from scripts.palimpsest_cli import cmd_tasks_backfill

    class A:
        apply = False

    cmd_tasks_backfill(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/tasks/backfill"
    assert call["json"] == {"apply": False}


def test_cmd_consolidate_apply_sends_apply_true(fake_httpx):
    from scripts.palimpsest_cli import cmd_consolidate

    class A:
        apply = True
        threshold = 0.9
        max_importance = 0.7

    cmd_consolidate(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/consolidate"
    assert call["json"]["apply"] is True
    assert call["json"]["threshold"] == 0.9


def test_cmd_fts_search_reads_node_via_rest(monkeypatch, capsys):
    """fts-search 的 payload 补查走 GET /memory/{id}，不再本地开库。"""
    import scripts.palimpsest_cli as cli

    calls = []

    def fake_call(method, path, body=None, timeout=None):
        calls.append((method, path))
        if path == "/fts/search":
            return json.dumps({"results": [{"node_id": 7, "content": "命中全文"}]})
        return json.dumps({"payload": {"content": "x"}})

    monkeypatch.setattr(cli, "_rest_call", fake_call)
    cli.cmd_fts_search(type("A", (), {"query": "关键词", "limit": 5})())
    out = capsys.readouterr().out
    assert calls[-1] == ("GET", "/memory/7"), calls
    assert "node_id=7" in out
    assert "命中全文" in out


def test_cmd_promote_sends_params(fake_httpx):
    from scripts.palimpsest_cli import cmd_promote

    class A:
        apply = False
        days = 14
        min_hits = 3

    cmd_promote(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/promote"
    assert call["json"] == {"apply": False, "days": 14, "min_hits": 3}


def test_cmd_ingest_git_sends_repo_and_since(fake_httpx):
    from scripts.palimpsest_cli import cmd_ingest_git

    class A:
        repo = ""
        since = 3

    cmd_ingest_git(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/ingest-git"
    assert call["json"]["since"] == 3


def test_cmd_reindex_uses_long_timeout(fake_httpx):
    from scripts.palimpsest_cli import _REINDEX_TIMEOUT, cmd_reindex

    class A:
        check = False
        dry_run = False
        restart = False
        force = False
        resume = True
        only = "memory"
        skip = ""
        batch = 32
        yes = True

    with contextlib.suppress(SystemExit):
        cmd_reindex(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/reindex"
    assert call["json"]["dry_run"] is False
    assert call["kw"]["timeout"] == _REINDEX_TIMEOUT


# ---- 3. 端点行为（TestClient + conftest 临时库）----


def _client():
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app)


@pytest.fixture
def admin_client():
    """端点级测试的隔离夹具：临时库 + 重建服务端单例。

    与 test_reembed_and_readonly._isolated_store 同款隔离：换 Config.DB_PATH、
    重置连接注册表、把 main._store 置 None 重开——不读 conftest 的共享 session
    库，也不往共享库里写节点（往共享 session 库插入会污染其后跑的
    test_mem_communities 等全库图断言，issue 期间实测踩过）。
    """
    import os as _os
    import shutil
    import tempfile

    from fastapi.testclient import TestClient

    import core.trivium_store as ts_mod
    from config import Config

    tmp = tempfile.mkdtemp(prefix="palimpsest_admin_rest_")
    old = Config.DB_PATH
    Config.DB_PATH = _os.path.join(tmp, "admin.db")
    ts_mod.reset_connections_for_test()
    import main

    main._store = None
    try:
        yield TestClient(main.app)
    finally:
        main._store = None
        ts_mod.reset_connections_for_test()
        Config.DB_PATH = old
        shutil.rmtree(tmp, ignore_errors=True)


def _server_store():
    from main import _get_store

    return _get_store()


def test_new_admin_routes_registered():
    from main import app

    paths = {r.path for r in app.routes}
    for p in (
        "/tasks/backfill",
        "/tasks/archive",
        "/mem/review",
        "/mem/promote",
        "/mem/ingest-git",
        "/fts/rebuild",
        "/fts/search",
        "/mem/reindex",
    ):
        assert p in paths, f"{p} 未注册"


def test_all_new_endpoints_in_root_index():
    """root() 端点索引必须与实际路由一致（readme_check #4 检查项）。"""

    # 注意：不要用 `with TestClient(app)`（会启动 app lifespan / MCP session
    # manager，只能进一次，进了会把 test_mcp_path_normalization 顶爆）。
    client = _client()
    listed = set(client.get("/").json()["endpoints"])
    for p in ("/tasks/backfill", "/fts/search", "/mem/reindex", "/fts/rebuild"):
        assert p in listed


def test_dry_run_endpoints_do_not_mutate(admin_client):
    """dry-run：backfill / archive / consolidate / promote 不写库。"""
    client = admin_client
    store = _server_store()

    emb = store.embed_text("dry-run 契约测试节点")
    nid = store.insert_node({"type": "task", "content": "fix(demo): 样例任务", "importance": 0.5}, emb)

    for test_case in (
        ("/tasks/backfill", {"apply": False}),
        ("/tasks/archive", {"apply": False}),
        ("/mem/consolidate", {"apply": False}),
        ("/mem/promote", {"apply": False}),
    ):
        resp = client.post(test_case[0], json=test_case[1])
        assert resp.status_code == 200, f"{test_case[0]} 失败：{resp.text[:300]}"
        assert resp.json().get("dry_run") is True, f"{test_case[0]} 应返回 dry_run=True"

    node = store.get_node(nid)
    assert node is not None
    assert node["payload"].get("task_state") is None, "backfill dry-run 不应写 task_state"


def test_consolidate_apply_merges(admin_client):
    """apply=true 才真正写库（合并路径复用 core.consolidator）。"""
    client = admin_client
    store = _server_store()

    # 一高一低 importance（0.3 / 0.5）且均 < max_importance(0.9)：确保走合并路径
    base = "consolidate 回归测试：一条用于验证 REST 合并路径的记忆内容"
    emb_a = store.embed_text(base)
    emb_b = store.embed_text(base + "（副本乙）")
    a = store.insert_node({"type": "memory", "content": base, "importance": 0.3}, emb_a)
    b = store.insert_node({"type": "memory", "content": base + "（副本乙）", "importance": 0.5}, emb_b)
    resp = client.post("/mem/consolidate", json={"apply": True, "threshold": 0.70, "max_importance": 0.9})
    assert resp.status_code == 200, resp.text[:300]
    merged_ids = [m for m in resp.json().get("merged_ids", []) if {m.get("old_a"), m.get("old_b")} == {a, b}]
    assert merged_ids, f"apply 合并应发生：{resp.json()}"
    assert store.get_node(a)["payload"]["status"] == "outdated"
    assert store.get_node(b)["payload"]["status"] == "outdated"


def test_fts_rebuild_and_search_endpoint(admin_client):
    client = admin_client
    store = _server_store()

    emb = store.embed_text("FTS 专属检索串：琥珀色算盘")
    store.insert_node({"type": "memory", "content": "琥珀色算盘检索串", "importance": 0.5}, emb)
    resp = client.post("/fts/rebuild")
    assert resp.status_code == 200
    assert resp.json()["indexed"] >= 1
    resp = client.post("/fts/search", json={"query": "琥珀色算盘", "limit": 5})
    assert resp.status_code == 200
    assert any(r.get("content", "") for r in resp.json()["results"])


def test_review_endpoint_counts_decisions(admin_client):
    client = admin_client
    store = _server_store()

    emb = store.embed_text("decision 复盘盘点测试")
    store.insert_node({"type": "decision", "content": "选择方案 A", "importance": 0.6}, emb)
    resp = client.post("/mem/review", json={"days": 7})
    assert resp.status_code == 200
    assert resp.json()["stats"]["decision"] >= 1


def test_reindex_endpoint_check_mode_captures_output(admin_client):
    client = admin_client
    resp = client.post("/mem/reindex", json={"check": True})
    assert resp.status_code == 200
    data = resp.json()
    assert "exit_code" in data and isinstance(data["output"], str)
