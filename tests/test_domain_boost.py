# -*- coding: utf-8 -*-
"""
domain_boost 加性软加权回归测试
==============================
直接调用 _mem_search_impl（mem_search 的真实实现）验证：
  1. 默认空值不改变任何行为（与不传该参数完全一致）
  2. 非空时对同域候选在语义分上恰好加 Config.DOMAIN_BOOST_EPS
  3. DOMAIN_BOOST_EPS=0.0 可整体关闭
  4. 不存在的域等于不加
  5. kb_chunk（node_domain == "kb"）同样被加分
不依赖 Ollama：conftest 的 fake_embedder 提供确定性向量。
"""

import pytest

from config import Config
from mcp_tools._common import store
from mcp_tools.memory import _mem_search_impl


def _insert(payload: dict, content: str) -> int:
    """直写节点（绕过 mem_ingest 冲突检测），返回 node_id。"""
    node_payload = dict(payload)
    node_payload["content"] = content
    return store.insert_node(node_payload, store.embed_text(content))


def _run(query: str, domain_boost: str = "") -> dict:
    """真实调用被测函数，返回 {node_id: score}。"""
    out = _mem_search_impl(query, scope="all", top_k=20,
                           domain_boost=domain_boost)
    return {r["id"]: r["score"] for r in out.get("results", [])}


def _seed_two_domains(marker: str):
    """构造两个异域节点：B 为查询精确命中（不放boost时居前），
    A 为查询的略稀释变体（同重要性，放 boost 后靠加成反超）。
    用 type=event 而非 memory：避免在会话共享临时库留下近重复 memory 对
    （consolidate 的 find_similar_pairs 只扫 type=memory，会误判为合并候选）。"""
    q = f"域加权护栏：{marker} 紫色独角兽在彩虹桥上吃蓝色发光浆果"
    a_id = _insert({"type": "event", "domain": "alpha", "importance": 0.5},
                   q + "（注）")
    b_id = _insert({"type": "event", "domain": "beta", "importance": 0.5}, q)
    return a_id, b_id, q


# ---------------------------------------------------------------------------
# 1. 默认不改变行为
# ---------------------------------------------------------------------------
def test_default_equals_omitted(db_path):
    a_id = _insert({"type": "memory", "domain": "alpha", "importance": 0.5},
                   "域加权护栏甲：檀香山火山口附近的红外望远镜观测记录")
    _insert({"type": "memory", "domain": "beta", "importance": 0.5},
            "域加权护栏乙：东京湾海底隧道的通风系统设计参数")
    q = "檀香山火山口附近的红外望远镜观测记录"

    explicit = _mem_search_impl(q, scope="all", top_k=20, domain_boost="")
    implicit = _mem_search_impl(q, scope="all", top_k=20)
    result_explicit = [(r["id"], r["score"]) for r in explicit["results"]]
    result_implicit = [(r["id"], r["score"]) for r in implicit["results"]]
    assert result_explicit == result_implicit
    assert any(r["id"] == a_id for r in implicit["results"]), implicit
    assert "bias" not in explicit and "bias" not in implicit


# ---------------------------------------------------------------------------
# 2. 加分生效：同域候选 score 恰好 +DOMAIN_BOOST_EPS，且排序按新分数
# ---------------------------------------------------------------------------
def test_boost_adds_eps_and_reorders(db_path):
    a_id, b_id, q = _seed_two_domains("第一组")

    base = _run(q)
    boosted = _run(q, "alpha")

    # 不放boost：精确命中（beta）居前，变体（alpha）居后
    assert base[b_id] >= base[a_id], (base, a_id, b_id)
    order_base = sorted(base, key=lambda nid: base[nid], reverse=True)
    assert order_base.index(b_id) < order_base.index(a_id), order_base

    # 放boost后 alpha 恰好 +EPS（放 4 位小数舍入的容差）
    assert boosted[a_id] == pytest.approx(
        base[a_id] + Config.DOMAIN_BOOST_EPS, abs=5e-4,
        rel=1e-6), (base[a_id], boosted[a_id])

    # beta 不被加分
    assert boosted[b_id] == pytest.approx(base[b_id], abs=5e-4,
                                          rel=1e-6), (base[b_id], boosted[b_id])

    # 排序确实按新分数：alpha 因加成反超 beta
    assert boosted[a_id] > boosted[b_id], (boosted, a_id, b_id)
    order_boost = sorted(boosted, key=lambda nid: boosted[nid], reverse=True)
    assert order_boost.index(a_id) < order_boost.index(b_id), order_boost


# ---------------------------------------------------------------------------
# 3. 环境变量可关闭
# ---------------------------------------------------------------------------
def test_boost_disable_via_env(monkeypatch, db_path):
    a_id, b_id, q = _seed_two_domains("第二组")
    monkeypatch.setattr(Config, "DOMAIN_BOOST_EPS", 0.0)

    no_boost = _run(q)
    disabled = _run(q, "alpha")
    assert disabled == no_boost
    assert a_id in no_boost and b_id in no_boost


# ---------------------------------------------------------------------------
# 4. 不存在的域等于不加
# ---------------------------------------------------------------------------
def test_unknown_domain_noop(db_path):
    a_id, b_id, q = _seed_two_domains("第三组")

    no_boost = _run(q)
    ghost = _run(q, "no_such_domain")
    assert ghost == no_boost
    assert a_id in no_boost and b_id in no_boost


# ---------------------------------------------------------------------------
# 5. kb_chunk 键：node_domain == "kb" 的候选被加分
# ---------------------------------------------------------------------------
def test_kb_chunk_domain_key(db_path):
    from core.trivium_store import node_domain

    kb_content = "域加权护栏知识块：量子点显示器的色彩转换效率研究综述"
    kb_id = _insert({"type": "kb_chunk", "domain": "kb"}, kb_content)
    mem_id = _insert({"type": "memory", "domain": "alpha", "importance": 0.5},
                     kb_content + "（记忆副本，非 kb）")

    payload = store.get_node(kb_id)["payload"]
    assert payload.get("type") == "kb_chunk"
    assert node_domain(payload) == "kb"

    base = _run(kb_content)
    boosted = _run(kb_content, "kb")

    assert kb_id in base and mem_id in base, base
    assert boosted[kb_id] == pytest.approx(
        base[kb_id] + Config.DOMAIN_BOOST_EPS, abs=5e-4,
        rel=1e-6), (base[kb_id], boosted[kb_id])
    # 非 kb 域不被加分
    assert boosted[mem_id] == pytest.approx(base[mem_id], abs=5e-4,
                                            rel=1e-6), (base[mem_id],
                                                        boosted[mem_id])