"""CLI / REST coexistence, and the semantic-drift half of #52.

Two gaps the write-path audit flagged (2026-10-03):

1. **CLI vs REST as two writers.** The CLI used to open the memory DB in its
   own process for *read* commands, racing the resident REST service for the
   single-writer triviumdb lock — an intermittent failure that corrupted DBs
   historically. Read commands were moved onto REST (see
   ``scripts/palimpsest_cli.py``); the lock-timing primitives are already
   covered by ``test_concurrency.py`` / ``test_db_busy_error.py``. What is NOT
   covered is the *Palimpsest-level contract* those fixes rest on: a dry-run
   preview must never write, and a lock conflict must surface as a readable,
   actionable error rather than a silent no-op.

2. **#52's other half.** ``test_reembed_and_readonly.py`` asserts that a
   re-embed picks up new content; it never asserts the *drift* itself — that
   after changing ``content`` WITHOUT re-embedding, semantic search still
   ranks by the OLD text. That missing assertion is why #52 shipped.

These tests pin those contracts. The lock *timing* is deliberately not
asserted (triviumdb's open-window behaviour is non-deterministic); the tests
assert the properties that must hold regardless of who wins the race.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from config import Config  # noqa: E402
from core import trivium_store as ts_mod  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


@contextlib.contextmanager
def _isolated_store(**kw):
    tmp = tempfile.mkdtemp(prefix="palimpsest_mw_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    ts_mod.reset_connections_for_test()  # 干净起点：注册表不残留其他库的共享连接
    try:
        yield TriviumStore(**kw)
    finally:
        # 单例共享连接不再随操作关闭：reset 释放库文件（Windows 下不 reset 则
        # 临时目录里的库文件保持打开），再还原 DB_PATH。
        ts_mod.reset_connections_for_test()
        Config.DB_PATH = old


def _fake_embed(text):
    from tests.conftest import _fake_embed as fe

    return fe(text)


# ---------------------------------------------------------------------------
# Dry-run previews must never mutate the DB, even as a second opener.
# ---------------------------------------------------------------------------


class TestDryRunNeverWrites:
    def test_read_only_store_cannot_advance_generation(self):
        """A read-only store is the CLI's dry-run mode: it must not write.

        This is the contract behind #53. Under the #167 singleton it has two
        halves: on a path where the read-only store owns the connection,
        reads work and writes are rejected; when a read_write shared
        connection already exists for the same path (the resident REST
        store), a read_only request fails loudly with AccessModeConflictError
        instead of silently reusing the wrong connection (covered by
        ``test_mixed_access_mode_on_same_path_raises``).
        """
        tmp = tempfile.mkdtemp(prefix="palimpsest_ro_dry_")
        old = Config.DB_PATH
        Config.DB_PATH = os.path.join(tmp, "ro.db")
        try:
            w = TriviumStore()
            nid = w.insert_node({"type": "memory", "content": "seed"}, _fake_embed("seed"))
            ts_mod.reset_connections_for_test()  # 关掉 rw 共享连接，让 ro 连接独占开库
            ro = TriviumStore(read_only=True)
            assert ro.read_only is True
            assert ro.get_node(nid) is not None  # reads work
            with pytest.raises(RuntimeError):
                ro.insert_node({"type": "memory", "content": "nope"}, _fake_embed("nope"))
        finally:
            ts_mod.reset_connections_for_test()
            Config.DB_PATH = old

    def test_mixed_access_mode_on_same_path_raises(self):
        """同进程同库已有 read_write 共享连接时，read_only 请求必须明确报错。

        reader 与 writer 在 triviumdb 层互斥，单例只能取一种模式（issue #167
        约束 3）：既不能静默复用错的连接，也不能静默失败。
        """
        from core.trivium_store import AccessModeConflictError

        with _isolated_store() as seed:
            seed.insert_node({"type": "memory", "content": "seed"}, _fake_embed("seed"))
            ro = TriviumStore(read_only=True)
            with pytest.raises(AccessModeConflictError) as ei:
                ro.get_node(999999)
            msg = str(ei.value)
            assert "同一进程" in msg and "访问模式" in msg
            assert Config.DB_PATH in msg, "消息应点名是哪个库"
            assert "REST" in msg, "消息应给出出路（复用实例或改用 REST）"

    def test_dry_run_consolidate_does_not_add_nodes(self):
        """consolidate(dry_run=True) must leave the node set unchanged."""
        from core.consolidator import consolidate

        with _isolated_store() as store:
            a = "自动化测试dry运行预览不得写入节点内容A"
            b = "自动化测试dry运行预览不得写入节点内容B"
            for c in (a, b):
                store.insert_node(
                    {"type": "memory", "content": c, "importance": 0.3, "status": "active"}, _fake_embed(c)
                )
            before = {nid for nid, _ in store.iter_payloads()}
            res = consolidate(store, dry_run=True, sim_threshold=0.70, max_importance=0.9)
            assert res["dry_run"] is True
            after = {nid for nid, _ in store.iter_payloads()}
            assert before == after, "dry-run preview must not create nodes"


# ---------------------------------------------------------------------------
# A lock conflict must be a readable, actionable error (not a silent no-op).
# ---------------------------------------------------------------------------


class TestLockConflictIsReadable:
    def test_busy_error_names_the_path_and_suggests_action(self):
        """When the DB is held, the error must name the DB and suggest what to do.

        Under the #167 singleton, same-process contention queues on the shared
        connection instead of failing — so the held connection here is a RAW
        triviumdb connection (registry bypass), equivalent to another process
        holding the DB. The cross-process timing itself is covered by
        ``test_concurrency.py``.
        """
        import triviumdb

        from core.trivium_store import DatabaseBusyError

        with _isolated_store() as holder:
            holder.insert_node({"type": "memory", "content": "held"}, _fake_embed("held"))
            ts_mod.reset_connections_for_test()  # 关共享连接，让裸连接能独占开库
            raw = triviumdb.TriviumDB(Config.DB_PATH, dim=holder.dim)
            try:
                # a second store on the same path must fail fast, with guidance
                try:
                    other = TriviumStore()
                    other.insert_node({"type": "memory", "content": "x"}, _fake_embed("x"))
                    opened = True
                except DatabaseBusyError as e:
                    opened = False
                    msg = str(e)
                    assert Config.DB_PATH in msg, "error must name the DB path"
                    assert "占用" in msg or "busy" in msg.lower()
                assert not opened, "a held connection must make the conflicting open fail fast with a guided error"
            finally:
                raw.close()
                ts_mod.reset_connections_for_test()


# ---------------------------------------------------------------------------
# #52: the drift itself — semantic search keeps ranking by the OLD text.
# ---------------------------------------------------------------------------


class TestContentChangeDriftsSemanticRanking:
    def test_semantic_search_uses_old_text_until_reembed(self):
        """Changing content without re-embedding must leave the OLD vector in place.

        This is the *drift* #52 was about: the node's payload says the new text
        but its vector still encodes the old text, so semantic ranking keeps
        matching the old meaning. ``test_reembed_and_readonly.py`` only checks
        the post-reembed state; this pins the pre-reembed drift.
        """
        with _isolated_store() as store:
            old = "苹果香蕉橘子水果篮子的原始描述"
            new = "螺丝刀扳手钳子工具箱的全新描述"
            nid = store.insert_node({"type": "memory", "content": old}, _fake_embed(old))
            old_vec = store.get_node(nid)["vector"]

            # change content via the shallow-merge path (no vector touch)
            store.update_payload(nid, {"content": new})

            node = store.get_node(nid)
            assert node["payload"]["content"] == new, "payload holds the new text"
            assert node["vector"] == old_vec, (
                "the vector must still be the OLD one until reembed — this is "
                "exactly the drift semantic search exhibits"
            )

    def test_reembed_switches_ranking_to_new_text(self):
        """After reembed the vector must encode the NEW text."""
        with _isolated_store() as store:
            old = "苹果香蕉橘子水果篮子的原始描述"
            new = "螺丝刀扳手钳子工具箱的全新描述"
            nid = store.insert_node({"type": "memory", "content": old}, _fake_embed(old))
            store.update_payload(nid, {"content": new})
            assert store.reembed_node(nid) is True
            after = store.get_node(nid)["vector"]
            expected = _fake_embed(new)
            assert all(abs(a - b) < 1e-5 for a, b in zip(after, expected, strict=True)), (
                "reembed must move the vector onto the new text"
            )
