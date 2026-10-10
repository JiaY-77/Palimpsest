"""bi-temporal 二期：as_of 历史视图查询测试。

覆盖三个层次：

A. core.bitemporal.is_valid_at 纯函数契约（无需隔离）
   - as_of=None → 恒 True（关闭）
   - 非事实类型 → 恒 True（不受影响）
   - valid_at 在 as_of 之后 → False（当时还没为真）
   - invalid_at 在 as_of 之前（或恰在那一刻）→ False（当时已不再为真）
   - 字段缺失兜底：valid_at 缺失 → True；invalid_at 缺失 → True
   - 边界：valid_at == as_of → True；invalid_at == as_of → False

B. 端到端（mem_search / mem_recent 的 as_of 过滤）
   - 写一条事实 → 被新事实取代（旧标 outdated + invalid_at）
   - 用旧 T 查 as_of → 旧节点返回（当时为真），新节点不返回（当时还没为真）
   - 用新 T 查 as_of → 新节点返回，旧节点不返回（已失效）
   - as_of=None → 行为不变（默认只回当前有效）

隔离保证：B 段会真的写库，故用**独立临时库**（自建 TriviumStore + 自指
Config.DB_PATH + 重指 mcp_tools 共享单例），照 tests/test_bitemporal.py 的
iso_store 模式。A 段是纯函数，无需隔离。
"""

import contextlib
import os
import shutil
import tempfile

import pytest

# ---------------------------------------------------------------------------
# A. 纯函数契约
# ---------------------------------------------------------------------------


def test_as_of_none_always_true():
    """as_of=None → 恒 True（关闭 as_of）。"""
    from core.bitemporal import is_valid_at

    # 字段什么样的都恒 True
    assert is_valid_at({"type": "memory", "valid_at": 100.0, "invalid_at": 200.0}, None) is True
    assert is_valid_at({"type": "memory"}, None) is True
    assert is_valid_at({}, None) is True


def test_non_fact_type_unaffected():
    """非事实类型不受 as_of 影响（恒 True，避免静默丢弃）。"""
    from core.bitemporal import is_valid_at

    # kb_chunk / record 等无世界时间语义
    assert is_valid_at({"type": "kb_chunk", "valid_at": 999.0}, 100.0) is True
    assert is_valid_at({"type": "record", "invalid_at": 50.0}, 100.0) is True


def test_valid_at_after_as_of_is_false():
    """as_of 早于 valid_at → 当时还没为真 → False。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory", "valid_at": 100.0}, 50.0) is False


def test_invalid_at_before_as_of_is_false():
    """invalid_at 在 as_of 之前 → 当时已不再为真 → False。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory", "valid_at": 10.0, "invalid_at": 50.0}, 100.0) is False


def test_valid_window_passes():
    """valid_at <= as_of < invalid_at → 当时为真 → True。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory", "valid_at": 10.0, "invalid_at": 200.0}, 100.0) is True


def test_boundary_valid_at_equals_as_of():
    """边界：valid_at == as_of → 恰在开始为真之时 → True。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory", "valid_at": 100.0}, 100.0) is True


def test_boundary_invalid_at_equals_as_of():
    """边界：invalid_at == as_of → 恰在停止为真之时 → False（此刻已不为真）。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory", "valid_at": 10.0, "invalid_at": 100.0}, 100.0) is False


def test_missing_valid_at_treated_as_always_true():
    """历史数据无 valid_at → 视为「一直在为真」→ 不过滤（保守）。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory"}, 100.0) is True
    # 但 invalid_at 仍生效：曾在 as_of 前失效 → False
    assert is_valid_at({"type": "memory", "invalid_at": 50.0}, 100.0) is False


def test_missing_invalid_at_treated_as_still_true():
    """无 invalid_at → 视为「仍为真」→ 通过。"""
    from core.bitemporal import is_valid_at

    assert is_valid_at({"type": "memory", "valid_at": 10.0}, 100.0) is True


# ---------------------------------------------------------------------------
# 独立库 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与 conftest 共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_asof_iso_")
    from config import Config as C
    from mcp_tools import store as shared_store

    old_db = C.DB_PATH
    old_shared = shared_store.db_path
    C.DB_PATH = os.path.join(tmp, "iso.db")
    shared_store.db_path = C.DB_PATH
    from core.trivium_store import TriviumStore

    s = TriviumStore()
    try:
        yield s
    finally:
        C.DB_PATH = old_db
        shared_store.db_path = old_shared
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _mk(store, content, type, domain="", importance=0.5, **extra):
    return store.insert_node(
        {"type": type, "content": content, "importance": importance, "domain": domain, **extra},
        store.embed_text(content),
    )


# ---------------------------------------------------------------------------
# B. 端到端
# ---------------------------------------------------------------------------


