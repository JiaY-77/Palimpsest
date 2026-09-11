"""
reindex 脚本测试 —— 覆盖维度校验 / 过滤 / dry-run / 断点续跑等路径。

隔离策略（返工）：
  - 每个测试通过 monkeypatch 把 store.db_path 指向【本测试专用的临时库】，
    绝不在 conftest 的会话级共享库 insert / update 任何数据。
  - 状态文件按库绑定规则自动落在该临时库同目录，绝不落到真实 data/ 目录。
  - 全部使用 conftest.py 的 fake embedder，不依赖真实 Ollama。

注意：triviumdb 单进程只允许一个写连接。iter_payloads()/iter_nodes() 会持有
连接直到生成器被消费完毕，必须先完全消费再加 update_* 方法。
"""

import hashlib
import json
import math
import os
import re

import pytest

from mcp_tools import store


@pytest.fixture
def iso_db(tmp_path, monkeypatch):
    """隔离库夹具：把 store.db_path 指向本次测试专用临时库。

    状态文件按库绑定规则自动落在 <tmp>/reindex_state_mh_test.db.json，
    与库同目录，绝不落到真实 data/ 目录。

    返回 (store, db_path)。调用方必须在 insert 之前使用本夹具。
    """
    db = tmp_path / "mh_test.db"
    monkeypatch.setattr(store, "db_path", str(db))
    return store, db


# ---- 辅助 ----

def _insert_nodes(s, types: list[str]):
    """向隔离库插入测试节点，返回 {type: [node_id, ...]} 映射。"""
    result: dict[str, list[int]] = {}
    for tp in types:
        nid = s.insert_node(
            {"type": tp, "content": f"测试内容 {tp}", "importance": 0.5},
            s.embed_text(f"测试内容 {tp}"),
        )
        result.setdefault(tp, []).append(nid)
    return result


def _read_vectors(s, node_ids: list[int]) -> dict[int, list[float]]:
    return {nid: s.get_node(nid)["vector"] for nid in node_ids}


def _collect_nodes(s) -> list[tuple[int, dict, list]]:
    """收集所有节点 (id, payload, vec)，完全消费生成器释放 DB 连接。"""
    return [(nid, node.get("payload") or {}, node.get("vector"))
            for nid, node in s.iter_nodes()]


def _update_payloads(s, updates: dict[int, dict]):
    for nid, payload in updates.items():
        s.update_payload(nid, payload)


# ================================================================
# 维度一致 → 重嵌后向量改变、节点总数不变、payload 与边未被改动
# ================================================================

def test_reindex_dim_match_updates_vectors(iso_db):
    """维度一致时：向量被更新，节点数不变，payload 不动。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record", "task"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    # 建一条边（验证边不受影响）
    s.create_edge(all_ids[0], all_ids[1], "RELATED_TO")

    # 修改 content 使新向量不同
    updates = {}
    for nid, payload, _v in _collect_nodes(s):
        if nid in all_ids:
            tp = payload.get("type") or "unknown"
            updates[nid] = {**payload, "content": f"重嵌后内容 {tp}"}
    _update_payloads(s, updates)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, yes=True, batch=100)
    assert code == 0

    # 向量应该变化
    new_vectors = _read_vectors(s, all_ids)
    for nid in all_ids:
        assert new_vectors[nid] != old_vectors[nid], f"ID={nid} 向量未变"

    # 节点总数不变
    assert len(_collect_nodes(s)) == len(all_ids)

    # payload 未被改动（content 仍是改后的值；边仍在）
    for nid in all_ids:
        payload = s.get_node(nid)["payload"]
        assert payload["content"] == f"重嵌后内容 {payload.get('type', '')}"
    edges = s.get_edges(all_ids[0])
    assert any(e.target_id == all_ids[1] and e.label == "RELATED_TO"
               for e in edges)


# ================================================================
# 维度不一致 → 退出码 2 且库未被修改
# （校验对象 = 库的实际维度，不是 config 维度）
# ================================================================

def test_reindex_dim_mismatch_no_writes(iso_db, monkeypatch):
    """实测维度 512 != 库实际维度 1024：退出码 2，库中向量逐条不变。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    # 假 embedder 返回 512 维
    def _dim512_embed(text):
        vec = [0.0] * 512
        if text:
            padded = " " + text + " "
            for i in range(len(padded) - 1):
                gram = padded[i:i + 2]
                h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:4], 16)
                vec[h % 512] += 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
        return vec

    from core.trivium_store import TriviumStore
    monkeypatch.setattr(TriviumStore, "embed_text", staticmethod(_dim512_embed))
    s.embed_text = _dim512_embed

    try:
        from scripts.reindex import _check_dims, _get_db_dim, cmd_reindex
        # 库实际维度应为 1024（iso_db 建库时 config dim）
        db_dim, err = _get_db_dim(s)
        assert err is None and db_dim == 1024

        ok, actual, msg = _check_dims(s)
        assert not ok
        assert actual == 512
        # 报告里必须点名「库实际维度」，而不是「库配置维度」
        assert "库实际" in msg

        code = cmd_reindex(s, yes=True, batch=100)
        assert code == 2

        # 库中向量未被修改
        new_vectors = _read_vectors(s, all_ids)
        for nid in all_ids:
            assert new_vectors[nid] == old_vectors[nid], f"ID={nid} 向量被意外修改"
    finally:
        from tests.conftest import _fake_embed
        monkeypatch.setattr(
            TriviumStore, "embed_text", staticmethod(_fake_embed))
        s.embed_text = _fake_embed


