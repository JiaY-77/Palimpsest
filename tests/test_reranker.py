"""语义重排（rerank，opt-in，issue #272 / #274）失败路径 + 挂载回归测试。

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

local_ce 后端（本地 cross-encoder，issue #274）的契约测试见文件后半部分 §10：
全部 mock 掉 llama_cpp（CI 没有 635MB 模型），重点钉死：
rank-only 不变量 / 打分器异常 fail-open / 模型缺失只走一次磁盘 /
逐条调用契约（防止后人改成批量子弹打死进程）/ 关闭时不 import。
"""

from __future__ import annotations

import sys
from typing import ClassVar

import pytest
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
        "RERANK_BACKEND": "llm",
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


# ================================================================
# 10. local_ce 后端（issue #274：本地 cross-encoder；llama_cpp 全部 mock，
#     CI 没有 635MB 的模型文件）
# ================================================================


@pytest.fixture()
def _reset_ce_state():
    """local_ce 模块级单例/门闩复位：测试之间互不串台。"""
    import core.reranker as rr

    rr._CE_MODEL = None
    rr._CE_BLOCKED = ""
    rr._BACKEND_WARNED = False
    yield
    rr._CE_MODEL = None
    rr._CE_BLOCKED = ""
    rr._BACKEND_WARNED = False


class _FakeCE:
    """替身本地 cross-encoder：记录每次 create_embedding 收到的输入列表。

    返回向量刻意做成 1024 维：第 0 维是分数，其余填充「分数 + 1」——
    实现若不老实读第 0 维、对整向量做任何聚合，测试当场红
    （真实 llama.cpp 的填充位是 NaN/0，聚合出来的必然是垃圾）。
    """

    def __init__(self, scores: dict[str, float], fail: Exception | None = None):
        self._scores = scores
        self._fail = fail
        self.inputs: list[list[str]] = []  # 原样记录每次调用收到的列表

    def create_embedding(self, inputs: list):
        self.inputs.append(list(inputs))
        if self._fail is not None:
            raise self._fail
        text = inputs[0]
        _, _, doc = text.partition("</s>")  # 输入形状 f"{query}</s>{doc}"
        score = self._scores[doc]
        return {"data": [{"embedding": [score] + [score + 1.0] * 1023}]}


def _ce_enable(monkeypatch, **over):
    """rerank 已启用 + local_ce 后端的最小配置现场。"""
    _enable_rerank(monkeypatch, RERANK_BACKEND="local_ce", **over)


def _install_ce(monkeypatch, scores: dict, fail: Exception | None = None) -> _FakeCE:
    """把 local_ce 的模型装配缝换成替身（真实现根本不加载模型）。"""
    import core.reranker as rr

    fake = _FakeCE(scores, fail=fail)
    monkeypatch.setattr(rr, "_get_ce_model", lambda: fake)
    return fake


CE_SCORES_DESC_3_5_1_2_4 = {
    "正文-1": 0.50,
    "正文-2": 0.20,
    "正文-3": 0.95,
    "正文-4": 0.10,
    "正文-5": 0.70,
}


# ----------------------------------------------------------------
# 10.1 rank-only 不变量：长度不变/无重复/只有顺序变化
# ----------------------------------------------------------------


def test_local_ce_rank_only_invariant(monkeypatch, _reset_ce_state):
    """local_ce 重排：前 top_k 按分数降序、其余按原顺序补齐，长度与传参一致。"""
    import core.reranker as rr

    _ce_enable(monkeypatch, RERANK_POOL=20)
    _install_ce(monkeypatch, CE_SCORES_DESC_3_5_1_2_4)
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "ok"
    # 分数降序取前 2（正文-3 > 正文-5），其余 1/2/4 按**原顺序**补在后面
    assert [it["id"] for it in out] == [3, 5, 1, 2, 4]
    assert len(out) == len(items) == 5  # rank-only：长度不变，绝不置空（红线 1）
    assert [it["id"] for it in out].count(3) == 1  # 无丢失、无重复
    assert {it["id"] for it in out} == set(range(1, 6))
    assert out[0] is items[2]  # 元素本身未被拷贝改写


