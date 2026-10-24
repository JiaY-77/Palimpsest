"""TDB 文本通道「写入同步」与 task_state 日志节点 FTS 缺口的测试。

覆盖任务书要求的六条场景：

1. 关闭态零成本：TDB_TEXT_ENABLED=False → sync_node 返回 False，且既不
   转调 index_node，也不开任何 TDB 连接（monkeypatch _acquire 计数）。
2. 开启态转发：True → 内部调 index_node(node_id, content) 恰一次。
3. 空 content = 清空：sync_node(nid, "") → index_node(nid, "")。
4. 异常不抛：index_node 抛异常 → sync_node 返回 False + warning 日志。
5. 接线点生效：trivium_store.update_content / delete_node 会同步调
   tdb_text_index.sync_node（monkeypatch 换成记录桩，断言参数）。
6. task_state 日志节点补 FTS：apply_task_patch 状态变化 → FTS sync_node
   被调，参数 = 该 type=record 日志节点的 id 与 content。

隔离：沿用 conftest 的临时库 + fake embedder（不依赖 Ollama）；接线用例
monkeypatch tdb_text_index.sync_node / task_state 侧 fts 节点，不真碰
TriviumDB 库文件（默认关闭箱子也保证了不会开侧车）。
"""

import contextlib
import logging
import uuid

import pytest

from config import Config
from core import tdb_text_index
from core.task_state import apply_task_patch
from core.trivium_store import TriviumStore
from mcp_tools import store


def _ns() -> str:
    return f"tdbsync_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def _record_index_node(monkeypatch):
    """把 tdb_text_index.index_node 换成记录桩，返回 (calls, 原样恢复)。"""
    calls: list[tuple[int, str]] = []

    def fake_index_node(node_id: int, content: str) -> bool:
        calls.append((int(node_id), content if content is None else str(content)))
        return True

    monkeypatch.setattr(tdb_text_index, "index_node", fake_index_node)
    return calls


# ---------------------------------------------------------------
# 1. 关闭态零成本
# ---------------------------------------------------------------


def test_sync_node_disabled_is_free(monkeypatch, _record_index_node):
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", False)
    calls_forbidden: list = []
    monkeypatch.setattr(tdb_text_index, "_acquire", lambda *a, **k: calls_forbidden.append(1))

    ret = tdb_text_index.sync_node(101, "正文")

    assert ret is False
    assert _record_index_node == []  # 未转发 index_node
    assert calls_forbidden == []  # 未开任何 TDB 连接


def test_sync_node_disabled_none_content_is_free(monkeypatch):
    """空串/None content 同样在门控处短路（不触碰 index_node）。"""
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", False)
    forbidden: list = []

    def _forbid(*_a, **_k):
        forbidden.append(1)
        raise AssertionError("关闭态不得触达 index_node")

    monkeypatch.setattr(tdb_text_index, "index_node", _forbid)
    assert tdb_text_index.sync_node(1, "") is False
    assert tdb_text_index.sync_node(1, "  ") is False  # 按契约 strip 后仍是清除
    assert forbidden == []


# ---------------------------------------------------------------
# 2/3. 开启态转发 + 空 content = 清空
# ---------------------------------------------------------------


def test_sync_node_enabled_forwards(monkeypatch, _record_index_node):
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", True)

    ret = tdb_text_index.sync_node(202, "任务正文")

    assert ret is True
    assert _record_index_node == [(202, "任务正文")]


def test_sync_node_empty_string_means_clear(monkeypatch, _record_index_node):
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", True)

    tdb_text_index.sync_node(303, "")

    assert _record_index_node == [(303, "")]


# ---------------------------------------------------------------
# 4. 异常不抛（失败 warning 留痕）
# ---------------------------------------------------------------
def test_sync_node_failure_returns_false_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", True)

    def _boom(node_id: int, content: str) -> bool:
        raise RuntimeError("TDB 连接被占")

    monkeypatch.setattr(tdb_text_index, "index_node", _boom)
    with caplog.at_level(logging.WARNING, logger="core.tdb_text_index"):
        ret = tdb_text_index.sync_node(505, "正文")

    assert ret is False  # 绝不抛
    assert any("TDB 文本索引同步失败" in r.message for r in caplog.records)


