"""语义重排（rerank，opt-in，issue #272）失败路径 + 挂载回归测试。

覆盖任务书要求的硬性场景（全部用替身发请求，绝不真打网络）：

1. RERANK_ENABLED=0                → status "disabled"，不发请求（红线 3：关闭零成本）
2. 合法 ranking                    → 按模型的顺序重排、截回 top_k
3. requests 抛 RequestException    → 原顺序 + "degraded"（红线 2：fail-open）
4. content 非 JSON                 → 原顺序 + "degraded"
5. {"ranking": []} 空列表          → 原顺序 + "degraded"（红线 1：只排序不筛选）
6. finish_reason="length"          → 原顺序 + "degraded"（reasoning 截断实测场景）
7. DEEPSEEK_API_KEY 为空           → "degraded" 且不发请求
8. RERANK_ENABLED=0 时 _hybrid_rrf 的 channels 无 "rerank" 键（回归红线）

另附 build_prompt / parse_ranking 的单元测试（label 严格性 / 围栏容忍 / 去重兜底）。
"""

from __future__ import annotations

import requests

# ================================================================
# 公共替身：requests.post / 配置现场
# ================================================================


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _chat_payload(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}]}


def _enable_rerank(monkeypatch, **over):
    """把 Config 现场拉成「rerank 已启用」的确定状态（getattr 读取 → monkeypatch 生效）。"""
    from config import Config

    settings = {
        "RERANK_ENABLED": True,
        "DEEPSEEK_API_KEY": "test-key",
        "DEEPSEEK_BASE_URL": "https://api.test.example",
        "DEEPSEEK_MODEL": "base-model",
        "RERANK_MODEL": "",
        "RERANK_POOL": 40,
        "RERANK_SNIPPET_CHARS": 240,
        "RERANK_TIMEOUT": 5.0,
        "RERANK_MAX_TOKENS": 8000,
        "RERANK_REASONING_EFFORT": "low",
    }
    settings.update(over)
    for k, v in settings.items():
        monkeypatch.setattr(Config, k, v)


def _five_items() -> list[dict]:
    """5 条候选（id 1..5，原顺序），与 top_k=2 组成「重排可观察」的最小场景。"""
    return [
        {
            "id": i,
            "type": "memory",
            "score": 0.9 - 0.1 * (i - 1),
            "summary": f"候选{i}",
            "content": f"正文-{i}",
            "meta": {},
        }
        for i in range(1, 6)
    ]


def _stub_post(monkeypatch, calls: list, *, payload: dict | None = None, exc: Exception | None = None):
    """打桩 core.reranker.requests.post：记录调用并按 payload / exc 交付。"""
    import core.reranker as rr

    def _post(url, headers=None, json=None, timeout=None, **kw):
        calls.append({"url": url, "headers": headers or {}, "body": json, "timeout": timeout})
        if exc is not None:
            raise exc
        return _FakeResp(payload or _chat_payload('{"ranking": []}'))

    monkeypatch.setattr(rr.requests, "post", _post)


# ================================================================
# 1. 默认关闭：disabled + 零请求（红线 3）
# ================================================================


def test_disabled_by_default(monkeypatch):
    """RERANK_ENABLED=False → 原列表原样返回 + "disabled"，且 stub 计数为 0。"""
    import core.reranker as rr
    from config import Config

    monkeypatch.setattr(Config, "RERANK_ENABLED", False)
    calls: list = []
    _stub_post(monkeypatch, calls)
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "disabled"
    assert out is items  # 原对象原样（不是拷贝重排），行为与未引入前一致
    assert calls == []


# ================================================================
# 2-7. rerank() 的 ok / fail-open 契约
# ================================================================


