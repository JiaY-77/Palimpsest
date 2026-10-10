"""bi-temporal 事实时间字段测试。

覆盖三个层次：

A. core.bitemporal 纯函数契约
   - 事实类类型打标 / 非事实类（kb_chunk、record…）跳过
   - stamp_new_fact 幂等（已有 valid_at 不被覆盖）
   - mark_superseded 的 invalid_at = 取代者 valid_at；expired_at 只在首标时写
   - time_fields 只返回存在的键

B. 写入链路端到端（mem_ingest）
   - 新写入的 memory 节点带 valid_at
   - kb_chunk 不带 valid_at（豁免）
   - 高相似覆盖：旧节点被标 outdated 时补 invalid_at / expired_at

C. 检索与追溯
   - mem_search 结果 meta.times 透出时间字段
   - mem_fact_history 能沿 REVISED_BY 正/反向查到取代关系

隔离保证：B / C 段会真的写库，故用**独立临时库**（自建 TriviumStore + 自指
Config.DB_PATH + 重指 mcp_tools 共享单例），绝不写 conftest 的会话共享库——
否则会污染 test_mem_communities / test_mem_recent_behavior 这类数节点/数簇的
测试（实测踩过）。照 tests/test_mem_stats.py 的 iso_store 模式。A 段是纯函数，无需隔离。
"""

import contextlib
import os
import shutil
import tempfile
import time

import pytest

