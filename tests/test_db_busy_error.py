"""DatabaseBusyError —— 库被其他进程占用时的 fail-fast 语义。

背景：TriviumDB 对库文件是**连接级排他**的——只要有一个连接持着库，第二个连接
连打开都会失败。issue #167 单例长连接后，同进程内的并发经由共享连接**排队复用
同一条连接**、不再失败；「库被占用」只剩一种来源：**另一个进程**（或本进程内
绕过注册表的裸连接，其排他语义与另一进程等价）。若把占用失败静默吞掉（此前
`_init_indexes()` 的 `except Exception` 正是如此），冲突就会退化成「偶发写失败
→ 文件组残留 → 库从可读写变读不动」。

本文件锁定三件事：
  1. 锁错误被识别并转成带指引的 `DatabaseBusyError`（而非裸 RuntimeError）；
  2. 锁错误**不会**被 `_init_indexes()` 静默降级掉；
  3. 与锁无关的错误**不许**被伪装成 `DatabaseBusyError`（否则会掩盖真 bug）。

隔离保证：每个用例自建临时库并临时改 `Config.DB_PATH`，前后
`reset_connections_for_test()` 清空单例共享连接注册表，用完还原，不触碰
conftest 的会话共享库。
"""

import contextlib
import os

import pytest
import triviumdb

from config import Config
from core import trivium_store as ts_mod
from core.trivium_store import DatabaseBusyError, TriviumStore, _is_db_locked_error


@pytest.fixture
def iso_db_path(tmp_path):
    """把 Config.DB_PATH 指向一个全新的临时库，用完还原（并清空共享连接注册表）。"""
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(str(tmp_path), "busy_test.db")
    ts_mod.reset_connections_for_test()  # 干净起点：注册表不得残留其他库的连接
    try:
        yield Config.DB_PATH
    finally:
        ts_mod.reset_connections_for_test()
        Config.DB_PATH = old


def _hold_db_raw(dim: int):
    """开一条**绕过单例注册表**的裸连接，模拟「另一个进程正占着库」。

    单例长连接后，同进程内经 `_acquire_shared` 的并发会排队复用同一条连接、
    不再失败；裸连接不经过注册表，与另一进程在 triviumdb 连接级排他下等价。
    """
    return triviumdb.TriviumDB(Config.DB_PATH, dim=dim)


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
    """被另一进程占用时抛 DatabaseBusyError，且消息带路径与处理指引。"""

    def test_second_process_holder_raises_database_busy(self, iso_db_path):
        """库被另一个进程持有时（裸连接模拟）→ DatabaseBusyError，而不是静默或裸 RuntimeError。

        单例（issue #167）后「同进程第二个 _acquire」复用共享连接、不再失败；
        跨进程独占语义不变，用绕过注册表的裸连接等价模拟另一进程。
        """
        store = TriviumStore()  # 构造即建共享连接（_init_indexes）
        ts_mod.reset_connections_for_test()  # 关掉共享连接，让裸连接能独占开库
        holder = _hold_db_raw(store.dim)
        try:
            with pytest.raises(DatabaseBusyError):
                store._acquire()  # 重开必撞连接级排他
        finally:
            with contextlib.suppress(Exception):
                holder.close()

    def test_busy_message_includes_path_and_guidance(self, iso_db_path):
        """异常消息要能直接指导排障：含库路径 + 下一步怎么做。"""
        store = TriviumStore()
        ts_mod.reset_connections_for_test()
        holder = _hold_db_raw(store.dim)
        try:
            with pytest.raises(DatabaseBusyError) as excinfo:
                store._acquire()
        finally:
            with contextlib.suppress(Exception):
                holder.close()

        msg = str(excinfo.value)
        assert "busy_test.db" in msg, "消息应含库路径，便于定位是哪个库"
        assert "127.0.0.1:8090" in msg, "消息应指明可改走 REST 服务"
        assert "只允许一个进程" in msg, "消息应说明根本约束"

    def test_busy_error_is_not_swallowed_by_init_indexes(self, iso_db_path):
        """_init_indexes 只该降级「索引问题」，不许吞掉「库被占用」。"""
        ts_mod.reset_connections_for_test()
        holder = _hold_db_raw(dim=1024)  # dim 任意：占住库即可，busy 与维度无关
        try:
            # 若 _init_indexes 仍在静默降级，这里会构造成功而不抛 —— 用例即红
            with pytest.raises(DatabaseBusyError):
                TriviumStore()
        finally:
            with contextlib.suppress(Exception):
                holder.close()


class TestBackwardCompatibility:
    """`DatabaseBusyError` 必须保持既有的 `RuntimeError` 契约。

    既有代码与测试（tests/test_concurrency.py）按 `RuntimeError` 捕获锁冲突；
    引入精确类型不能把它们的 except 打空。
    """

    def test_is_runtime_error(self):
        assert issubclass(DatabaseBusyError, RuntimeError)

    def test_catchable_as_runtime_error(self, iso_db_path):
        ts_mod.reset_connections_for_test()
        holder = _hold_db_raw(dim=1024)
        try:
            with pytest.raises(RuntimeError):
                TriviumStore()
        finally:
            with contextlib.suppress(Exception):
                holder.close()


class TestNoFalsePositive:
    """与锁无关的错误必须原样抛出，不能被伪装成「库被占用」。"""

    def test_unrelated_error_passes_through(self, iso_db_path, monkeypatch):
        store = TriviumStore()
        # 构造已把真实连接登记进注册表；清空后 _acquire 才会走到被 mock 的开库点
        ts_mod.reset_connections_for_test()

        def _boom(*args, **kwargs):
            raise ValueError("某个与锁无关的错误")

        monkeypatch.setattr(ts_mod.triviumdb, "TriviumDB", _boom)
        with pytest.raises(ValueError):
            store._acquire()
