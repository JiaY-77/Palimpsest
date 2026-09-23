"""DatabaseBusyError —— 库被其他进程占用时的 fail-fast 语义。

背景：TriviumDB 对库文件是**连接级排他**的——只要有一个连接持着库，第二个连接
连打开都会失败。Palimpsest 采用「每操作开-关库」模式，所以冲突是**间歇性**的：
其他入口有时能抢到空闲窗口、有时不能。若把这种失败静默吞掉（此前
`_init_indexes()` 的 `except Exception` 正是如此），冲突就会退化成
「偶发写失败 → 文件组残留 → 库从可读写变读不动」。

本文件锁定三件事：
  1. 锁错误被识别并转成带指引的 `DatabaseBusyError`（而非裸 RuntimeError）；
  2. 锁错误**不会**被 `_init_indexes()` 静默降级掉；
  3. 与锁无关的错误**不许**被伪装成 `DatabaseBusyError`（否则会掩盖真 bug）。

隔离保证：每个用例自建临时库并临时改 `Config.DB_PATH`，用完还原，不触碰
conftest 的会话共享库。
"""

import contextlib
import os

import pytest

from config import Config
from core import trivium_store as ts_mod
from core.trivium_store import DatabaseBusyError, TriviumStore, _is_db_locked_error


@pytest.fixture
def iso_db_path(tmp_path):
    """把 Config.DB_PATH 指向一个全新的临时库，用完还原。"""
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(str(tmp_path), "busy_test.db")
    try:
        yield Config.DB_PATH
    finally:
        Config.DB_PATH = old


class TestLockErrorRecognition:
    """锁错误识别：按消息标记判断，不靠异常类型。"""

    def test_recognizes_english_marker(self):
        assert _is_db_locked_error(RuntimeError("Database locked: already opened with an incompatible access mode"))

    def test_recognizes_chinese_marker(self):
        assert _is_db_locked_error(RuntimeError("数据库已锁定 (Database locked)"))

    def test_ignores_unrelated_error(self):
        assert not _is_db_locked_error(ValueError("节点 ID=1 不存在"))
        assert not _is_db_locked_error(OSError("磁盘只读"))


class TestAcquireFailsFast:
    """被占用时抛 DatabaseBusyError，且消息带路径与处理指引。"""

    def test_second_connection_raises_database_busy(self, iso_db_path):
        """同一库上的第二个连接 → DatabaseBusyError（而不是静默或裸 RuntimeError）。"""
        holder = TriviumStore()
        held = holder._acquire()  # 手动持有一个连接，模拟「另一个进程正占着库」
        try:
            with pytest.raises(DatabaseBusyError):
                TriviumStore()  # 构造即触发 _init_indexes → _acquire
        finally:
            with contextlib.suppress(Exception):
                held.close()

    def test_busy_message_includes_path_and_guidance(self, iso_db_path):
        """异常消息要能直接指导排障：含库路径 + 下一步怎么做。"""
        holder = TriviumStore()
        held = holder._acquire()
        try:
            with pytest.raises(DatabaseBusyError) as excinfo:
                holder._acquire()
        finally:
            with contextlib.suppress(Exception):
                held.close()

        msg = str(excinfo.value)
        assert "busy_test.db" in msg, "消息应含库路径，便于定位是哪个库"
        assert "127.0.0.1:8090" in msg, "消息应指明可改走 REST 服务"
        assert "只允许一个进程" in msg, "消息应说明根本约束"

    def test_busy_error_is_not_swallowed_by_init_indexes(self, iso_db_path):
        """_init_indexes 只该降级「索引问题」，不许吞掉「库被占用」。"""
        holder = TriviumStore()
        held = holder._acquire()
        try:
            # 若 _init_indexes 仍在静默降级，这里会构造成功而不抛 —— 用例即红
            with pytest.raises(DatabaseBusyError):
                TriviumStore()
        finally:
            with contextlib.suppress(Exception):
                held.close()


class TestBackwardCompatibility:
    """`DatabaseBusyError` 必须保持既有的 `RuntimeError` 契约。

    既有代码与测试（tests/test_concurrency.py）按 `RuntimeError` 捕获锁冲突；
    引入精确类型不能把它们的 except 打空。
    """

    def test_is_runtime_error(self):
        assert issubclass(DatabaseBusyError, RuntimeError)

    def test_catchable_as_runtime_error(self, iso_db_path):
        holder = TriviumStore()
        held = holder._acquire()
        try:
            with pytest.raises(RuntimeError):
                TriviumStore()
        finally:
            with contextlib.suppress(Exception):
                held.close()


class TestNoFalsePositive:
    """与锁无关的错误必须原样抛出，不能被伪装成「库被占用」。"""

    def test_unrelated_error_passes_through(self, iso_db_path, monkeypatch):
        store = TriviumStore()

        def _boom(*args, **kwargs):
            raise ValueError("某个与锁无关的错误")

        monkeypatch.setattr(ts_mod.triviumdb, "TriviumDB", _boom)
        with pytest.raises(ValueError):
            store._acquire()
