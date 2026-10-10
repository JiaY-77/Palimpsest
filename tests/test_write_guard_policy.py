"""写入口约束护栏（write-guard policy）测试。

覆盖三个层次：

A. core.policy 纯函数契约（无需隔离）
   - is_protected：type=rule / payload.protected=True 命中；普通节点不命中
   - check_ingest：空内容 / 超长拒绝（与既有 MEM_INGEST_MAX_LENGTH 一致）
   - check_ingest：分级上限 warn 模式只 warning 不拒；enforce 模式拒
   - get_policy_mode：非法值回退 warn
   - check_protected_overwrite：受保护 → 文案；非受保护 → None

B. 只读保护门（core.conflict.resolve_conflict）
   - warn 模式：受保护旧节点**照常**标 outdated，且返回 policy_warnings
   - enforce 模式：受保护旧节点**跳过** outdated，不建 REVISED_BY 边

C. 端到端（mem_ingest）
   - warn 模式默认行为与改动前等价（写入成功、行为不变）
   - 受保护候选被覆盖时结果含 policy_warnings

隔离保证：B / C 段会真的写库，故用**独立临时库**（自建 TriviumStore + 自指
Config.DB_PATH + 重指 mcp_tools 共享单例），绝不写 conftest 的会话共享库——
照 tests/test_bitemporal.py 的 iso_store 模式。A 段是纯函数，无需隔离。
"""

import contextlib
import os
import shutil
import tempfile

import pytest

from config import Config

# ---------------------------------------------------------------------------
# A. 纯函数契约
# ---------------------------------------------------------------------------


def test_is_protected_rule_type():
    from core.policy import is_protected

    assert is_protected({"type": "rule"}) is True


def test_is_protected_flag():
    from core.policy import is_protected

    assert is_protected({"type": "memory", "protected": True}) is True


def test_is_protected_plain_node():
    from core.policy import is_protected

    assert is_protected({"type": "memory"}) is False
    assert is_protected({}) is False
    # protected 显式 False 不算保护
    assert is_protected({"type": "memory", "protected": False}) is False


def test_get_policy_mode_default_and_fallback(monkeypatch):
    from core import policy

    monkeypatch.setattr(Config, "POLICY_MODE", "warn", raising=False)
    assert policy.get_policy_mode() == "warn"

    monkeypatch.setattr(Config, "POLICY_MODE", "enforce", raising=False)
    assert policy.get_policy_mode() == "enforce"

    # 非法值回退 warn
    monkeypatch.setattr(Config, "POLICY_MODE", "banana", raising=False)
    assert policy.get_policy_mode() == "warn"


def test_check_ingest_rejects_empty():
    from core.policy import check_ingest

    r = check_ingest("", "memory", "")
    assert r["ok"] is False
    assert r["error"]
    r2 = check_ingest("   ", "memory", "")
    assert r2["ok"] is False


def test_check_ingest_rejects_too_long(monkeypatch):
    from core import policy

    monkeypatch.setattr(Config, "MEM_INGEST_MAX_LENGTH", 10, raising=False)
    r = policy.check_ingest("x" * 11, "memory", "")
    assert r["ok"] is False
    assert "超长" in r["error"]


def test_check_ingest_type_limit_warn_does_not_reject(monkeypatch):
    """warn 模式：type 级上限命中只 warning，不拒绝。"""
    from core import policy

    monkeypatch.setattr(Config, "POLICY_MODE", "warn", raising=False)
    monkeypatch.setattr(Config, "POLICY_TYPE_LIMITS", "task:5", raising=False)
    r = policy.check_ingest("x" * 6, "task", "")
    assert r["ok"] is True
    assert len(r["warnings"]) >= 1


def test_check_ingest_type_limit_enforce_rejects(monkeypatch):
    """enforce 模式：type 级上限命中才拒绝。"""
    from core import policy

    monkeypatch.setattr(Config, "POLICY_MODE", "enforce", raising=False)
    monkeypatch.setattr(Config, "POLICY_TYPE_LIMITS", "task:5", raising=False)
    r = policy.check_ingest("x" * 6, "task", "")
    assert r["ok"] is False


def test_check_protected_overwrite():
    from core.policy import check_protected_overwrite

    assert check_protected_overwrite({"type": "rule"}) is not None
    assert check_protected_overwrite({"type": "memory"}) is None


