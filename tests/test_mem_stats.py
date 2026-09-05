# -*- coding: utf-8 -*-
"""
mem_stats —— 库盘点统计测试
==========================
覆盖 core.stats.compute_stats：
  - 构造不同 type/domain/importance/created_at 的节点，断言各分节统计正确；
  - 空库不崩溃。
隔离保证：本文件用独立临时库（自建 TriviumStore + 自指 DB_PATH），不触碰
conftest 的会话共享临时库，也不与 test_promote 等文件互相污染。
"""

import os
import shutil
import tempfile
import time

import pytest

from core.trivium_store import TriviumStore  # noqa: E402
from core.stats import compute_stats  # noqa: E402


@pytest.fixture
def iso_store():
    """构造一个全新临时库上的独立 TriviumStore（与共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_stats_iso_")
    from config import Config

    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old
        try:
            s._acquire().close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def _mk(store, content, type, domain, importance, **extra):
    """在独立 store 直写一个节点并返回 node_id。"""
    nid = store.insert_node(
        {"type": type, "content": content, "importance": importance,
         "domain": domain, **extra},
        store.embed_text(content),
    )
    return nid


def _ts(y, m, d):
    """构造本地时区的时间戳。"""
    return time.mktime((y, m, d, 10, 0, 0, 0, 0, -1))


def test_stats_sections(iso_store):
    """构造多类型/多 domain/多 importance/多 created_at 节点，断言各分节。"""
    s = iso_store
    # ① character/novel 0.5，2026-08
    id1 = _mk(s, "角色甲盘点", "character", "novel", 0.5)
    s.update_payload(id1, {"created_at": _ts(2026, 8, 15)})
    # ② memory/general 0.3，2026-07
    id2 = _mk(s, "记忆乙盘点", "memory", "general", 0.3)
    s.update_payload(id2, {"created_at": _ts(2026, 7, 10)})
    # ③ character/novel 0.9，标 outdated
    id3 = _mk(s, "角色丙盘点", "character", "novel", 0.9)
    s.update_payload(id3, {"status": "outdated"})
    # ④⑤ novel_chunk + kind
    id4 = _mk(s, "设定丁盘点", "novel_chunk", "novel", 0.5, kind="character")
    id5 = _mk(s, "设定戊盘点", "novel_chunk", "novel", 0.6, kind="setting",
              hit_count=3)
    # ⑥ todo/task 0.2，无 created_at（time 分节应跳过）
    id6 = _mk(s, "任务己盘点", "todo", "task", 0.2)

    # 建一条边（id1 → id2），供 graph 分节
    s.create_edge(id1, id2, "RELATED_TO", weight=0.9)

    stats = compute_stats(s)

    # ---- totals ----
    totals = stats["totals"]
    assert totals["total_nodes"] == 6, totals
    assert totals["active"] == 5, totals
    assert totals["outdated"] == 1, totals
    assert totals["by_type"].get("character") == 2, totals
    assert totals["by_type"].get("memory") == 1, totals
    assert totals["by_type"].get("novel_chunk") == 2, totals
    assert totals["by_type"].get("todo") == 1, totals
    assert totals["by_domain"].get("novel") == 4, totals
    assert totals["by_domain"].get("general") == 1, totals
    assert totals["by_domain"].get("task") == 1, totals

    # ---- kinds：仅 novel_chunk 且带 kind 的节点 ----
    kinds = stats["kinds"]
    assert kinds.get("character") == 1, kinds
    assert kinds.get("setting") == 1, kinds
    assert len(kinds) == 2, kinds

    # ---- importance 分桶 ----
    imp = stats["importance"]
    assert imp["lt_0_4"] == 2, imp      # memory 0.3 + todo 0.2
    assert imp["0_4_to_0_6"] == 2, imp  # character 0.5 + novel_chunk 0.5
    assert imp["0_6_to_0_8"] == 1, imp  # novel_chunk 0.6
    assert imp["ge_0_8"] == 1, imp      # character 0.9

    # ---- time：按 created_at 月份（null/0 跳过）----
    tm = stats["time"]
    assert tm.get("2026-07") == 1, tm
    assert tm.get("2026-08") == 1, tm
    assert sum(tm.values()) == 2, tm  # ④⑤⑥ 无 created_at 不计入

    # ---- graph ----
    g = stats["graph"]
    assert g["nodes_with_edges"] == 1, g  # 只有 id1 有出边
    assert g["total_edges"] == 1, g
    assert g["label_dist_top10"].get("RELATED_TO") == 1, g
    assert g["avg_outdegree"] == round(1 / 6, 2), g
    assert g["hit_count_total"] == 3, g  # id5 hit_count=3
    assert g["top_hit_nodes"][0]["id"] == id5, g
    assert g["top_hit_nodes"][0]["hit_count"] == 3, g

    # 耗时字段存在且非负
    assert isinstance(stats["elapsed_ms"], (int, float))
    assert stats["elapsed_ms"] >= 0


def test_stats_empty_db(iso_store):
    """空库不崩溃：各分节返回 0 值 / 空结构。"""
    stats = compute_stats(iso_store)
    assert stats["totals"]["total_nodes"] == 0, stats
    assert stats["totals"]["active"] == 0
    assert stats["totals"]["outdated"] == 0
    assert stats["totals"]["by_type"] == {}
    assert stats["totals"]["by_domain"] == {}
    assert stats["kinds"] == {}
    assert all(v == 0 for v in stats["importance"].values()), stats
    assert stats["time"] == {}
    assert stats["graph"]["total_edges"] == 0
    assert stats["graph"]["top_hit_nodes"] == []
    assert stats["graph"]["avg_outdegree"] == 0.0
