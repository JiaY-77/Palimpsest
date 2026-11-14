"""机械去重闸门测试（mem_ingest 的 dedupe 三档，issue #280）
====================================================================

闸门定位：**只问「这事你确认过了吗」，不做语义判断**——是否取代、怎么处置
仍归调用方。三档：

  - ``allow``（默认）：旧行为完全不变；未声明 replaces 且撞到高相似
    （> ``DEDUPE_REJECT_THRESHOLD``）时返回值附带 ``duplicate_candidates``。
  - ``reject``：命中即拒写（stored:false），返回 ``duplicate_of`` + 候选。
  - ``merge``：合并进 ``dedupe_target`` 指定节点（不新建）；语义 = 追加正文。
  - 显式 ``replaces`` 优先于 dedupe：声明了取代就不再触发拒写 / 合并。

测试矩阵（三档 × 相似/不相似 × 显式 replaces/未声明 = 12 格）全覆盖，
外加幂等、参数校验、merge 护栏、候选查找单元测试、观测率与 REST 契约。

确定性来源：conftest 的 fake embedder（字符 2-gram）——**同文本相似度恒为
1.0、无关文本恒为 0.0**（本模块动笔前实测），所以「相似」用逐字相同内容、
「不相似」用无关内容，不依赖阈值 monkeypatch。

隔离级别：**自建独立库**（tempfile + 临时 DB_PATH + 替换 memory 模块的 store），
本模块会真实写入节点；绝不触碰 conftest 的共享 session 库。照
tests/test_conflict_supersede.py 的既有模式。
"""

import contextlib
import json
import shutil

import pytest

import mcp_tools.memory as memory_tool
from config import Config
from core.conflict import find_duplicate_candidates
from core.trivium_store import TriviumStore

SIMILAR = "小帕去重闸门测试：这一条会被逐字重复写入以触发相似命中"
UNRELATED = "完全不同领域的量子纠缠实验数据分析报告正文"


@pytest.fixture
def iso_store(tmp_path, monkeypatch):
    """独立数据库 + 替换 memory 模块的 store，绝不触碰共享 session 库。"""
    db_path = tmp_path / "dedupe_gate_test.db"
    monkeypatch.setattr(Config, "DB_PATH", str(db_path))
    store = TriviumStore()
    original = memory_tool.store
    memory_tool.store = store
    try:
        yield store
    finally:
        memory_tool.store = original
        with contextlib.suppress(Exception):
            store._acquire().close()
        shutil.rmtree(tmp_path, ignore_errors=True)


def _get(result: str) -> dict:
    return json.loads(result)


def _ingest(**kwargs) -> dict:
    return _get(memory_tool.mem_ingest(**kwargs))


def _node_count(store) -> int:
    return sum(1 for _ in store.iter_payloads())


# ---------------------------------------------------------------------------
# allow 档（4 格）：旧行为不变 + 高相似时附 duplicate_candidates
# ---------------------------------------------------------------------------


def test_allow_similar_no_replaces_writes_with_candidates(iso_store):
    """allow + 相似 + 未声明 → 照写，返回 duplicate_candidates（id + score + excerpt）。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs")
    assert r1["stored"] is True and r2["stored"] is True, (r1, r2)
    cands = r2["duplicate_candidates"]
    assert cands, r2
    assert cands[0]["id"] == r1["node_id"], r2
    assert cands[0]["score"] > Config.DEDUPE_REJECT_THRESHOLD, r2
    assert cands[0]["excerpt"][: len(SIMILAR)] == SIMILAR[: len(cands[0]["excerpt"])], r2
    # 旧行为不变：不标 outdated，旧节点照常进 related_ids
    assert r2["outdated_ids"] == [] and r1["node_id"] in r2["related_ids"], r2
    assert _node_count(iso_store) == 2


def test_allow_similar_with_replaces_skips_gate(iso_store):
    """allow + 相似 + 显式 replaces → 取代生效，且不再计算候选（旧路径逐字节形状）。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs", replaces=str(r1["node_id"]))
    assert r2["stored"] is True, r2
    assert r1["node_id"] in r2["outdated_ids"], r2
    assert "duplicate_candidates" not in r2, r2  # replaces 优先：闸门不触发


