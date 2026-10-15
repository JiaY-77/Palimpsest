"""
写入判定观测（write_judgment）测试
==================================
覆盖 core/judgment_metrics.py 的 record_ingest / aggregate，及其与
mem_ingest / compute_stats 的集成：
  - 文件缺失、坏行、窗口过滤、类型白名单均降级不抛异常；
  - 三个判定率（related / explicit_replace / high_similar_unreplaced）口径正确；
  - 集成链路：mem_ingest 成功写入后观测文件恰多一行、compute_stats 带 write_judgment。
隔离保证：独立临时库 + 临时改 Config.DB_PATH（集成测试另重指
mcp_tools._common.store.db_path 模块级单例），finally 还原；观测文件只落在
临时目录，绝不污染真实 data/ 目录；embedding 走 conftest 的 fake embedder。
"""

import contextlib
import json
import os
import shutil
import tempfile
import time

import pytest

from config import Config
from core.judgment_metrics import aggregate, record_ingest
from core.stats import compute_stats


@pytest.fixture
def iso_dir():
    """独立临时目录：观测文件 / 假库路径全部落在这里，用完即删。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_judgment_")
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _db(tmp: str) -> str:
    """临时库路径（文件不必存在，观测文件只用它的 dirname）。"""
    return os.path.join(tmp, "judgment.db")


def _row(ts, type_="memory", related=0, replaced=0, outdated=0) -> dict:
    """构造一行观测记录（字段与 record_ingest 输出一致）。"""
    return {
        "ts": ts,
        "node_id": 1,
        "type": type_,
        "domain": "general",
        "related_count": related,
        "replaced_count": replaced,
        "outdated_count": outdated,
    }


def _append_raw(path: str, text: str) -> None:
    """往观测文件裸追加一行文本（用于构造坏行 / 手工 ts）。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")


# ---- 1. 文件不存在 → 不抛异常、file_exists=False、writes=0 ----


def test_aggregate_missing_file_returns_zero_structure(iso_dir):
    """观测文件不存在：aggregate 不抛异常，返回全 0 结构 + file_exists=False。"""
    res = aggregate(db_path=_db(iso_dir))
    assert res["file_exists"] is False
    assert res["writes"] == 0
    assert res["outdated_in_window"] == 0
    assert res["explicit_replace_rate"] == 0.0
    assert res["related_hit_rate"] == 0.0
    assert res["high_similar_unreplaced_rate"] == 0.0
    assert set(res["by_type"]) == {"memory", "task", "plan"}
    assert all(v["writes"] == 0 for v in res["by_type"].values())


# ---- 2. 三行记录 → writes=3 与三个判定率口径 ----


def test_aggregate_rates_over_three_rows(iso_dir):
    """A: related=2,replaced=0；B: 0,0；C: related=1,replaced=1 → 三个率的分子分母正确。"""
    record_ingest(1, "memory", "general", related_count=2, replaced_count=0, outdated_count=0, db_path=_db(iso_dir))
    record_ingest(2, "memory", "general", related_count=0, replaced_count=0, outdated_count=0, db_path=_db(iso_dir))
    record_ingest(3, "memory", "general", related_count=1, replaced_count=1, outdated_count=1, db_path=_db(iso_dir))

    res = aggregate(days=30, db_path=_db(iso_dir))
    assert res["file_exists"] is True
    assert res["writes"] == 3
    assert abs(res["related_hit_rate"] - 2 / 3) < 1e-9
    assert abs(res["high_similar_unreplaced_rate"] - 1 / 3) < 1e-9
    assert abs(res["explicit_replace_rate"] - 1 / 3) < 1e-9
    assert res["outdated_in_window"] == 1
    assert res["by_type"]["memory"]["writes"] == 3
    assert abs(res["by_type"]["memory"]["related_hit_rate"] - 2 / 3) < 1e-9
    assert abs(res["by_type"]["memory"]["high_similar_unreplaced_rate"] - 1 / 3) < 1e-9
    # 观测文件必须落在临时目录（不污染真实 data/）
    assert os.path.isfile(os.path.join(iso_dir, "judgment_metrics.jsonl"))


# ---- 3. 窗口过滤：40 天前的行 days=30 不计、days=60 计入 ----


def test_aggregate_window_filtering(iso_dir):
    """窗口过滤：ts 为 40 天前的行在 30 天窗外、60 天窗内。"""
    path = os.path.join(iso_dir, "judgment_metrics.jsonl")
    _append_raw(path, json.dumps(_row(time.time() - 40 * 86400, related=1), ensure_ascii=False))

    assert aggregate(days=30, db_path=_db(iso_dir))["writes"] == 0
    assert aggregate(days=60, db_path=_db(iso_dir))["writes"] == 1


