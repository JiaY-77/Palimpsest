"""Tests for pool_filter.should_write_output guard."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from pool_filter import should_write_output


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


def test_existing_items_with_empty_new_items_blocks_write() -> None:
    """有旧题集、新题集为空 → 覆盖会清空已有数据，必须拦截。"""
    allow, reason = should_write_output(3, [{"qid": "old"}], [])
    assert allow is False
    assert "1" in reason


def test_existing_items_with_new_items_allows_write() -> None:
    """有旧题集、也有新题集 → 正常覆盖放行。"""
    allow, reason = should_write_output(3, [{"qid": "old"}], [{"qid": "new"}])
    assert allow is True
    assert reason == ""


# ---------------------------------------------------------------------------
# _generate_batch：模型返回条数与 batch 不一致 → 整批放弃
# ---------------------------------------------------------------------------
# 这条守卫生效是 `zip(items, batch, strict=True)` 安全的前提：长度不一致时
# 在上游就 return None，不会走到 zip。守卫被删掉的话，strict=True 会抛
# ValueError 而不是静默截断。


def _gen_eval_set():
    """导入 eval.gen_eval_set 并还原它改掉的全局 DB_PATH。

    该模块 import 时会把 DB_PATH 指向 eval/.tmp 下的库副本（它自身的隔离手段），
    在同一个 pytest 会话里会连累后续用例的库路径（曾让 test_smoke 的归档用例
    落到 eval/.tmp 的持久目录上）。导入后立刻还原。
    """
    import os

    saved = os.environ.get("DB_PATH")
    from eval import gen_eval_set as gen

    if saved is not None:
        os.environ["DB_PATH"] = saved
    return gen


def test_generate_batch_rejects_length_mismatch(monkeypatch) -> None:
    import json

    gen = _gen_eval_set()

    batch = [
        {"node_id": i, "payload": {"content": f"节点内容 {i}"}} for i in range(3)
    ]
    monkeypatch.setattr(
        gen,
        "_call_deepseek",
        lambda *a, **k: json.dumps({"items": [{"idx": 0, "query": "只有一个查询"}]}),
    )
    assert gen._generate_batch(batch) is None


def test_generate_batch_returns_none_on_bad_json(monkeypatch) -> None:
    gen = _gen_eval_set()

    batch = [{"node_id": 0, "payload": {"content": "节点内容"}}]
    monkeypatch.setattr(gen, "_call_deepseek", lambda *a, **k: "not json at all")
    assert gen._generate_batch(batch) is None
