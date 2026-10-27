"""单例长连接（issue #167）——进程级共享连接的语义锁定。

TriviumDB 是连接级排他的，此前「每操作开-关库」在并发下会静默失败（检索类
失败路径吞异常返回空列表）。单例后每个 (库路径, 访问模式) 进程内只开一条
长连接、全程复用，本文件锁定新语义：

  1. 同进程多次 ``_acquire()`` 拿到同一个底层连接（跨 store 实例也一样）；
  2. N 线程并发检索：零静默空结果（有命中的查询必须命中）；
  3. N 线程并发写：零失败、零重复 ID；
  4. 同库混用 read_only / read_write → 双向明确抛 AccessModeConflictError；
  5. 代理 close() 幂等（不重复释放锁）；shutdown() 后可重新 _acquire()（重建连接）；
  6. load_text_index=True 确实传到 TriviumDB 构造参数；
  7. shutdown() 先 flush 落盘再 close。

隔离保证：每个用例独立临时库（monkeypatch Config.DB_PATH），teardown 统一
reset_connections_for_test()，不触碰 conftest 的会话共享库与生产库。
"""

import threading

import pytest
import triviumdb

from config import Config
from core import trivium_store as ts_mod
from core.trivium_store import AccessModeConflictError, TriviumStore

_FAKE_DIM = 1024  # 与 tests/conftest._fake_embed 的维度一致


def _fake_embed(text):
    from tests.conftest import _fake_embed as fe

    return fe(text)


@pytest.fixture
def iso_env(tmp_path, monkeypatch):
    """独立临时库 + 已播种 10 条记忆的 store；teardown 清空单例注册表。"""
    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "solo.db"))
    ts_mod.reset_connections_for_test()
    store = TriviumStore()
    ids_by_content: dict[str, int] = {}
    for i in range(10):
        content = f"并发检索种子记忆内容第{i}号文档"
        ids_by_content[content] = store.insert_node({"type": "memory", "content": content}, _fake_embed(content))
    yield store, ids_by_content
    ts_mod.reset_connections_for_test()


# ---------------------------------------------------------------------------
# 1. 同一底层连接（验收 4：id 相等）
# ---------------------------------------------------------------------------
def test_acquires_share_one_underlying_connection(iso_env):
    """同进程内多次 _acquire()（跨实例、关窗后再取）都拿到同一个底层连接对象。"""
    store, _ids = iso_env

    a = store._acquire()
    b = store._acquire()  # 同线程重入：RLock 可重入，不死锁
    assert b._wrapped is a._wrapped, "同线程两次 _acquire 必须复用同一条连接"
    a.close()
    b.close()

    c = store._acquire()  # 关窗后再取：连接不关，仍是同一条
    assert c._wrapped is a._wrapped, "close（关窗）后再次 _acquire 必须复用同一条连接"
    c.close()

    other = TriviumStore()  # 不同实例、同路径同模式 → 同一条
    d = other._acquire()
    assert d._wrapped is a._wrapped, "不同 TriviumStore 实例也必须共享同一条底层连接"
    d.close()


