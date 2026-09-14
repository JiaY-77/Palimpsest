"""
/report 链路异步化回归测试
==========================
`core.reporting.generate_report` 由 async 端点 `main.py:/report` 直接 await。
本文件锁三条：

  1. LLM 调用必须走 `AsyncOpenAI` + await —— 用同步 client 会把整个 FastAPI
     事件循环卡住（一次 max_tokens=4000 的生成可能几十秒）；
  2. 全库记忆扫描不得在事件循环里同步跑（走 asyncio.to_thread）；
  3. 空库短路时不得发起任何 LLM 调用。

实现方式：把 `openai.AsyncOpenAI` 换成假客户端记录调用参数；把 `openai.OpenAI`
换成「一旦被实例化就断言失败」的哨兵类 —— 只要代码退回同步路径，测试必红。
"""

import asyncio
import time

import openai
import pytest


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, rec):
        self._rec = rec

    async def create(self, **kwargs):
        """async 方法：调用方不 await 就会拿到 coroutine 而非响应 → 测试直接失败。"""
        self._rec["create_kwargs"] = kwargs
        return _FakeResponse("报告正文")


class _FakeChat:
    def __init__(self, rec):
        self.completions = _FakeCompletions(rec)


class _FakeAsyncOpenAI:
    def __init__(self, rec, **kwargs):
        rec["client_kwargs"] = kwargs
        self.chat = _FakeChat(rec)


class _SyncOpenAISentinel:
    """同步 client 一旦被使用即失败 —— 异步回归的硬门槛。"""

    def __init__(self, *args, **kwargs):
        raise AssertionError("generate_report 不得使用同步 OpenAI 客户端（会阻塞事件循环）")


class _Store:
    """最小 store 桩：generate_report 只依赖 iter_payloads。"""

    def __init__(self, payloads):
        self._payloads = payloads

    def iter_payloads(self):
        for i, payload in enumerate(self._payloads):
            yield i + 1, payload


@pytest.fixture
def patch_llm(monkeypatch):
    """把 OpenAI 客户端与 LLM 配置都换成测试替身，返回记录字典。"""
    rec: dict = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: _FakeAsyncOpenAI(rec, **kw))
    monkeypatch.setattr(openai, "OpenAI", _SyncOpenAISentinel)

    from config import Config
    monkeypatch.setattr(Config, "get_llm_config", staticmethod(lambda: {
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "test-model",
    }))
    return rec


def test_generate_report_uses_async_client(patch_llm):
    from core.reporting import generate_report

    store = _Store([
        {"type": "memory", "content": "记忆一"},
        {"type": "task", "content": "记忆二"},
    ])
    result = asyncio.run(generate_report(store))

    assert result["status"] == "ok", result
    assert result["report"] == "报告正文"
    # 客户端与请求参数都来自 Config（证明走的是被 patch 的 AsyncOpenAI 分支）
    assert patch_llm["client_kwargs"]["api_key"] == "test-key"
    sent = patch_llm["create_kwargs"]
    assert sent["model"] == "test-model"
    prompt = sent["messages"][0]["content"]
    assert "记忆一" in prompt and "记忆二" in prompt, prompt


def test_generate_report_does_not_block_event_loop(patch_llm):
    """全库扫描（同步 DB 调用）必须移出事件循环：扫描期间心跳协程仍应被调度。"""
    from core.reporting import generate_report

    class _SlowStore(_Store):
        def iter_payloads(self):
            time.sleep(0.2)  # 模拟大库扫描
            yield 1, {"type": "memory", "content": "慢记忆"}

    ticks: list[int] = []

    async def main():
        async def ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks.append(1)

        task = asyncio.create_task(ticker())
        try:
            return await generate_report(_SlowStore([]))
        finally:
            task.cancel()

    result = asyncio.run(main())
    assert result["status"] == "ok", result
    assert len(ticks) >= 3, f"扫描期间事件循环被阻塞（心跳仅 {len(ticks)} 次）"


def test_generate_report_empty_store_skips_llm(patch_llm):
    from core.reporting import generate_report

    result = asyncio.run(generate_report(_Store([])))

    assert result["status"] == "error", result
    assert "没有记忆" in result["message"], result
    assert "create_kwargs" not in patch_llm, "空库不应发起 LLM 调用"
