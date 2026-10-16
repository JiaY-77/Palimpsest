"""MemoryHub 记忆包入籍侧测试
============================

覆盖「入籍侧」契约（与 test_memoryhub.py 读侧互补）：
  1.  预览模式不写：decide_import 缺省 select → nodes/edges 空、每条 not_selected；
  2.  入籍 payload 改写：origin_id/origin_pack 写入，hit_count/last_hit_at 剔除，
      created_at 有值；
  3.  空 content → empty_content；
  4.  强敏感 → secret_requires_confirm；include_secrets=True 放行；
  5.  status != "active" → not_active；
  6.  select 里的不存在 id → not_found；
  7.  边映射：只保留两端都入籍的边，键改为 source_origin_id/target_origin_id；
  8.  apply_import 端到端：A 库 build_pack → read_pack → decide_import → B 库落库
      （实测节点数 / origin_id / 边）；
  9.  read_pack 脏包守卫：nodes 非 list → code == "malformed"；
  10. 只读保证：预览前后目标库节点数不变。

隔离保证：照 tests/test_memoryhub.py 的做法，用独立临时库（自建 TriviumStore +
临时 Config.DB_PATH + conftest 的确定性 fake embedder），绝不触碰正式库、绝不连 Ollama。
"""

import contextlib
import json
import os
import shutil
import tempfile

import pytest
from conftest import _fake_embed

from core.memoryhub import apply_import, build_pack, read_pack
from core.secret_scan import scan_secret_classified
from core.strategy import decide_import
from core.trivium_store import TriviumStore


def _make_store(tmp: str, db_name: str = "import.db") -> TriviumStore:
    """在临时目录建独立 TriviumStore（fake embedder，不连 Ollama）。"""
    from config import Config

    old_db = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, db_name)
    try:
        s = TriviumStore()
    finally:
        Config.DB_PATH = old_db
    s.embed_text = _fake_embed
    return s


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_memoryhub_import_iso_")
    s = _make_store(tmp)
    try:
        yield s
    finally:
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _write_pack(pack: dict) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="palimpsest_memoryhub_import_pack_"), "pack.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False)
    return path


def _valid_pack(nodes=None, edges=None, manifest=None) -> dict:
    return {
        "format": "memoryhub",
        "format_version": 1,
        "manifest": manifest if manifest is not None else {"kind": "memory_pack", "fingerprint": "abc123"},
        "nodes": nodes if nodes is not None else [],
        "edges": edges if edges is not None else [],
    }


def _node(node_id, content, **payload_extra) -> dict:
    payload = {"type": "memory", "content": content, "importance": 0.6, "domain": "mh_import"}
    payload.update(payload_extra)
    return {"node_id": node_id, "payload": payload}


# ---------------------------------------------------------------------------
# 1. 预览模式不写
# ---------------------------------------------------------------------------


def test_preview_mode_no_writes():
    pack = _valid_pack(nodes=[_node(1, "甲"), _node(2, "乙")], edges=[{"source_id": 1, "target_id": 2}])
    plan = decide_import(pack)  # select 缺省

    assert plan["nodes"] == []
    assert plan["edges"] == []
    assert plan["decision_log"]["mode"] == "preview"
    assert len(plan["skipped"]) == 2
    assert all(s["reason"] == "not_selected" for s in plan["skipped"])
    assert {s["origin_id"] for s in plan["skipped"]} == {1, 2}
    assert plan["decision_log"]["type_counts"] == {"memory": 2}


# ---------------------------------------------------------------------------
# 2. 入籍 payload 改写
# ---------------------------------------------------------------------------