def test_reindex_db_real_dim_2560_probe_1024(iso_db, monkeypatch, tmp_path):
    """回归硬伤 1：库实际维度 2560、实测 1024 → 预检必须拦住（退出码 2）。

    旧实现只比 config 维度（1024==1024 放行），导致逐节点写入才失败。
    新实现必须读库实际维度（storage_info）拦截，且一字节不写、退出码 2。
    """
    import triviumdb

    # 在独立临时路径上建一个真实 dim=2560 的库：不依赖 config 的 1024
    dim2560_db = tmp_path / "dim2560" / "mh_large.db"
    dim2560_db.parent.mkdir(parents=True, exist_ok=True)
    d = triviumdb.TriviumDB(str(dim2560_db), dim=2560)
    try:
        d.insert([0.0] * 2560, {"type": "memory", "content": "会话节点"})
        for i in range(4):
            d.insert([0.1 + i] * 2560, {"type": "record", "content": f"记录 {i}"})
    finally:
        d.close()

    # store 指向这个 2560 库；embed_text 仍用 conftest 的 1024 维 fake
    s, _ = iso_db
    monkeypatch.setattr(s, "db_path", str(dim2560_db))
    from core.trivium_store import TriviumStore
    monkeypatch.setattr(TriviumStore, "embed_text", staticmethod(
        __import__("tests.conftest", fromlist=["_fake_embed"])._fake_embed))
    s.embed_text = __import__("tests.conftest", fromlist=["_fake_embed"])._fake_embed

    from scripts.reindex import _check_dims, _get_db_dim, cmd_reindex
    db_dim, err = _get_db_dim(s)
    assert err is None and db_dim == 2560

    ok, actual, msg = _check_dims(s)
    assert not ok
    assert actual == 1024
    assert "2560" in msg and "库实际" in msg

    code = cmd_reindex(s, yes=True, batch=100)
    assert code == 2

    # 一字节不写：库中所有节点向量仍为 2560 维原值
    old_vectors = {nid: _read_vectors(s, [nid])[nid] for nid in _all_ids(dim2560_db)}
    new_vectors = {nid: _read_vectors(s, [nid])[nid] for nid in _all_ids(dim2560_db)}
    assert new_vectors == old_vectors
    for vec in new_vectors.values():
        assert len(vec) == 2560


def _all_ids(db_path):
    import triviumdb
    d = triviumdb.TriviumDB(str(db_path))
    try:
        return d.all_node_ids()
    finally:
        d.close()


# ================================================================
# embedding 抛异常 → 退出码非 0、已完成计数正确、库仍可正常打开
# ================================================================

