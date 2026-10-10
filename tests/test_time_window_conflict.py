"""bi-temporal 三期：时间窗冲突判定测试。

覆盖三个层次：

A. core.bitemporal.windows_overlap 纯函数契约（无需隔离）
   - 明显重叠 / 明显不重叠（过去 vs 现在）
   - 边界：相接（new_valid == old_invalid）→ 不重叠
   - 任一方仍为真（invalid_at 缺失）→ 重叠
   - 缺 valid_at（历史数据）→ 保守返回 True

B. 端到端（resolve_conflict 的时间窗判定）
   - CONFLICT_TIME_WINDOW=false（默认）：行为逐字节不变（相似即标 outdated）
   - CONFLICT_TIME_WINDOW=true：时间窗不重叠的相似事实不被标 outdated
   - CONFLICT_TIME_WINDOW=true：时间窗重叠的相似事实仍被标

隔离保证：B 段会真的写库，用独立临时库（照 tests/test_bitemporal.py 的 iso_store）。
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


def test_overlap_obvious_true():
    """明显重叠：两个窗都覆盖 1500。"""
    from core.bitemporal import windows_overlap

    new = {"valid_at": 1000.0}
    old = {"valid_at": 1200.0}
    assert windows_overlap(new, old) is True


def test_overlap_disjoint_past_vs_now():
    """不重叠：十年前住北京 vs 现在住上海（旧已失效，新在旧失效后才开始）。"""
    from core.bitemporal import windows_overlap

    new = {"valid_at": 2000.0}  # 现在（仍为真，+∞）
    old = {"valid_at": 1000.0, "invalid_at": 1500.0}  # 十年前，1500 已失效
    assert windows_overlap(new, old) is False


def test_overlap_boundary_touching():
    """边界：new_valid == old_invalid → 相接不算重叠。"""
    from core.bitemporal import windows_overlap

    new = {"valid_at": 1500.0}
    old = {"valid_at": 1000.0, "invalid_at": 1500.0}
    assert windows_overlap(new, old) is False


def test_overlap_old_still_true():
    """旧仍为真（invalid_at 缺失 = +∞）→ 与任何新事实重叠。"""
    from core.bitemporal import windows_overlap

    new = {"valid_at": 9999.0}
    old = {"valid_at": 1000.0}  # 仍为真
    assert windows_overlap(new, old) is True


def test_overlap_both_invalid_concrete():
    """两个窗都有明确起止，完全不重叠。"""
    from core.bitemporal import windows_overlap

    new = {"valid_at": 3000.0, "invalid_at": 4000.0}
    old = {"valid_at": 1000.0, "invalid_at": 2000.0}
    assert windows_overlap(new, old) is False


def test_overlap_missing_valid_at_conservative():
    """缺 valid_at（历史数据）→ 保守返回 True（不静默改变既有判定）。"""
    from core.bitemporal import windows_overlap

    assert windows_overlap({}, {"valid_at": 1000.0, "invalid_at": 2000.0}) is True
    assert windows_overlap({"valid_at": 1000.0}, {}) is True


# ---------------------------------------------------------------------------
# 独立库 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与 conftest 共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_timewin_iso_")
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


def _mk(store, content, type, domain="", **extra):
    return store.insert_node(
        {"type": type, "content": content, "importance": 0.5, "domain": domain, **extra},
        store.embed_text(content),
    )


# ---------------------------------------------------------------------------
# B. 端到端（resolve_conflict）
# ---------------------------------------------------------------------------


def test_time_window_disabled_unchanged(iso_store, monkeypatch):
    """默认关闭：时间窗不重叠的相似事实**仍被标 outdated**（行为不变）。"""
    from core.conflict import resolve_conflict

    monkeypatch.setattr(Config, "CONFLICT_TIME_WINDOW", False, raising=False)
    s = iso_store
    # 旧事实：十年前，已失效
    old = _mk(s, "时间窗测试：某人的居住城市", "memory", "tw_reg", valid_at=1000.0, invalid_at=1500.0)
    new_content = "时间窗测试：某人的居住城市"
    emb = s.embed_text(new_content)
    # 新事实：现在
    new = _mk(s, new_content, "memory", "tw_reg", valid_at=2000.0)
    res = resolve_conflict(s, emb, new, new_payload={"type": "memory", "domain": "tw_reg", "valid_at": 2000.0})
    assert old in res["outdated_ids"], "默认关闭时应维持现行为（标 outdated）"


def test_time_window_enabled_disjoint_not_superseded(iso_store, monkeypatch):
    """开启：时间窗不重叠的相似事实**不被标 outdated**（改这里）。"""
    from core.conflict import resolve_conflict

    monkeypatch.setattr(Config, "CONFLICT_TIME_WINDOW", True, raising=False)
    s = iso_store
    old = _mk(s, "时间窗测试：某人的居住城市二", "memory", "tw_on", valid_at=1000.0, invalid_at=1500.0)
    new_content = "时间窗测试：某人的居住城市二"
    emb = s.embed_text(new_content)
    new = _mk(s, new_content, "memory", "tw_on", valid_at=2000.0)
    res = resolve_conflict(s, emb, new, new_payload={"type": "memory", "domain": "tw_on", "valid_at": 2000.0})
    assert old not in res["outdated_ids"], "时间窗不重叠时不应标 outdated"
    assert old in res["related_ids"], "应归入 related_ids（话题相关但不矛盾）"


def test_time_window_enabled_overlapping_still_superseded(iso_store, monkeypatch):
    """开启：时间窗重叠的相似事实**仍被标 outdated**。"""
    from core.conflict import resolve_conflict

    monkeypatch.setattr(Config, "CONFLICT_TIME_WINDOW", True, raising=False)
    s = iso_store
    # 旧事实：仍为真（invalid_at 缺失）
    old = _mk(s, "时间窗测试：某人的居住城市三", "memory", "tw_on2", valid_at=1000.0)
    new_content = "时间窗测试：某人的居住城市三"
    emb = s.embed_text(new_content)
    new = _mk(s, new_content, "memory", "tw_on2", valid_at=2000.0)
    res = resolve_conflict(s, emb, new, new_payload={"type": "memory", "domain": "tw_on2", "valid_at": 2000.0})
    assert old in res["outdated_ids"], "时间窗重叠时应照常标 outdated"