# ---------------------------------------------------------------------------
# 2. 并发检索：零静默空结果
# ---------------------------------------------------------------------------
def test_concurrent_search_zero_silent_empty(iso_env):
    """进程内 8 线程并发检索：每个有命中的查询都必须命中，零静默空结果。

    单例前该场景间歇性拿到空列表（并发 open 撞连接级排他 → 检索失败路径
    吞掉异常返回 []）；单例后请求在 _DB_ACCESS_LOCK 上排队，不允许退化。
    """
    store, ids_by_content = iso_env
    contents = list(ids_by_content)
    rounds, workers = 20, 8
    failures: list[tuple] = []

    def _worker(wid: int) -> None:
        for r in range(rounds):
            content = contents[(wid * rounds + r) % len(contents)]
            hits = store.search_similar(_fake_embed(content), top_k=3)
            if not hits:
                failures.append((wid, r, "EMPTY", content))
            elif ids_by_content[content] not in {h["id"] for h in hits}:
                failures.append((wid, r, "MISS", content))

    threads = [threading.Thread(target=_worker, args=(i,), name=f"search-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads), "检索线程 120s 未结束（疑似死锁）"
    assert failures == [], f"并发检索出现静默空结果/丢命中 {len(failures)} 例: {failures[:5]}"


# ---------------------------------------------------------------------------
# 3. 并发写：零失败
# ---------------------------------------------------------------------------
def test_concurrent_writes_zero_failure(iso_env):
    """进程内 8 线程并发写：全部成功、无异常、无重复 ID。"""
    store, _ids = iso_env
    workers, per_thread = 8, 10
    results: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def _worker(wid: int) -> None:
        for r in range(per_thread):
            content = f"并发写入压测内容{wid}组第{r}条"
            try:
                nid = store.insert_node({"type": "memory", "content": content}, _fake_embed(content))
                with lock:
                    results.append(nid)
            except Exception as e:  # noqa: BLE001 —— 记下错误继续打完，最后统一断言
                with lock:
                    errors.append(repr(e))

    threads = [threading.Thread(target=_worker, args=(i,), name=f"write-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads), "写入线程 120s 未结束（疑似死锁）"
    assert errors == [], f"并发写失败 {len(errors)} 条: {errors[:5]}"
    assert len(results) == workers * per_thread, "有写入没返回 ID"
    assert len(set(results)) == len(results), "返回了重复节点 ID"


# ---------------------------------------------------------------------------
# 4. 同库混用访问模式 → 明确报错（双向）
# ---------------------------------------------------------------------------
def test_mixed_access_mode_raises_both_directions(tmp_path, monkeypatch):
    """同库先 read_write 后 read_only（及其反向）都必须抛清晰的 AccessModeConflictError。

    reader 与 writer 在 triviumdb 层互斥，同进程同一库只能存在一条连接；
    静默复用错的连接或静默失败都会掩盖调用方的模式假设（issue #167 约束 3）。
    """

    # —— 方向一：先 read_write 后 read_only ——
    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mix_rw_first.db"))
    ts_mod.reset_connections_for_test()
    try:
        w = TriviumStore()
        w.insert_node({"type": "memory", "content": "种子"}, _fake_embed("种子"))
        ro = TriviumStore(read_only=True)
        with pytest.raises(AccessModeConflictError) as ei:
            ro.get_node(999999)
        msg = str(ei.value)
        assert "同一进程" in msg and "访问模式" in msg, f"消息要说明约束本身: {msg}"
        assert "REST" in msg, f"消息要给出出路（复用实例或改用 REST）: {msg}"
    finally:
        ts_mod.reset_connections_for_test()

    # —— 方向二：先 read_only 后 read_write ——
    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mix_ro_first.db"))
    ts_mod.reset_connections_for_test()
    try:
        raw = triviumdb.TriviumDB(str(tmp_path / "mix_ro_first.db"), dim=_FAKE_DIM, auto_build_quiver=False)
        raw.insert([0.1] * _FAKE_DIM, {"type": "memory", "content": "种子"})
        raw.close()
        ro = TriviumStore(read_only=True)
        assert ro.get_node(1) is not None, "前置：read-only 共享连接已建立"
        with pytest.raises(AccessModeConflictError):
            # 构造（_init_indexes）或首次写请求撞模式，两处任一抛出即契约成立
            w2 = TriviumStore()
            w2.insert_node({"type": "memory", "content": "x"}, _fake_embed("x"))
    finally:
        ts_mod.reset_connections_for_test()


# ---------------------------------------------------------------------------
# 5. close 幂等 / shutdown 后可重建
# ---------------------------------------------------------------------------
def test_proxy_close_is_idempotent_and_releases_for_other_threads(iso_env):
    """代理 close() 幂等：重复 close 不得重复释放锁，其他线程能立刻拿到窗口。"""
    store, _ids = iso_env
    db = store._acquire()
    db.close()
    db.close()  # 第二次起为 no-op（RLock 过量 release 会 RuntimeError）
    db.close()

    done = threading.Event()

    def _worker() -> None:
        w = store._acquire()
        w.close()
        done.set()

    t = threading.Thread(target=_worker, name="close-idempotent-probe")
    t.start()
    assert done.wait(10), "其他线程 10s 内拿不到窗口——锁释放不平衡（漏 release / 多 release）"
    t.join(10)


def test_shutdown_closes_then_reacquire_rebuilds(iso_env):
    """shutdown() 关闭所有共享连接并清空注册表；此后 _acquire() 重新建连接。"""
    store, ids_by_content = iso_env
    a = store._acquire()
    old_conn = a._wrapped
    a.close()

    ts_mod.reset_connections_for_test()  # 等价 shutdown
    assert ts_mod._SHARED_CONNECTIONS == {}, "shutdown 后注册表必须清空"

    b = store._acquire()
    assert b._wrapped is not old_conn, "shutdown 后必须重新建连接，而不是复活已关闭的旧对象"
    b.close()

    some_content, some_id = next(iter(ids_by_content.items()))
    assert store.get_node(some_id) is not None, f"重建连接后数据必须原样可读（{some_content}）"


# ---------------------------------------------------------------------------
# 6. load_text_index=True 传到构造参数（验收 5）
# ---------------------------------------------------------------------------
def test_load_text_index_true_reaches_constructor(iso_env, monkeypatch):
    """共享连接必须以 load_text_index=True 构造（防侧车洗写），auto_build_quiver=False 保持不变。"""
    captured: dict = {}
    real = triviumdb.TriviumDB

    def _spy(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(triviumdb, "TriviumDB", _spy)
    ts_mod.reset_connections_for_test()  # 注册表清空，下一次 _acquire 才会真的构造
    store, _ids = iso_env
    db = store._acquire()
    db.close()

    assert captured.get("load_text_index") is True, "单例必须统一 load_text_index=True（防侧车洗写）"
    assert captured.get("auto_build_quiver") is False
    assert captured.get("access_mode") == "read_write"


# ---------------------------------------------------------------------------
# 7. shutdown：先 flush 后 close
# ---------------------------------------------------------------------------
def test_shutdown_flushes_before_close(tmp_path, monkeypatch):
    """退出落盘（issue #167 约束 4）：read_write 连接必须先 flush() 再 close()。"""
    from unittest.mock import Mock

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "flush.db"))
    calls: list[str] = []
    fake = Mock()
    fake.flush.side_effect = lambda: calls.append("flush")
    fake.close.side_effect = lambda: calls.append("close")
    monkeypatch.setattr(triviumdb, "TriviumDB", lambda *a, **k: fake)

    ts_mod.reset_connections_for_test()
    try:
        TriviumStore()  # 构造把共享连接（fake）登记进注册表
        ts_mod.reset_connections_for_test()  # 走 shutdown 路径
        assert calls == ["flush", "close"], f"必须先 flush 落盘再 close：{calls}"
        assert ts_mod._SHARED_CONNECTIONS == {}
    finally:
        ts_mod.reset_connections_for_test()
