"""服务守护必须区分「启动中」与「已死」，不得重复拉起
=====================================================

背景：TriviumDB 以**独占**方式打开数据库文件——一个进程持有时，第二个进程
连只读都打不开。Palimpsest 采用「每操作开-关库」模式，因此同时跑两个 REST
进程会争抢库文件。一次抢输的写入被中途打断，会让 storage generation 不一致
（``.flush_ok`` 与 ``.vec``/``.pld`` 对不上），库从「可读写」退化为「读不动」。

一个只看 HTTP 的守护会制造这种场景：服务**正在启动**（加载 embedding、建索引）
时 HTTP 无响应，于是被判定为「已死」，守护再拉起一个进程——旧进程其实还活着。

``scripts/service_guard.py`` 为此加了两道防线，本文件锁定它们的契约：

  1. ``_service_up`` 在端口被占用时即判定「在跑」（哪怕 HTTP 还没响应）；
  2. 端口探测必须**先于** HTTP 探测——HTTP 探测会阻塞并在监听 backlog 上占位，
     先跑它会让端口探测自身超时（实测的 false-negative）；
  3. ``_within_grace`` 在启动宽限窗口内阻止再次拉起。

隔离保证：用例只使用临时端口与本机回环，不触碰真实服务；状态目录指向 tmp。
"""

import importlib.util
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "service_guard.py"


@pytest.fixture(scope="module")
def guard():
    """加载 service_guard 模块，状态目录隔离到临时目录。"""
    tmp = Path(tempfile.mkdtemp(prefix="guard_test_"))
    os.environ["HERMES_HOME"] = str(tmp)
    spec = importlib.util.spec_from_file_location("service_guard", _GUARD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    """取一个当前空闲的端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening_stub(port: int, backlog: int = 5):
    """建一个只监听、不回应 HTTP 的假服务（模拟「启动中」）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(backlog)
    return srv


def test_port_listening_detects_bound_socket(guard):
    """只监听不响应的端口应被判定为「有进程占用」。"""
    port = _free_port()
    srv = _listening_stub(port)
    try:
        assert guard._port_listening("127.0.0.1", port) is True
    finally:
        srv.close()


def test_port_listening_false_for_free_port(guard):
    """空闲端口应被判为未被占用。"""
    port = _free_port()
    assert guard._port_listening("127.0.0.1", port) is False


def test_service_up_true_while_starting(guard):
    """核心契约：服务启动中（只监听、HTTP 不响应）必须判为 up。

    这是防止重复拉起的关键——旧实现只用 HTTP 探测，此场景会误判为死。
    """
    port = _free_port()
    srv = _listening_stub(port)
    try:
        url = f"http://127.0.0.1:{port}/"
        # 旧判据（仅 HTTP）确实会误判
        assert guard._http_alive(url) is False
        # 新判据（先端口后 HTTP）正确识别
        assert guard._service_up(url, "127.0.0.1", port) is True
    finally:
        srv.close()


def test_service_up_false_when_port_free(guard):
    """端口空闲时应判为 down，允许拉起。"""
    port = _free_port()
    assert guard._service_up(f"http://127.0.0.1:{port}/", "127.0.0.1", port) is False


def test_port_probe_precedes_http_probe(guard):
    """顺序契约：端口探测必须先执行，且不得被 HTTP 探测拖垮。

    若先跑 HTTP 探测，它会阻塞直到超时；端口探测必须能在同一目标上
    独立、可重复地得出正确结论。用一个**会 accept 的**桩（贴近真实服务）
    连续探测多次，确认协议栈层面的 backpressure 不会让结论翻转。
    """
    port = _free_port()
    srv = _listening_stub(port, backlog=5)
    # 用一个接受随后立即关闭连接的线程模拟真实 accept 行为
    stop = threading.Event()

    def _accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except TimeoutError:
                continue
            except OSError:
                break

    worker = threading.Thread(target=_accept_loop, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        for _ in range(3):
            assert guard._service_up(url, "127.0.0.1", port) is True
    finally:
        stop.set()
        worker.join(timeout=1)
        srv.close()


def test_within_grace_blocks_immediate_restart(guard):
    """启动宽限窗口内应阻止再次拉起。"""
    service = "rest"
    guard.STARTUP_GRACE_SECONDS = 90
    assert guard._within_grace(service) is False
    guard._record_start(service)
    assert guard._within_grace(service) is True


def test_grace_expiry_allows_restart(guard):
    """宽限窗口过期后应放行。"""
    service = "ollama"
    guard.STARTUP_GRACE_SECONDS = 0.3
    guard._record_start(service)
    time.sleep(0.5)
    assert guard._within_grace(service) is False


def test_record_event_appends_jsonl(guard):
    """事件记录应写入一行 JSONL。"""
    guard._record_event("rest", "test_marker")
    events = Path(guard.EVENTS)
    assert events.exists()
    assert "test_marker" in events.read_text(encoding="utf-8")