def test_reindex_embedding_failure(iso_db, monkeypatch):
    """embedding 异常时：退出码非 0，库仍可正常打开。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record", "task"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    call_count = 0

    def _fail_after_2(text):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("模拟 embedding 服务不可用")
        return [0.1] * 1024

    from core.trivium_store import TriviumStore
    monkeypatch.setattr(TriviumStore, "embed_text", staticmethod(_fail_after_2))
    s.embed_text = _fail_after_2

    try:
        from scripts.reindex import cmd_reindex
        code = cmd_reindex(s, yes=True, batch=100)
        assert code == 4  # embedding 服务不可用 → 退出码 4（与维度不匹配 2 区分）

        # 维度探针消耗第 1 次调用，首个节点 embed（第 2 次）即抛异常 → 未写任何向量
        nodes = _collect_nodes(s)
        assert len(nodes) == len(all_ids)
        new_vectors = _read_vectors(s, all_ids)
        for nid in all_ids:
            assert new_vectors[nid] == old_vectors[nid], f"ID={nid} 不应被写"
    finally:
        from tests.conftest import _fake_embed
        monkeypatch.setattr(
            TriviumStore, "embed_text", staticmethod(_fake_embed))
        s.embed_text = _fake_embed


# ================================================================
# --only / --skip 过滤正确
# ================================================================

def test_reindex_only_filter(iso_db):
    """--only 只重嵌指定类型，跳过其他。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record", "task", "kb_chunk"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    updates = {}
    for nid, payload, _v in _collect_nodes(s):
        if nid in all_ids:
            updates[nid] = {**payload, "content": "新内容" + str(nid)}
    _update_payloads(s, updates)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, only=["memory", "task"], yes=True, batch=100)
    assert code == 0

    new_vectors = _read_vectors(s, all_ids)
    for nid in node_map["memory"] + node_map["task"]:
        assert new_vectors[nid] != old_vectors[nid], f"ID={nid} 应被重嵌"
    for nid in node_map["record"] + node_map["kb_chunk"]:
        assert new_vectors[nid] == old_vectors[nid], f"ID={nid} 不应被重嵌"


def test_reindex_skip_filter(iso_db):
    """--skip 跳过指定类型。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record", "task", "kb_chunk"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    updates = {}
    for nid, payload, _v in _collect_nodes(s):
        if nid in all_ids:
            updates[nid] = {**payload, "content": "新内容" + str(nid)}
    _update_payloads(s, updates)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, skip=["kb_chunk"], yes=True, batch=100)
    assert code == 0

    new_vectors = _read_vectors(s, all_ids)
    for nid in node_map["kb_chunk"]:
        assert new_vectors[nid] == old_vectors[nid], f"ID={nid} 不应被重嵌"
    for tp in ["memory", "record", "task"]:
        for nid in node_map[tp]:
            assert new_vectors[nid] != old_vectors[nid], f"ID={nid} 应被重嵌"


# ================================================================
# --dry-run 不写库
# ================================================================

def test_reindex_dry_run(iso_db):
    """--dry-run 不修改库中任何向量。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    updates = {}
    for nid, payload, _v in _collect_nodes(s):
        if nid in all_ids:
            updates[nid] = {**payload, "content": "新内容" + str(nid)}
    _update_payloads(s, updates)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, dry_run=True, batch=100)
    assert code == 0

    new_vectors = _read_vectors(s, all_ids)
    for nid in all_ids:
        assert new_vectors[nid] == old_vectors[nid], f"ID={nid} 被 dry-run 修改了"


# ================================================================
# --resume 跳过已完成节点
# ================================================================

def test_reindex_resume(iso_db, tmp_path, monkeypatch):
    """--resume 跳过已完成节点（state 中 max_done_id 以下的跳过）。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "memory", "memory", "memory"])
    all_ids = sorted(nid for ids in node_map.values() for nid in ids)

    updates = {}
    for nid, payload, _v in _collect_nodes(s):
        if nid in all_ids:
            updates[nid] = {**payload, "content": "新内容" + str(nid)}
    _update_payloads(s, updates)

    old_vectors = _read_vectors(s, all_ids)

    import scripts.reindex as ri
    state_file = ri.db_state_file(s)
    state = {
        "provider": s.provider,
        "model": "test",
        "dim": s.dim,
        "db_dim": s.dim,
        "db_path": os.path.abspath(s.db_path),
        "max_done_id": all_ids[1],
        "done_count": 2,
        "timestamp": 0,
    }
    assert state_file.endswith("reindex_state_mh_test.db.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, yes=True, batch=100)
    assert code == 0

    new_vectors = _read_vectors(s, all_ids)
    # 前 2 个（ID <= max_done_id）应被跳过
    for nid in all_ids[:2]:
        assert new_vectors[nid] == old_vectors[nid], f"ID={nid} 不应被 resume 重嵌"
    # 后 2 个应被重嵌
    for nid in all_ids[2:]:
        assert new_vectors[nid] != old_vectors[nid], f"ID={nid} 应被 resume 重嵌"


# ================================================================
# --restart 从头重嵌
# ================================================================

def test_reindex_restart(iso_db):
    """--restart 忽略 state，从头重嵌所有节点。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "memory", "memory"])
    all_ids = sorted(nid for ids in node_map.values() for nid in ids)

    updates = {}
    for nid, payload, _v in _collect_nodes(s):
        if nid in all_ids:
            updates[nid] = {**payload, "content": "新内容" + str(nid)}
    _update_payloads(s, updates)

    old_vectors = _read_vectors(s, all_ids)

    import scripts.reindex as ri
    state = {
        "provider": s.provider,
        "model": "test",
        "dim": s.dim,
        "db_dim": s.dim,
        "db_path": os.path.abspath(s.db_path),
        "max_done_id": all_ids[-1],
        "done_count": len(all_ids),
        "timestamp": 0,
    }
    with open(ri.db_state_file(s), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, restart=True, yes=True, batch=100)
    assert code == 0

    new_vectors = _read_vectors(s, all_ids)
    for nid in all_ids:
        assert new_vectors[nid] != old_vectors[nid], f"ID={nid} 未被 --restart 重嵌"


