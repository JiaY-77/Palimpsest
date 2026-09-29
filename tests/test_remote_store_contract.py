"""RemoteStore 契约测试（不对着真服务）
========================================

RemoteStore 是 ``protocols.Store`` 的 HTTP 实现，它的每一处偏离都是**静默**的
（调用方拿到错结果却不报错）。这里用记录型假的 ``httpx.Client`` 把 HTTP 契约
逐条钉死：方法、路径、body 形状、错误分类。

不真连网、不需要起 REST 服务。
"""

from __future__ import annotations

import json

import httpx
import pytest

from client.remote_store import RemoteStore, RemoteStoreError


class _Resp:
    def __init__(self, status_code: int = 200, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.content = b"1" if payload is not None else b""

    def json(self):
        return self._payload


@pytest.fixture
def http(monkeypatch):
    """记录所有出站请求；按 (method, path) 取预置响应。"""
    state: dict = {"responses": {}, "calls": []}

    class _Client:
        def __init__(self, *args, **kwargs):
            self.base_url = kwargs.get("base_url", "")

        def request(self, method, path, **kwargs):
            state["calls"].append((method, path, kwargs))
            resp = state["responses"].get((method, path))
            assert resp is not None, f"未登记的请求：{method} {path}"
            return resp

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    return state


@pytest.fixture
def rs(http):
    store = RemoteStore(base_url="http://127.0.0.1:9", timeout=1.0)
    yield store
    store.close()


# ---- update_vector：PATCH + 顶层 list body ----

def test_update_vector_uses_patch_with_top_level_list(http, rs):
    http["responses"][("PATCH", "/memory/7/vector")] = _Resp(204)

    rs.update_vector(7, [0.1, 0.2, 0.3])

    method, path, kwargs = http["calls"][0]
    assert (method, path) == ("PATCH", "/memory/7/vector"), http["calls"][0]
    assert kwargs["json"] == [0.1, 0.2, 0.3], (
        f"body 必须是顶层向量数组（服务端形参是 list[float]）：{kwargs['json']}"
    )


# ---- recent_ids：走真正的 recent 端点并保留响应顺序 ----

def test_recent_ids_uses_recent_endpoint(http, rs):
    http["responses"][("POST", "/mem/recent")] = _Resp(
        200, {"results": [{"id": 9}, {"id": 5}, {"id": 7}], "total": 3}
    )

    ids = rs.recent_ids(limit=3)

    method, path, kwargs = http["calls"][0]
    assert (method, path) == ("POST", "/mem/recent"), http["calls"][0]
    assert kwargs["json"] == {"limit": 3}, kwargs
    assert ids == [9, 5, 7], f"必须保留服务端返回的「最近」顺序：{ids}"


def test_recent_ids_does_not_use_export(http, rs):
    """回归：/export 按 importance 降序，是「最重要前 N」而非「最近 N」。"""
    http["responses"][("POST", "/mem/recent")] = _Resp(200, {"results": []})

    rs.recent_ids(limit=5)

    paths = [path for _m, path, _k in http["calls"]]
    assert "/export" not in paths, f"recent_ids 不得走 /export：{paths}"


# ---- get_node：404 精确分类 ----

def test_get_node_returns_none_on_404(http, rs):
    http["responses"][("GET", "/memory/404")] = _Resp(404, text="not found")

    assert rs.get_node(404) is None


def test_get_node_raises_on_non_404_error(http, rs):
    http["responses"][("GET", "/memory/500")] = _Resp(500, text="boom")

    with pytest.raises(RemoteStoreError):
        rs.get_node(500)


def test_not_found_error_is_remote_store_error_subclass():
    from client.remote_store import RemoteStoreNotFoundError

    assert issubclass(RemoteStoreNotFoundError, RemoteStoreError)


def test_404_detection_uses_status_code_not_message_matching():
    """回归：404 必须按 status_code 判定，不得再抠错误消息字符串。"""
    import inspect

    from client import remote_store as mod

    req_src = inspect.getsource(mod.RemoteStore._req)
    assert "status_code == 404" in req_src, "404 未按状态码判定"
    get_node_src = inspect.getsource(mod.RemoteStore.get_node)
    assert "str(exc)" not in get_node_src, "仍在用错误消息字符串判定 404"


# ---- search_similar：payload_filter 不得静默忽略 ----

def test_search_similar_rejects_payload_filter_fail_loud(http, rs):
    with pytest.raises(NotImplementedError):
        rs.search_similar([0.1, 0.2], payload_filter={"type": "skill_chunk"})


def test_search_similar_without_filter_still_works(http, rs):
    http["responses"][("POST", "/mem/search")] = _Resp(
        200, {"results": [{"id": 1, "score": 0.9, "payload": {"type": "memory"}}]}
    )

    out = rs.search_similar([0.1, 0.2], top_k=1)

    assert out, out
    assert http["calls"][0][0] == "POST" and http["calls"][0][1] == "/mem/search"


# ---- iter_payloads：必须要求完整 payload，缺了不许静默退回摘要 ----

def test_iter_payloads_requests_full_payload(http, rs):
    http["responses"][("GET", "/export")] = _Resp(200, {
        "memories": [{"id": 3, "content": "x", "payload": {"content": "x",
                                                           "domain": "hero"}}],
        "total_nodes": 1, "page": 1, "page_size": 500, "total_pages": 1,
    })

    pairs = list(rs.iter_payloads())

    method, path, kwargs = http["calls"][0]
    assert (method, path) == ("GET", "/export"), http["calls"][0]
    assert kwargs["params"]["include_payload"] == "true", kwargs["params"]
    assert pairs == [(3, {"content": "x", "domain": "hero"})], pairs


def test_iter_payloads_fails_loud_when_payload_missing(http, rs):
    """服务端忽略 include_payload 时必须报错，不能静默退回残缺摘要。"""
    http["responses"][("GET", "/export")] = _Resp(200, {
        "memories": [{"id": 3, "content": "x"}],
        "total_nodes": 1, "page": 1, "page_size": 500, "total_pages": 1,
    })

    with pytest.raises(RemoteStoreError, match="payload"):
        list(rs.iter_payloads())