# ---- 4. 坏行跳过：非 JSON 行 + ts 非数值行 ----


def test_aggregate_skips_bad_lines(iso_dir):
    """坏行（not json / ts 为字符串）跳过继续，不抛异常且不计入。"""
    path = os.path.join(iso_dir, "judgment_metrics.jsonl")
    _append_raw(path, "not json")
    _append_raw(path, json.dumps({"ts": "x"}))

    res = aggregate(db_path=_db(iso_dir))
    assert res["file_exists"] is True
    assert res["writes"] == 0


# ---- 5. 类型白名单：type="record" 不计入 ----


def test_aggregate_whitelist_excludes_record(iso_dir):
    """白名单过滤：type=record 的行不计入 writes，也不进 by_type。"""
    record_ingest(9, "record", "general", related_count=1, replaced_count=1, outdated_count=1, db_path=_db(iso_dir))

    res = aggregate(db_path=_db(iso_dir))
    assert res["writes"] == 0
    assert res["outdated_in_window"] == 0
    assert "record" not in res["by_type"]


# ---- 6. record_ingest 写失败静默降级、不抛异常 ----


def test_record_ingest_write_failure_is_silent(iso_dir):
    """db_path 的 dirname 指向一个已存在的普通文件（当目录用）→ 追加必然失败，但不抛。"""
    blocked = os.path.join(iso_dir, "blocked")
    with open(blocked, "w", encoding="utf-8") as f:
        f.write("x")

    out = record_ingest(
        1,
        "memory",
        "general",
        related_count=1,
        replaced_count=0,
        outdated_count=0,
        db_path=os.path.join(blocked, "x.db"),
    )
    assert out is None
    assert not os.path.exists(os.path.join(iso_dir, "judgment_metrics.jsonl"))


# ---- 7. 集成：mem_ingest 成功一次 → 观测文件恰多一行 ----


@pytest.fixture
def ingest_env():
    """独立临时库 + 重指模块级 store 单例（只改 Config 不够），finally 全部还原。"""
    import mcp_tools._common as _common
    from mcp_tools.memory import mem_ingest  # 确保工具模块已加载（store 单例随其导入初始化）

    tmp = tempfile.mkdtemp(prefix="palimpsest_judgment_ingest_")
    old_cfg = Config.DB_PATH
    old_store_db = _common.store.db_path
    Config.DB_PATH = os.path.join(tmp, "ingest.db")
    _common.store.db_path = Config.DB_PATH
    try:
        yield tmp, mem_ingest, _common.store
    finally:
        Config.DB_PATH = old_cfg
        _common.store.db_path = old_store_db
        with contextlib.suppress(Exception):
            _common.store._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_mem_ingest_records_one_observation_line(ingest_env):
    """mem_ingest 成功写入一次 → 观测文件恰多一行，7 个字段值与返回结果一致。"""
    tmp, mem_ingest, _store = ingest_env
    res = json.loads(mem_ingest("写入判定观测集成测试：窗边的蓝色雨伞", type="memory", domain="general"))
    assert res["stored"] is True, res

    path = os.path.join(tmp, "judgment_metrics.jsonl")
    assert os.path.isfile(path), "观测文件应落在临时库同目录"
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert len(rows) == 1, rows
    row = rows[0]
    assert set(row) == {
        "ts",
        "node_id",
        "type",
        "domain",
        "related_count",
        "replaced_count",
        "outdated_count",
    }
    assert isinstance(row["ts"], (int, float))
    assert row["node_id"] == res["node_id"]
    assert row["type"] == "memory"
    assert row["domain"] == res["domain"]
    assert row["related_count"] == len(res["related_ids"])
    assert row["replaced_count"] == 0
    assert row["outdated_count"] == len(res["outdated_ids"])


# ---- 8. 集成：compute_stats 返回含 write_judgment，writes >= 1 ----


def test_compute_stats_includes_write_judgment(ingest_env):
    """同一 store 上 compute_stats 输出 write_judgment 分节，且观测文件已在窗口内被计入。"""
    tmp, mem_ingest, store = ingest_env
    res = json.loads(mem_ingest("写入判定观测盘点测试：山间的晨雾", type="memory", domain="general"))
    assert res["stored"] is True, res

    stats = compute_stats(store)
    assert "write_judgment" in stats
    wj = stats["write_judgment"]
    assert wj["writes"] >= 1, wj
    assert wj["file_exists"] is True, wj
    assert os.path.samefile(os.path.dirname(wj["file"]), tmp), wj["file"]
    assert wj["by_type"]["memory"]["writes"] >= 1, wj