def test_reorders_by_model_ranking(monkeypatch):
    """模型返回合法 ranking → 按其顺序重排并截回 top_k；请求体参数齐全正确。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload('{"ranking": ["C2", "C4"]}'))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "ok"
    assert [it["id"] for it in out] == [2, 4]  # 按模型顺序，截回 top_k=2

    assert len(calls) == 1
    req = calls[0]
    assert req["url"].endswith("/chat/completions")
    assert req["headers"]["Authorization"] == "Bearer test-key"
    assert req["headers"]["Content-Type"] == "application/json"
    assert req["timeout"] == 5.0
    body = req["body"]
    assert body["model"] == "base-model"  # RERANK_MODEL 空 → 回退 DEEPSEEK_MODEL
    assert body["temperature"] == 0
    assert body["max_tokens"] == 8000  # reasoning 模型：必须给 8000 级（issue #272 实测）
    assert body["reasoning_effort"] == "low"
    assert "问题" in body["messages"][0]["content"]
    assert "[C1] 正文-1" in body["messages"][0]["content"]  # 候选按传入顺序标号 C1..Cn


def test_no_reasoning_effort_when_empty(monkeypatch):
    """RERANK_REASONING_EFFORT="" → body 不含该字段（兼容非 reasoning 模型）。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch, RERANK_REASONING_EFFORT="")
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload('{"ranking": ["C2", "C4", "C5"]}'))
    items = _five_items()
    out, status = rr.rerank("问题", items, 3)
    assert status == "ok"
    assert [it["id"] for it in out] == [2, 4, 5]
    assert calls[0]["body"].get("reasoning_effort") is None


def test_fail_open_on_exception(monkeypatch):
    """网络/HTTP 层抛异常 → 原顺序 + "degraded"，绝不抛、绝不置空（红线 2）。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls, exc=requests.RequestException("connection reset"))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items
    assert calls  # 确实尝试过请求后才降级


def test_fail_open_on_malformed_json(monkeypatch):
    """content 非 JSON（"sorry I cannot"）→ 原顺序 + "degraded"。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload("sorry I cannot"))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items


def test_fail_open_on_empty_ranking(monkeypatch):
    """{"ranking": []} 空列表 → 原顺序 + "degraded"（禁止空列表筛选，红线 1）。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload('{"ranking": []}'))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items


def test_fail_open_on_partial_ranking(monkeypatch):
    """模型只给出部分编号（截断）→ 半截排序不可信 → 原顺序 + "degraded"。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload('{"ranking": ["C2"]}'))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert [it["id"] for it in out] == [1, 2, 3, 4, 5]


def test_fail_open_on_length_finish(monkeypatch):
    """finish_reason="length"（reasoning 截断 content 为空的实测场景）→ degraded。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload("", finish_reason="length"))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items


def test_no_api_key_degraded(monkeypatch):
    """DEEPSEEK_API_KEY 为空 → "degraded" 且不发请求（不发裸请求）。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch, DEEPSEEK_API_KEY="")
    calls: list = []
    _stub_post(monkeypatch, calls)
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items
    assert calls == []