from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与 conftest 共享库完全隔离）。

    ``mcp_tools._common.store`` 是导入时构造的模块级单例，其 db_path 在 import
    那刻已固定——因此必须把该单例的 db_path 一并指向独立库，否则 mem_ingest
    仍写共享库、污染其它按节点计数断言的测试。
    """
    tmp = tempfile.mkdtemp(prefix="palimpsest_bite_iso_")
    from config import Config
    from mcp_tools import store as shared_store

    old_db = Config.DB_PATH
    old_shared = shared_store.db_path
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    shared_store.db_path = Config.DB_PATH
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old_db
        shared_store.db_path = old_shared
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================
# A. 纯函数
# ================================================================


def test_is_fact_type():
    from core.bitemporal import is_fact_type

    assert is_fact_type({"type": "memory"})
    assert is_fact_type({"type": "task"})
    assert is_fact_type({"type": "plan"})
    assert not is_fact_type({"type": "kb_chunk"})
    assert not is_fact_type({"type": "record"})
    assert not is_fact_type({"type": "event"})
    assert not is_fact_type({})


def test_stamp_new_fact_sets_valid_at_default_now():
    from core.bitemporal import VALID_AT, stamp_new_fact

    p = {"type": "memory", "content": "x"}
    stamp_new_fact(p, now=1000.0)
    assert p[VALID_AT] == 1000.0


def test_stamp_new_fact_preserves_explicit_valid_at():
    """显式传入的 valid_at（未来时态计划）不被覆盖——幂等。"""
    from core.bitemporal import VALID_AT, stamp_new_fact

    p = {"type": "plan", VALID_AT: 2000.0}
    stamp_new_fact(p, now=1000.0)
    assert p[VALID_AT] == 2000.0


def test_stamp_new_fact_skips_non_fact_types():
    from core.bitemporal import VALID_AT, stamp_new_fact

    p = {"type": "kb_chunk"}
    stamp_new_fact(p, now=1000.0)
    assert VALID_AT not in p


def test_mark_superseded_uses_superseder_valid_at():
    from core.bitemporal import EXPIRED_AT, INVALID_AT, mark_superseded

    old = {"type": "memory"}
    mark_superseded(old, new_valid_at=1500.0, now=1600.0)
    assert old[INVALID_AT] == 1500.0  # 世界时间：旧事实从新事实为真起不再为真
    assert old[EXPIRED_AT] == 1600.0  # 系统时间：本次标记时刻


def test_mark_superseded_falls_back_to_now_without_new_valid_at():
    from core.bitemporal import INVALID_AT, mark_superseded

    old = {"type": "memory"}
    mark_superseded(old, new_valid_at=None, now=1600.0)
    assert old[INVALID_AT] == 1600.0


def test_mark_superseded_keeps_first_expired_at():
    """expired_at 只在首标时写：第二次被取代不覆盖（进入历史的时刻不变）。"""
    from core.bitemporal import EXPIRED_AT, mark_superseded

    old = {"type": "memory"}
    mark_superseded(old, new_valid_at=100.0, now=200.0)
    mark_superseded(old, new_valid_at=300.0, now=400.0)
    assert old[EXPIRED_AT] == 200.0


def test_mark_superseded_skips_non_fact_types():
    from core.bitemporal import EXPIRED_AT, INVALID_AT, mark_superseded

    p = {"type": "record"}
    mark_superseded(p, new_valid_at=1.0, now=2.0)
    assert INVALID_AT not in p and EXPIRED_AT not in p


def test_time_fields_only_present_keys():
    from core.bitemporal import time_fields

    assert time_fields({"type": "memory"}) == {}
    got = time_fields({"valid_at": 1.0, "created_at": 2.0, "invalid_at": None})
    assert got == {"valid_at": 1.0, "created_at": 2.0}


# ================================================================
# B. 写入链路
# ================================================================


def test_mem_ingest_stamps_valid_at(iso_store):
    import json

    from mcp_tools.memory import mem_ingest

    before = time.time()

    res = json.loads(mem_ingest("bi-temporal 测试：主人喜欢深烘焙的手冲", type="memory", domain="hermes"))
    assert res["stored"] is True
    payload = iso_store.get_node(res["node_id"])["payload"]
    assert "valid_at" in payload
    # time.time() 不保证单调（Windows 底层 GetSystemTimeAsFileTime，分辨率
    # 约 15.6ms，且可能被系统时钟同步微调回退），故不能假设跨调用的
    # before <= valid_at <= after 严格成立——用系统时钟分辨率为容差，
    # 断言「valid_at 落在本次写入的时刻附近」这一真实意图。
    clock_slack = time.get_clock_info("time").resolution or 0.02
    assert before - clock_slack <= payload["valid_at"] <= time.time() + clock_slack
    assert payload.get("created_at") is not None


def test_mem_ingest_kb_chunk_exempt_from_valid_at(iso_store):
    import json

    from mcp_tools.memory import mem_ingest

    res = json.loads(mem_ingest("知识库切片不应带世界时间字段", type="kb_chunk", domain="kb_test"))
    assert res["stored"] is True
    payload = iso_store.get_node(res["node_id"])["payload"]
    assert "valid_at" not in payload


def test_superseded_old_node_gets_invalid_and_expired(iso_store):
    """高相似覆盖：旧节点被标 outdated 时补 invalid_at / expired_at。"""
    import json

    from mcp_tools.memory import mem_ingest

    first = json.loads(mem_ingest("小帕的服务监听 8090 端口", type="memory", domain="bite_test"))
    old_id = first["node_id"]
    # 极高相似措辞触发 >0.75 取代
    second = json.loads(mem_ingest("小帕的服务监听 8090 端口", type="memory", domain="bite_test"))
    assert second["conflict_found"], "同措辞应触发取代"
    assert old_id in second["outdated_ids"]

    old_payload = iso_store.get_node(old_id)["payload"]
    new_payload = iso_store.get_node(second["node_id"])["payload"]
    assert old_payload.get("status") == "outdated"
    assert old_payload.get("invalid_at") is not None
    assert old_payload.get("expired_at") is not None
    # invalid_at 应等于新事实的 valid_at（世界时间的接续）
    assert old_payload["invalid_at"] == new_payload["valid_at"]


# ================================================================
# C. 检索与追溯
# ================================================================


def test_search_meta_exposes_times(iso_store):
    import json

    from mcp_tools.memory import mem_ingest, mem_search

    mem_ingest("bi-temporal 检索透出：檀木书桌放在二楼书房", type="memory", domain="bite_search")
    res = json.loads(mem_search("檀木书桌放在哪", domain="bite_search", top_k=5))
    hits = [r for r in res["results"] if "书桌" in r["summary"]]
    assert hits, "应命中刚写入的记忆"
    assert "times" in hits[0]["meta"]
    assert "valid_at" in hits[0]["meta"]["times"]


def test_fact_history_shows_supersede_chain(iso_store):
    import json

    from mcp_tools.memory import mem_fact_history, mem_ingest

    a = json.loads(mem_ingest("实验对象编号 A-7 的读数上限是 42", type="memory", domain="bite_hist"))
    b = json.loads(mem_ingest("实验对象编号 A-7 的读数上限是 42", type="memory", domain="bite_hist"))
    assert b["conflict_found"]

    # 查旧节点：应看到「谁取代了我」
    old_hist = json.loads(mem_fact_history(a["node_id"]))
    assert old_hist["found"]
    assert "invalid_at" in old_hist["times"]
    assert any(x["id"] == b["node_id"] for x in old_hist["superseded_by"])

    # 查新节点：应看到「我取代了谁」
    new_hist = json.loads(mem_fact_history(b["node_id"]))
    assert any(x["id"] == a["node_id"] for x in new_hist["superseded"])


def test_fact_history_missing_node(iso_store):
    import json

    from mcp_tools.memory import mem_fact_history

    res = json.loads(mem_fact_history(999999999))
    assert res["found"] is False
