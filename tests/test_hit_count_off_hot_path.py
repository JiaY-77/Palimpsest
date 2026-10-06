"""hit_count 必须离开检索热路径
================================

``search_similar`` 是只读检索路径，不得在返回路径上写库（每次检索 = 一次写
事务会放大延迟、并发下丢计数）。命中计数先进进程内缓冲，由 ``flush_hit_counts()``
显式落库。

隔离：自建独立临时库（复用 conftest 的确定性 fake embedder）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading

import pytest
from conftest import _fake_embed

from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_hitcount_iso_")
    from config import Config

    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "hit.db")
    s = TriviumStore()
    s.embed_text = _fake_embed
    try:
        yield s
    finally:
        Config.DB_PATH = old
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _insert(s: TriviumStore, content: str) -> int:
    return s.insert_node(
        {"type": "memory", "content": content, "importance": 0.5, "domain": "general"},
        s.embed_text(content),
    )


def test_search_does_not_write_hit_count(iso_store):
    """检索返回后，库里不应出现 hit_count（写库已移出热路径）。"""
    s = iso_store
    content = "热路径不写库唯一标记词hotpathunique"
    nid = _insert(s, content)

    results = s.search_similar(s.embed_text(content), top_k=5, expand_depth=1)
    assert any(r.get("id") == nid for r in results), results

    assert s.get_node(nid)["payload"].get("hit_count") is None, "search_similar 仍在热路径里写 hit_count"


def test_flush_persists_accumulated_hits(iso_store):
    """flush 后计数按累加值落库。"""
    s = iso_store
    content = "缓冲落库唯一标记词flushunique"
    nid = _insert(s, content)
    emb = s.embed_text(content)

    for _ in range(3):
        s.search_similar(emb, top_k=5, expand_depth=1)
    assert s.get_node(nid)["payload"].get("hit_count") is None

    s.flush_hit_counts()

    payload = s.get_node(nid)["payload"]
    assert payload["hit_count"] == 3, payload
    assert payload.get("last_hit_at") is not None, payload


def test_second_flush_does_not_double_count(iso_store):
    """缓冲清空后再次 flush 不得重复累加。"""
    s = iso_store
    content = "幂等落库唯一标记词idemunique"
    nid = _insert(s, content)
    emb = s.embed_text(content)

    s.search_similar(emb, top_k=5, expand_depth=1)
    s.flush_hit_counts()
    assert s.get_node(nid)["payload"]["hit_count"] == 1

    s.flush_hit_counts()
    assert s.get_node(nid)["payload"]["hit_count"] == 1, "重复 flush 被算了两次"


def test_concurrent_searches_do_not_lose_counts(iso_store):
    """并发检索：不丢计数，且不得因进程内撞库静默返回空结果。

    回归背景：triviumdb 是**连接级排他**，而 Palimpsest 每操作开-关库。
    未做进程内串行化时，4 并发 100 次检索实测「命中 44 / 静默空结果 56」
    ——端点改线程池（真并发）后这类静默空结果会直接上线。
    """
    s = iso_store
    content = "并发计数唯一标记词concurrentunique"
    nid = _insert(s, content)
    emb = s.embed_text(content)

    threads_n, per_thread = 4, 5
    errors: list[BaseException] = []
    empty: list[object] = []

    def worker() -> None:
        for _ in range(per_thread):
            try:
                res = s.search_similar(emb, top_k=5, expand_depth=1)
            except BaseException as exc:  # noqa: BLE001 —— 记录后由主线程断言
                errors.append(exc)
                continue
            if not any(r.get("id") == nid for r in res):
                empty.append(res)

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert not empty, f"{len(empty)} 次并发检索静默返回空结果（进程内 DB 访问未串行化）：{empty[:1]}"
    s.flush_hit_counts()
    payload = s.get_node(nid)["payload"]
    assert payload["hit_count"] == threads_n * per_thread, payload


def test_flush_merges_with_existing_count(iso_store):
    """落库是合并语义：与库中已有 hit_count 相加，不清零其它字段。"""
    s = iso_store
    content = "合并语义唯一标记词mergeunique"
    nid = s.insert_node(
        {"type": "memory", "content": content, "importance": 0.5, "domain": "general", "hit_count": 7},
        s.embed_text(content),
    )
    s.search_similar(s.embed_text(content), top_k=5, expand_depth=1)
    s.flush_hit_counts()

    payload = s.get_node(nid)["payload"]
    assert payload["hit_count"] == 8, payload
    assert payload["content"] == content, payload