def test_local_ce_degenerate_scores_fall_back(monkeypatch, _reset_ce_state, caplog):
    """全池分数完全相同（打分失效，issue #307 观测 4 个 doc 全 -10.572）→ 不采用
    重排、按原序返回，status 标 ``rerank_degenerate``（区别于 degraded：模型没坏，
    是这一轮打分没有区分度，任何排序都只是噪声）。"""
    import logging

    import core.reranker as rr

    _ce_enable(monkeypatch)
    _install_ce(monkeypatch, {f"正文-{i}": -10.572 for i in range(1, 6)})
    items = _five_items()
    with caplog.at_level(logging.WARNING, logger="core.reranker"):
        out, status = rr.rerank("问题", items, 2)
    assert status == "rerank_degenerate"
    assert out is items  # 原对象、原顺序（不采用重排）
    assert [it["id"] for it in out] == [1, 2, 3, 4, 5]
    assert any("打分失效" in r.getMessage() for r in caplog.records)  # 日志留痕


def test_local_ce_pool_bigger_than_items_skips_scoring(monkeypatch, _reset_ce_state):
    """候选数 ≤ top_k 时整池都要 → 与 llm 路径同语义：原样返回 ok，不碰模型。"""
    import core.reranker as rr

    _ce_enable(monkeypatch)
    fake = _install_ce(monkeypatch, {})
    items = _five_items()
    out, status = rr.rerank("问题", items, 5)
    assert status == "ok"
    assert out is items
    assert fake.inputs == []  # 没有任何打分调用、模型从未加载


# ----------------------------------------------------------------
# 10.2 fail-open：打分器异常 / 模型缺失
# ----------------------------------------------------------------


def test_local_ce_scorer_exception_fail_open(monkeypatch, _reset_ce_state):
    """create_embedding 抛异常 → 原顺序 + degraded，绝不抛（红线 2）。"""
    import core.reranker as rr

    _ce_enable(monkeypatch)
    _install_ce(monkeypatch, {"正文-1": 0.9}, fail=RuntimeError("cross-encoder boom"))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items  # 原对象、原顺序


def test_local_ce_missing_model_loads_disk_once(monkeypatch, _reset_ce_state):
    """模型文件缺失 → degraded，且第二次调用不再走磁盘（不重复加载尝试）。"""
    import core.reranker as rr

    _ce_enable(monkeypatch, RERANK_LOCAL_MODEL_PATH="models/definitely-missing.gguf")

    hits: list[int] = []

    def counting_exists(path):
        hits.append(1)
        return False  # 模型文件不存在

    monkeypatch.setattr(rr, "_model_file_exists", counting_exists)

    items = _five_items()
    out1, status1 = rr.rerank("问题", items, 2)
    out2, status2 = rr.rerank("问题", items, 2)
    assert status1 == "degraded" and status2 == "degraded"
    assert out1 is items and out2 is items
    assert len(hits) == 1  # 只探测过一次磁盘，第二次直接被门闩挡住


def test_local_ce_missing_model_real_path_resolve(monkeypatch, _reset_ce_state):
    """真实文件检查路径下同样 degraded：相对路径按项目根解析，缺文件留痕原序返回。"""
    import core.reranker as rr

    _ce_enable(monkeypatch, RERANK_LOCAL_MODEL_PATH="models/no-such-model.gguf")
    resolved = rr._resolve_local_model_path()
    assert resolved.name == "no-such-model.gguf"  # 相对路径基于项目根解析
    assert not resolved.is_file()

    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "degraded"
    assert out is items  # 原顺序原对象，绝不置空（红线 1+2）


# ----------------------------------------------------------------
# 10.3 后端分派：llm 显式走 HTTP；非法值视同 local_ce（warn 一次）
# ----------------------------------------------------------------


def test_backend_llm_explicit_routes_to_http(monkeypatch):
    """RERANK_BACKEND="llm" → 走远端 HTTP listwise（假端点），绝不触碰 local_ce 装配。"""
    import core.reranker as rr

    _enable_rerank(monkeypatch, RERANK_BACKEND="llm")

    def _boom():
        raise AssertionError("llm 后端不应触碰 local_ce 模型装配缝")

    monkeypatch.setattr(rr, "_get_ce_model", _boom)
    calls: list = []
    _stub_post(monkeypatch, calls, payload=_chat_payload('{"ranking": ["C2", "C4"]}'))
    items = _five_items()
    out, status = rr.rerank("问题", items, 2)
    assert status == "ok"
    assert [it["id"] for it in out] == [2, 4]  # llm 行为与 issue #272 落地时逐位一致
    assert len(calls) == 1  # HTTP 路径确实命中


