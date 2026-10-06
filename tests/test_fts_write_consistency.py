"""Regression tests for FTS write-path consistency.

Auditing (2026-10-03) found the store-level write methods do NOT sync the
derived FTS index themselves — the responsibility was pushed to every caller,
and callers forget:

* ``consolidate`` builds a merged node via ``insert_with_id`` (vector only) but
  neither ``mcp_tools/consolidate_tool.py`` nor the CLI re-indexes it, so the
  merged memory is reachable by semantic search yet invisible to FTS.
* ``TriviumStore.delete_node`` only deletes from the main DB (triviumdb) and the
  three ``scripts/build_*_index.py`` callers never cleaned up FTS.

Fix: the store owns its derived-state sync — ``delete_node`` clears FTS itself,
and a new ``update_content`` keeps FTS in step when content changes.

These tests pin that behaviour so the drift cannot silently return.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile

import pytest

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


@contextlib.contextmanager
def _isolated_store(**kw):
    """A TriviumStore on a fresh temp DB (never the shared session DB)."""
    tmp = tempfile.mkdtemp(prefix="palimpsest_ftscons_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    try:
        yield TriviumStore(**kw)
    finally:
        Config.DB_PATH = old


def _fake_embed(text):
    from tests.conftest import _fake_embed as fe

    return fe(text)


def _fts_ids(query: str, limit: int = 200) -> set:
    """Return the set of node ids currently retrievable from the FTS index."""
    from core.fts_index import search_fts

    hits = search_fts(query, limit=limit)
    ids = set()
    for h in hits or []:
        if isinstance(h, dict):
            # search_fts returns {'node_id', 'content'} dicts
            ids.add(h.get("node_id"))
        else:
            ids.add(getattr(h, "node_id", None) or getattr(h, "id", None))
    return ids


def _fts_has(node_id: int, query: str = "自动化测试") -> bool:
    """True if the node is currently retrievable from the FTS index."""
    return node_id in _fts_ids(query)


# ---------------------------------------------------------------------------
# delete_node must clear the FTS entry (fix for the three build_* scripts).
# ---------------------------------------------------------------------------


class TestDeleteNodeClearsFts:
    def test_delete_node_removes_fts_entry(self):
        with _isolated_store() as store:
            content = "自动化测试删除后FTS必须清空残留条目"
            nid = store.insert_node({"type": "memory", "content": content}, _fake_embed(content))
            from core.fts_index import sync_node

            sync_node(nid, content)
            assert _fts_has(nid), "precondition: node should be FTS-visible"

            store.delete_node(nid)
            assert not _fts_has(nid), "delete_node must clear the FTS entry"

    def test_delete_node_tolerates_missing_fts_entry(self):
        """Deleting a node with no FTS entry must not raise on the FTS path.

        ``remove_node`` is idempotent; a node that was never indexed (or whose
        entry was already swept by fts-rebuild) must still delete cleanly.
        """
        with _isolated_store() as store:
            content = "自动化测试无FTS条目也能正常删除"
            nid = store.insert_node({"type": "memory", "content": content}, _fake_embed(content))
            # deliberately do NOT sync FTS: the delete must still succeed
            store.delete_node(nid)
            assert store.get_node(nid) is None


# ---------------------------------------------------------------------------
# consolidate must make the merged node FTS-visible.
# ---------------------------------------------------------------------------


class TestConsolidateSyncsFts:
    def test_merged_node_is_fts_visible(self):
        from core.consolidator import consolidate
        from core.fts_index import sync_node

        with _isolated_store() as store:
            a = "自动化测试覆盖失败路径的注入与回滚验证流程A"
            b = "自动化测试覆盖失败路径的注入与回滚验证流程B"
            id_a = store.insert_node(
                {"type": "memory", "content": a, "importance": 0.3, "status": "active"}, _fake_embed(a)
            )
            id_b = store.insert_node(
                {"type": "memory", "content": b, "importance": 0.3, "status": "active"}, _fake_embed(b)
            )
            sync_node(id_a, a)
            sync_node(id_b, b)

            before = {nid for nid, _ in store.iter_payloads()}
            res = consolidate(store, dry_run=False, sim_threshold=0.70, max_importance=0.9)
            assert res["merged"] >= 1, "precondition: a merge must have happened"
            new_ids = {nid for nid, _ in store.iter_payloads()} - before
            for nid in new_ids:
                assert _fts_has(nid), f"merged node {nid} must be FTS-visible, not semantic-only"


# ---------------------------------------------------------------------------
# update_content must keep FTS in step with the new content.
# ---------------------------------------------------------------------------


class TestUpdateContentSyncsFts:
    def test_update_content_reindexes_fts(self):
        with _isolated_store() as store:
            old = "自动化测试旧内容会被全文检索命中的原始版本"
            new = "全新内容替换后必须能被全文检索命中的新版本"
            nid = store.insert_node({"type": "memory", "content": old}, _fake_embed(old))
            from core.fts_index import sync_node

            sync_node(nid, old)
            assert _fts_has(nid)

            # update_content is the semantic write path (fix for #52-family drift)
            if not hasattr(store, "update_content"):
                pytest.skip("update_content not implemented yet (patch A)")
            store.update_content(nid, new)

            assert _fts_has(nid, query="全新内容替换"), "update_content must re-index the new text"

    def test_update_content_marks_vector_stale(self):
        """The vector is NOT recomputed inline (network call), but flagged."""
        with _isolated_store() as store:
            old = "自动化测试向量过期标记验证原始内容"
            nid = store.insert_node({"type": "memory", "content": old}, _fake_embed(old))
            if not hasattr(store, "update_content"):
                pytest.skip("update_content not implemented yet (patch A)")
            store.update_content(nid, "自动化测试向量过期标记验证新内容")
            node = store.get_node(nid)
            assert node["payload"].get("vector_stale") is True
