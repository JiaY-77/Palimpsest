"""Tests for eval/pool_filter.py — no DB, no network, no Ollama."""

from __future__ import annotations

import sys
from pathlib import Path

# Import pool_filter directly (no project modules)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
from pool_filter import filter_pool


def _node(nid: int, source: str | None = None, content: str = "hello") -> dict:
    payload: dict = {"content": content}
    if source is not None:
        payload["source"] = source
    return {"node_id": nid, "payload": payload}


# ── ① source 命中被排除 ────────────────────────────────────────────────
def test_source_excluded():
    nodes = [_node(1, source="hermes-session_end"), _node(2, source="manual")]
    kept, stats = filter_pool(nodes, exclude_sources=("hermes-session_end",))
    assert len(kept) == 1
    assert kept[0]["node_id"] == 2
    assert stats["excluded_source"] == 1


# ── ② 空 source 不被排除 ────────────────────────────────────────────────
def test_empty_source_not_excluded():
    nodes = [_node(1, source=None, content="aaa"), _node(2, source="", content="bbb")]
    kept, stats = filter_pool(nodes, exclude_sources=("hermes-session_end",))
    assert len(kept) == 2
    assert stats["excluded_source"] == 0


# ── ③ 内容去空白后相同的两条只留 node_id 小的 ─────────────────────────
def test_dedup_keeps_smaller_node_id():
    nodes = [
        _node(10, content="a b c"),
        _node(5, content="a  b  c"),  # same after stripping whitespace
    ]
    kept, stats = filter_pool(nodes, drop_duplicates=True)
    assert len(kept) == 1
    assert kept[0]["node_id"] == 5
    assert stats["excluded_duplicate"] == 1


# ── ④ content 为空被丢弃 ────────────────────────────────────────────────
def test_empty_content_dropped():
    nodes = [_node(1, content=""), _node(2, content="   ")]
    kept, stats = filter_pool(nodes, drop_duplicates=True)
    assert len(kept) == 0
    assert stats["excluded_duplicate"] == 2
    assert stats["kept"] == 0


# ── ⑤ stats 四个键数值正确 ─────────────────────────────────────────────
def test_stats_keys_and_values():
    nodes = [
        _node(1, source="hermes-session_end", content="aaa"),
        _node(2, source="manual", content="bbb"),
        _node(3, source="manual", content="bbb"),  # duplicate of 2
        _node(4, source=None, content=""),
    ]
    kept, stats = filter_pool(
        nodes, exclude_sources=("hermes-session_end",), drop_duplicates=True,
    )
    assert set(stats.keys()) == {"input", "excluded_source", "excluded_duplicate", "kept"}
    assert stats["input"] == 4
    assert stats["excluded_source"] == 1
    assert stats["excluded_duplicate"] == 2  # 1 duplicate + 1 empty
    assert stats["kept"] == 1
    assert kept[0]["node_id"] == 2


# ── ⑥ exclude_sources=() 且 drop_duplicates=False 时原样返回 ──────────
def test_no_filter_passthrough():
    nodes = [_node(1, content="dup"), _node(2, content="dup"), _node(3, content="")]
    kept, stats = filter_pool(nodes, exclude_sources=(), drop_duplicates=False)
    assert len(kept) == 3
    assert [n["node_id"] for n in kept] == [1, 2, 3]
    assert stats == {"input": 3, "excluded_source": 0, "excluded_duplicate": 0, "kept": 3}


# ── ⑦ 保持输入顺序 ─────────────────────────────────────────────────────
def test_order_preserved():
    nodes = [_node(5, content="x"), _node(3, content="y"), _node(1, content="z")]
    kept, _ = filter_pool(nodes, exclude_sources=(), drop_duplicates=True)
    assert [n["node_id"] for n in kept] == [5, 3, 1]


def test_order_preserved_with_dedup_replacement():
    """When a duplicate with smaller node_id replaces the first occurrence,
    the replacement stays at the first occurrence's position."""
    nodes = [
        _node(8, content="same"),
        _node(2, content="same"),
        _node(9, content="other"),
    ]
    kept, stats = filter_pool(nodes, drop_duplicates=True)
    assert [n["node_id"] for n in kept] == [2, 9]
    assert stats["excluded_duplicate"] == 1
