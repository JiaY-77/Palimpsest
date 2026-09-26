"""CLI REST 读路径测试
====================

锁定「CLI 读命令不再自己开库」这一契约：

1. **静态**：读命令的源码里不得再出现 ``TriviumStore(`` / 直接调用
   ``mem_search`` 等 mcp_tools 函数（那些函数内部用共享 store，会在本进程开库、
   与常驻 REST 争抢同一份库文件）。
2. **代理**：读命令经 ``_rest_call`` 发 HTTP，用 monkeypatch 替换 httpx，
   断言请求路径与参数正确——不真连网，也不需要起服务。
3. **POST /mem/recent**：新增端点返回结构与 mcp_tools.mem_recent 一致。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

# ---- 1. 静态检查：读命令不得自己开库 ----


def _cli_source() -> str:
    import scripts.palimpsest_cli as cli_mod

    return inspect.getsource(cli_mod)


def _code_only(source: str) -> str:
    """剥掉 docstring 与注释，只留代码——文档里可以（也应该）解释历史做法。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


# 已迁移到 REST 的读/写命令
_MIGRATED = ["cmd_search", "cmd_hybrid_search", "cmd_recent",
             "cmd_graph", "cmd_kb", "cmd_stats", "cmd_ingest", "cmd_link"]


def test_migrated_commands_use_rest():
    """迁移后的命令必须调用 _rest_call，且不得出现 TriviumStore()。"""
    source = _cli_source()
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    for name in _MIGRATED:
        # cmd_stats 尚未迁移（需服务端端点），跳过
        if name == "cmd_stats":
            continue
        assert name in funcs, f"{name} 不存在"
        body_src = ast.unparse(funcs[name])
        assert "_rest_call" in body_src, f"{name} 未走 REST"
        assert "TriviumStore(" not in body_src, f"{name} 仍在直接开库"


def test_cli_module_code_has_no_new_triviumstore_in_migrated_paths():
    """全模块代码里，TriviumStore() 只应出现在尚未迁移的命令中。"""
    code = _code_only(_cli_source())
    tree = ast.parse(code)
    holders = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            fn = ast.unparse(node)
            if "TriviumStore(" in fn:
                holders.append(node.name)
    # 未迁移命令（consolidate / review / promote / doctor 等）仍可持有；
    # 迁移过的读命令不得持有
    for name in _MIGRATED:
        if name == "cmd_stats":
            continue
        assert name not in holders, f"{name} 不应再持有 TriviumStore()"


def test_cli_does_not_import_mcp_tools_at_module_level():
    """mcp_tools 不得在模块级 import（含顶层 try 块内）。

    ``mcp_tools._common`` 在模块级执行 ``store = TriviumStore()``——仅 import
    就会初始化库（实测会创建 data/ 目录）。CLI 的读命令若仍触发它，就等于
    在本进程开库，与常驻 REST 争抢同一份库文件。因此这类符号必须惰性 import。
    """
    source = _cli_source()
    tree = ast.parse(source)

    # 收集所有函数体的行号范围；函数体内的 import 属惰性，允许
    fn_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_ranges.append((node.lineno, node.end_lineno or node.lineno))

    def _inside_function(lineno: int) -> bool:
        return any(lo <= lineno <= hi for lo, hi in fn_ranges)

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mcp_tools"):
            if not _inside_function(node.lineno):
                offenders.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mcp_tools") and not _inside_function(node.lineno):
                    offenders.append((node.lineno, alias.name))

    assert not offenders, (
        f"模块级 import 了 mcp_tools（会初始化库）：{offenders}；"
        "应改为命令函数内惰性 import"
    )


# ---- 2. 代理层：_rest_call 请求形状 ----


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode()

    def json(self):
        return json.loads(self.text)


class _FakeClient:
    """记录请求，返回固定响应。"""

    calls: list = []  # noqa: RUF012 —— 测试夹具，显式类属性以跨实例共享记录

    def __init__(self, *a, **kw):
        pass

    def request(self, method, path, json=None):
        _FakeClient.calls.append({"method": method, "path": path, "json": json})
        return _FakeResponse('{"results": []}')

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


def test_rest_call_returns_body(fake_httpx):
    from scripts.palimpsest_cli import _rest_call

    out = _rest_call("POST", "/mem/search", {"query": "x"})
    assert json.loads(out) == {"results": []}
    assert fake_httpx.calls[0]["path"] == "/mem/search"


def test_rest_call_unreachable_returns_json_error(monkeypatch):
    """REST 不可达时返回可读 JSON 错误，而不是抛栈。"""
    import httpx

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def request(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "Client", _Boom)
    from scripts.palimpsest_cli import _rest_call

    out = json.loads(_rest_call("POST", "/mem/search", {}))
    assert "error" in out
    assert "action" in out  # 给出下一步（起服务）


def test_cmd_search_sends_expected_body(fake_httpx):
    from scripts.palimpsest_cli import cmd_search

    class A:
        query, scope, domain = "hello", "all", ""
        top_k, neighbors, block, tier = 3, False, "", "facts"

    cmd_search(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/search"
    assert call["json"]["query"] == "hello"
    assert call["json"]["top_k"] == 3


def test_cmd_recent_sends_expected_body(fake_httpx):
    from scripts.palimpsest_cli import cmd_recent

    class A:
        domain, limit = "", 5

    cmd_recent(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/recent"
    assert call["json"] == {"domain": "", "limit": 5}


def test_cmd_kb_scopes_to_kb(fake_httpx):
    from scripts.palimpsest_cli import cmd_kb

    class A:
        query, top_k = "小帕", 2

    cmd_kb(A())
    call = fake_httpx.calls[0]
    assert call["path"] == "/mem/search"
    assert call["json"]["scope"] == "kb"


# ---- 3. POST /mem/recent 端点 ----


def test_mem_recent_endpoint_registered():
    """main.py 的路由表含 /mem/recent。"""
    from main import app

    paths = {r.path for r in app.routes}
    assert "/mem/recent" in paths


def test_mem_recent_endpoint_returns_results(db_path):
    """端点返回结构含 results/total，与 mcp_tools.mem_recent 一致。"""
    from fastapi.testclient import TestClient

    from main import app
    from mcp_tools import store

    # 写一条可辨识的记忆
    emb = store.embed_text("CLI REST 读路径测试：最近记忆端点")
    store.insert_node({"type": "memory", "content": "CLI REST 读路径测试：最近记忆端点",
                       "importance": 0.5, "domain": "clitest"}, emb)

    client = TestClient(app)
    resp = client.post("/mem/recent", json={"domain": "clitest", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "total" in data
    assert any("最近记忆端点" in r.get("content", "") for r in data["results"])
