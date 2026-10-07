"""generation 损坏必须 fail-fast，不得被 ``_init_indexes`` 静默降级
==================================================================

背景：``core/trivium_store.py`` 的 ``_init_indexes`` 对「索引创建失败」静默降级、
不阻塞启动；但「存储 generation 损坏」属致命状态——若一并降级，``startup-check``
会全绿而库实际不可用（2026-10 一起存储事故的延误根因）。

本文件锁定三件事：
  1. 损坏措辞被 ``_is_db_corrupt_error`` 识别（含实测真实中英措辞）；
  2. 损坏**不会**被 ``_init_indexes()`` 静默降级，而是 fail-fast 抛出；
  3. 「库被占用」/ 无关错误不被误判成损坏。

隔离保证：每个涉及真实库的用例自建临时库并临时改 ``Config.DB_PATH``，用完还原。
"""

import os

import pytest

from config import Config
from core import trivium_store as ts_mod
from core.trivium_store import TriviumStore, _is_db_corrupt_error

# 实测真实损坏消息（2026-10 事故现场措辞）
_REAL_CORRUPT = (
    "文件损坏 (Corrupted file): 拒绝不完整的 .tdb/.vec generation："
    ".flush_ok 缺失或不匹配，WAL 不能证明基础向量可完整重建"
)


@pytest.fixture
def iso_db_path(tmp_path):
    """把 Config.DB_PATH 指向全新临时库，用完还原。"""
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(str(tmp_path), "corrupt_test.db")
    try:
        yield Config.DB_PATH
    finally:
        Config.DB_PATH = old


class TestCorruptRecognition:
    """损坏识别：按消息标记判断，不靠异常类型（triviumdb 损坏抛裸 RuntimeError）。"""

    def test_recognizes_real_message(self):
        assert _is_db_corrupt_error(RuntimeError(_REAL_CORRUPT))

    def test_recognizes_english_variants(self):
        assert _is_db_corrupt_error(RuntimeError("(Immutable generation invalid): foo"))
        assert _is_db_corrupt_error(RuntimeError("(Graph block generation mismatch)"))
        assert _is_db_corrupt_error(RuntimeError("(Property index does not match the main database generation)"))

    def test_ignores_busy_and_unrelated(self):
        assert not _is_db_corrupt_error(RuntimeError("Database locked: already opened"))
        assert not _is_db_corrupt_error(RuntimeError("数据库已锁定"))
        assert not _is_db_corrupt_error(ValueError("节点 ID=1 不存在"))


class TestInitIndexesFailsFast:
    """损坏必须冒泡，不被 ``_init_indexes`` 的静默降级吞掉。"""

    def test_corruption_not_swallowed(self, iso_db_path, monkeypatch):
        store = TriviumStore()

        def _boom(*args, **kwargs):
            raise RuntimeError(_REAL_CORRUPT)

        monkeypatch.setattr(ts_mod.triviumdb, "TriviumDB", _boom)
        # 若 _init_indexes 仍静默降级，这里不会抛 —— 用例即红
        with pytest.raises(RuntimeError):
            store._init_indexes()

    def test_benign_index_error_still_degrades(self, iso_db_path, monkeypatch):
        """基准：与损坏无关的索引错误仍应静默降级（不回归既有行为）。"""
        store = TriviumStore()

        def _boom(*args, **kwargs):
            raise RuntimeError("某个无关的索引创建失败")

        monkeypatch.setattr(ts_mod.triviumdb, "TriviumDB", _boom)
        store._init_indexes()  # 不该抛
