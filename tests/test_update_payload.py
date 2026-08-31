# -*- coding: utf-8 -*-
"""
回归测试：PUT/PATCH 部分更新合并语义 + Embedding fail-fast
===========================================================
覆盖两个 P0 修复：
  1. update_payload 改为合并语义：部分更新不覆盖未涉及的字段（T061 防清空）。
  2. embedding 失败不再静默返回零向量，而是抛 EmbeddingUnavailableError（fail-fast）。

使用 conftest 的临时库隔离（DB_PATH 指向临时目录），不碰正式库；
embedding 相关测试用 monkeypatch 模拟失败，不依赖真实 Ollama。
"""


def _store():
    """复用 conftest 建立的、指向临时库的全局 store。"""
    from mcp_tools import store
    return store


# ---------------------------------------------------------------------------
# 改动 1：update_payload 合并语义
# ---------------------------------------------------------------------------
def test_put_partial_update_keeps_fields():
    """部分更新只改传入字段，content/type/importance 等保留。"""
    store = _store()
    emb = store.embed_text("部分更新护栏内容")
    node_id = store.insert_node(
        {
            "type": "memory",
            "content": "保留的正文",
            "importance": 0.8,
            "domain": "hero",
        },
        emb,
    )
    store.update_payload(node_id, {"status": "active"})
    node = store.get_node(node_id)
    payload = node["payload"]
    assert payload["status"] == "active"
    assert payload["content"] == "保留的正文"
    assert payload["type"] == "memory"
    assert payload["importance"] == 0.8
    assert payload["domain"] == "hero"


def test_update_payload_full_replace_still_works():
    """全量 payload 更新（兼容既有调用方）行为不变。"""
    store = _store()
    emb = store.embed_text("全量覆盖护栏内容")
    node_id = store.insert_node({"type": "memory", "content": "旧内容"}, emb)
    store.update_payload(
        node_id,
        {
            "type": "memory",
            "content": "新内容",
            "importance": 0.5,
            "status": "active",
            "domain": "general",
        },
    )
    payload = store.get_node(node_id)["payload"]
    assert payload["content"] == "新内容"
    assert payload["status"] == "active"


def test_update_payload_missing_node_raises():
    """对不存在的 node_id 调用 update_payload 应抛异常。"""
    store = _store()
    missing_id = 999_999_999
    assert store.get_node(missing_id) is None
    raised = False
    try:
        store.update_payload(missing_id, {"status": "active"})
    except (KeyError, ValueError) as e:
        raised = True
        assert str(e)
    assert raised


# ---------------------------------------------------------------------------
# 改动 2：embedding fail-fast —— 失败抛 EmbeddingUnavailableError
# ---------------------------------------------------------------------------
def test_embedding_failure_raises(monkeypatch):
    """requests.post 抛 ConnectionError → embed_text 应抛 EmbeddingUnavailableError。"""
    from core.trivium_store import EmbeddingUnavailableError

    import requests

    def _boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("模拟 Ollama 未启动")

    monkeypatch.setattr(requests, "post", _boom)

    store = _store()
    try:
        store._embed_ollama("x")
        assert False, "embed_text 失败时应抛 EmbeddingUnavailableError，而不是返回零向量"
    except EmbeddingUnavailableError as e:
        assert str(e), "异常应带修复指引信息"
