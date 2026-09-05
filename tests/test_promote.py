# -*- coding: utf-8 -*-
"""
promote —— 高频记忆自动升级测试
===============================
覆盖 core.promoter + trivium_store.search_similar 命中计数：
  - 候选推荐：dry-run 候选只含 甲（hit_count 达标、非 kb_chunk、active、importance<0.8）；
  - apply 升权打标：甲 importance 0.5→0.6、prev_importance=0.5、promoted=true；
  - 幂等：再次 apply 输出 0 变更（hit_count 未增长则跳过）；
  - 命中追踪：真实检索（store.search_similar）命中节点 hit_count 增 1。
隔离保证：本文件用独立临时库（自建 TriviumStore + 自指 DB_PATH + 复用 conftest
确定性 fake embedder），不触碰 conftest 会话共享临时库，也不污染 FTS 索引。
"""

import os
import shutil
import tempfile
import time

import pytest

from conftest import _fake_embed  # noqa: E402

from core.promoter import find_promote_candidates, promote  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


@pytest.fixture
def iso_store():
    """构造一个全新临时库上的独立 TriviumStore（embed_text 用 conftest fake）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_promote_iso_")
    from config import Config

    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "promote.db")
    s = TriviumStore()
    s.embed_text = _fake_embed
    try:
        yield s
    finally:
        Config.DB_PATH = old
        try:
            s._acquire().close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def _mk(store, content, type, importance, hit_count, domain="general",
        **extra):
    nid = store.insert_node({
        "type": type, "content": content, "importance": importance,
        "domain": domain, "hit_count": hit_count, "last_hit_at": time.time(),
        **extra,
    }, store.embed_text(content))
    return nid


def test_promote_candidates_filter(iso_store):
    """dry-run 候选只含甲（升权门槛 + 非 kb_chunk + active + importance<0.8）。"""
    s = iso_store
    jia = _mk(s, "升级候选甲：反复被检索命中的记忆", "memory", 0.5, 10)
    yi = _mk(s, "普通记忆乙：命中不足门槛", "memory", 0.5, 2)
    bing = _mk(s, "知识块丙：不该进入升级候选", "kb_chunk", 0.5, 10)

    result = promote(s, dry_run=True, days=30, min_hits=5)
    assert result["dry_run"] is True
    cand_ids = {c["id"] for c in result["candidates"]}
    assert cand_ids == {jia}, f"候选只应含甲: {cand_ids}"
    assert len(result["candidates"]) == 1
    assert yi not in cand_ids and bing not in cand_ids
    c = result["candidates"][0]
    for k in ("id", "content", "type", "domain", "importance", "hit_count",
              "suggested_action"):
        assert k in c, f"候选缺字段 {k}: {c}"


def test_promote_apply_and_idempotent(iso_store):
    """apply 升权打标；再次 apply 幂等输出 0 变更。"""
    s = iso_store
    jia = _mk(s, "升级候选甲解析变量2：反复被检索命中的记忆", "memory", 0.5, 10)

    r1 = promote(s, dry_run=False, days=30, min_hits=5)
    assert r1["dry_run"] is False
    assert r1["promoted_count"] == 1, r1
    assert {c["id"] for c in r1["changes"]} == {jia}, r1

    payload = s.get_node(jia)["payload"]
    assert payload["importance"] == 0.6, payload
    assert payload["prev_importance"] == 0.5, payload
    assert payload["promoted"] is True, payload
    assert payload["promoted_at"] is not None, payload
    assert payload["promoted_hit_base"] == 10, payload

    # 再次 apply：甲已 promoted 且 hit_count 未增长（== promoted_hit_base）→ 跳过
    r2 = promote(s, dry_run=False, days=30, min_hits=5)
    assert r2["promoted_count"] == 0, r2
    assert r2["changes"] == [], r2
    # importance 不再被二次抬升
    assert s.get_node(jia)["payload"]["importance"] == 0.6


def test_hit_tracking_via_search(iso_store):
    """真实检索路径：search_similar 命中节点 hit_count 增 1。"""
    s = iso_store
    content = "命中追踪护栏唯一标记词promoteunique"
    nid = s.insert_node({"type": "memory", "content": content,
                         "importance": 0.5, "domain": "general"},
                        s.embed_text(content))
    assert s.get_node(nid)["payload"].get("hit_count") is None

    results = s.search_similar(s.embed_text(content), top_k=5, expand_depth=1)
    assert any(r.get("id") == nid for r in results), f"应命中插入节点: {results}"

    payload = s.get_node(nid)["payload"]
    assert payload["hit_count"] == 1, payload
    assert payload.get("last_hit_at") is not None, payload


def test_find_promote_candidates_api(iso_store):
    """find_promote_candidates 直接调用返回候选列表（供底层复用验证）。"""
    s = iso_store
    nid = _mk(s, "升级候选丁API：直接调 find 验证", "memory", 0.5, 6)
    cands = find_promote_candidates(s, days=30, min_hits=5)
    assert any(c["id"] == nid for c in cands), cands
    hit = next(c for c in cands if c["id"] == nid)
    assert hit["hit_count"] == 6, hit
    assert hit["importance"] == 0.5, hit