def test_budget_equal_to_pool_skips_call(monkeypatch):
    """候选数 ≤ top_k 时重排无意义（整池都要）→ 直接原样返回，不出网。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch)
    calls: list = []
    _stub_post(monkeypatch, calls)
    items = _five_items()
    out, status = rr.rerank("问题", items, 5)
    assert status == "ok"
    assert out is items
    assert calls == []


# ================================================================
# 8. 挂载回归：关闭时 channels 无 "rerank" 键（逐位一致红线）
# ================================================================


def test_hybrid_rrf_output_unchanged_when_disabled(monkeypatch):
    """RERANK_ENABLED=False：_hybrid_rrf 的 channels 不含 "rerank" 键、无重排调用。"""
    import mcp_tools.memory as mm
    from config import Config

    monkeypatch.setattr(Config, "RERANK_ENABLED", False)

    calls: list = []
    _stub_post(monkeypatch, calls)

    def _fake_sem(query, scope, domain, bias, top_k, block, **kw):
        return [
            {"id": 11, "type": "memory", "score": 0.9, "summary": "甲", "meta": {}},
            {"id": 13, "type": "memory", "score": 0.7, "summary": "丙", "meta": {}},
        ]

    def _fts(q, limit=50):
        return [{"node_id": 11}, {"node_id": 13}], "ok"

    monkeypatch.setattr(mm, "_sem_candidate_items", _fake_sem)
    monkeypatch.setattr(mm, "search_fts_status", _fts)

    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "memory", "", "", 5, 50, "")
    assert calls == []  # 关闭时零请求
    assert "rerank" not in channels
    assert set(channels) == {"semantic", "fts", "exact_query", "fts_weight"}  # 输出与引入前逐位一致
    # candidate 关闭时 = top_k：stub 里 sem 只给 2 条，id 顺序由 RRF 分决定（11 领先）
    assert [it["id"] for it in items] == [11, 13]


def test_hybrid_cascade_output_unchanged_when_disabled(monkeypatch):
    """同上，cascade 路径：channels 不含 "rerank"、候选深度保持 top_k。"""
    import mcp_tools.memory as mm
    from config import Config

    monkeypatch.setattr(Config, "RERANK_ENABLED", False)

    seen_sem_k: list = []
    calls: list = []
    _stub_post(monkeypatch, calls)

    def _fake_sem(query, scope, domain, bias, top_k, block, **kw):
        seen_sem_k.append(top_k)
        return [
            {"id": 11, "type": "memory", "score": 0.9, "summary": "甲", "meta": {}},
            {"id": 13, "type": "memory", "score": 0.7, "summary": "丙", "meta": {}},
        ]

    monkeypatch.setattr(mm, "_sem_candidate_items", _fake_sem)
    monkeypatch.setattr(mm, "search_fts_status", lambda q, limit=50: ([{"node_id": 13}], "ok"))

    items, channels = mm._hybrid_cascade("固定查询甲乙丙", "memory", "", "", 5, 50, "")
    assert calls == []
    assert "rerank" not in channels
    assert seen_sem_k[-1] == 5  # 未拉深（candidate_k == top_k）
    # 级联：命中 FTS 粗筛的排在前（in_candidate），不足 top_k 从语义序（rest）补足
    assert [it["id"] for it in items] == [13, 11]
    assert all(it["meta"]["fts_hit"] == (it["id"] == 13) for it in items)


# ================================================================
# 附加：build_prompt / parse_ranking 单元测试
# ================================================================


def test_build_prompt_labels_and_snippet():
    from core.reranker import build_prompt

    items = [{"content": "甲\n乙" * 100}, {"summary": "只有摘要"}]  # 第二条 content 缺 → build_prompt 原样读
    prompt = build_prompt("查询", items, 10)
    lines = [ln for ln in prompt.splitlines() if ln.startswith("[C")]
    assert lines[0].startswith("[C1] 甲 乙")  # 换行替换成空格
    assert len(lines[0]) <= len("[C1] ") + 10 + 10  # 截到 snippet_chars 字内
    assert "候选片段：" in prompt and "问题：查询" in prompt
    assert '{"ranking": [' in prompt


def test_parse_ranking_strict_labels_and_dedup():
    from core.reranker import parse_ranking

    ids = list(range(10, 16))  # C1..C6 → 10..15
    # 围栏容忍 + 严格 C<n> token（不吃 "C99 这 kind of" 之类）
    raw = '```json\n{"ranking": ["C3", "C1", "C3", "C99", "c6", "C1"]}\n```'
    assert parse_ranking(raw, ids) == [12, 10, 15]  # 重复取首次；越界丢弃；c6 大小写兼容
    assert parse_ranking("完全没有花括号", ids) == []
    assert parse_ranking('{"rank": [1,2]}', ids) == []  # 键名错 → 空（degraded）
    assert parse_ranking('{"ranking": ["无编号"]}', ids) == []
    assert parse_ranking("", ids) == []


# ================================================================
# 9. 启用时：语义候选也要拉深到 RERANK_POOL（只放大融合那一步不够）
# ================================================================


def test_hybrid_rrf_deepens_semantic_pool_when_enabled(monkeypatch):
    """启用 rerank 时 `_sem_candidate_items` 必须拿到 RERANK_POOL 深度。

    只把 `_rrf_fuse` 的 top_k 放大、语义通道仍只取 top_k 条的话，深池里几乎
    没有语义候选，重排退化成「给 FTS 结果排序」，issue #272 的收益会丢失。
    """
    import mcp_tools.memory as mm

    _enable_rerank(monkeypatch, RERANK_POOL=40)
    seen: list = []

    def _fake_sem(query, scope, domain, bias, top_k, block, **kw):
        seen.append(top_k)
        return [
            {"id": i, "type": "memory", "score": 0.9 - i * 0.01, "summary": f"甲{i}", "meta": {}} for i in range(1, 41)
        ]

    monkeypatch.setattr(mm, "_sem_candidate_items", _fake_sem)
    monkeypatch.setattr(mm, "search_fts_status", lambda q, limit=50: ([], "ok"))

    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload('{"ranking": ["C1", "C2", "C3", "C4", "C5"]}'))
    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "all", "", "", 5, 50, "")

    assert seen and seen[-1] == 40  # 语义候选按 RERANK_POOL 拉深
    assert channels.get("rerank") == "ok"
    assert len(items) == 5  # 重排后仍截回 top_k
