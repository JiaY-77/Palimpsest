"""Tests for pool_filter.should_write_output guard."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from pool_filter import should_write_output  # noqa: E402


def test_zero_success_blocks_write() -> None:
    allow, reason = should_write_output(0, [], [])
    assert allow is False
    assert len(reason) > 0


def test_positive_success_allows_write() -> None:
    allow, reason = should_write_output(3, [], [{"qid": "q1"}])
    assert allow is True
    assert reason == ""


def test_negative_success_blocks_write() -> None:
    allow, reason = should_write_output(-1, [{"qid": "old"}], [])
    assert allow is False
    assert len(reason) > 0