def test_import_payload_rewrite():
    original = _node(7, "入籍改写测试内容", hit_count=99, last_hit_at=123.0, num_edges=3)
    original["payload"]["created_at"] = None
    pack = _valid_pack(nodes=[original])
    plan = decide_import(pack, select=[7])

    assert len(plan["nodes"]) == 1
    entry = plan["nodes"][0]
    assert entry["origin_id"] == 7
    payload = entry["payload"]
    assert payload["origin_id"] == 7
    assert payload["origin_pack"] == "abc123"
    assert "hit_count" not in payload
    assert "last_hit_at" not in payload
    assert "num_edges" not in payload
    assert payload["created_at"] is not None
    # 业务字段保留
    assert payload["content"] == "入籍改写测试内容"
    assert payload["type"] == "memory"
    assert payload["importance"] == 0.6


# ---------------------------------------------------------------------------
# 3. 空 content
# ---------------------------------------------------------------------------


def test_empty_content_skipped():
    pack = _valid_pack(nodes=[{"node_id": 3, "payload": {"type": "memory", "content": "   "}}])
    plan = decide_import(pack, select=[3])

    assert plan["nodes"] == []
    reasons = {s["origin_id"]: s["reason"] for s in plan["skipped"]}
    assert reasons[3] == "empty_content"


# ---------------------------------------------------------------------------
# 4. 强敏感
# ---------------------------------------------------------------------------


def test_secret_requires_confirm_and_override():
    secret = "sk-" + "a" * 30
    # 先确认该串确实命中 strong 规则（与写入路径同判据）
    assert scan_secret_classified(secret)["strong"], scan_secret_classified(secret)

    pack = _valid_pack(nodes=[_node(4, secret)])
    plan = decide_import(pack, select=[4])
    assert plan["nodes"] == []
    assert {s["origin_id"]: s["reason"] for s in plan["skipped"]}[4] == "secret_requires_confirm"

    plan2 = decide_import(pack, select=[4], include_secrets=True)
    assert len(plan2["nodes"]) == 1
    assert plan2["nodes"][0]["origin_id"] == 4


# ---------------------------------------------------------------------------
# 5. not_active
# ---------------------------------------------------------------------------


def test_not_active_skipped():
    pack = _valid_pack(nodes=[_node(5, "已过期的记忆", status="outdated")])
    plan = decide_import(pack, select=[5])

    assert plan["nodes"] == []
    assert {s["origin_id"]: s["reason"] for s in plan["skipped"]}[5] == "not_active"


# ---------------------------------------------------------------------------
# 6. not_found
# ---------------------------------------------------------------------------


def test_not_found():
    pack = _valid_pack(nodes=[_node(1, "存在")])
    plan = decide_import(pack, select=[999999])

    assert plan["nodes"] == []
    assert {s["origin_id"]: s["reason"] for s in plan["skipped"]}[999999] == "not_found"


# ---------------------------------------------------------------------------
# 7. 边映射
# ---------------------------------------------------------------------------


def test_edge_mapping_only_imported_both_ends():
    nodes = [_node(1, "甲"), _node(2, "乙"), _node(3, "丙")]
    edges = [
        {"source_id": 1, "target_id": 2, "label": "RELATED_TO", "weight": 0.9},
        {"source_id": 1, "target_id": 3, "label": "RELATED_TO", "weight": 0.9},
    ]
    pack = _valid_pack(nodes=nodes, edges=edges)
    plan = decide_import(pack, select=[1, 2])  # 只选两端 1、2

    assert len(plan["edges"]) == 1
    edge = plan["edges"][0]
    assert edge["source_origin_id"] == 1
    assert edge["target_origin_id"] == 2
    assert edge["label"] == "RELATED_TO"
    assert "source_id" not in edge and "target_id" not in edge


# ---------------------------------------------------------------------------
# 8. apply_import 端到端（A 库 → 包 → B 库）
# ---------------------------------------------------------------------------


