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

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


@contextlib.contextmanager
def _isolated_store(**kw):
    tmp = tempfile.mkdtemp(prefix="palimpsest_mw_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    try:
        yield TriviumStore(**kw)
    finally:
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

        This is the contract behind #53 — a preview that silently opened
        read_write and advanced the generation could not run alongside REST.
        """
        import pytest

        with _isolated_store() as seed:
            nid = seed.insert_node({"type": "memory", "content": "seed"},
                                   _fake_embed("seed"))
            # a second store on the SAME path (Config.DB_PATH is the iso db here)
            ro = TriviumStore(read_only=True)
            assert ro.read_only is True
            assert ro.get_node(nid) is not None  # reads work
            with pytest.raises(RuntimeError):
                ro.insert_node({"type": "memory", "content": "nope"},
                               _fake_embed("nope"))

    def test_dry_run_consolidate_does_not_add_nodes(self):
        """consolidate(dry_run=True) must leave the node set unchanged."""
        from core.consolidator import consolidate

        with _isolated_store() as store:
            a = "自动化测试dry运行预览不得写入节点内容A"
            b = "自动化测试dry运行预览不得写入节点内容B"
            for c in (a, b):
                store.insert_node({"type": "memory", "content": c,
                                   "importance": 0.3, "status": "active"},
                                  _fake_embed(c))
            before = {nid for nid, _ in store.iter_payloads()}
            res = consolidate(store, dry_run=True, sim_threshold=0.70,
                              max_importance=0.9)
            assert res["dry_run"] is True
            after = {nid for nid, _ in store.iter_payloads()}
            assert before == after, "dry-run preview must not create nodes"


# ---------------------------------------------------------------------------
# A lock conflict must be a readable, actionable error (not a silent no-op).
# ---------------------------------------------------------------------------


class TestLockConflictIsReadable:
    def test_busy_error_names_the_path_and_suggests_action(self):
        """When the DB is held, the error must name the DB and suggest what to do.

        Rather than race a second process (non-deterministic), this holds a
        connection in-process and asserts the error our CLI/REST callers see
        is the guided ``DatabaseBusyError``. The cross-process timing itself is
        covered by ``test_concurrency.py``.
        """
        from core.trivium_store import DatabaseBusyError

        with _isolated_store() as holder:
            holder.insert_node({"type": "memory", "content": "held"},
                               _fake_embed("held"))
            held = holder._acquire()
            try:
                # a second store on the same path must fail fast, with guidance
                try:
                    other = TriviumStore()
                    other.insert_node({"type": "memory", "content": "x"},
                                      _fake_embed("x"))
                    opened = True
                except DatabaseBusyError as e:
                    opened = False
                    msg = str(e)
                    assert Config.DB_PATH in msg, "error must name the DB path"
                    assert "占用" in msg or "busy" in msg.lower()
                assert not opened, (
                    "a held connection must make the conflicting open fail "
                    "fast with a guided error"
                )
            finally:
                held.close()


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
            nid = store.insert_node({"type": "memory", "content": old},
                                    _fake_embed(old))
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
            nid = store.insert_node({"type": "memory", "content": old},
                                    _fake_embed(old))
            store.update_payload(nid, {"content": new})
            assert store.reembed_node(nid) is True
            after = store.get_node(nid)["vector"]
            expected = _fake_embed(new)
            assert all(abs(a - b) < 1e-5 for a, b in zip(after, expected, strict=True)), \
                "reembed must move the vector onto the new text"