def test_backend_invalid_falls_back_to_local_ce_once(monkeypatch, _reset_ce_state, caplog):
    """RERANK_BACKEND 非法值 → 视同 local_ce 继续干活，并只 warn 一次（不抛）。"""
    import logging

    import core.reranker as rr

    _enable_rerank(monkeypatch, RERANK_BACKEND="weird-backend")
    _install_ce(monkeypatch, CE_SCORES_DESC_3_5_1_2_4)

    with caplog.at_level(logging.WARNING, logger="core.reranker"):
        out1, status1 = rr.rerank("问题", _five_items(), 2)
        _out2, status2 = rr.rerank("问题", _five_items(), 2)
    assert status1 == "ok" and status2 == "ok"  # 非法后端值没有打断检索
    assert [it["id"] for it in out1] == [3, 5, 1, 2, 4]
    warns = [r for r in caplog.records if "RERANK_BACKEND" in r.getMessage()]
    assert len(warns) == 1  # 只 warn 一次，不是每条查询刷一遍


# ----------------------------------------------------------------
# 10.4 逐条调用契约：一次调用只送一条 (query, doc)，防批量 abort 生产进程
# ----------------------------------------------------------------


def test_local_ce_one_pair_per_call_contract(monkeypatch, _reset_ce_state):
    """mock create_embedding：每次调用只收 1 条输入，任何批量都算违约。

    真实 llama.cpp 的 encoder 路径在批量下会触发 C 层断言直接 abort，
    Python 捕不到这条异常——所以这个契约必须由测试钉死，防止后人图省事。
    """
    import core.reranker as rr

    _ce_enable(monkeypatch)
    fake = _install_ce(monkeypatch, CE_SCORES_DESC_3_5_1_2_4)
    out, status = rr.rerank("问题", _five_items(), 2)
    assert status == "ok"
    assert [it["id"] for it in out] == [3, 5, 1, 2, 4]
    assert all(len(inputs) == 1 for inputs in fake.inputs)  # 每次 1 条，绝不批量
    assert len(fake.inputs) == 5  # 5 条候选 = 5 次逐条调用
    assert fake.inputs[0][0].index("</s>") == len("问题")  # 输入形状 f"{query}</s>{doc}"


# ----------------------------------------------------------------
# 10.5 关闭时零成本：连 core.reranker 模块都不被导入（红线 3 回归）
# ----------------------------------------------------------------


def test_reranker_module_not_imported_when_disabled(monkeypatch):
    """RERANK_ENABLED=0 → _hybrid_rrf 全程不 import core.reranker。"""
    import mcp_tools.memory as mm
    from config import Config

    monkeypatch.setattr(Config, "RERANK_ENABLED", False)
    sys.modules.pop("core.reranker", None)

    monkeypatch.setattr(
        mm,
        "_sem_candidate_items",
        lambda *a, **k: [
            {"id": 11, "type": "memory", "score": 0.9, "summary": "甲", "meta": {}},
        ],
    )
    monkeypatch.setattr(mm, "search_fts_status", lambda q, limit=50: ([{"node_id": 11}], "ok"))

    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "all", "", "", 5, 50, "")
    assert "core.reranker" not in sys.modules  # 调用后仍未导入
    assert "rerank" not in channels
    assert [it["id"] for it in items] == [11]


# ----------------------------------------------------------------
# 10.6 挂载集成：local_ce 启用时全长返回、零出网、候选深度 = RERANK_POOL
# ----------------------------------------------------------------


def test_hybrid_rrf_local_ce_keeps_full_pool_and_no_network(monkeypatch, _reset_ce_state):
    """启用 local_ce：零出网请求；返回池全长（top_k 之外仍按原顺序补齐）。"""
    import mcp_tools.memory as mm

    _ce_enable(monkeypatch, RERANK_POOL=20)
    fake = _install_ce(
        monkeypatch,
        {f"甲{i}": i * 0.1 for i in range(1, 9)},  # 甲8 最高
    )
    calls: list = []
    _stub_post(monkeypatch, calls)  # 一旦走网络立刻可观察

    def _fake_sem(query, scope, domain, bias, top_k, block, **kw):
        return [
            {"id": i, "type": "memory", "score": 0.9 - i * 0.01, "summary": f"甲{i}", "meta": {}} for i in range(1, 9)
        ]

    monkeypatch.setattr(mm, "_sem_candidate_items", _fake_sem)
    monkeypatch.setattr(mm, "search_fts_status", lambda q, limit=50: ([], "ok"))

    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "all", "", "", 5, 50, "")
    assert channels.get("rerank") == "ok"
    assert calls == []  # local_ce 全程零出网
    assert len(items) == 8  # rank-only：池全长（不是截回 top_k）
    assert items[0]["id"] == 8  # 打分最高者排最前
    assert len(fake.inputs) == 8  # 逐条打分


