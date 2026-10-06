"""Regression tests for issues #52 and #53.

#52 — PUT/PATCH of ``content`` never recomputed the vector.
    ``update_payload`` only shallow-merges the payload; FTS is resynced but
    the embedding is not. Semantic search and conflict detection therefore
    kept scoring/ordering the node by its OLD text, silently. The only fix
    was a manual two-step (embed externally → PATCH /memory/{id}/vector).
    Fix: ``TriviumStore.reembed_node`` + ``POST /memory/{id}/reembed``, and a
    ``warning`` in the PUT/PATCH response whenever ``content`` changed — so
    the drift is never silent.

#53 — dry-run previews opened the DB read_write and advanced the generation.
    ``task-archive`` / ``consolidate`` default to a dry run but constructed a
    plain ``TriviumStore()`` (read_write → writes the file group, cannot run
    concurrently with REST). Fix: ``TriviumStore(read_only=True)`` opens with
    triviumdb's ``access_mode="read_only"`` and skips index creation; the CLI
    passes ``read_only=not args.apply``.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from unittest.mock import patch

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


@contextlib.contextmanager
def _isolated_store(**kw):
    """A TriviumStore on a fresh temp DB (never the shared session DB)."""
    tmp = tempfile.mkdtemp(prefix="palimpsest_reembed_iso_")
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
# #52 — reembed_node recomputes the vector from the current content.
# ---------------------------------------------------------------------------


class TestReembedNode:
    def test_reembed_updates_vector_to_new_content(self):
        with _isolated_store() as store:
            nid = store.insert_node({"type": "memory", "content": "alpha topic"}, _fake_embed("alpha topic"))
            before = store.get_node(nid)["vector"]
            # change content via the same path PUT/PATCH uses (no vector touch)
            store.update_payload(nid, {"content": "beta topic"})
            still_old = store.get_node(nid)["vector"]
            assert still_old == before, "update_payload must not touch the vector"
            # now reembed
            assert store.reembed_node(nid) is True
            after = store.get_node(nid)["vector"]
            assert after != before, "reembed must change the vector"
            # f32 storage vs f64 compute → compare approximately, not exactly
            expected = _fake_embed("beta topic")
            assert len(after) == len(expected)
            assert max(abs(a - b) for a, b in zip(after, expected, strict=False)) < 1e-5, (
                "vector must match new content"
            )

    def test_reembed_missing_node_returns_false(self):
        with _isolated_store() as store:
            assert store.reembed_node(999999) is False

    def test_reembed_is_idempotent_for_same_content(self):
        with _isolated_store() as store:
            nid = store.insert_node({"type": "memory", "content": "stable"}, _fake_embed("stable"))
            assert store.reembed_node(nid) is True
            first = store.get_node(nid)["vector"]
            assert store.reembed_node(nid) is True
            assert store.get_node(nid)["vector"] == first


# ---------------------------------------------------------------------------
# #52 — the endpoint and the PUT/PATCH warning.
# ---------------------------------------------------------------------------


class TestReembedEndpointAndWarning:
    def _client(self):
        from fastapi.testclient import TestClient

        import main

        return main, TestClient(main.app)

    def test_put_content_returns_warning(self):
        main, client = self._client()
        with _isolated_store() as store:
            nid = store.insert_node({"type": "memory", "content": "x"}, _fake_embed("x"))
            with patch.object(main, "_get_store", return_value=store):
                resp = client.put(f"/memory/{nid}", json={"content": "y"})
        assert resp.status_code == 200
        assert "warning" in resp.json(), "changing content must not be silent"

    def test_put_without_content_has_no_warning(self):
        main, client = self._client()
        with _isolated_store() as store:
            nid = store.insert_node({"type": "memory", "content": "x"}, _fake_embed("x"))
            with patch.object(main, "_get_store", return_value=store):
                resp = client.put(f"/memory/{nid}", json={"importance": 0.9})
        assert resp.status_code == 200
        assert "warning" not in resp.json()

    def test_reembed_endpoint_recomputes(self):
        main, client = self._client()
        with _isolated_store() as store:
            nid = store.insert_node({"type": "memory", "content": "old"}, _fake_embed("old"))
            before = store.get_node(nid)["vector"]
            with patch.object(main, "_get_store", return_value=store):
                client.put(f"/memory/{nid}", json={"content": "new"})
                resp = client.post(f"/memory/{nid}/reembed")
        assert resp.status_code == 200
        assert store.get_node(nid)["vector"] != before

    def test_reembed_endpoint_404_for_missing(self):
        main, client = self._client()
        with _isolated_store() as store, patch.object(main, "_get_store", return_value=store):
            resp = client.post("/memory/999999/reembed")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# #53 — read-only store never writes the file group.
# ---------------------------------------------------------------------------


class TestReadOnlyStore:
    def test_read_only_flag_defaults_false(self):
        with _isolated_store() as store:
            assert store.read_only is False

    def test_read_only_store_can_read(self):
        # seed with a write store, then reopen read-only and read it back
        tmp = tempfile.mkdtemp(prefix="palimpsest_ro_iso_")
        old = Config.DB_PATH
        Config.DB_PATH = os.path.join(tmp, "ro.db")
        try:
            w = TriviumStore()
            nid = w.insert_node({"type": "memory", "content": "seed"}, _fake_embed("seed"))
            del w
            ro = TriviumStore(read_only=True)
            assert ro.read_only is True
            assert ro.get_node(nid) is not None
        finally:
            Config.DB_PATH = old

    def test_read_only_store_rejects_writes(self):
        """A read-only store must not be able to insert."""
        import pytest

        tmp = tempfile.mkdtemp(prefix="palimpsest_ro_reject_")
        old = Config.DB_PATH
        Config.DB_PATH = os.path.join(tmp, "reject.db")
        try:
            w = TriviumStore()
            w.insert_node({"type": "memory", "content": "seed"}, _fake_embed("seed"))
            del w
            ro = TriviumStore(read_only=True)
            # triviumdb raises RuntimeError("只读数据库不允许执行操作…") on write
            with pytest.raises(RuntimeError):
                ro.insert_node({"type": "memory", "content": "nope"}, _fake_embed("nope"))
        finally:
            Config.DB_PATH = old

    def test_dry_run_cli_uses_read_only_store(self):
        """The CLI's dry-run commands must construct read_only=not apply."""
        src = (_REPO_ROOT / "scripts" / "palimpsest_cli.py").read_text(encoding="utf-8")
        # both dry-run-defaulting commands must gate read_only on --apply
        assert "TriviumStore(read_only=not args.apply)" in src