# ================================================================
# --check 模式（只读）
# ================================================================

def test_check_readonly(iso_db):
    """--check 不修改库中任何数据。"""
    s, _db = iso_db
    node_map = _insert_nodes(s, ["memory", "record"])
    all_ids = [nid for ids in node_map.values() for nid in ids]
    old_vectors = _read_vectors(s, all_ids)

    from scripts.reindex import cmd_check
    code = cmd_check(s)
    assert code == 0

    new_vectors = _read_vectors(s, all_ids)
    for nid in all_ids:
        assert new_vectors[nid] == old_vectors[nid]


# ================================================================
# 库为空时正常工作
# ================================================================

def test_reindex_empty_db(iso_db):
    """空库重嵌入应正常退出。"""
    s, _db = iso_db
    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, yes=True, batch=100)
    assert code == 0


# ================================================================
# 缺 content 的节点被跳过（计入跳过，不中断整轮）
# ================================================================

def test_reindex_skips_nodes_without_content(iso_db, capsys):
    """缺失/空 content 的节点应被跳过并按 type 计入结束报告。"""
    s, _db = iso_db
    s.insert_node(
        {"type": "memory", "content": "有内容"}, s.embed_text("有内容"))
    nid2 = s.insert_node(
        {"type": "inspiration", "content": ""}, s.embed_text(""))  # 空 content
    nid3 = s.insert_node({"type": "record", "content": ""},
                         s.embed_text(""))  # 空 content

    from scripts.reindex import _text_for_embed, cmd_reindex
    assert _text_for_embed(s.get_node(nid2)["payload"]) == ""
    assert _text_for_embed(s.get_node(nid3)["payload"]) == ""

    code = cmd_reindex(s, yes=True, batch=100)
    assert code == 0

    out = capsys.readouterr().out
    assert re.search(r"跳过（缺 content）:\s+2 个", out)
    assert re.search(r"inspiration:\s+1", out)
    assert re.search(r"record:\s+1", out)

    # 空 content 节点向量未被改动
    assert all(v == 0.0 for v in s.get_node(nid2)["vector"])
    assert all(v == 0.0 for v in s.get_node(nid3)["vector"])


# ================================================================
# 修复空向量计数
# ================================================================

def test_reindex_counts_fixed_empty_vectors(iso_db, capsys):
    """旧向量全零的节点重嵌后非零 → 计入「修复空向量」。"""
    s, _db = iso_db
    # 人为插入全零向量节点
    nid = s.insert_node(
        {"type": "memory", "content": "空向量节点"}, [0.0] * s.dim)

    from scripts.reindex import cmd_reindex
    code = cmd_reindex(s, yes=True, batch=100)
    assert code == 0

    out = capsys.readouterr().out
    assert re.search(r"修复空向量:\s+1 个", out)

    # 重嵌后非零
    vec = s.get_node(nid)["vector"]
    assert any(v != 0.0 for v in vec)


# ================================================================
# apply_filter 辅助函数
# ================================================================

def test_apply_filter():
    """过滤函数正确性。"""
    from scripts.reindex import _apply_filter
    only = {"memory", "record"}
    skip = {"kb_chunk"}

    assert _apply_filter("memory", only, None)
    assert _apply_filter("record", only, None)
    assert not _apply_filter("task", only, None)

    assert _apply_filter("memory", None, skip)
    assert not _apply_filter("kb_chunk", None, skip)
    assert _apply_filter("task", None, skip)

    # 无过滤 = 全部通过
    assert _apply_filter("anything", None, None)


# ================================================================
# _text_for_embed 只用 content（回归）
# ================================================================

def test_text_for_embed_uses_only_content():
    """_text_for_embed 只用 payload['content']，不用 label。"""
    from scripts.reindex import _text_for_embed
    assert _text_for_embed({"content": "abc", "label": "xyz"}) == "abc"
    assert _text_for_embed({"label": "xyz"}) == ""
    assert _text_for_embed({"content": 123}) == ""
    assert _text_for_embed({"content": ""}) == ""
