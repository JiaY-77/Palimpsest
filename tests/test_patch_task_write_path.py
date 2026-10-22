"""PATCH /memory/{id} 走任务写路径（与 PUT 同一写入口）。

锁定三个行为：
  1. ``type=task`` 节点经 PATCH 改状态必须留痕：``last_touched_at`` 补写、
     ``type=record`` 日志节点、``record -[LOGS]-> task`` 边——PATCH 曾直连
     ``store.update_payload`` 绕过这些（无痕状态变更），现与 PUT 共用
     ``_apply_payload_update``；
  2. 非法 ``task_state`` → 400 且不落库、无日志（与 PUT 同为 ValueError → 400）；
  3. 非 task 节点：部分更新合并保持；``content`` 触发 reembed 警告；不产生任务日志。

隔离与模式：与 ``test_task_state.py`` / ``test_rest_governance.py`` 相同——
conftest 的 session 级临时库 + fake embedder；节点断言按【唯一 project 前缀】
过滤，不假设全库为空。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from core.fts_index import remove_node
from mcp_tools import store


@pytest.fixture
def cleanup_nodes():
    """每条测试插入的节点测试结束全部删除（节点 + 相连边 + FTS 条目）。

    背景：conftest 是全文件共享的 session 级临时库，后跑的 test_smoke 的
    ``test_fts_check``（FTS 一致性巡检）与 ``test_task_archive``（全库扫描
    归档候选）对库里「别人插的节点」非常敏感——本文件的直插节点不经过
    任何 FTS 同步（``insert_node`` 不同步 FTS，只有 mem_ingest /
    update_content / 显式 sync_node 会），PATCH 被拒绝的节点更是从未碰过
    FTS，会以 ``missing_in_fts`` 污染 test_fts_check、以「多出的未完成
    任务」污染 test_task_archive 的 skipped 断言。因此本文件**不留残骸**：
    谁插的谁删（含 apply_task_patch 生成的 record 日志节点），并从
    fts.db 一并移除，保证跑完后共享库处于测试前的原状。
    """
    created: list[int] = []
    yield created
    for nid in created:
        # 先收 LOGS 入边指向的 record 日志节点（apply_task_patch 的留痕）
        for edge in store.get_incoming_edges(nid):
            if getattr(edge, "label", "") == "LOGS":
                src = getattr(edge, "source_id", None)
                if src:
                    store.delete_node(src)
                    remove_node(src)
        store.delete_node(nid)
        remove_node(nid)


def _ns() -> str:
    return f"pdw_{uuid.uuid4().hex[:10]}"


def _insert_task(content: str, project: str, **extra) -> int:
    """插入一个 task 域活跃节点（status=active 由 insert_node 自动写）。"""
    payload = {
        "type": "task",
        "domain": "task",
        "character_name": "task",
        "content": content,
        "importance": extra.pop("importance", 0.6),
        "project": project,
        **extra,
    }
    return store.insert_node(payload, store.embed_text(content))


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _logs_edges(node_id: int) -> int:
    """该节点 incoming 的 LOGS 边数量（无留痕时应为 0）。"""
    return len([e for e in store.get_incoming_edges(node_id) if getattr(e, "label", "") == "LOGS"])


def test_patch_task_node_writes_log_and_edge(client, cleanup_nodes):
    """PATCH task 节点走任务写路径：状态留痕 + 日志节点 + LOGS 边。"""
    nid = _insert_task("[todo] PATCH-1 任务写路径", _ns(), task_state="todo")
    cleanup_nodes.append(nid)
    resp = client.patch(f"/memory/{nid}", json={"task_state": "doing"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_state"] == "doing"
    assert body["state_changed"] is True
    assert body["previous_state"] == "todo"

    payload = store.get_node(nid)["payload"]
    assert payload["task_state"] == "doing"
    assert payload.get("last_touched_at"), "PATCH 必须补 last_touched_at"

    logs_edges = [e for e in store.get_incoming_edges(nid) if getattr(e, "label", "") == "LOGS"]
    assert len(logs_edges) == 1, "状态变化必须建 LOGS 边"
    # 日志节点本体存在且是 record（LOGS 边方向是 log -> task，source 是日志节点）
    log_payload = store.get_node(logs_edges[0].source_id)["payload"]
    assert log_payload["type"] == "record"
    # 部分 payload 不写空未出现字段：content 保留
    assert "PATCH-1 任务写路径" in payload["content"]


def test_patch_rejects_illegal_task_state(client, cleanup_nodes):
    """非法 task_state → 400，且 task_state 不变、无日志（与 PUT 同一护栏）。"""
    nid = _insert_task("[todo] PATCH-2 非法状态护栏", _ns(), task_state="todo")
    cleanup_nodes.append(nid)
    before = _logs_edges(nid)
    resp = client.patch(f"/memory/{nid}", json={"task_state": "flying"})
    assert resp.status_code == 400, resp.text
    payload = store.get_node(nid)["payload"]
    assert payload["task_state"] == "todo", "拒绝后状态必须保持原值"
    assert _logs_edges(nid) == before == 0, "拒绝路径不得产生任何留痕"


def test_patch_missing_node_returns_404(client):
    """PATCH 不存在的节点 → 404（与 PUT 同一 404 预判）。"""
    resp = client.patch("/memory/999999999", json={"task_state": "doing"})
    assert resp.status_code == 404, resp.text


def test_patch_non_task_node_merge_and_content_warning(client, cleanup_nodes):
    """普通 memory 节点：部分更新正常合并；改 content 返回 reembed 警告；无任务日志。"""
    content = "PATCH-4 合并护栏正文"
    nid = store.insert_node(
        {
            "type": "memory",
            "domain": "general",
            "content": content,
            "importance": 0.7,
        },
        store.embed_text(content),
    )
    cleanup_nodes.append(nid)
    # 部分更新：只动 importance，其余字段保留
    resp = client.patch(f"/memory/{nid}", json={"importance": 0.9})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    payload = store.get_node(nid)["payload"]
    assert payload["importance"] == 0.9
    assert payload["content"] == content, "未传入的 content 必须保留"
    assert payload["type"] == "memory"
    assert "warning" not in body, "不动 content 不应出现 reembed 警告"

    # 改 content → 必须返回 reembed 警告（与 PUT 非任务路径同一告警）
    resp = client.patch(f"/memory/{nid}", json={"content": "PATCH-4 改后的正文"})
    assert resp.status_code == 200, resp.text
    assert "warning" in resp.json()

    assert _logs_edges(nid) == 0, "普通 memory 节点不得产生任务日志"
