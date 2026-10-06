"""MCP-over-HTTP 路径规范化回归测试
================================

锁定一个真实踩过的坑：Starlette 的 ``Mount("/mcp", ...)`` 在收到**不带尾斜杠**
的 ``/mcp`` 时会返回 307 重定向到 ``/mcp/``；而多数 MCP 客户端（含 Hermes）
不跟随重定向，于是按 README/注释给出的接入方式
（``url = http://127.0.0.1:8090/mcp``）连接会直接失败（表现为 503）。

修复是在 ``main.app`` 上挂一个 ASGI 中间件 ``_NormalizeMcpPath``，在路由匹配
前把 ``/mcp`` 就地改写成 ``/mcp/``。

本测试断言：

1. 中间件存在且挂在 app 上（防止被误删/改回 ``app = wrapper`` 那种写法——
   替换 ``app`` 对象会让其后的 ``@app.exception_handler`` 装饰器静默失效）。
2. 用真实 ``main.app`` 起 TestClient：``POST /mcp`` 与 ``POST /mcp/`` 都返回
   200 且能拿到 MCP initialize 结果，而不是 307。
3. REST 端点未被破坏。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

_MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "regression-test", "version": "1.0"},
    },
}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _init_payload(text: str) -> dict:
    """从 SSE（``event: message\\ndata: {...}``）或纯 JSON 中解析 initialize 结果。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    return json.loads(text)


@pytest.fixture(scope="module")
def client():
    import main

    # base_url 必须用 127.0.0.1：FastMCP 的 transport_security 会拒绝未知 Host
    # （TestClient 默认 host "testserver" 会拿到 421 Misdirected Request）。
    with TestClient(main.app, base_url="http://127.0.0.1:8090") as c:
        yield c


def test_middleware_is_installed():
    """静态契约：``_NormalizeMcpPath`` 必须通过 add_middleware 挂载，且 app 未被替换。"""
    from fastapi import FastAPI

    import main

    assert isinstance(main.app, FastAPI), (
        "main.app 必须是 FastAPI 实例——若被换成 ASGI 包装器，"
        "其后的 @app.exception_handler / @app.get 等装饰器会静默失效"
    )

    assert hasattr(main, "_NormalizeMcpPath"), "缺少 _NormalizeMcpPath 中间件定义"

    # 中间件应出现在构建好的栈里（Starlette 的 user_middleware）
    stack = getattr(main.app, "user_middleware", [])
    names = [getattr(m, "cls", None) for m in stack]
    assert main._NormalizeMcpPath in names, (
        "_NormalizeMcpPath 未挂到 app.user_middleware——缺少它，/mcp（无尾斜杠）会 307，客户端连接失败"
    )


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_endpoint_reachable_with_and_without_trailing_slash(client, path):
    """两种写法都必须直达 MCP 子应用并成功 initialize（不得 307）。

    注意：``follow_redirects=False`` 是必须的——TestClient/httpx 默认跟随重定向，
    会把 307 自动跟到 ``/mcp/`` 从而「假过」；真实 MCP 客户端（含 Hermes）不跟随，
    307 就是连接失败。必须按客户端真实行为断言。
    """
    resp = client.post(
        path,
        json=_MCP_INIT_BODY,
        headers=_MCP_HEADERS,
        follow_redirects=False,
    )

    assert resp.status_code == 200, (
        f"POST {path} 返回 {resp.status_code}（307 = 未规范化；真实客户端不跟随重定向会连接失败）"
    )
    payload = _init_payload(resp.text)
    result = payload.get("result", {})
    assert result.get("serverInfo", {}).get("name"), f"POST {path} 未返回有效的 MCP initialize 结果：{payload}"


def test_rest_root_still_ok(client):
    """REST 主入口未被中间件破坏。"""
    resp = client.get("/")
    assert resp.status_code == 200