# ---------------------------------------------------------------------------
# 独立库 fixture（B / C 段用）
# ---------------------------------------------------------------------------


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与 conftest 共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_guard_iso_")
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
# B. 只读保护门
# ---------------------------------------------------------------------------


def test_protected_overwrite_warn_mode(iso_store, monkeypatch):
    """warn 模式：受保护旧节点照常标 outdated，但返回 policy_warnings。"""
    from core.conflict import resolve_conflict

    monkeypatch.setattr(Config, "POLICY_MODE", "warn", raising=False)
    s = iso_store
    old = _mk(s, "某条受保护的治理规则内容", "memory", importance=0.5)
    # 把旧节点改成受保护（模拟 payload.protected=True 的既有节点）
    old_node = s.get_node(old)
    pay = dict(old_node.get("payload", {}))
    pay["protected"] = True
    s.update_payload(old, pay)

    new_content = "某条受保护的治理规则内容"  # 高相似 → 触发冲突
    emb = s.embed_text(new_content)
    new_id = _mk(s, new_content, "memory")
    res = resolve_conflict(s, emb, new_id, new_payload={"type": "memory", "domain": ""})
    # warn 模式：旧节点仍在 outdated_ids（行为不变）
    assert old in res["outdated_ids"]
    # 但带 policy_warnings
    assert res.get("policy_warnings")


def test_protected_overwrite_enforce_mode(iso_store, monkeypatch):
    """enforce 模式：受保护旧节点被跳过，不标 outdated。"""
    from core.conflict import resolve_conflict

    monkeypatch.setattr(Config, "POLICY_MODE", "enforce", raising=False)
    s = iso_store
    old = _mk(s, "某条受保护的治理规则内容二", "memory", importance=0.5)
    old_node = s.get_node(old)
    pay = dict(old_node.get("payload", {}))
    pay["protected"] = True
    s.update_payload(old, pay)

    new_content = "某条受保护的治理规则内容二"
    emb = s.embed_text(new_content)
    new_id = _mk(s, new_content, "memory")
    res = resolve_conflict(s, emb, new_id, new_payload={"type": "memory", "domain": ""})
    assert old not in res["outdated_ids"]


def test_unprotected_node_still_superseded(iso_store, monkeypatch):
    """回归红线：非保护节点在 enforce 模式下仍照常被标 outdated。"""
    from core.conflict import resolve_conflict

    monkeypatch.setattr(Config, "POLICY_MODE", "enforce", raising=False)
    s = iso_store
    old = _mk(s, "某条普通的可被取代的事实内容", "memory", importance=0.5)
    new_content = "某条普通的可被取代的事实内容"
    emb = s.embed_text(new_content)
    new_id = _mk(s, new_content, "memory")
    res = resolve_conflict(s, emb, new_id, new_payload={"type": "memory", "domain": ""})
    assert old in res["outdated_ids"]


# ---------------------------------------------------------------------------
# C. 端到端
# ---------------------------------------------------------------------------


def test_mem_ingest_default_behavior_unchanged(iso_store, monkeypatch):
    """warn 模式 + 空 TYPE_LIMITS：写入成功，行为与改动前等价。"""
    from mcp_tools import memory as mem

    monkeypatch.setattr(Config, "POLICY_MODE", "warn", raising=False)
    monkeypatch.setattr(Config, "POLICY_TYPE_LIMITS", "", raising=False)
    res = mem.mem_ingest("一条完全普通的测试记忆内容", type="memory", domain="testguard")
    import json

    payload = json.loads(res)
    assert payload.get("stored") is True
    assert payload.get("node_id") is not None


def test_mem_ingest_protected_reports_warning(iso_store, monkeypatch):
    """受保护候选被覆盖时，结果含 policy_warnings（warn 模式）。"""
    import json

    from mcp_tools import memory as mem

    monkeypatch.setattr(Config, "POLICY_MODE", "warn", raising=False)
    s = iso_store
    old = _mk(s, "受保护节点要被覆盖测试内容", "memory")
    old_node = s.get_node(old)
    pay = dict(old_node.get("payload", {}))
    pay["protected"] = True
    s.update_payload(old, pay)

    res = mem.mem_ingest("受保护节点要被覆盖测试内容", type="memory")
    payload = json.loads(res)
    assert payload.get("stored") is True
    # warn 模式下覆盖受保护节点 → 结果带 policy_warnings
    assert "policy_warnings" in payload
