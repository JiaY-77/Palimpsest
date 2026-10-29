"""
created_at 写入路径回归测试（issue #226）
========================================
kb_chunk / novel_chunk / git_commit 此前在 insert_node / insert_node_tx 里
created_at 硬编码 None，入库即缺字段：
  - freshness_decay 对这些层静默失效（返回 None）；
  - /mem/recent 混合 float/None 排序脆弱；
  - split_eval_set 时间切分放不下（missing_timestamp）。

断言：三类节点走两条写路径（普通 + 事务）插入后，
payload.created_at 都是可解析的 epoch 秒数值；且调用方显式传入的
created_at 不被覆盖（透传）。

隔离保证：本文件用独立临时库（自建 TriviumStore + 自指 DB_PATH，照
tests/test_mem_stats.py 模式），不触碰 conftest 共享库与生产库。
"""

import contextlib
import os
import shutil
import tempfile

import pytest

from config import Config
from core.trivium_store import TriviumStore

_ISSUE_226_TYPES = ("kb_chunk", "novel_chunk", "git_commit")


@pytest.fixture
def iso_store():
    """构造一个全新临时库上的独立 TriviumStore（与共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_created_at_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _payload_of(store, nid):
    node = store.get_node(nid)
    assert node is not None, f"节点 {nid} 不存在"
    return node["payload"]


@pytest.mark.parametrize("ntype", _ISSUE_226_TYPES)
def test_insert_node_stamps_created_at(iso_store, ntype):
    """issue #226：insert_node 未提供 created_at 时落 epoch 秒数值。"""
    s = iso_store
    nid = s.insert_node({"type": ntype, "content": f"{ntype} 节点"}, s.embed_text(ntype))
    created_at = _payload_of(s, nid)["created_at"]
    assert isinstance(created_at, (int, float)), f"{ntype} 的 created_at 不是数值: {created_at!r}"


@pytest.mark.parametrize("ntype", _ISSUE_226_TYPES)
def test_insert_node_honors_caller_created_at(iso_store, ntype):
    """调用方显式传入的 created_at 透传（此前被基础 payload 丢弃）。"""
    s = iso_store
    nid = s.insert_node(
        {"type": ntype, "content": "显式时间", "created_at": 1234.5},
        s.embed_text("显式时间"),
    )
    assert _payload_of(s, nid)["created_at"] == 1234.5


@pytest.mark.parametrize("ntype", _ISSUE_226_TYPES)
def test_insert_node_tx_stamps_created_at(iso_store, ntype):
    """issue #226：insert_node_tx（事务链路）未提供时同样落 epoch 秒。"""
    s = iso_store
    with s._acquire() as db, db.transaction() as tx:
        nid = s.insert_node_tx(tx, {"type": ntype, "content": f"事务 {ntype}"}, s.embed_text(ntype))
    created_at = _payload_of(s, nid)["created_at"]
    assert isinstance(created_at, (int, float)), f"{ntype} 事务写 created_at 不是数值: {created_at!r}"
