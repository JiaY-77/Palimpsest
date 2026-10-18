"""检索护栏与生命周期契约测试（issue #121 / #122 / #124）。

覆盖：
  - #121：policy.check_search_query 超长拒绝；三个检索入口的上限行为
    （/mem/search 与 /mem/hybrid-search 422、pre-turn fail-open 可观测）；
    embedding HTTP 错误判别把「输入超上下文」与「服务不可用」拆开。
  - #122：hybrid-search 不再把 embedding 失败伪装成 200 + 空结果（向上抛、
    与 /mem/search 同语义）；真·局部降级保留结果并附 degraded/errors；
    正常 query 结果不带 degraded 字段。
  - #124：pre-turn 早退分支（trivial / slash / 纯空白 / 过短）回显
    session_id / turn_index。

隔离保证：REST 测试只经端点写库并按节点差集清理，不触碰正式库；
异常判别为纯单元测试（不连任何 embedding 服务）。
"""

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """经端点写库的测试测完清理（照 test_lifecycle_endpoints 的差集模式）。"""
    from mcp_tools import store

    def _ids() -> set:
        try:
            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 桩失败则不清理（不掩盖真实错误）
            return set()

    before = _ids()
    yield
    for nid in _ids() - before:
        with contextlib.suppress(Exception):
            store.delete_node(nid)


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# issue #121 —— policy 与检索入口
# ---------------------------------------------------------------------------


def test_policy_check_search_query_rejects_overlong():
    from core.policy import check_search_query

    ok = check_search_query("普通查询")
    assert ok == {"ok": True, "error": None}

    long_query = "字" * 50_000
    bad = check_search_query(long_query)
    assert bad["ok"] is False
    assert "50_000" in bad["error"].replace(",", "_") or "50000" in bad["error"].replace("_", "").replace(",", "")
    assert "8000" in bad["error"]


def test_mem_search_rejects_50k_query_with_422():
    client = _client()
    r = client.post("/mem/search", json={"query": "字" * 50_000})
    assert r.status_code == 422, r.text
    assert "超长" in r.json()["detail"]


def test_mem_search_accepts_8000_char_query():
    client = _client()
    r = client.post("/mem/search", json={"query": "测试" * 4000})
    assert r.status_code == 200, r.text


def test_hybrid_search_rejects_50k_query_with_422():
    client = _client()
    r = client.post("/mem/hybrid-search", json={"query": "字" * 50_000})
    assert r.status_code == 422, r.text
    assert "超长" in r.json()["detail"]


def test_pre_turn_overlong_query_fail_open_observable():
    """超长 query 的 pre-turn：不炸、不注入，但明确标 query_too_long。"""
    client = _client()
    r = client.post(
        "/lifecycle/pre-turn",
        json={"user_message": "字" * 50_000, "session_id": "guard-s", "turn_index": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skip"] is True
    assert body["skip_reason"] == "query_too_long"
    assert body["inject_text"] == ""
    assert body["session_id"] == "guard-s"
    assert body["turn_index"] == 3


def test_embedding_http_error_split_too_long_vs_unavailable():
    """模型侧超上下文错误 → EmbeddingInputTooLongError；其余 → Unavailable。"""
    from core.trivium_store import (
        EmbeddingInputTooLongError,
        _raise_embedding_http_error,
    )

    class _Resp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests

                raise requests.HTTPError(f"{self.status_code}", response=self)

    with pytest.raises(EmbeddingInputTooLongError):
        _raise_embedding_http_error(
            _Resp(500, '{"error": "the input length exceeds the context length"}'),
            provider="Ollama",
        )
    # 不带长度关键词的 HTTP 错误：helper 原样 re-raise，由 _embed_ollama 外层
    # 统一包成 EmbeddingUnavailableError（503 语义在外层收口）；
    # TooLong 是它的子类，外层 except EmbeddingUnavailableError 的调用方不受影响。
    import requests as _requests

    with pytest.raises(_requests.HTTPError):
        _raise_embedding_http_error(_Resp(500, "boom"), provider="Ollama")
    _raise_embedding_http_error(_Resp(200, "ok"), provider="Ollama")


# ---------------------------------------------------------------------------
# issue #122 —— hybrid-search 的失败透明度
# ---------------------------------------------------------------------------


def test_hybrid_embedding_failure_propagates(monkeypatch):
    import mcp_tools.memory as mm
    from core.trivium_store import EmbeddingUnavailableError

    def _boom(*a, **k):
        raise EmbeddingUnavailableError("embedding 服务不可用（测试桩）")

    monkeypatch.setattr(mm, "_hybrid_rrf", _boom)
    with pytest.raises(EmbeddingUnavailableError):
        mm._hybrid_search_impl("查询一个问题", scope="memory")


def test_hybrid_embedding_too_long_failure_propagates(monkeypatch):
    import mcp_tools.memory as mm
    from core.trivium_store import EmbeddingInputTooLongError

    def _boom(*a, **k):
        raise EmbeddingInputTooLongError("输入超出 embedding 模型可处理长度（测试桩）")

    monkeypatch.setattr(mm, "_hybrid_cascade", _boom)
    with pytest.raises(EmbeddingInputTooLongError):
        mm._hybrid_search_impl("查询一个问题", scope="memory", mode="cascade")


def test_hybrid_local_failure_degrades_structured(monkeypatch):
    import mcp_tools.memory as mm

    def _boom(*a, **k):
        raise RuntimeError("局部子路径炸了（测试桩）")

    monkeypatch.setattr(mm, "_hybrid_rrf", _boom)
    result = mm._hybrid_search_impl("查询一个问题", scope="memory")
    assert result.get("degraded") is True
    assert result.get("errors") and "局部子路径" in result["errors"][0]
    assert isinstance(result.get("results"), list)


def test_hybrid_normal_query_has_no_degraded_field():
    """正常 query（FTS 通道健康）结果不带 degraded/errors 契约字段。

    先垫一条入 FTS 索引：fts.db 未建的空库上 FTS 通道判 degraded 是契约内
    行为，垫一条索引让本测试只检验「健康路径无降级字段」。
    """
    from core.fts_index import index_node
    from mcp_tools import store

    nid = store.insert_node(
        {
            "type": "memory",
            "content": "混合检索正常路径的决定：契约字段只在降级时出现",
            "importance": 0.5,
            "domain": "hermes",
        },
        store.embed_text("混合检索正常路径的决定：契约字段只在降级时出现"),
    )
    index_node(nid, "混合检索正常路径的决定：契约字段只在降级时出现")
    client = _client()
    r = client.post("/mem/hybrid-search", json={"query": "混合检索正常路径"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "degraded" not in body
    assert "errors" not in body


# ---------------------------------------------------------------------------
# issue #124 —— pre-turn 早退分支回显
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msg", ["谢谢", "/new", "   ", "ab"])
def test_pre_turn_early_exit_echoes_session_fields(msg):
    client = _client()
    r = client.post(
        "/lifecycle/pre-turn",
        json={"user_message": msg, "session_id": "echo-s", "turn_index": 7},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "echo-s"
    assert body["turn_index"] == 7


def test_pre_turn_normal_branch_still_echoes_session_fields():
    """正常分支的回显不受改动影响。"""
    client = _client()
    r = client.post(
        "/lifecycle/pre-turn",
        json={"user_message": "帮我查一下上周的会议记录", "session_id": "echo-n", "turn_index": 9, "min_score": 0.99},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "echo-n"
    assert body["turn_index"] == 9