def test_allow_unrelated_no_replies_no_candidates_field(iso_store):
    """allow + 不相似 + 未声明 → 照写；无命中时不加键（返回形状与旧行为一致）。"""
    _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=UNRELATED, type="memory", domain="cs")
    assert r2["stored"] is True, r2
    assert "duplicate_candidates" not in r2, r2
    assert _node_count(iso_store) == 2


def test_allow_unrelated_with_replaces_unchanged(iso_store):
    """allow + 不相似 + 显式 replaces → 旧行为：取代生效，无候选字段。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=UNRELATED, type="memory", domain="cs", replaces=str(r1["node_id"]))
    assert r2["stored"] is True and r1["node_id"] in r2["outdated_ids"], r2
    assert "duplicate_candidates" not in r2, r2


# ---------------------------------------------------------------------------
# reject 档（4 格）：命中即拒写；replaces 优先
# ---------------------------------------------------------------------------


def test_reject_similar_no_replaces_rejected(iso_store):
    """reject + 相似 + 未声明 → 拒写（stored:false + duplicate_of），库内节点数不变。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    before = _node_count(iso_store)
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="reject")
    assert r2["stored"] is False, r2
    assert r2["duplicate_of"]["id"] == r1["node_id"], r2
    assert r2["duplicate_of"]["score"] > Config.DEDUPE_REJECT_THRESHOLD, r2
    cands = r2["duplicate_candidates"]
    assert cands, r2
    assert cands[0]["id"] == r1["node_id"], r2
    assert _node_count(iso_store) == before == 1
    # 被撞的旧节点仍 active（闸门不做任何取代）
    assert iso_store.get_node(r1["node_id"])["payload"].get("status") != "outdated"


def test_reject_similar_with_replaces_writes(iso_store):
    """reject + 相似 + 显式 replaces → replaces 优先：照写并取代，不触发拒写。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="reject", replaces=str(r1["node_id"]))
    assert r2["stored"] is True, r2
    assert r1["node_id"] in r2["outdated_ids"], r2


def test_reject_unrelated_no_replaces_writes(iso_store):
    """reject + 不相似 + 未声明 → 未命中阈值，照常写入。"""
    _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=UNRELATED, type="memory", domain="cs", dedupe="reject")
    assert r2["stored"] is True, r2
    assert "duplicate_candidates" not in r2, r2
    assert _node_count(iso_store) == 2


def test_reject_unrelated_with_replaces_writes(iso_store):
    """reject + 不相似 + 显式 replaces → replaces 优先：照写并取代。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=UNRELATED, type="memory", domain="cs", dedupe="reject", replaces=str(r1["node_id"]))
    assert r2["stored"] is True and r1["node_id"] in r2["outdated_ids"], r2


# ---------------------------------------------------------------------------
# merge 档（4 格）：不新建节点、目标被更新；replaces 优先
# ---------------------------------------------------------------------------


def test_merge_similar_no_replaces_merges_into_target(iso_store):
    """merge + 相似 + 未声明 → 不新建节点，目标 content 被追加，向量按合并全文重算。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    target_id = r1["node_id"]
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="merge", dedupe_target=target_id)
    assert r2["stored"] is True, r2
    assert r2["node_id"] == target_id and r2["merged"] is True, r2
    assert "duplicate_candidates" in r2, r2  # 未声明 replaces 且撞高相似：候选必带
    # 不新建节点
    assert _node_count(iso_store) == 1
    payload = iso_store.get_node(target_id)["payload"]
    assert payload["content"] == SIMILAR + "\n\n" + SIMILAR, payload["content"]
    # merge 不做语义动作：不标 outdated、不建 REVISED_BY
    assert payload.get("status") != "outdated", payload
    # 向量按合并全文重算（vector_stale 已清；向量与合并全文的 fake 嵌入一致）
    assert payload.get("vector_stale") is not True, payload
    merged_vec = iso_store.embed_text(payload["content"])
    stored_vec = iso_store.get_node(target_id).get("vector") or []
    assert stored_vec and max(abs(a - b) for a, b in zip(stored_vec, merged_vec, strict=True)) < 1e-5


def test_merge_unrelated_no_replaces_still_merges(iso_store):
    """merge + 不相似 + 未声明 → 目标显式指定即执行（merge 不看阈值，语义归调用方）。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=UNRELATED, type="memory", domain="cs", dedupe="merge", dedupe_target=r1["node_id"])
    assert r2["stored"] is True and r2["node_id"] == r1["node_id"], r2
    assert "duplicate_candidates" not in r2, r2  # 未撞高相似：无候选键
    assert _node_count(iso_store) == 1