def test_apply_import_end_to_end():
    tmp = tempfile.mkdtemp(prefix="palimpsest_memoryhub_import_e2e_")
    store_a = _make_store(tmp, "a.db")
    store_b = _make_store(tmp, "b.db")
    try:
        # A 库写 3 节点 + 1 边
        id_a = store_a.insert_node({"type": "memory", "content": "端到端甲", "domain": "e2e"}, _fake_embed("端到端甲"))
        id_b = store_a.insert_node({"type": "memory", "content": "端到端乙", "domain": "e2e"}, _fake_embed("端到端乙"))
        id_c = store_a.insert_node({"type": "memory", "content": "端到端丙", "domain": "e2e"}, _fake_embed("端到端丙"))
        store_a.create_edge(id_a, id_b, "RELATED_TO")

        pack = build_pack(store_a, name="e2e_pack")
        path = _write_pack(pack)
        result = read_pack(path)
        assert result["ok"] is True

        plan = decide_import(result, select=[id_a, id_b, id_c])
        assert len(plan["nodes"]) == 3
        assert len(plan["edges"]) == 1

        res = apply_import(store_b, plan)
        assert res["ok"] is True
        assert len(res["imported"]) == 3
        assert res["edges_created"] == 1

        # 实测 B 库（不信返回值）：节点数、origin_id 对应、边数
        payloads = dict(store_b.iter_payloads())
        assert len(payloads) == 3

        imported_ids = [item["node_id"] for item in res["imported"]]
        assert set(imported_ids) == set(payloads.keys())

        got_origins = {p["origin_id"] for p in payloads.values()}
        assert got_origins == {id_a, id_b, id_c}

        all_edges = []
        for nid in payloads:
            all_edges.extend(store_b.get_edges(nid))
        assert len(all_edges) == 1
    finally:
        for s in (store_a, store_b):
            with contextlib.suppress(Exception):
                s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. read_pack 脏包守卫
# ---------------------------------------------------------------------------


def test_read_pack_malformed_nodes(tmp_path):
    pack = {"format": "memoryhub", "format_version": 1, "nodes": "oops", "edges": []}
    path = tmp_path / "dirty.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = read_pack(str(path))

    assert result["ok"] is False
    assert result["code"] == "malformed"


def test_read_pack_malformed_edges(tmp_path):
    pack = {"format": "memoryhub", "format_version": 1, "nodes": [], "edges": "oops"}
    path = tmp_path / "dirty_edges.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = read_pack(str(path))

    assert result["ok"] is False
    assert result["code"] == "malformed"


def test_read_pack_malformed_node_element(tmp_path):
    """nodes 是 list 但元素不是 dict → 记 malformed_node，不抛异常。"""
    pack = {"format": "memoryhub", "format_version": 1, "nodes": ["bad", _node(2, "好")], "edges": []}
    path = tmp_path / "dirty_el.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = read_pack(str(path))

    assert result["ok"] is True
    malformed = [i for i in result["issues"] if i["kind"] == "malformed_node"]
    assert len(malformed) == 1
    assert malformed[0]["node_id"] is None
    assert result["stats"]["node_total"] == 2
    assert result["stats"]["type_counts"] == {"memory": 1}


# ---------------------------------------------------------------------------
# 10. 只读保证
# ---------------------------------------------------------------------------


def test_decide_import_is_read_only(iso_store):
    s = iso_store
    s.insert_node({"type": "memory", "content": "库内既有节点"}, _fake_embed("库内既有节点"))
    before = len(list(s.iter_payloads()))

    pack = _valid_pack(nodes=[_node(1, "外来包节点")])
    decide_import(pack)  # 预览
    decide_import(pack, select=[1])  # 判定（纯决策，不写库）

    after = len(list(s.iter_payloads()))
    assert before == after == 1


def test_mem_import_preview_does_not_write(iso_store, monkeypatch):
    """mem_import 预览路径（select 空）不写目标库。"""
    import mcp_tools.memory as memory_tool

    monkeypatch.setattr(memory_tool, "store", iso_store)
    before = len(list(iso_store.iter_payloads()))

    pack = _valid_pack(nodes=[_node(11, "预览不应写入的内容")])
    path = _write_pack(pack)
    res = json.loads(memory_tool.mem_import(path, select=""))

    assert res["ok"] is True
    assert res["mode"] == "preview"
    assert res["hint"]
    after = len(list(iso_store.iter_payloads()))
    assert before == after == 0
