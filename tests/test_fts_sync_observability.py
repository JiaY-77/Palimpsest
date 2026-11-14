"""issue #296 回归测试 —— 索引同步失败必须可观测，正常同步必须如实上报。

问题独立成立（与任何单次检索问题无关的工程弱点）：``core.fts_index.sync_node``
内部把索引写失败吞成 ``False``（仅 logger.warning），而 ``_sync_fts_after_update``
此前把该返回值直接丢掉——两层都不断链，FTS/TDB 写入失败（典型为 fts.db 被其他
进程的读连接瞬间占锁）不留任何 API 侧信号，也没有补同步机制。

本文件钉四件事：
  1. 主用例：新建 → PUT 改 content → reembed → 新正文独有关键词必须命中
     （FTS 行与向量都按新正文落位）；
  2. 反例保护：既有能命中的 task 节点经同样的 PUT 重写不得变差；
  3. 失败路径：FTS 写入抛异常 → 主更新仍成功（200、payload 已更新），
     响应 ``fts_synced=False`` + ``warning``，且普通分支与 task 分支都覆盖；
  4. 自愈路径：故障期间主更新成功而索引行停在旧正文，``fts_index.rebuild``
     全量重建即可恢复新正文可检索。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config  # noqa: E402
from core import fts_index  # noqa: E402
from core import trivium_store as ts_mod  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


@contextlib.contextmanager
def _isolated_store(**kw):
    """独立临时库（与 test_reembed_and_readonly 同构），绝不触碰生产 data/。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_296_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    ts_mod.reset_connections_for_test()  # 干净起点（issue #167 单例注册表）
    try:
        yield TriviumStore(**kw)
    finally:
        ts_mod.reset_connections_for_test()  # 释放库文件（连接不再随操作关闭）
        Config.DB_PATH = old


def _client():
    from fastapi.testclient import TestClient

    import main

    return main, TestClient(main.app)


def _fts_rows(node_id: int) -> list[str]:
    """临时 fts.db 里该节点的 content 行（与当前 DB_PATH 同目录）。"""
    conn = sqlite3.connect(fts_index._db_path())
    try:
        return [
            (r[0] or "")
            for r in conn.execute("SELECT content FROM mem_fts WHERE node_id = ?", (int(node_id),)).fetchall()
        ]
    finally:
        conn.close()


def _fake_embed(text: str):
    from tests.conftest import _fake_embed as fe

    return fe(text)


def _vectors_close(vec, expected) -> bool:
    return len(vec) == len(expected) and max(abs(a - b) for a, b in zip(vec, expected, strict=False)) < 1e-5


# ---------------------------------------------------------------------------
# 1. 主用例：PUT 改写 + reembed → 新正文独有关键词必须命中
# ---------------------------------------------------------------------------


class TestPutReembedRetrievable:
    def test_memory_node_put_then_reembed_keyword_hits(self):
        main, client = _client()
        with _isolated_store() as store:
            old_content = "AAAA 初次内容 kw-old-j77"
            new_content = "BBBB 改写后的内容 kw-new-m94"
            nid = store.insert_node({"type": "memory", "content": old_content}, _fake_embed(old_content))
            # 模拟 mem_ingest 的初始索引状态（写入层才建索引，insert_node 不带）
            fts_index.index_node(nid, old_content)

            with patch.object(main, "_get_store", return_value=store):
                resp = client.put(f"/memory/{nid}", json={"content": new_content})
                assert resp.status_code == 200, resp.text
                assert resp.json()["fts_synced"] is True, "同步成功必须如实上报"
                # FTS 行必须已是新正文
                rows = _fts_rows(nid)
                assert rows == [new_content], f"FTS 行未按新正文重写: {rows}"
                # 新正文独有关键词必须命中（混合检索里 FTS 通道的一票）
                hits = [h["node_id"] for h in fts_index.search_fts("kw-new-m94")]
                assert nid in hits, f"改写后新关键词检索不到: {hits}"
                old_hits = [h["node_id"] for h in fts_index.search_fts("kw-old-j77")]
                assert nid not in old_hits, "旧关键词不该再命中同一行（行已整体重写）"
                # reembed 后一切不变且向量按新正文重算
                before_payload = store.get_node(nid)["payload"]
                resp2 = client.post(f"/memory/{nid}/reembed")
                assert resp2.status_code == 200, resp2.text
                assert resp2.json()["message"] and "重算" in resp2.json()["message"]
                node = store.get_node(nid)
                assert "vector_stale" not in node["payload"] or node["payload"].get("vector_stale") is False
                assert _vectors_close(node["vector"], _fake_embed(new_content)), "reembed 必须按新正文重算向量"
                assert (before_payload["content"]) == new_content
                hits2 = [h["node_id"] for h in fts_index.search_fts("kw-new-m94")]
                assert nid in hits2, "reembed 之后新关键词必须仍命中"

    def test_task_node_put_rewrite_keyword_hits(self):
        """type=task 路径（apply_task_patch）同样必须把 FTS 写到位。"""
        main, client = _client()
        with _isolated_store() as store:
            old_content = "[todo] 任务写路径旧正文 tk-old-b31"
            new_content = "[doing] 任务写路径新正文 tk-new-f55"
            nid = store.insert_node({"type": "task", "content": old_content}, _fake_embed(old_content))
            fts_index.index_node(nid, old_content)

            with patch.object(main, "_get_store", return_value=store):
                resp = client.put(f"/memory/{nid}", json={"content": new_content})
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["fts_synced"] is True
                rows = _fts_rows(nid)
                assert rows == [new_content], f"task 路径 FTS 行未按新正文重写: {rows}"
                hits = [h["node_id"] for h in fts_index.search_fts("tk-new-f55")]
                assert nid in hits, f"task 路径改写后新关键词检索不到: {hits}"
                # 日志节点等副作用不回呛：state 也是新的
                assert body["task_state"] == "doing"