def test_merge_similar_with_replaces_falls_back_to_insert(iso_store):
    """merge + 相似 + 显式 replaces → replaces 优先：照常新建节点并取代，不合并。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(
        content=SIMILAR,
        type="memory",
        domain="cs",
        dedupe="merge",
        dedupe_target=r1["node_id"],
        replaces=str(r1["node_id"]),
    )
    assert r2["stored"] is True, r2
    assert r2["node_id"] != r1["node_id"], r2  # 新建了节点
    assert r1["node_id"] in r2["outdated_ids"], r2
    assert "merged" not in r2, r2
    assert _node_count(iso_store) == 2


def test_merge_unrelated_with_replaces_falls_back_to_insert(iso_store):
    """merge + 不相似 + 显式 replaces → 同上：replaces 优先，正常写入。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(
        content=UNRELATED,
        type="memory",
        domain="cs",
        dedupe="merge",
        dedupe_target=r1["node_id"],
        replaces=str(r1["node_id"]),
    )
    assert r2["stored"] is True and r2["node_id"] != r1["node_id"], r2
    assert r1["node_id"] in r2["outdated_ids"], r2


# ---------------------------------------------------------------------------
# 幂等 / 参数校验 / merge 护栏
# ---------------------------------------------------------------------------


def test_reject_idempotent_second_write_rejected(iso_store):
    """幂等：同内容连写两次，reject 第二次必拒，节点数不变。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="reject")
    assert r1["stored"] is True, r1
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="reject")
    assert r2["stored"] is False and r2["duplicate_of"]["id"] == r1["node_id"], r2
    assert _node_count(iso_store) == 1


def test_allow_idempotent_both_written(iso_store):
    """幂等：同内容连写两次，allow 两次都写（旧行为不变）。"""
    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs")
    assert r1["stored"] is True and r2["stored"] is True, (r1, r2)
    assert _node_count(iso_store) == 2


def test_invalid_dedupe_value_rejected_before_embedding(iso_store):
    """非法档位直接拒绝（stored:false），不写库。"""
    r = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="auto")
    assert r["stored"] is False and "dedupe" in r["error"], r
    assert _node_count(iso_store) == 0


def test_merge_requires_explicit_target(iso_store):
    """merge 档未给 dedupe_target → 拒绝（机械闸门不猜目标）。"""
    r = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="merge")
    assert r["stored"] is False and "dedupe_target" in r["error"], r
    assert _node_count(iso_store) == 0


def test_dedupe_target_only_valid_in_merge(iso_store):
    """dedupe_target 传给非 merge 档 → 拒绝（防调用方笔误静默失效）。"""
    r = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="reject", dedupe_target=1)
    assert r["stored"] is False and "dedupe_target" in r["error"], r


def test_merge_target_missing_rejected(iso_store):
    """merge 档目标不存在 → 拒绝，不写库。"""
    r = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="merge", dedupe_target=999999)
    assert r["stored"] is False and "不存在" in r["error"], r
    assert _node_count(iso_store) == 0


def test_merge_cross_type_rejected(iso_store):
    """merge 目标 type 与本次不一致 → 硬拒（跨 type 合并几乎必是调用方笔误）。"""
    r1 = _ingest(content=SIMILAR, type="task", domain="cs")
    r2 = _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="merge", dedupe_target=r1["node_id"])
    assert r2["stored"] is False and "type" in r2["error"], r2
    assert _node_count(iso_store) == 1


def test_reject_cross_domain_not_triggered(iso_store):
    """domain 隔离：同相似内容但跨域（双方非 general）→ 不算重复，照写。"""
    _ingest(content=SIMILAR, type="memory", domain="cs")
    r2 = _ingest(content=SIMILAR, type="memory", domain="novel", dedupe="reject")
    assert r2["stored"] is True, r2
    assert _node_count(iso_store) == 2


# ---------------------------------------------------------------------------
# find_duplicate_candidates 单元测试（假 store，分数可控）
# ---------------------------------------------------------------------------


class _FakeStore:
    """最小假 store：search_similar 返回预置命中，get_node 读预置 payload。"""

    def __init__(self, hits, nodes):
        self._hits = hits
        self._nodes = nodes

    def search_similar(self, query, top_k=5, expand_depth=1, apply_decay=True, **_):
        return self._hits[:top_k]

    def get_node(self, node_id):
        payload = self._nodes.get(node_id)
        return {"id": node_id, "payload": payload} if payload else None


def test_find_duplicate_candidates_filters_and_sorts():
    store = _FakeStore(
        hits=[
            {"id": 1, "score": 0.9},
            {"id": 2, "score": 0.86},
            {"id": 3, "score": 0.5},  # 低于阈值：丢弃
            {"id": None, "score": 0.99},  # 无 id：丢弃
            {"id": 4, "score": 0.93},  # 跨 type：丢弃
            {"id": 5, "score": 0.95},  # 跨域（双方非 general）：丢弃
        ],
        nodes={
            1: {"type": "memory", "domain": "cs", "content": "候选一"},
            2: {"type": "memory", "domain": "general", "content": "候选二"},
            3: {"type": "memory", "domain": "cs", "content": "低分"},
            4: {"type": "task", "domain": "cs", "content": "跨 type"},
            5: {"type": "memory", "domain": "rp", "content": "跨域"},
        },
    )
    cands = find_duplicate_candidates(store, [0.0], {"type": "memory", "domain": "cs"}, threshold=0.85)
    assert [c["id"] for c in cands] == [1, 2], cands  # 按 score 降序
    assert cands[0]["score"] == 0.9 and cands[0]["excerpt"] == "候选一", cands


def test_find_duplicate_candidates_kb_chunk_never_participates():
    store = _FakeStore(
        hits=[{"id": 7, "score": 0.99}],
        nodes={7: {"type": "kb_chunk", "domain": "cs", "content": "知识切片"}},
    )
    assert find_duplicate_candidates(store, [0.0], {"type": "kb_chunk", "domain": "cs"}, threshold=0.85) == []
    assert find_duplicate_candidates(store, [0.0], {"type": "memory", "domain": "cs"}, threshold=0.85) == []


def test_find_duplicate_candidates_search_failure_degrades_to_empty():
    class _BrokenStore:
        def search_similar(self, *_a, **_k):
            raise RuntimeError("引擎故障")

        def get_node(self, _nid):
            return None

    assert find_duplicate_candidates(_BrokenStore(), [0.0], {"type": "memory", "domain": "cs"}, threshold=0.85) == []


def test_find_duplicate_candidates_threshold_zero_disables():
    store = _FakeStore(hits=[{"id": 1, "score": 0.9}], nodes={1: {"type": "memory", "domain": "cs", "content": "x"}})
    assert find_duplicate_candidates(store, [0.0], {"type": "memory", "domain": "cs"}, threshold=0.0) == []


# ---------------------------------------------------------------------------
# 观测（write_judgment 新增两率）与默认阈值契约
# ---------------------------------------------------------------------------


def test_write_judgment_records_reject_and_merge(iso_store):
    """reject / merge 各记一行观测；不计入 writes；两率分母 = 尝试总数。"""
    from core.judgment_metrics import aggregate

    r1 = _ingest(content=SIMILAR, type="memory", domain="cs")
    _ingest(content=SIMILAR, type="memory", domain="cs", dedupe="reject")  # 拒
    _ingest(content=UNRELATED, type="memory", domain="cs", dedupe="merge", dedupe_target=r1["node_id"])  # 合
    stats = aggregate(db_path=str(Config.DB_PATH))
    assert stats["dedupe_rejects"] == 1 and stats["dedupe_merges"] == 1, stats
    assert stats["writes"] == 1, stats  # 只有第一条照写计入 writes
    assert stats["dedupe_reject_rate"] == pytest.approx(1 / 3), stats
    assert stats["dedupe_merge_rate"] == pytest.approx(1 / 3), stats
    # 既有口径不受影响：explicit_replace_rate 仍按 writes 计
    assert stats["explicit_replace_rate"] == 0.0, stats


def test_write_judgment_old_rows_without_dedupe_field_unaffected(tmp_path):
    """老行（7 字段、无 dedupe）照旧计入 writes，新率恒 0 —— 老行不改的兼容承诺。"""
    from core.judgment_metrics import aggregate, record_ingest

    record_ingest(
        1,
        "memory",
        "cs",
        related_count=2,
        replaced_count=0,
        outdated_count=0,
        db_path=str(tmp_path / "legacy.db"),
    )
    stats = aggregate(db_path=str(tmp_path / "legacy.db"))
    assert stats["writes"] == 1 and stats["dedupe_rejects"] == 0 and stats["dedupe_merges"] == 0, stats
    assert stats["dedupe_reject_rate"] == 0.0 and stats["dedupe_merge_rate"] == 0.0, stats


def test_default_threshold_contract():
    """阈值默认 0.85（Phase 0 实测分布选定；README 配置表与 .env.example 同值）。"""
    assert Config.DEDUPE_REJECT_THRESHOLD == 0.85


# ---------------------------------------------------------------------------
# REST 契约（共享 session 库 + 测完清理，模式照 tests/test_meta_tag_purge.py）
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_leak_to_shared_db():
    """REST 用例跑在共享 session 库上，测完删掉本用例新建的节点。"""
    from mcp_tools import store

    def _ids() -> set:
        try:
            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 桩失败则不清理（不掩盖真实错误）
            return set()

    before = _ids()
    yield
    for nid in _ids() - before:
        with contextlib.suppress(Exception):
            store.delete_node(nid)


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


@pytest.mark.usefixtures("_no_leak_to_shared_db")
def test_rest_ingest_dedupe_reject_and_validation():
    c = _client()
    first = c.post("/mem/ingest", json={"content": SIMILAR, "type": "memory", "domain": "rp"})
    assert first.status_code == 200, first.text
    first_id = first.json()["node_id"]
    # 撞高相似 + reject → 200 但 stored:false + duplicate_of
    dup = c.post("/mem/ingest", json={"content": SIMILAR, "type": "memory", "domain": "rp", "dedupe": "reject"})
    assert dup.status_code == 200, dup.text
    body = dup.json()
    assert body["stored"] is False and body["duplicate_of"]["id"] == first_id, body
    # merge 缺 target → 422
    assert c.post("/mem/ingest", json={"content": "x", "dedupe": "merge"}).status_code == 422
    # 非法档位 → 422
    assert c.post("/mem/ingest", json={"content": "x", "dedupe": "auto"}).status_code == 422
    # target 传给非 merge 档 → 422
    assert c.post("/mem/ingest", json={"content": "x", "dedupe": "reject", "dedupe_target": 1}).status_code == 422
    # 缺省 = allow：行为与旧版一致
    plain = c.post("/mem/ingest", json={"content": UNRELATED, "type": "memory", "domain": "rp"})
    assert plain.status_code == 200 and plain.json()["stored"] is True, plain.text


@pytest.mark.usefixtures("_no_leak_to_shared_db")
def test_rest_ingest_dedupe_merge():
    c = _client()
    first = c.post("/mem/ingest", json={"content": SIMILAR, "type": "memory", "domain": "rp"})
    first_id = first.json()["node_id"]
    merged = c.post(
        "/mem/ingest",
        json={"content": UNRELATED, "type": "memory", "domain": "rp", "dedupe": "merge", "dedupe_target": first_id},
    )
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["stored"] is True and body["node_id"] == first_id and body["merged"] is True, body