# ---------------------------------------------------------------
# 5. 接线点生效：update_content / delete_node
# ---------------------------------------------------------------


@pytest.fixture()
def _record_tdb_sync(monkeypatch):
    """把 tdb_text_index.sync_node 换成记录桩（接线点经 _tdb_text.sync_node
    动态解析模块属性，monkeypatch 即可命中）。"""
    calls: list[tuple[int, str]] = []

    def fake_sync(node_id: int, content: str) -> bool:
        calls.append((int(node_id), content if content is None else str(content)))
        return True

    monkeypatch.setattr(tdb_text_index, "sync_node", fake_sync)
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", True)
    return calls


def test_wiring_update_content(_record_tdb_sync):
    ns = _ns()
    content = f"{ns} 更新前的正文"
    nid = store.insert_node({"type": "memory", "content": content, "importance": 0.5}, store.embed_text(content))
    try:
        new_content = f"{ns} 更新后的正文"
        store.update_content(nid, new_content)
        assert _record_tdb_sync.count((nid, new_content)) == 1
    finally:
        store.delete_node(nid)


def test_wiring_delete_node(_record_tdb_sync):
    ns = _ns()
    content = f"{ns} 将被删除的正文"
    nid = store.insert_node({"type": "memory", "content": content, "importance": 0.5}, store.embed_text(content))
    assert (nid, content) not in _record_tdb_sync
    store.delete_node(nid)
    assert _record_tdb_sync.count((nid, "")) == 1  # 空串 = 清除该节点正文


def test_wiring_failure_does_not_block_delete(monkeypatch, _record_tdb_sync):
    """TDB 同步抛异常也不阻断主删除（主写已提交，索引可 rebuild 兜底）。"""
    ns = _ns()
    content = f"{ns} 删除阻断验证"

    def _boom(node_id: int, content_: str) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(tdb_text_index, "sync_node", _boom)
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", True)
    nid = store.insert_node({"type": "memory", "content": content, "importance": 0.5}, store.embed_text(content))
    store.delete_node(nid)  # 不抛即通过
    assert store.get_node(nid) is None


def test_wiring_disabled_never_routes(monkeypatch):
    """关闭态走真实接线路径也不打任何 TDB 桩（门控短路 + 返回值不变）。"""
    forbidden: list = []
    monkeypatch.setattr(tdb_text_index, "_acquire", lambda *a, **k: forbidden.append(1) or None)
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", False)
    ns = _ns()
    content = f"{ns} 关闭态回归"
    nid = store.insert_node({"type": "memory", "content": content, "importance": 0.5}, store.embed_text(content))
    try:
        store.update_content(nid, f"{ns} 关闭态新正文")
        # 返回结果不受影响（update_content 返回 None；无异常即视为未短路破坏）
        assert forbidden == []
        assert tdb_text_index.sync_node(nid, content) is False
        assert forbidden == []
    finally:
        store.delete_node(nid)


# ---------------------------------------------------------------
# 6. task_state 日志节点补 FTS
# ---------------------------------------------------------------


