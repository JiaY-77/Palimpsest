"""Regression tests for issues #50 and #51.

#50 — Conflict detection mislabelled independent tasks as outdated.
    `core/conflict.py` treated every `memory`/`task`/`plan` node alike and, on
    a >0.75 similarity hit, marked the older node `status=outdated` and drew a
    `REVISED_BY` edge. Tasks accumulate — two independent tasks whose wording
    overlaps are not "the same fact superseded". Once mislabelled the old task
    vanished from default retrieval (`include_outdated=False`).
    Fix: a `CONFLICT_SKIP_TYPES` opt-in list (default empty → behaviour
    unchanged) makes listed types skip conflict detection entirely.

#51 — No public API to delete a graph edge.
    `mem_link` could create edges, `graph_neighbors` could read them, but
    nothing could delete one: a mislabelled `REVISED_BY` edge required
    stopping the service and hand-writing `db.unlink`. The underlying
    triviumdb `unlink(src, dst, label)` existed but was never surfaced.
    Fix: `TriviumStore.delete_edge` + `mem_unlink` MCP tool + `DELETE /mem/edge`.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load core.conflict through a real package path (it imports `config`).
# ---------------------------------------------------------------------------

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config  # noqa: E402
from core import conflict as _conflict  # noqa: E402


class _Result:
    """Minimal stand-in for the (outdated_ids, related_ids) return shape."""

    def __init__(self, rid, score):
        self._d = {"id": rid, "score": score}

    def get(self, key, default=None):
        return self._d.get(key, default)


class _FakeStore:
    """Store double: records update_payload / create_edge calls."""

    def __init__(self, payloads, similar):
        self._payloads = payloads
        self._similar = similar
        self.updated = []
        self.edges = []

    def search_similar(self, embedding, top_k=3, expand_depth=0, apply_decay=False):
        return self._similar

    def get_node(self, node_id):
        p = self._payloads.get(node_id)
        return {"payload": dict(p)} if p else None

    def update_payload(self, node_id, payload):
        self.updated.append(node_id)
        self._payloads[node_id] = dict(payload)

    def create_edge(self, source, target, relation, **kw):
        self.edges.append((source, target, relation))


def _resolve(new_type, old_payload, score, store_payloads=None, new_payload=None):
    payloads = store_payloads or {}
    store = _FakeStore(payloads, [_Result(1, score)])
    return _conflict.resolve_conflict(
        store, [0.0], 2, tx=None,
        new_payload=new_payload if new_payload is not None
        else {"type": new_type, "domain": "task"},
    ), store


# ---------------------------------------------------------------------------
# #50 — CONFLICT_SKIP_TYPES exempts a type from conflict detection.
# ---------------------------------------------------------------------------


class TestConflictSkipTypes:
    def test_task_still_detected_by_default(self):
        """Default (empty skip list) must preserve the pre-fix behaviour."""
        old = {"type": "task", "domain": "task", "status": "active"}
        result, store = _resolve("task", old, 0.9, store_payloads={1: old})
        assert result["outdated_ids"] == [1]
        assert store.edges == [(2, 1, "REVISED_BY")]

    def test_task_exempt_when_listed(self):
        """With task listed, the old task is left untouched."""
        old = {"type": "task", "domain": "task", "status": "active"}
        with patch.object(Config, "CONFLICT_SKIP_TYPES", frozenset({"task"})):
            result, store = _resolve("task", old, 0.9, store_payloads={1: old})
        assert result["outdated_ids"] == []
        assert result["related_ids"] == []
        assert store.updated == []
        assert store.edges == []

    def test_exemption_is_per_type(self):
        """Skipping task must not silently exempt memory too."""
        old = {"type": "memory", "domain": "hermes", "status": "active"}
        with patch.object(Config, "CONFLICT_SKIP_TYPES", frozenset({"task"})):
            result, _ = _resolve(
                "memory", old, 0.9,
                store_payloads={1: old},
                new_payload={"type": "memory", "domain": "hermes"},
            )
        assert result["outdated_ids"] == [1]

    def test_below_threshold_not_marked_even_without_exemption(self):
        """Sanity: the 0.75 threshold still governs related vs outdated."""
        old = {"type": "task", "domain": "task", "status": "active"}
        result, _ = _resolve("task", old, 0.5, store_payloads={1: old})
        assert result["outdated_ids"] == []
        assert result["related_ids"] == [1]

    def test_config_parses_comma_separated_list(self):
        """CONFLICT_SKIP_TYPES reads a comma-separated env value."""
        import subprocess

        code = (
            "import os; os.environ['CONFLICT_SKIP_TYPES']='task, plan';"
            "from config import Config;"
            "print(sorted(Config.CONFLICT_SKIP_TYPES))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "['plan', 'task']"

    def test_config_default_is_empty(self):
        import subprocess

        code = "from config import Config; print(len(Config.CONFLICT_SKIP_TYPES))"
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "0"


# ---------------------------------------------------------------------------
# #51 — TriviumStore.delete_edge against a real (tmp) store.
# ---------------------------------------------------------------------------


def _make_store():
    """Real TriviumStore on an ISOLATED throwaway DB (context manager).

    The session-wide temp DB (conftest) is shared by many tests, and several
    of them count nodes / clusters — writing our fixture nodes there would
    pollute them. Follow the local convention (test_mem_stats / test_promote):
    point ``Config.DB_PATH`` at a fresh mkdtemp for the duration.
    """
    import contextlib
    import tempfile

    from core.trivium_store import TriviumStore

    @contextlib.contextmanager
    def _cm():
        tmp = tempfile.mkdtemp(prefix="palimpsest_edge_iso_")
        old = Config.DB_PATH
        Config.DB_PATH = os.path.join(tmp, "edge.db")
        try:
            yield TriviumStore()
        finally:
            Config.DB_PATH = old

    return _cm()


class TestDeleteEdge:
    def _two_nodes(self, store):
        from tests.conftest import _fake_embed

        a = store.insert_node({"type": "memory", "content": "node A"}, _fake_embed("node A"))
        b = store.insert_node({"type": "memory", "content": "node B"}, _fake_embed("node B"))
        return a, b

    def test_delete_existing_edge_returns_true(self):
        with _make_store() as store:
            a, b = self._two_nodes(store)
            store.create_edge(a, b, "REVISED_BY")
            assert store.delete_edge(a, b, "REVISED_BY") is True
            labels = {getattr(e, "label", None) for e in (store.get_edges(a) or [])}
            assert "REVISED_BY" not in labels

    def test_delete_missing_edge_is_idempotent(self):
        with _make_store() as store:
            a, b = self._two_nodes(store)
            # never created → must return False, not raise
            assert store.delete_edge(a, b, "REVISED_BY") is False

    def test_delete_only_matching_label(self):
        with _make_store() as store:
            a, b = self._two_nodes(store)
            store.create_edge(a, b, "RELATED_TO")
            store.create_edge(a, b, "REVISED_BY")
            assert store.delete_edge(a, b, "REVISED_BY") is True
            labels = {getattr(e, "label", None) for e in (store.get_edges(a) or [])}
            assert labels == {"RELATED_TO"}


# ---------------------------------------------------------------------------
# #51 — mem_unlink MCP tool contract.
# ---------------------------------------------------------------------------


class TestMemUnlinkTool:
    def test_mem_unlink_is_exposed_and_delegates(self):
        import json as _json

        from mcp_tools import graph as _graph

        calls = []

        class _FakeStore:
            def delete_edge(self, s, t, rel):
                calls.append((s, t, rel))
                return True

        with patch.object(_graph, "store", _FakeStore()):
            out = _json.loads(_graph.mem_unlink(97, 96, "revised_by"))  # lower-case
        assert out["deleted"] is True
        assert out["relation"] == "REVISED_BY"  # normalised to upper
        assert calls == [(97, 96, "REVISED_BY")]

    def test_mem_unlink_reports_missing_edge(self):
        import json as _json

        from mcp_tools import graph as _graph

        class _FakeStore:
            def delete_edge(self, s, t, rel):
                return False

        with patch.object(_graph, "store", _FakeStore()):
            out = _json.loads(_graph.mem_unlink(1, 2))
        assert out["deleted"] is False

    def test_mem_unlink_registered_in_tool_list(self):
        from mcp_tools import graph as _graph

        assert callable(getattr(_graph, "mem_unlink", None))