def test_mem_search_as_of_history_view(iso_store):
    """写事实 → 被取代 → 用旧/新 T 分别回看。"""
    import json

    from mcp_tools import memory as mem

    s = iso_store
    # 旧事实：valid_at=1000
    old = _mk(s, "as_of 测试：配置项 X 的取值是甲", "memory", "asof_test", valid_at=1000.0)
    # 新事实：valid_at=2000，取代旧事实（模拟冲突检测的标记结果）
    new = _mk(s, "as_of 测试：配置项 X 的取值是甲", "memory", "asof_test", valid_at=2000.0)
    # 手工标旧为 outdated + 补 invalid_at（模拟 resolve_conflict 的效果）
    old_payload = s.get_node(old)["payload"]
    old_payload["status"] = "outdated"
    old_payload["invalid_at"] = 2000.0
    old_payload["expired_at"] = 2000.0
    s.update_payload(old, old_payload)

    q = "配置项 X 的取值"
    # 用旧 T=1500 回看 → 旧节点当时为真（valid_at 1000 <= 1500 < invalid_at 2000）
    r_old = json.loads(mem.mem_search(q, domain="asof_test", as_of=1500.0, tier=""))
    ids_old = {it["id"] for it in r_old["results"]}
    assert old in ids_old, "旧 T 应回看得到旧节点"
    assert new not in ids_old, "旧 T 时新节点还没为真"

    # 用新 T=2500 回看 → 新节点为真，旧节点已失效
    r_new = json.loads(mem.mem_search(q, domain="asof_test", as_of=2500.0, tier=""))
    ids_new = {it["id"] for it in r_new["results"]}
    assert new in ids_new, "新 T 应回看得到新节点"
    assert old not in ids_new, "新 T 时旧节点已不再为真"


def test_mem_search_as_of_none_unchanged(iso_store):
    """as_of=None → 默认只回当前有效（旧节点被 status 过滤）。"""
    import json

    from mcp_tools import memory as mem

    s = iso_store
    await_x = _mk(s, "as_of 回归测试：某条被取代的旧记忆内容", "memory", "asof_reg")
    new_x = _mk(s, "as_of 回归测试：某条被取代的旧记忆内容", "memory", "asof_reg")
    p = s.get_node(await_x)["payload"]
    p["status"] = "outdated"
    s.update_payload(await_x, p)

    r = json.loads(mem.mem_search("某条被取代的旧记忆内容", domain="asof_reg", tier=""))
    ids = {it["id"] for it in r["results"]}
    assert await_x not in ids, "默认应过滤 outdated 旧节点"
    assert new_x in ids


def test_mem_recent_as_of_filter(iso_store):
    """mem_recent 的 as_of 过滤。"""
    import json

    from mcp_tools import memory as mem

    s = iso_store
    a = _mk(s, "recent as_of 测试事实甲", "memory", "asof_recent", valid_at=1000.0)
    b = _mk(s, "recent as_of 测试事实乙", "memory", "asof_recent", valid_at=3000.0)

    # T=2000：只有 a 为真（b 还没开始）
    r = json.loads(mem.mem_recent(domain="asof_recent", as_of=2000.0))
    ids = {it["id"] for it in r["results"]}
    assert a in ids
    assert b not in ids

    # T=4000：两条都为真
    r2 = json.loads(mem.mem_recent(domain="asof_recent", as_of=4000.0))
    ids2 = {it["id"] for it in r2["results"]}
    assert a in ids2 and b in ids2


def test_mem_retrieve_as_of(iso_store):
    """mem_retrieve 的 as_of 过滤。"""
    import json

    from mcp_tools import memory as mem

    s = iso_store
    x = _mk(s, "retrieve as_of 测试内容丙", "memory", "asof_ret", valid_at=5000.0)

    # T=1000：还没为真 → 不返回
    r = json.loads(mem.mem_retrieve("retrieve as_of 测试内容丙", domain="asof_ret", as_of=1000.0))
    assert {it["id"] for it in r["results"]} == set()
    # T=6000：为真 → 返回
    r2 = json.loads(mem.mem_retrieve("retrieve as_of 测试内容丙", domain="asof_ret", as_of=6000.0))
    assert x in {it["id"] for it in r2["results"]}


def test_mem_hybrid_search_as_of(iso_store):
    """mem_hybrid_search 的 as_of 过滤（语义侧 + FTS 侧同口径）。"""
    import json

    from mcp_tools import memory as mem

    s = iso_store
    y = _mk(s, "hybrid as_of 测试内容丁", "memory", "asof_hyb", valid_at=7000.0)

    r = json.loads(mem.mem_hybrid_search("hybrid as_of 测试内容丁", domain="asof_hyb", as_of=1000.0, tier=""))
    ids = {it["id"] for it in r["results"]}
    assert y not in ids, "FTS 侧与语义侧都应受 as_of 约束"

    r2 = json.loads(mem.mem_hybrid_search("hybrid as_of 测试内容丁", domain="asof_hyb", as_of=8000.0, tier=""))
    assert y in {it["id"] for it in r2["results"]}