def test_state_log_node_syncs_fts(monkeypatch, caplog):
    """apply_task_patch 状态变化 → type=record 日志节点此刻进 FTS（既有缺口补齐）。"""
    mocked_fts_calls: list[tuple[int, str]] = []
    import core.task_state as ts

    real_sync = ts.fts_sync_node

    def fake_fts_sync(node_id: int, content: str, *a, **k) -> bool:
        real_sync(node_id, content)  # 真执行（离线 FTS5），同时记录
        mocked_fts_calls.append((int(node_id), str(content)))
        return True

    monkeypatch.setattr(ts, "fts_sync_node", fake_fts_sync)
    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", False)  # TDB 通道关闭：零成本短路

    ns = _ns()
    content_old = f"[todo] {ns} 写注册表"
    nid = store.insert_node(
        {
            "type": "task",
            "domain": "task",
            "character_name": "task",
            "content": content_old,
            "importance": 0.6,
            "project": ns,
        },
        store.embed_text(content_old),
    )
    try:
        content_new = f"[doing] {ns} 写注册表"
        apply_task_patch(store, nid, {"content": content_new})

        # 从 FTS 同步记录里找出本轮日志节点（id 由 fake 入参拿到）：payload
        # 是 log_kind=task_state 且 task_id 指回该任务。
        log_candidates = []
        for synced_id, synced_content in mocked_fts_calls:
            payload = (store.get_node(synced_id) or {}).get("payload") or {}
            if payload.get("log_kind") == "task_state" and payload.get("task_id") == nid:
                log_candidates.append((synced_id, synced_content))

        assert len(log_candidates) == 1, f"日志节点必须恰好被 FTS 同步一次：{mocked_fts_calls}"
        log_id, synced_content = log_candidates[0]
        assert synced_content.startswith("[doing]") and ns in synced_content
        # 内容与日志节点 payload.content 一致（不只是参数恰好像）
        log_payload = (store.get_node(log_id) or {}).get("payload") or {}
        assert synced_content == log_payload.get("content")
        # 真进了 fts.db，可全文检索命中（FTS5 离线可查，不依赖 Ollama）
        from core.fts_index import search_fts_status

        rows, status = search_fts_status(ns)
        assert status == "ok"
        assert any(r.get("node_id") == log_id for r in rows)
    finally:
        for log_edge in store.get_edges(nid) or []:
            tid = getattr(log_edge, "target_id", None)
            if tid is not None:
                with contextlib.suppress(Exception):
                    store.delete_node(int(tid))
        with contextlib.suppress(Exception):
            store.delete_node(nid)


def test_state_log_no_change_no_fts_call(monkeypatch):
    """状态没变 → 不写日志节点 → FTS 也不多打同步调用。

    注意：新插入节点 payload 无 task_state 字段（previous=None），首次
    apply_task_patch 即使 content 不变也会补一条「→ todo」日志（既有语义）。
    所以先 apply 一次把字段归一化，再测「状态真没变」的路径。
    """
    import core.task_state as ts

    calls: list = []
    monkeypatch.setattr(ts, "fts_sync_node", lambda *a, **k: calls.append(1) or True)

    ns = _ns()
    content = f"[todo] {ns} 不变任务"
    nid = store.insert_node(
        {
            "type": "task",
            "domain": "task",
            "character_name": "task",
            "content": content,
            "importance": 0.6,
            "project": ns,
        },
        store.embed_text(content),
    )
    try:
        apply_task_patch(store, nid, {})  # 归一化：把 task_state 字段落上（会写一条日志）
        before = len(calls)
        payload_after = ((store.get_node(nid) or {}).get("payload") or {}).get("task_state")
        apply_task_patch(store, nid, {"content": content})  # 同状态：无变化
        assert payload_after == "todo"
        assert len(calls) == before
    finally:
        with contextlib.suppress(Exception):
            store.delete_node(nid)


# ---------------------------------------------------------------
# 孤立 TriviumStore 实例也照常接线（不依赖 conftest 共享 store）
# ---------------------------------------------------------------


def test_wiring_on_isolated_bare_store(tmp_path, monkeypatch, _record_tdb_sync):
    """自建临时库验证接线不依赖共享 store 的全局状态（fake embedder 是
    conftest 的类级替换，覆盖自建实例，离线可跑）。"""
    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "bare.db"))
    bare = TriviumStore()
    nid = bare.insert_node(
        {"type": "memory", "content": "bare 原文", "importance": 0.5},
        bare.embed_text("bare 原文"),
    )
    bare.update_content(nid, "bare 新文")
    assert _record_tdb_sync.count((nid, "bare 新文")) == 1
    bare.delete_node(nid)
    assert _record_tdb_sync.count((nid, "")) == 1
