"""CORS 契约测试：本机浏览器工具（file:// 页面 / 酒馆前端）能否直连 REST。

背景：本地 HTML 工具从 file:// 打开时，浏览器发出的 Origin 是字面量 "null"，
不会被视为与 127.0.0.1 同源。缺 CORS 中间件时浏览器直接拦掉请求（预检 405），
工具表现为「读不到数据」，而服务端日志里一切正常——很难排查。

本测试锁定三件事：
1. Origin "null" 的预检（OPTIONS）被放行，且回显 Access-Control-Allow-Origin；
2. 真实请求（POST /mem/recent）带 "null" 来源时也带 CORS 响应头；
3. 非白名单来源（外部站点）**不得**带 Access-Control-Allow-Origin（防敞开）。
"""

from fastapi.testclient import TestClient

ALLOWED_FILE_ORIGIN = "null"
ALLOWED_TAVERN_ORIGIN = "http://127.0.0.1:8000"
FOREIGN_ORIGIN = "http://evil.example.com"


def _client():
    from main import app

    return TestClient(app)


def test_preflight_allows_file_origin():
    """file:// 页面（Origin: null）的预检必须通过，否则浏览器不会发真实请求。"""
    c = _client()
    r = c.options(
        "/mem/recent",
        headers={
            "Origin": ALLOWED_FILE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == ALLOWED_FILE_ORIGIN


def test_preflight_allows_tavern_frontend_origin():
    """酒馆前端（:8000）同样在白名单内。"""
    c = _client()
    r = c.options(
        "/mem/recent",
        headers={
            "Origin": ALLOWED_TAVERN_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == ALLOWED_TAVERN_ORIGIN


def test_actual_request_from_file_origin_has_cors_header():
    """真实请求（非预检）也要回 CORS 头，浏览器才把响应交给页面 JS。"""
    c = _client()
    r = c.post(
        "/mem/recent",
        json={"domain": "cors_test_domain", "limit": 1},
        headers={"Origin": ALLOWED_FILE_ORIGIN},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == ALLOWED_FILE_ORIGIN


def test_foreign_origin_is_not_allowed():
    """外部站点不得获得 CORS 放行（本机库不能被任意网页读写）。"""
    c = _client()
    r = c.post(
        "/mem/recent",
        json={"domain": "cors_test_domain", "limit": 1},
        headers={"Origin": FOREIGN_ORIGIN},
    )
    assert r.headers.get("access-control-allow-origin") is None