# ---------------------------------------------------------------------------
# 3. 失败路径：FTS 写入抛异常 → 主更新仍成功，且 fts_synced=False + warning
# ---------------------------------------------------------------------------


class TestFtsFailureObservable:
    def test_memory_patch_fts_failure_no_block_and_observable(self):
        main, client = _client()
        with _isolated_store() as store:
            nid = store.insert_node({"type": "memory", "content": "初始正文"}, _fake_embed("初始正文"))

            def _boom(node_id, content, source_path=""):
                raise sqlite3.OperationalError("database is locked")  # 生产典型的瞬时占锁

            with (
                patch.object(main, "_get_store", return_value=store),
                patch.object(fts_index, "index_node", _boom),
            ):
                resp = client.patch(f"/memory/{nid}", json={"importance": 0.9})
            assert resp.status_code == 200, "一次索引失败不得毁掉主写路径"
            body = resp.json()
            assert body["fts_synced"] is False, "失败必须如实上报"
            assert "warning" in body and "索引" in body["warning"]
            assert store.get_node(nid)["payload"]["importance"] == 0.9, "主更新必须已完成"
            # 解除故障后再次更新 → 如实恢复 True
            with patch.object(main, "_get_store", return_value=store):
                resp = client.patch(f"/memory/{nid}", json={"importance": 0.8})
            assert resp.status_code == 200
            assert resp.json()["fts_synced"] is True

    def test_task_put_fts_failure_no_block_and_observable(self):
        """task 分支同口径：主更新（状态/正文）成功，fts_synced=False + warning。"""
        main, client = _client()
        with _isolated_store() as store:
            nid = store.insert_node(
                {"type": "task", "content": "[todo] 失败路径旧正文 fx-old-a1"}, _fake_embed("旧正文")
            )

            def _boom(node_id, content, source_path=""):
                raise sqlite3.OperationalError("database is locked")

            with (
                patch.object(main, "_get_store", return_value=store),
                patch.object(fts_index, "index_node", _boom),
            ):
                resp = client.put(f"/memory/{nid}", json={"task_state": "doing"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["fts_synced"] is False
            assert "warning" in body
            assert body["task_state"] == "doing", "状态更新必须照常完成"
            payload = store.get_node(nid)["payload"]
            assert payload["task_state"] == "doing"
            assert payload.get("last_touched_at"), "留痕链路不受索引失败影响"

    def test_put_fts_failure_then_rebuild_recovers(self):
        """自愈路径：故障期间主更新成功、索引行停在旧正文；rebuild 全量重建即恢复可检索。"""
        main, client = _client()
        with _isolated_store() as store:
            old_content = "初始正文 rb-old-q3"
            new_content = "改写后的正文 rb-new-z7"
            nid = store.insert_node({"type": "memory", "content": old_content}, _fake_embed(old_content))
            fts_index.index_node(nid, old_content)

            def _boom(node_id, content, source_path=""):
                raise sqlite3.OperationalError("database is locked")

            with (
                patch.object(main, "_get_store", return_value=store),
                patch.object(fts_index, "index_node", _boom),
            ):
                resp = client.put(f"/memory/{nid}", json={"content": new_content})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["fts_synced"] is False
            assert "warning" in body
            node = store.get_node(nid)
            assert node is not None and node["payload"]["content"] == new_content, "主更新必须已完成"
            assert _fts_rows(nid) == [old_content], "静默失联形态：索引行应停留在旧正文"
            # 补救路径（warning 提示的 rebuild）：全量重建后索引行按新正文落位
            count = fts_index.rebuild(store)
            assert count >= 1, "重建应至少索引一个节点"
            assert _fts_rows(nid) == [new_content], "rebuild 后 FTS 行必须按新正文重写"
            hits = [h["node_id"] for h in fts_index.search_fts("rb-new-z7")]
            assert nid in hits, "rebuild 之后新关键词必须命中"
