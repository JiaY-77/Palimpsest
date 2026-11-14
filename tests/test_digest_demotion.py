"""
对话残渣降权 + 命中计数拆分（issue #278）回归测试
====================================================
覆盖四条验收：
  1. count_kind 三档（hit / inject / none）分别累加到正确计数；
  2. digest 类节点默认不出现在 mem_search 结果，include_digest=True 时出现；
  3. 迁移脚本幂等（跑两遍第二遍 0 变更；只补字段不改正文不删节点）；
另覆盖 pre-turn 注入路径显式传 count_kind="inject" 的接线，与 mem_stats
的 digest 残留占比 / top_hit_nodes 排除口径。

使用 conftest 的临时库隔离与确定性 fake embedding（字符 2-gram）。
"""

import contextlib
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本文件新建的节点。

    conftest 用 session 级临时库，所有测试文件共享；后续的顺序敏感测试
    （mem_recent 的「全局最新 N 条」、store_protocol 对拍等）会被残留节点
    污染。删除节点会级联删边 + 清 FTS，因此只需追踪 node_id 集合差集。
    """
    from mcp_tools import store

    def _ids() -> set:
        try:
            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 桩失败则不清理（不掩盖真实错误）
            return set()

    before = _ids()
    yield
    for nid in _ids() - before:
        with contextlib.suppress(Exception):  # 清理尽力而为，失败不掩盖真实用例错误
            store.delete_node(nid)


def _store():
    """复用 conftest 建立的、指向临时库的全局 store。"""
    from mcp_tools import store

    return store


def _flush(store):
    """把命中计数缓冲清空落库（避免测试间串味），返回回写计数。"""
    store.flush_hit_counts()


_DIGEST_PREFIX = "会话要点（Palimpsest 策略引擎提炼）："


# ---------------------------------------------------------------------------
# 1. count_kind 三档
# ---------------------------------------------------------------------------
def test_count_kind_hit_inject_none_buckets():
    store = _store()
    _flush(store)
    hit_text = "独立锁窗口的并发收敛策略说明"
    inj_text = "注入预算的分段裁剪规则说明"
    emb = store.embed_text(hit_text)
    hit_id = store.insert_node({"type": "memory", "content": hit_text, "importance": 0.6, "domain": "general"}, emb)
    inj_id = store.insert_node(
        {"type": "memory", "content": inj_text, "importance": 0.6, "domain": "general"},
        store.embed_text(inj_text),
    )

    def _counts(nid):
        payload = store.get_node(nid)["payload"]
        return int(payload.get("hit_count", 0)), int(payload.get("inject_count", 0))

    # hit：命中计数累加 hit_count；纯 hit 场景不产生 inject_count 键
    store.search_similar(emb, top_k=1, count_kind="hit")
    _flush(store)
    h0, i0 = _counts(hit_id)
    assert h0 == 1
    assert "inject_count" not in store.get_node(hit_id)["payload"]

    # inject：累加 inject_count，hit_count 不动
    store.search_similar(store.embed_text(inj_text), top_k=1, count_kind="inject")
    _flush(store)
    h1, i1 = _counts(inj_id)
    assert h1 == 0
    assert i1 == 1

    # none：内部判重不计任何计数
    store.search_similar(emb, top_k=1, count_kind="none")
    _flush(store)
    h2, i2 = _counts(hit_id)
    assert (h2, i2) == (h0, i0)

    # 非法值保守回退 hit
    store.search_similar(emb, top_k=1, count_kind="bogus")
    _flush(store)
    h3, _i3 = _counts(hit_id)
    assert h3 == h0 + 1


# ---------------------------------------------------------------------------
# 2. digest 默认不可达，include_digest=True 可达
# ---------------------------------------------------------------------------
def test_digest_hidden_by_default_visible_on_optin():
    store = _store()
    from mcp_tools.memory import _mem_search_impl

    digest_content = _DIGEST_PREFIX + "\n- 讨论了对话残渣的治理方案\n- 定了 hit_count 拆分口径"
    digest_id = store.insert_node(
        {"type": "memory", "content": digest_content, "importance": 0.5, "domain": "general"},
        store.embed_text(digest_content),
    )
    # meta.origin 打标路径（新写入形态）：content 是正常句子，靠 origin 识别
    meta_digest_content = "meta 打标的对话沉淀节点内容示例"
    meta_digest_id = store.insert_node(
        {
            "type": "record",
            "content": meta_digest_content,
            "importance": 0.6,
            "domain": "general",
            "meta": {"origin": "dialogue_digest"},
        },
        store.embed_text(meta_digest_content),
    )
    normal_content = "结论型记忆：架构评审定稿时间是周四下午"
    normal_id = store.insert_node(
        {"type": "memory", "content": normal_content, "importance": 0.8, "domain": "general"},
        store.embed_text(normal_content),
    )

    query = "对话残渣的治理方案"
    default_hits = _mem_search_impl(query, scope="memory", top_k=10)["results"]
    default_ids = {h["id"] for h in default_hits}
    assert digest_id not in default_ids
    assert meta_digest_id not in default_ids
    assert normal_id in default_ids  # 正常记忆不受影响

    audit_hits = _mem_search_impl(query, scope="memory", top_k=10, include_digest=True)["results"]
    audit_ids = {h["id"] for h in audit_hits}
    assert digest_id in audit_ids
    assert meta_digest_id in audit_ids

    # content 前缀兜底识别不依赖迁移完成（meta.origin 未打也过滤）
    from core.digest import digest_origin_of

    assert digest_origin_of(store.get_node(digest_id)["payload"]) == "session_digest"
    assert digest_origin_of(store.get_node(meta_digest_id)["payload"]) == "dialogue_digest"
    assert digest_origin_of(store.get_node(normal_id)["payload"]) is None


# ---------------------------------------------------------------------------
# 3. 迁移脚本幂等
# ---------------------------------------------------------------------------
def test_migrate_digest_origin_idempotent():
    store = _store()
    from scripts.migrate_digest_origin import migrate_digest_origin

    _flush(store)
    contents = {
        "s_new": _DIGEST_PREFIX + "\n- 老格式一（策略引擎）",
        "s_old": "会话要点（Palimpsest 插件提炼）：\n- 老格式二（插件）",
        "d": "[对话沉淀] 用户: 帮我把周报模板固化成 md",
        "normal": "火山碎屑流的形成机制研究笔记",
    }
    ids = {
        key: store.insert_node(
            {"type": "record" if key == "d" else "memory", "content": text, "importance": 0.5, "domain": "general"},
            store.embed_text(text),
        )
        for key, text in contents.items()
    }
    # 已有 origin 的 digest（任何值）→ 跳过不覆盖
    pre_marked = _DIGEST_PREFIX + "\n- 已打标的老节点"
    pre_marked_id = store.insert_node(
        {
            "type": "memory",
            "content": pre_marked,
            "importance": 0.5,
            "domain": "general",
            "meta": {"origin": "session_digest", "chat_id": "keep"},
        },
        store.embed_text(pre_marked),
    )

    # 第一遍 dry-run：统计但不写（库里可能有其他用例遗留的 digest，断言下界即可）
    dry = migrate_digest_origin(apply=False)
    assert dry["patched"] >= 3
    assert dry["skipped_already_marked"] >= 1
    assert dry["by_origin"]["session_digest"] >= 2
    assert dry["by_origin"]["dialogue_digest"] >= 1
    assert "origin" not in (store.get_node(ids["s_new"])["payload"].get("meta") or {})

    # 第一遍 apply：补标，正文不动，其他 meta 键保留
    first = migrate_digest_origin(apply=True)
    assert first["patched"] == dry["patched"]
    assert store.get_node(ids["s_new"])["payload"]["meta"]["origin"] == "session_digest"
    assert store.get_node(ids["s_old"])["payload"]["meta"]["origin"] == "session_digest"
    assert store.get_node(ids["d"])["payload"]["meta"]["origin"] == "dialogue_digest"
    assert store.get_node(ids["s_new"])["payload"]["content"] == contents["s_new"]
    assert store.get_node(ids["normal"])["payload"].get("meta") is None
    kept = store.get_node(pre_marked_id)["payload"]["meta"]
    assert kept["origin"] == "session_digest"
    assert kept["chat_id"] == "keep"

    # 第二遍：0 变更（幂等核心断言：所有前缀命中的节点此时都已有 origin 标记）
    second = migrate_digest_origin(apply=True)
    assert second["patched"] == 0


# ---------------------------------------------------------------------------
# 5. pre-turn 注入路径接线：count_kind="inject"
# ---------------------------------------------------------------------------
def test_pre_turn_passes_count_kind_inject(monkeypatch):
    import main as main_mod

    captured: dict = {}

    def fake_search(*args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"results": []})

    monkeypatch.setattr(main_mod, "_mcp_mem_search", fake_search)
    req = main_mod.PreTurnRequest(user_message="把记忆库的盘点流程梳理一遍", session_id="s1", turn_index=3)
    resp = main_mod.lifecycle_pre_turn(req)
    # 端点正常返回且召回路确实被调用（而非 trivial 早退）
    assert captured, "pre-turn 未走到语义召回（trivial 早退？）"
    assert captured.get("count_kind") == "inject"
    assert "session_id" in resp


# ---------------------------------------------------------------------------
# 6. mem_stats：digest 分节 + top_hit_nodes 排除口径
# ---------------------------------------------------------------------------
def test_mem_stats_digest_section_and_top_exclusion():
    from core.stats import compute_stats

    store = _store()
    _flush(store)
    # 自写节点（前面用例的节点已被 _no_leak 夹具清理，本测试必须自洽）
    d_content = _DIGEST_PREFIX + "\n- 盘点口径验证用要点"
    d_id = store.insert_node(
        # 存量命中非零：digest 分节只收命中过的节点，且它的命中数故意高于结论型节点，
        # 用来钉「top_hit_nodes 即使 digest 排第一也必须排除它」
        {"type": "memory", "content": d_content, "importance": 0.5, "domain": "general", "hit_count": 9},
        store.embed_text(d_content),
    )
    n_content = "盘点口径验证用的结论型记忆"
    n_id = store.insert_node(
        {"type": "memory", "content": n_content, "importance": 0.8, "domain": "general", "hit_count": 5},
        store.embed_text(n_content),
    )
    stats = compute_stats(store)
    assert "digest" in stats
    digest = stats["digest"]
    assert set(digest) >= {"active", "share_of_active", "by_origin", "top_hit_nodes"}
    assert digest["active"] >= 1
    assert digest["share_of_active"] > 0
    assert "session_digest" in digest["by_origin"]
    graph = stats["graph"]
    assert "inject_count_total" in graph and "top_inject_nodes" in graph
    # top_hit_nodes 不得出现 digest 类节点
    top_ids = [n["id"] for n in graph["top_hit_nodes"]]
    assert n_id in top_ids, "结论型节点应出现在 top_hit_nodes（排除 digest 不是靠空集侥幸）"
    assert d_id not in top_ids
    # digest 的存量命中单列 digest 分节
    assert d_id in [n["id"] for n in digest["top_hit_nodes"]]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
