"""端点必须跑在事件循环之外
============================

背景：``main.py`` 的业务端点体内是阻塞同步代码（embedding HTTP + TriviumDB
读写 + ``/export`` 全表扫描）。若定义成 ``async def`` 且体内没有 ``await``，
FastAPI 会把它放在事件循环里直接执行——单个慢 embedding 会阻塞整个 uvicorn
（含 ``/mcp``）。

守卫：所有「体内没有 await 的业务端点」必须是同步 ``def``（FastAPI 会把
同步端点丢到线程池执行，从而真正并发）。
"""

from __future__ import annotations

import inspect

import main

# 业务端点：体内都是阻塞同步代码，必须是同步 def
BLOCKING_ENDPOINTS = [
    "root",
    "export_memories",
    "summary",
    "get_memory",
    "delete_memory",
    "update_memory_payload",
    "patch_memory_payload",
    "update_memory_vector",
    "mem_search",
    "skill_search",
    "mem_hybrid_search",
    "mem_ingest",
    "mem_link",
    "graph_neighbors",
    "graph_communities",
    "mem_recent",
    "mem_stats",
]


def test_blocking_endpoints_are_sync_def():
    offenders = [name for name in BLOCKING_ENDPOINTS if inspect.iscoroutinefunction(getattr(main, name))]
    assert not offenders, f"这些端点是 async def 但体内无 await，会在事件循环里跑阻塞代码、串行化所有请求：{offenders}"


def test_report_endpoint_stays_async():
    """唯一例外：``/report`` 真的 ``await generate_report(...)``，必须保持 async。"""
    assert inspect.iscoroutinefunction(main.report_endpoint), "/report 走了 await，改成同步 def 会让协程不被执行"
