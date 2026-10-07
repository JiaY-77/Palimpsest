"""写入失败的回滚路径：不留半状态、触发健康探测
==================================================

覆盖 ``mcp_tools/memory.py`` 的 ``mem_ingest`` 异常分支：

  1. ``test_normal_write_succeeds`` —— 对照组：正常写入落库；
  2. ``test_write_failure_leaves_no_partial_state`` —— 事务中途抛异常时节点未落库
     （无半状态），且返回 ``stored: False``；
  3. ``test_write_failure_triggers_health_check`` —— 失败后必须调用
     ``check_db_health``，把「库是否已被污染」作为明确信号回报，而不是让调用方
     在坏库上继续重试。

隔离保证：本文件不自行建库，直接复用 ``conftest.py`` 的全局临时库与 fake
embedder（``DB_PATH`` 在 import 本项目模块前已指向临时目录）。这正是既有测试
的约定——自行改写 ``Config.DB_PATH`` 是无效的，因为 ``mcp_tools._common.store``
是导入时构造的模块级单例，其路径在 import 那一刻已经固定。
"""

import json

import pytest

import mcp_tools.memory as memory_mod
from mcp_tools._common import store
from mcp_tools.memory import mem_ingest


def _node_count() -> int:
    """通过 store 的临时连接读取节点数（TriviumStore 未直接暴露 node_count）。"""
    with store._acquire() as db:
        return db.node_count()


@pytest.fixture
def spy_health_check(monkeypatch):
    """把 ``mcp_tools.memory.check_db_health`` 换成一个可观测的 spy。"""
    calls = {"count": 0}
    real = memory_mod.check_db_health

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(memory_mod, "check_db_health", _spy)
    return calls


def test_normal_write_succeeds():
    """对照组：正常写入应落库，返回 stored:True 与有效 node_id。"""
    result = json.loads(mem_ingest("回滚路径测试-正常写入", type="memory", importance=0.5, domain="test"))
    assert result["stored"] is True
    assert result.get("node_id") is not None


def test_write_failure_leaves_no_partial_state(monkeypatch):
    """事务中途抛异常 → 节点不落库（无半状态），返回 stored:False。"""
    before = _node_count()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟事务中途写入失败：磁盘满/IO 错误")

    # insert_node_tx 是事务内的插入入口，让它抛异常即可走回滚分支
    monkeypatch.setattr(store, "insert_node_tx", _boom)

    result = json.loads(mem_ingest("回滚路径测试-失败写入", type="memory", importance=0.5, domain="test"))

    assert result["stored"] is False
    assert "error" in result or "hint" in result
    # 关键：rollback 后节点数不变
    assert _node_count() == before


def test_write_failure_triggers_health_check(monkeypatch, spy_health_check):
    """写入失败路径必须调用 check_db_health，把库健康状态回报给调用方。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟事务中途写入失败")

    monkeypatch.setattr(store, "insert_node_tx", _boom)

    result = json.loads(mem_ingest("回滚路径测试-健康探测", type="memory", importance=0.5, domain="test"))

    assert result["stored"] is False
    assert spy_health_check["count"] >= 1, "写入失败路径必须调用 check_db_health"
    # 失败结果应携带健康状态字段
    assert "db_healthy" in result