# ----------------------------------------------------------------
# 10.7 n_batch / n_ubatch 装配契约（issue #307：本地 embedding + local_ce
#     交替使用多轮，decode_batch 段错误——n_ubatch 必须显式可配且默认生效）
# ----------------------------------------------------------------


class _RecordingLlama:
    """替身 llama_cpp.Llama：只记录构造参数，绝不加载真模型（CI 无 635MB 文件）。

    create_embedding 按输入文本给一个确定性且互不相同的假分数——构造参数断言
    之外，还要求整条 local_ce 链路（装配 → 逐条打分 → 排序）在替身上能走通到
    status "ok"，防止测试悄悄退化成「构造即崩、fail-open 兜底」的假绿。
    """

    instances: ClassVar[list[_RecordingLlama]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    def create_embedding(self, inputs: list) -> dict:
        text = inputs[0]
        score = float(abs(hash(text)) % 10_000) / 10_000  # 确定性、逐文本不同
        return {"data": [{"embedding": [score]}]}


def _install_fake_llama_cpp(monkeypatch) -> type[_RecordingLlama]:
    """把 sys.modules['llama_cpp'] 换成替身模块（_get_ce_model 是函数内 import，
    from-import 走 sys.modules 查找，注入即生效；monkeypatch 自动还原）。"""
    import types

    fake_mod = types.ModuleType("llama_cpp")
    # setattr 动态挂（ModuleType 上点号赋值 Pyright 判 unknown attribute）；
    # 常量属性 setattr 被 B010 误伤，这里确实需要运行期动态挂。
    setattr(fake_mod, "LLAMA_POOLING_TYPE_RANK", 4)  # noqa: B010
    setattr(fake_mod, "Llama", _RecordingLlama)  # noqa: B010
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_mod)
    _RecordingLlama.instances = []
    return _RecordingLlama


def test_local_ce_n_ubatch_passed_to_llama(monkeypatch, _reset_ce_state):
    """默认配置（RERANK_LOCAL_N_UBATCH=512）→ Llama(...) 必须同时收到
    n_batch=4096 与 n_ubatch=512（#307 修复：只设 n_batch 不设 n_ubatch 是崩因）。"""
    import core.reranker as rr
    from config import Config

    _ce_enable(monkeypatch, RERANK_LOCAL_N_THREADS=0)
    monkeypatch.setattr(Config, "RERANK_LOCAL_MODEL_PATH", "models/bge-reranker-v2-m3-Q8_0.gguf")
    recording = _install_fake_llama_cpp(monkeypatch)
    monkeypatch.setattr(rr, "_model_file_exists", lambda path: True)

    assert rr.rerank("问题", _five_items(), 2)[1] == "ok"
    assert len(recording.instances) == 1
    kwargs = recording.instances[0].kwargs
    assert kwargs["n_batch"] == 4096  # 历史默认不变（Config.RERANK_LOCAL_N_BATCH）
    assert kwargs["n_ubatch"] == 512  # 显式传入（修复点）
    assert kwargs["embedding"] is True
    assert rr._CE_MODEL is recording.instances[0]  # 单例缓存的就是替身实例


def test_local_ce_n_ubatch_zero_omits_kwarg(monkeypatch, _reset_ce_state):
    """RERANK_LOCAL_N_UBATCH=0 → 不传 n_ubatch（= #307 修复前历史行为，逃生口）。"""

    import core.reranker as rr

    _ce_enable(monkeypatch, RERANK_LOCAL_N_BATCH=4096, RERANK_LOCAL_N_UBATCH=0)
    monkeypatch.setattr(rr, "_model_file_exists", lambda path: True)
    recording = _install_fake_llama_cpp(monkeypatch)

    assert rr.rerank("问题", _five_items(), 2)[1] == "ok"
    kwargs = recording.instances[0].kwargs
    assert kwargs["n_batch"] == 4096
    assert "n_ubatch" not in kwargs  # 历史行为：由 llama.cpp 取 n_ubatch = n_batch
    assert recording.instances[0].kwargs.get("n_ctx") == rr._CE_MODEL_CTX
