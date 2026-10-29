"""任务节点状态注册表测试（core.task_state + REST /tasks/active + 回填）。

覆盖点（对应任务书「测试要求」七条）：
  1. ``parse_task_state`` 正常映射（含「部分完成→doing」「已拍板·待执行→todo」）；
  2. ``parse_task_state`` 失败路径：无括号、否定句、非法值 → None；
  3. ``apply_task_patch`` 状态变化 → 生成 record 节点 + LOGS 边（验证边真的建了）；
  4. ``apply_task_patch`` 非法 task_state → raise（不落库）；
  5. ``/tasks/active``：``legacy=true`` 的老节点不出现；
  6. ``/tasks/active``：states 过滤生效、排序正确；
  7. 回填 dry-run 不写库、--apply 才写。

隔离：conftest 已把 DB_PATH 指向 session 级临时库 + fake embedder；
本文件断言一律按【唯一 project / 唯一 content 前缀】过滤自己插入的节点，
不假设全库为空（其它测试文件的节点同库共存）。

第 10 节（会话接续点 kind=handoff）例外：那些用例自建独立临时库
（``iso_store``，照 test_mem_stats / test_promote 的既有模式）——回填用例
会改写全库节点，绝不写 conftest 的共享 session 库。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from corpus_real_cases import EXPECTED_NONE, REAL_CORPUS

from core.task_state import (
    HANDOFF_KIND,
    TASK_STATES,
    UNKNOWN_STATE,
    _handoff_title,
    _is_handoff,
    _iso_from_epoch,
    apply_task_patch,
    backfill_task_state,
    list_active_tasks,
    parse_task_state,
)
from core.trivium_store import TriviumStore
from mcp_tools import store


def _ns() -> str:
    return f"tsk_{uuid.uuid4().hex[:10]}"


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


# ---------------------------------------------------------------------------
# 1. parse_task_state —— 正常映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mark", "expected"),
    [
        ("[todo]", "todo"),
        ("[待办]", "todo"),
        ("[待启动]", "todo"),
        ("[已拍板·待执行]", "todo"),
        ("[doing]", "doing"),
        ("[进行中]", "doing"),
        ("[部分完成]", "doing"),  # 部分完成 = 还在做，不是 done
        ("[blocked]", "blocked"),
        ("[挂账]", "blocked"),
        ("[阻塞]", "blocked"),
        ("[done]", "done"),
        ("[完成]", "done"),
        ("[已完成]", "done"),
        ("[canceled]", "canceled"),
        ("[取消]", "canceled"),
    ],
)
def test_parse_task_state_mapping(mark, expected):
    assert parse_task_state(f"{mark} T-100 任务标题") == expected


def test_parse_task_state_tolerates_case_and_spaces():
    """大小写 / 首尾与内部空格容错。"""
    assert parse_task_state("[ Doing ] 写注册表") == "doing"
    assert parse_task_state("[DONE]收尾") == "done"
    assert parse_task_state("[ 已 完成 ] 收尾") == "done"


def test_parse_task_state_only_first_bracket_on_first_line():
    """只认首行第一个方括号：正文里的后续括号 / 第二行括号都忽略。"""
    assert parse_task_state("[doing] 详见 [todo] 那个旁注") == "doing"
    assert parse_task_state("[done] 首行\n[doing] 第二行") == "done"


# ---------------------------------------------------------------------------
# 2. parse_task_state —— 失败路径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("没有方括号的任务描述", "无括号"),
        ("", "空内容"),
        ("[unknown_value] 未登记的标记", "非法标记"),
        ("[in_progress] 未登记的标记", "非法标记"),
        ("还没完成 [done] 的收尾", "否定句"),
        ("[未完成] 任务", "否定标记"),
    ],
)
def test_parse_task_state_failure_paths(content, why):
    assert parse_task_state(content) is None, why


def test_parse_task_state_negated_mark_defers_to_next_block():
    """否定提示命中的块**跳过**，不再整条丢弃（除非没有可命中块）。

    旧实现是「标记前文本 / 标记本身含否定词 → 整条返回 None」；改为逐块跳过，
    因为真实语料里否定经常只否定某一项（如「未完成」是某块说明）。
    注意：否定前缀会**累积**——首个块带否定后，其后同段文本的块也会被跳过，
    这是刻意的保守取向（宁可返 None 交人工，也不在否定语境里误报完成）。
    """
    # 括号外否定词（前缀）→ 该块被跳过，无其它块 → None
    assert parse_task_state("还没完成 [done] 的任务") is None
    # 括号内否定词 → 该块被跳过，无其它块 → None
    assert parse_task_state("[未完成] 任务") is None


# ---------------------------------------------------------------------------
# 3. apply_task_patch —— 状态变化 → record 节点 + LOGS 边
# ---------------------------------------------------------------------------


def _logs_edges(log_id: int) -> list:
    return [e for e in (store.get_edges(log_id) or []) if getattr(e, "label", None) == "LOGS"]


def test_apply_task_patch_writes_log_node_and_edge():
    project = _ns()
    nid = _insert_task("[todo] T-200 写状态注册表", project, task_state="todo", task_key="T-200")

    result = apply_task_patch(store, nid, {"task_state": "doing"})

    assert result["changed"] is True
    assert result["previous_state"] == "todo"
    assert result["state"] == "doing"
    payload = store.get_node(nid)["payload"]
    assert payload["task_state"] == "doing"
    assert payload["last_touched_at"]
    assert payload["last_touched_at"] == result["last_touched_at"]

    # 找到这条任务的日志节点：record 类型 + task_id 指向自己
    logs = [
        (log_id, pl) for log_id, pl in store.iter_payloads() if pl.get("type") == "record" and pl.get("task_id") == nid
    ]
    assert len(logs) == 1, f"应恰好生成 1 个日志节点，实际 {len(logs)}"
    log_id, log_pl = logs[0]
    assert log_pl.get("log_kind") == "task_state"
    assert log_pl.get("task_state") == "doing"
    assert log_pl.get("previous_state") == "todo"
    assert "[doing]" in log_pl.get("content", "")
    assert "T-200" in log_pl.get("content", "")

    # 关键：边真的建了 —— log -[LOGS]-> task
    edges = _logs_edges(log_id)
    assert any(getattr(e, "target_id", None) == nid for e in edges), f"LOGS 边未建立: {edges}"


def test_apply_task_patch_same_state_only_touches_timestamp():
    """状态没变 → 不写日志节点，但 last_touched_at 更新。"""
    project = _ns()
    nid = _insert_task("[doing] T-201 状态未变", project, task_state="doing")
    before = store.get_node(nid)["payload"].get("last_touched_at")

    result = apply_task_patch(store, nid, {"task_state": "doing"})

    assert result["changed"] is False
    logs = [pl for _i, pl in store.iter_payloads() if pl.get("type") == "record" and pl.get("task_id") == nid]
    assert logs == [], "状态未变不应生成日志节点"
    after = store.get_node(nid)["payload"]["last_touched_at"]
    assert after and after != before


def test_apply_task_patch_parses_content_when_no_explicit_state():
    """未显式给 task_state → 从 content 首行解析。"""
    project = _ns()
    nid = _insert_task("[blocked] T-202 卡在依赖上", project)

    result = apply_task_patch(store, nid, {"next_action": "等上游发版"})

    assert result["state"] == "blocked"
    assert result["changed"] is True
    payload = store.get_node(nid)["payload"]
    assert payload["task_state"] == "blocked"
    assert payload["next_action"] == "等上游发版"


def test_apply_task_patch_content_update_reflects_new_state():
    """改 content 首行标记 → 状态跟着变（content 是状态的来源之一）。"""
    project = _ns()
    nid = _insert_task("[todo] T-203 老标记", project, task_state="todo")

    result = apply_task_patch(store, nid, {"content": "[doing] T-203 新标记"})

    assert result["changed"] is True
    assert store.get_node(nid)["payload"]["task_state"] == "doing"


def test_apply_task_patch_unparseable_keeps_state():
    """显式值也没有、content 也解析不出 → 保持原值，只更新时间戳。"""
    project = _ns()
    nid = _insert_task("没有方括号的任务", project, task_state="doing")

    result = apply_task_patch(store, nid, {"note": "补一条备注"})

    assert result["changed"] is False
    assert result["state"] == "doing"
    assert store.get_node(nid)["payload"]["task_state"] == "doing"


# ---------------------------------------------------------------------------
# 4. apply_task_patch —— 非法 task_state 必须 raise（不落库）
# ---------------------------------------------------------------------------


def test_apply_task_patch_rejects_illegal_state():
    project = _ns()
    nid = _insert_task("[todo] T-204 非法状态护栏", project, task_state="todo")

    with pytest.raises(ValueError):
        apply_task_patch(store, nid, {"task_state": "in_progress"})

    # 未落库：状态仍是原值，也没有日志
    assert store.get_node(nid)["payload"]["task_state"] == "todo"
    logs = [pl for _i, pl in store.iter_payloads() if pl.get("type") == "record" and pl.get("task_id") == nid]
    assert logs == []


def test_apply_task_patch_missing_node_raises():
    with pytest.raises(ValueError):
        apply_task_patch(store, 999_999_999, {"task_state": "doing"})


def test_task_states_whitelist_contents():
    assert {"todo", "doing", "blocked", "done", "canceled", "unknown"} == TASK_STATES


# ---------------------------------------------------------------------------
# 5/6. list_active_tasks —— legacy 排除、states 过滤、排序
# ---------------------------------------------------------------------------


def test_list_active_excludes_legacy_nodes():
    project = _ns()
    live = _insert_task("[doing] T-300 活跃任务", project, task_state="doing")
    old = _insert_task("[doing] T-301 老节点", project, task_state="doing", legacy=True)

    data = list_active_tasks(store, project=project)

    ids = [it["id"] for it in data["results"]]
    assert live in ids
    assert old not in ids, "legacy=true 的老节点不应出现"
    assert data["total"] == 1


def test_list_active_include_legacy_opt_in():
    project = _ns()
    live = _insert_task("[doing] T-302 活跃", project, task_state="doing")
    old = _insert_task("[doing] T-303 老节点", project, task_state="doing", legacy=True)

    data = list_active_tasks(store, project=project, include_legacy=True)

    ids = {it["id"] for it in data["results"]}
    assert {live, old} <= ids


def test_list_active_state_filter_and_priority_sort():
    project = _ns()
    todo = _insert_task("[todo] T-400 待办", project, task_state="todo", last_touched_at="2026-01-01T00:00:00+08:00")
    doing = _insert_task(
        "[doing] T-401 进行中", project, task_state="doing", last_touched_at="2026-01-01T00:00:00+08:00"
    )
    blocked = _insert_task(
        "[blocked] T-402 阻塞", project, task_state="blocked", last_touched_at="2026-01-01T00:00:00+08:00"
    )
    done = _insert_task("[done] T-403 完成", project, task_state="done", last_touched_at="2026-06-01T00:00:00+08:00")
    canceled = _insert_task("[canceled] T-404 取消", project, task_state="canceled")

    # fresh_days=0：本用例锁的是状态过滤与优先级排序语义，硬编码旧日期只为
    # 拉开 last_touched_at 差距——关掉新鲜度维度，避免与折叠逻辑互相污染
    data = list_active_tasks(store, project=project, fresh_days=0)
    ids = [it["id"] for it in data["results"]]

    # doing > blocked > todo；done / canceled 不在默认三态里
    assert ids == [doing, blocked, todo]
    assert done not in ids and canceled not in ids
    assert data["total"] == 3


def test_list_active_sort_by_last_touched_desc_within_state():
    project = _ns()
    older = _insert_task("[doing] T-410 旧", project, task_state="doing", last_touched_at="2026-01-01T00:00:00+08:00")
    newer = _insert_task("[doing] T-411 新", project, task_state="doing", last_touched_at="2026-06-01T00:00:00+08:00")

    # fresh_days=0：同上，本用例只锁组内 last_touched_at 倒序语义
    data = list_active_tasks(store, project=project, fresh_days=0)
    assert [it["id"] for it in data["results"]] == [newer, older]


def test_list_active_states_param_string_and_limit():
    project = _ns()
    ids = [
        _insert_task("[doing] T-420 一", project, task_state="doing"),
        _insert_task("[blocked] T-421 二", project, task_state="blocked"),
        _insert_task("[todo] T-422 三", project, task_state="todo"),
    ]

    data = list_active_tasks(store, project=project, states="doing,blocked", limit=10)
    assert [it["id"] for it in data["results"]] == [ids[0], ids[1]]
    assert data["total"] == 2

    limited = list_active_tasks(store, project=project, states="todo,doing,blocked", limit=2)
    assert len(limited["results"]) == 2
    assert limited["total"] == 3  # total 是过滤后总数，不被 limit 截断


def test_list_active_illegal_states_fall_back_to_defaults():
    project = _ns()
    nid = _insert_task("[doing] T-430 兜底", project, task_state="doing")

    data = list_active_tasks(store, project=project, states="bogus,nonsense")
    assert nid in [it["id"] for it in data["results"]]


def test_list_active_computes_state_when_payload_missing():
    """老节点没写 payload.task_state → 现算兜底也能被列出。"""
    project = _ns()
    nid = _insert_task("[blocked] T-440 无显式状态", project)  # 不写 task_state

    data = list_active_tasks(store, project=project)
    row = next((it for it in data["results"] if it["id"] == nid), None)
    assert row is not None
    assert row["state"] == "blocked"


def test_list_active_brief_shape():
    project = _ns()
    nid = _insert_task(
        "[doing] T-450 字段检查",
        project,
        task_state="doing",
        task_key="T-450",
        title="字段检查",
        next_action="补测试",
        aliases=["450"],
        last_touched_at="2026-05-05T10:00:00+08:00",
        importance=0.7,
    )

    # fresh_days=0：本用例锁 brief 字段形状，硬编码旧日期只为填字段值——关掉折叠
    row = next(it for it in list_active_tasks(store, project=project, fresh_days=0)["results"] if it["id"] == nid)
    assert set(row) == {
        "id",
        "task_key",
        "title",
        "state",
        "project",
        "next_action",
        "aliases",
        "last_touched_at",
        "created_at",
        "importance",
        "kind",
    }
    assert row["task_key"] == "T-450"
    assert row["next_action"] == "补测试"
    assert row["aliases"] == ["450"]
    assert row["importance"] == 0.7
    assert row["kind"] == ""  # 无 kind 的普通任务 → 空串（不是 None）


# ---------------------------------------------------------------------------
# 7. backfill —— dry-run 不写库、--apply 才写
# ---------------------------------------------------------------------------


def test_backfill_dry_run_does_not_write():
    project = _ns()
    nid = _insert_task("[doing] T-500 回填预览", project, project_marker=project)

    result = backfill_task_state(store, dry_run=True)

    assert result["dry_run"] is True
    row = next(c for c in result["changes"] if c["id"] == nid)
    assert row["task_state"] == "doing"
    # 未写库：payload 里没有 task_state / legacy
    payload = store.get_node(nid)["payload"]
    assert "task_state" not in payload
    assert payload.get("legacy") is None


def test_backfill_apply_writes_state_and_legacy_flag():
    project = _ns()
    nid = _insert_task("[doing] T-501 回填写入", project, project_marker=project)

    result = backfill_task_state(store, dry_run=False)

    assert result["dry_run"] is False
    row = next(c for c in result["changes"] if c["id"] == nid)
    assert row["task_state"] == "doing"
    payload = store.get_node(nid)["payload"]
    assert payload["task_state"] == "doing"
    # doing 仍要跟进 → 不打 legacy
    assert payload["legacy"] is False

    # 仍活跃的任务回填后应出现在 /tasks/active
    assert nid in [it["id"] for it in list_active_tasks(store, project=project)["results"]]


def test_backfill_inactive_states_get_legacy_flag():
    """done / canceled / unknown 等不再跟进的状态 → legacy=True，不进工作集。"""
    project = _ns()
    done_id = _insert_task("[完成] T-510 已做完", project, project_marker=project)
    unknown_id = _insert_task("没有状态标记的存量任务", project, project_marker=project)

    backfill_task_state(store, dry_run=False)

    assert store.get_node(done_id)["payload"]["legacy"] is True
    assert store.get_node(unknown_id)["payload"]["legacy"] is True
    active_ids = [it["id"] for it in list_active_tasks(store, project=project)["results"]]
    assert done_id not in active_ids
    assert unknown_id not in active_ids


def test_backfill_unparseable_marks_unknown_and_needs_review():
    project = _ns()
    nid = _insert_task("没有方括号的存量任务", project, project_marker=project)

    result = backfill_task_state(store, dry_run=False)

    row = next(c for c in result["changes"] if c["id"] == nid)
    assert row["task_state"] == "unknown"
    assert row["needs_review"] is True
    payload = store.get_node(nid)["payload"]
    assert payload["task_state"] == "unknown"
    assert payload["needs_review"] is True
    assert payload["legacy"] is True


def test_backfill_is_idempotent():
    """已回填的节点重跑被跳过（already），不再产生 changes。"""
    project = _ns()
    nid = _insert_task("[todo] T-502 幂等", project, project_marker=project)

    backfill_task_state(store, dry_run=False)
    assert store.get_node(nid)["payload"]["legacy"] is False

    second = backfill_task_state(store, dry_run=False)
    assert nid not in [c["id"] for c in second["changes"]]
    assert second["already"] >= 1


def test_backfill_ignores_non_task_nodes():
    """非 task 类型节点不受回填影响。"""
    content = f"普通记忆 {uuid.uuid4().hex[:8]}"
    nid = store.insert_node({"type": "memory", "content": content}, store.embed_text(content))

    backfill_task_state(store, dry_run=False)

    payload = store.get_node(nid)["payload"]
    assert "task_state" not in payload
    assert payload.get("legacy") is None


# ---------------------------------------------------------------------------
# 端点 / MCP 工具注册（REST 层与实现同一份逻辑）
# ---------------------------------------------------------------------------


def test_tasks_active_endpoint_registered_and_filters(db_path):
    from fastapi.testclient import TestClient

    from main import app

    project = _ns()
    live = _insert_task("[doing] T-600 端点活跃", project, task_state="doing")
    old = _insert_task("[doing] T-601 端点老节点", project, task_state="doing", legacy=True)

    client = TestClient(app)
    resp = client.get("/tasks/active", params={"project": project})
    assert resp.status_code == 200
    data = resp.json()
    ids = [it["id"] for it in data["results"]]
    assert live in ids
    assert old not in ids
    assert data["total"] == 1


def test_tasks_active_endpoint_states_query(db_path):
    from fastapi.testclient import TestClient

    from main import app

    project = _ns()
    _insert_task("[todo] T-610 端点待办", project, task_state="todo")
    doing = _insert_task("[doing] T-611 端点进行", project, task_state="doing")

    client = TestClient(app)
    data = client.get("/tasks/active", params={"project": project, "states": "doing"}).json()
    assert [it["id"] for it in data["results"]] == [doing]


def test_mcp_tasks_active_tool_registered():
    import json

    from mcp_tools import tasks_active

    project = _ns()
    nid = _insert_task("[doing] T-620 MCP 工具", project, task_state="doing")

    data = json.loads(tasks_active(project=project, states="doing,blocked,todo", limit=5))
    assert nid in [it["id"] for it in data["results"]]
    assert "total" in data


def test_put_endpoint_routes_task_nodes_to_apply_task_patch(db_path):
    """``PUT /memory/{id}`` 对 task 节点走 apply_task_patch（留痕 + 返回 task_state）。"""
    from fastapi.testclient import TestClient

    from main import app

    project = _ns()
    nid = _insert_task("[todo] T-630 端点写路径", project, task_state="todo")

    client = TestClient(app)
    resp = client.put(f"/memory/{nid}", json={"task_state": "doing"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_state"] == "doing"
    assert body["state_changed"] is True
    assert body["previous_state"] == "todo"

    logs = [pl for _i, pl in store.iter_payloads() if pl.get("type") == "record" and pl.get("task_id") == nid]
    assert len(logs) == 1


def test_put_endpoint_rejects_illegal_task_state(db_path):
    """非法 task_state → 400，不落库。"""
    from fastapi.testclient import TestClient

    from main import app

    project = _ns()
    nid = _insert_task("[todo] T-631 非法状态", project, task_state="todo")

    client = TestClient(app)
    resp = client.put(f"/memory/{nid}", json={"task_state": "whatever"})
    assert resp.status_code == 400
    assert store.get_node(nid)["payload"]["task_state"] == "todo"


def test_put_endpoint_non_task_node_unchanged(db_path):
    """非 task 节点仍走原合并语义，不生成任务日志。"""
    from fastapi.testclient import TestClient

    from main import app

    content = f"普通记忆端点 {uuid.uuid4().hex[:8]}"
    nid = store.insert_node({"type": "memory", "content": content, "importance": 0.5}, store.embed_text(content))

    client = TestClient(app)
    resp = client.put(f"/memory/{nid}", json={"importance": 0.9})
    assert resp.status_code == 200
    payload = store.get_node(nid)["payload"]
    assert payload["importance"] == 0.9
    assert payload["content"] == content
    assert "task_state" not in resp.json()


# ---------------------------------------------------------------------------
# 8. 真实存量语料回归（复核发现的硬伤：真实语料 3/18 → 必须 ≥13/18）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_id", "content", "expected", "_why"),
    REAL_CORPUS,
    ids=[f"n{i}" for i, _c, _e, _w in REAL_CORPUS],
)
def test_parse_task_state_real_corpus(node_id, content, expected, _why):
    """逐条真实语料解析：期望状态、不得误判（尤其「部分完成」「完成」区分）。"""
    got = parse_task_state(content)
    assert got == expected, f"node={node_id} 期望 {expected!r}，实得 {got!r}（{_why}）"


def test_real_corpus_parse_rate_meets_bar():
    """18 条真实语料至少 13 条解析出非 None（验收线）。"""
    parsed = sum(1 for _i, c, _e, _w in REAL_CORPUS if parse_task_state(c) is not None)
    total = len(REAL_CORPUS)
    assert parsed >= 13, f"真实语料解析率不足：{parsed}/{total}"


def test_real_corpus_labels_match_expectations():
    """夹具期望值自洽：非 None 期望必须命中 TASK_STATES，None 条数固定。"""
    assert len(REAL_CORPUS) == 18
    for _i, _c, exp, _w in REAL_CORPUS:
        if exp is not None:
            assert exp in TASK_STATES
    assert EXPECTED_NONE == 0, "本条前置：18 条里都用可判定状态"


def test_parse_defers_to_earliest_block_when_multiple_present():
    """多个括号块时按出现顺序取第一个可命中块（不跨块合并）。"""
    assert parse_task_state("【挂账·等触发】… → [待办, 明天]") == "blocked"
    assert parse_task_state("[部分完成] T-1 → [完成]") == "doing"
    assert parse_task_state("任务 [完成 2026-01-01] 收尾 [挂账]") == "done"


def test_parse_full_width_round_brackets_only():
    """全角圆括号 ``（进行中）`` 嵌在全角【】内也要命中（跨嵌套括号）。"""
    assert parse_task_state("【任务·改名（进行中）2026-10-08 启动】正文") == "doing"
    assert parse_task_state("【技能瘦身（挂起待续）】正文") == "blocked"


def test_parse_status_word_mid_line():
    """状态词在正文中间（非首行开头）也要命中。"""
    assert parse_task_state("T-73：doctor 命令 → [待办，2026-09-10 用户指令「计入 backlog」]") == "todo"
    assert parse_task_state("普通描述文字 [阻塞] 尾部") == "blocked"


def test_parse_long_block_with_spaces_and_punctuation():
    """超长 / 含空格 / 含全角标点的标记块能被整块取出并匹配。"""
    assert parse_task_state("[待办，2026-09-10 用户指令「计入 backlog」] content") == "todo"
    assert parse_task_state("[挂账持续] token 优化") == "blocked"


def test_parse_block_internal_earliest_state_wins():
    """块内含多个状态词时，**最先出现的**胜出（2026-10-08 复核修正的硬约束）。

    两类真实语料必须同时正确：
      - 「挂账」在前、「落地」在后 → blocked（落地是别人的条件）；
      - 「进行中」在前、「挂起」在后 → doing（本任务在做）。
    旧实现按全局优先级（blocked 词一律在前）会把第一类误判成 doing、
    第二类误判成 blocked——本测试锁死「按块内位置取胜出」这条规则。
    """
    # blocked 在前：别人的完成条件（落地）不能覆盖本任务的挂账
    assert parse_task_state("T-57：→ [挂账，T-55 换脑落地后实施]（用户 2026-08") == "blocked"
    # doing 在前：本任务进行中，后面的「挂起待续」指别的事项
    assert parse_task_state("【任务·技能库瘦身（进行中）2026-09-21 挂起待续】正文") == "doing"
    # 仅「落地」出现（本任务进展描述）→ doing（弱词仍兜底）
    assert parse_task_state("T-55：→ [第二步入落地]（换脑已激活") == "doing"
    # 对称性：同一对词交换位置 → 结果随首现者改变
    assert parse_task_state("[落地后再挂账] 正文") == "doing"
    assert parse_task_state("[挂账后落地] 正文") == "blocked"


# ---------------------------------------------------------------------------
# 9. 状态变更日志节点 created_at 不得被吞（复核小问题 3）
# ---------------------------------------------------------------------------


def test_state_log_node_has_created_at():
    """日志节点的 ``created_at`` 必须是真实时间戳（insert_node 会吞同名键）。"""
    project = _ns()
    nid = _insert_task("[todo] T-700 日志时间戳", project, task_state="todo")

    apply_task_patch(store, nid, {"task_state": "doing"})

    log_pl = next(pl for _i, pl in store.iter_payloads() if pl.get("type") == "record" and pl.get("task_id") == nid)
    assert log_pl.get("created_at"), f"日志节点 created_at 被吞：{log_pl.get('created_at')!r}"
    # 必须是数值时间戳（time.time()），与库内既有节点一致——ISO 字符串会让
    # /mem/recent 的 created_at 排序在 float/str 之间抛 TypeError。
    assert isinstance(log_pl["created_at"], (int, float)), (
        f"created_at 应为数值时间戳，实际是 {type(log_pl['created_at']).__name__}：{log_pl['created_at']!r}"
    )


# ---------------------------------------------------------------------------
# 10. 会话接续点（kind=handoff）—— 默认排除 / 显式取回 / 通道透传 / 回填 / 检索
# ---------------------------------------------------------------------------
#
# 隔离：本节用例全部走 iso_store（自建独立临时库）——回填用例会改写全库节点、
# 检索用例断言精确命中，写进 conftest 共享 session 库会污染其它测试。
# 模式照 test_mem_stats / test_promote 的既有 iso_store：改 Config.DB_PATH
# + 重指 mcp_tools._common.store.db_path（导入时构造的模块级单例，只改
# Config 不够）+ finally 还原。


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与 conftest 共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_task_iso_")
    from config import Config
    from mcp_tools import store as shared_store

    old_db = Config.DB_PATH
    old_shared = shared_store.db_path
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    shared_store.db_path = Config.DB_PATH
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old_db
        shared_store.db_path = old_shared
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _iso_insert_task(s, content: str, project: str, **extra) -> int:
    """往 iso 库插入一个 task 域活跃节点（不走共享库单例）。"""
    payload = {
        "type": "task",
        "domain": "task",
        "character_name": "task",
        "content": content,
        "importance": extra.pop("importance", 0.6),
        "project": project,
        **extra,
    }
    return s.insert_node(payload, s.embed_text(content))


def _handoff_and_normal(iso, project: str) -> tuple[int, int]:
    """一对固定搭配：handoff 接续点 + 陪跑普通任务（同一个 project 下）。"""
    handoff = _iso_insert_task(
        iso,
        "[doing] 接续点·2026-10-16 任务治理整理 · 新会话先读这条",
        project,
        task_state="doing",
        kind=HANDOFF_KIND,
    )
    normal = _iso_insert_task(iso, "[doing] T-800 普通活跃任务", project, task_state="doing")
    return handoff, normal


def test_list_active_excludes_handoff_by_default(iso_store):
    """kind=handoff 的活跃 task 默认不出现；陪跑普通任务照常出现（防一刀切假通过）。"""
    project = _ns()
    handoff, normal = _handoff_and_normal(iso_store, project)

    data = list_active_tasks(iso_store, project=project)

    ids = {it["id"] for it in data["results"]}
    assert normal in ids, "陪跑的普通任务应照常返回（排除不能是清空列表）"
    assert handoff not in ids, "kind=handoff 的接续点默认应被排除"
    assert data["total"] == 1


def test_list_active_include_handoff_opt_in(iso_store):
    """include_handoff=True 显式取回接续点（陪跑普通任务两种情况都在）。"""
    project = _ns()
    handoff, normal = _handoff_and_normal(iso_store, project)

    data = list_active_tasks(iso_store, project=project, include_handoff=True)

    ids = {it["id"] for it in data["results"]}
    assert handoff in ids and normal in ids
    assert data["total"] == 2
    # 显式取回时靠 kind 字段识别接续点
    row = next(it for it in data["results"] if it["id"] == handoff)
    assert row["kind"] == HANDOFF_KIND


def _iso_insert_handoff_with_age(s, project: str, *, importance: float, age_days: float) -> int:
    """插入一个 kind=handoff 接续点，并把 created_at 补写为 days 天前（生产同款补写）。

    insert_node 会吞掉传入的 created_at（占位 None），插入后 update_payload 补写
    （与 test_list_active_fresh_fallback_to_created_at_when_touched_null 同款做法）。
    """
    nid = _iso_insert_task(
        s,
        "[doing] 【★接续点】检索排序修复测试用接续点 · 新会话先读这条",
        project,
        task_state="doing",
        kind=HANDOFF_KIND,
        importance=importance,
    )
    s.update_payload(nid, {"created_at": time.time() - age_days * 86400})
    return nid


def test_handoff_order_recency_beats_importance(iso_store):
    """接续点组内 created_at 主序：老条 importance 更高也不能压住最新条（2026-10-22 实测回归）。

    生产实测：新接续点 importance=0.92，10-17 老条 0.93 → 旧排序（importance 主序）
    返回的第一条是老条，新会话「先读这条」读到过期上下文。修后时间为主、
    importance 只作同刻次序。
    """
    project = _ns()
    old_nid = _iso_insert_handoff_with_age(iso_store, project, importance=0.95, age_days=5)
    new_nid = _iso_insert_handoff_with_age(iso_store, project, importance=0.5, age_days=0)

    data = list_active_tasks(iso_store, project=project, include_handoff=True)

    ids = [it["id"] for it in data["results"]]
    assert ids.index(new_nid) < ids.index(old_nid), (
        f"最新的接续点必须排第一（时间主序）：实际顺序 {ids}，new={new_nid} old={old_nid}"
    )
    assert ids[0] == new_nid


def test_handoff_order_handoffs_still_pinned_and_default_excluded(iso_store):
    """时间主序不破坏置顶语义：接续点（含老条）全部排在普通任务之前；include_handoff=False 默认排除照旧。"""
    project = _ns()
    old_handoff = _iso_insert_handoff_with_age(iso_store, project, importance=0.95, age_days=5)
    new_handoff = _iso_insert_handoff_with_age(iso_store, project, importance=0.5, age_days=0)
    normal_a = _iso_insert_task(iso_store, "[doing] T-901 陪跑普通任务甲", project, task_state="doing")
    normal_b = _iso_insert_task(iso_store, "[doing] T-902 陪跑普通任务乙", project, task_state="doing")

    included = list_active_tasks(iso_store, project=project, include_handoff=True)
    excluded = list_active_tasks(iso_store, project=project)

    ids = [it["id"] for it in included["results"]]
    assert ids.index(new_handoff) < ids.index(old_handoff), "接续点组内仍按时间倒序"
    assert ids.index(old_handoff) < ids.index(normal_a), "老接续点也要置顶在普通任务之前"
    assert set(ids[:2]) == {new_handoff, old_handoff}, "前两名必须是两条接续点"
    assert {normal_a, normal_b} & set(ids) == {normal_a, normal_b}
    # 默认排除行为不受影响
    default_ids = {it["id"] for it in excluded["results"]}
    assert old_handoff not in default_ids and new_handoff not in default_ids
    assert {normal_a, normal_b} <= default_ids


def test_mcp_tasks_active_include_handoff_passes_through(iso_store):
    """MCP 工具 tasks_active：默认排除 / include_handoff=True 取回。"""
    import json

    from mcp_tools import tasks_active

    project = _ns()
    handoff, normal = _handoff_and_normal(iso_store, project)

    default = json.loads(tasks_active(project=project, limit=10))
    default_ids = {it["id"] for it in default["results"]}
    assert handoff not in default_ids and normal in default_ids

    included = json.loads(tasks_active(project=project, limit=10, include_handoff=True))
    included_ids = {it["id"] for it in included["results"]}
    assert handoff in included_ids and normal in included_ids


def test_tasks_active_endpoint_include_handoff_query(iso_store):
    """REST ``GET /tasks/active``：默认排除 / ``?include_handoff=true`` 取回。"""
    from fastapi.testclient import TestClient

    import main
    from main import app

    project = _ns()
    handoff, normal = _handoff_and_normal(iso_store, project)

    # main._store 是惰性单例（db_path 在首次构造那刻固定，可能已被其它测试
    # 指到共享库）——测试期间显式替换成 iso 实例，结束置 None 让其按需重建
    main._store = iso_store
    try:
        client = TestClient(app)
        default = client.get("/tasks/active", params={"project": project}).json()
        default_ids = {it["id"] for it in default["results"]}
        assert handoff not in default_ids and normal in default_ids

        included = client.get("/tasks/active", params={"project": project, "include_handoff": "true"}).json()
        included_ids = {it["id"] for it in included["results"]}
        assert handoff in included_ids and normal in included_ids
    finally:
        main._store = None


def test_backfill_handoff_marks_kind_and_title(iso_store):
    """回填：dry-run 只预览 → apply 打 kind + 补 title → 重跑幂等 0 变更。"""
    project = _ns()
    # 存量形态 A：整行就是一个全角标记块（状态词嵌块内 → doing；title 取块内
    # 信息量最大的段 = 日期+主题段，见 ``_handoff_title`` 的取最长段规则）
    wrapped = _iso_insert_task(
        iso_store,
        "【★接续点·2026-10-16 任务治理整理（进行中）+ 下一步外包 · 新会话先读这条】",
        project,
        task_key="T-810",
    )
    # 存量形态 B：锚词在块外正文、状态在半角块里（块外正文同样参与切段——
    # 只取块内容会把正文丢光、退化成状态词「进行中」）
    plain = _iso_insert_task(
        iso_store,
        "接续点 / 会话接续 / 新聊天先读 —— 2026-10-13~14 大修 · [进行中] 细节见正文",
        project,
    )
    # 陪跑：普通任务不被打 kind
    normal = _iso_insert_task(iso_store, "[doing] T-811 普通任务", project, task_state="doing")

    # dry-run：只预览不写库（kind / title 都不落盘）
    preview = backfill_task_state(iso_store, dry_run=True)
    assert preview["dry_run"] is True
    assert preview["handoff"] == 2
    assert [c["id"] for c in preview["changes"] if c["handoff"]] == [wrapped, plain]
    for nid in (wrapped, plain):
        payload = iso_store.get_node(nid)["payload"]
        assert "kind" not in payload, "dry-run 不得写库"
        assert "title" not in payload, "dry-run 不得写库"

    # apply：kind + title 落库
    result = backfill_task_state(iso_store, dry_run=False)
    assert result["handoff"] == 2
    wp = iso_store.get_node(wrapped)["payload"]
    assert wp["kind"] == HANDOFF_KIND
    # 整行标记块 → 块内切段，锚词段全丢，取最长 = 日期+主题段（不再是退回 task_key）
    assert wp["title"] == "2026-10-16 任务治理整理（进行中）+ 下一步外包"
    pp = iso_store.get_node(plain)["payload"]
    assert pp["kind"] == HANDOFF_KIND
    # 块外正文参与切段：锚词段丢弃后最长段 = 日期+主题段（不是状态词「进行中」）
    assert pp["title"] == "2026-10-13~14 大修"
    assert len(pp["title"]) <= 120
    np_ = iso_store.get_node(normal)["payload"]
    assert np_.get("kind") is None, "普通任务不得被打 kind"

    # 回填后：接续点默认排除、显式可取回（title 已补，取回时能认出来）
    default_ids = {it["id"] for it in list_active_tasks(iso_store, project=project)["results"]}
    assert wrapped not in default_ids and plain not in default_ids
    included = {it["id"]: it for it in list_active_tasks(iso_store, project=project, include_handoff=True)["results"]}
    assert wrapped in included and plain in included
    assert included[plain]["title"] == pp["title"]

    # 重跑幂等：第二次 changes 为空、handoff 计数为 0
    second = backfill_task_state(iso_store, dry_run=False)
    assert second["changes"] == []
    assert second["handoff"] == 0


def test_backfill_handoff_title_truncates_at_120(iso_store):
    """title 摘要超长截断到 120 字符（锚词段先丢弃，长段才是 title 主体）。"""
    project = _ns()
    long_line = "接续点·超长叙述" + "细节" * 80  # 首行远超 120 字符、无括号块
    nid = _iso_insert_task(iso_store, long_line, project)

    backfill_task_state(iso_store, dry_run=False)

    title = iso_store.get_node(nid)["payload"]["title"]
    assert title.startswith("超长叙述")
    assert len(title) == 120


def test_handoff_exclusion_does_not_affect_mem_search(iso_store):
    """被 tasks_active 排除的接续点，mem_search 仍能命中——排除只在查询层，不进检索层。"""
    import json

    from mcp_tools import mem_search

    project = _ns()
    content = "接续点：任务治理整理的跨会话交接，含下一步外包安排与注意事项"
    nid = _iso_insert_task(iso_store, content, project, task_state="doing", kind=HANDOFF_KIND)

    # 查询层默认排除
    data = list_active_tasks(iso_store, project=project)
    assert nid not in {it["id"] for it in data["results"]}

    # 检索层不受影响：type=task 属 facts 层，默认 tier 检索仍应命中
    hits = json.loads(mem_search(content, scope="all", top_k=5))
    ids = {h["id"] for h in hits["results"]}
    assert nid in ids, "排除逻辑不得进检索层：接续点必须仍可被 mem_search 命中"


# 生产库逐字首行（2026-10-16）：4 条真接续点 + 3 条「首行包含『接续点』但不在
# 开头」的反例。旧「首行包含」判据 7 条全中（反例误中 = 真待办被打 handoff、
# 从活跃列表隐形）；新「以标题词开头」判据只中前 4 条。
IS_HANDOFF_REAL_SHAPES = [
    # 真接续点：整行一个标记块（★ 前缀装饰）
    (True, "【★接续点·2026-10-16 OpenCode Go 大改落地 + 思维底片评估 · 新会话先读这条】"),
    # 真接续点：锚词块 + 日期主题块（两块）
    (True, "【接续点 / 会话接续 / 新聊天先读】【2026-10-12 工作台 待办级联 + 周总结删除】"),
    # 真接续点：块外正文直接以「接续点」开头（无 【】）
    (True, "接续点 / 会话接续 / 新聊天先读 —— 2026-10-14 文档同步 + 委派通道大修 + 「其他 agent 记忆槽位」调研"),
    # 真接续点：``[...]`` 前缀状态块 + 标记块
    (True, "[完成] 【★接续点·2026-10-16 Palimpsest 任务治理整理 · 新会话先读这条】"),
    # 反例（逐字）：「接续点」在句中——讨论机制的真待办
    (False, "[待办] Palimpsest「接续点 × 任务列表」解耦（走 PR，2026-10-16 立）"),
    # 反例（逐字）：「接续点」在句中且自述不是接续点
    (False, "【历史留痕·记忆同行实测 2026-10-10 第一步】（⚠️ 本条为中途状态，不是当前接续点）"),
    # 反例：普通待办（首行不含「接续点」）
    (False, "验收用普通待办：把某件事做完"),
]


@pytest.mark.parametrize(
    ("expected", "first_line"),
    IS_HANDOFF_REAL_SHAPES,
    ids=[
        "handoff-wrapped-star",
        "handoff-two-blocks",
        "handoff-plain-text",
        "handoff-state-prefix",
        "real-todo-2333",
        "self-declared-non-handoff-1844",
        "normal-todo",
    ],
)
def test_is_handoff_real_shapes(expected, first_line):
    """7 条生产库逐字首行：只有「以接续点作为标题词开头」的才判 True。

    旧「首行包含」判据把后 3 条也判成接续点——句中含「接续点」的真待办
    （真待办形态）会被打 kind=handoff 并从活跃列表隐形；自述「不是当前
    接续点」的留痕同样误中。
    """
    got = _is_handoff({"content": first_line})
    assert got is expected, f"首行={first_line!r} 期望 handoff={expected}，实际 {got}"


def test_backfill_handoff_vs_real_todo(iso_store):
    """回填联动（误判回归锁）：真接续点打 kind，句中含「接续点」的真待办不打。

    生产实测：旧「首行包含」判据 99 条 task 命中 60 条 handoff、活跃三态里
    5 条含真待办——直接 apply 会让真待办隐形，因此先收紧判据。
    """
    project = _ns()
    # 逐字首行：真待办（「接续点」在句中，讨论的是机制本身）
    real_todo = _iso_insert_task(
        iso_store,
        "[待办] Palimpsest「接续点 × 任务列表」解耦（走 PR，2026-10-16 立）",
        project,
        task_state="todo",
    )
    # 生产真实接续点逐字首行：``[完成]`` 前缀块 + 标记块形态
    handoff = _iso_insert_task(
        iso_store,
        "[完成] 【★接续点·2026-10-16 Palimpsest 任务治理整理 · 新会话先读这条】",
        project,
    )

    # dry-run：changes 里真接续点 handoff=True；真待办必须 handoff=False
    preview = backfill_task_state(iso_store, dry_run=True)
    by_id = {c["id"]: c for c in preview["changes"]}
    assert by_id[handoff]["handoff"] is True, "真接续点应判 handoff=True"
    assert by_id[real_todo]["handoff"] is False, "句中含「接续点」的真待办不得判为接续点"

    # apply：真接续点打 kind；真待办不得被打 kind（打上即从活跃列表隐形）
    backfill_task_state(iso_store, dry_run=False)
    assert iso_store.get_node(handoff)["payload"]["kind"] == HANDOFF_KIND
    assert iso_store.get_node(real_todo)["payload"].get("kind") is None, "真待办形态不得被打 kind=handoff"

    # 幂等：apply 后重跑 dry-run → changes 为空
    second = backfill_task_state(iso_store, dry_run=True)
    assert second["changes"] == []


# ---------------------------------------------------------------------------
# 11. title 摘要规则 —— 块内容 + 块外正文切段，取信息量最大的段
# ---------------------------------------------------------------------------
#
# 旧规则「先删括号块再取剩余」在生产主流形态（整行一个块）上删完为空 →
# title 退化成常量「接续点」，等于没补（2026-10-16 验收实测反例）。本节
# 用生产库 4 条接续点 + 2 条普通任务的**逐字**首行锁死新规则。


# 生产库真实接续点首行（逐字，2026-10-16 验收表）：(node_id, 首行, title 必含子串)
REAL_HANDOFF_SHAPES = [
    (
        2307,
        "【★接续点·2026-10-16 OpenCode Go 大改落地 + 思维底片评估 · 新会话先读这条】",
        "OpenCode Go 大改落地 + 思维底片评估",
    ),
    (2317, "【★接续点·2026-10-16 MemoryHub 读侧落地 + 验收通过 · 新会话先读这条】", "MemoryHub 读侧落地 + 验收通过"),
    (
        1914,
        "【接续点 / 会话接续 / 新聊天先读】【2026-10-12 工作台 待办级联 + 周总结删除】",
        "工作台 待办级联 + 周总结删除",
    ),
    (1919, "【接续点 / 会话接续 / 新聊天先读】【2026-10-12 待办降级为普通列表项（新命令）】", "待办降级为普通列表项"),
]


@pytest.mark.parametrize(
    ("node_id", "first_line", "must_contain"),
    REAL_HANDOFF_SHAPES,
    ids=[str(node_id) for node_id, _line, _must in REAL_HANDOFF_SHAPES],
)
def test_handoff_title_real_shapes(node_id, first_line, must_contain):
    """生产库 4 条接续点逐字首行：title 必含日期+主题段，不得退化成常量。

    旧「先删块」规则在这 4 条上全部退化为空串 → 回退「接续点」（或 task_key），
    title 互相无法区分；新「取最长段」规则必须让每条认得出自己是哪条。
    """
    title = _handoff_title({"content": first_line})
    assert must_contain in title, f"node={node_id} title={title!r} 应包含 {must_contain!r}"
    assert title != "接续点", f"node={node_id} title 退化为常量「接续点」"
    assert title != "", f"node={node_id} title 为空"


def test_handoff_title_anchor_only_falls_back():
    """兜底：整行只有锚词（块内/块外都没有信息量）→ 退回 task_key → 再退回「接续点」。"""
    assert _handoff_title({"content": "【★接续点】"}) == "接续点"
    assert _handoff_title({"content": "【★接续点·新会话先读这条】"}) == "接续点"
    assert _handoff_title({"content": "接续点 / 会话接续 / 新聊天先读"}) == "接续点"
    assert _handoff_title({"content": "【★接续点】", "task_key": "T-900"}) == "T-900"
    assert _handoff_title({"content": ""}) == "接续点"


def test_backfill_fills_empty_title_on_normal_tasks(iso_store):
    """非接续点、title 为空的普通任务：回填后 title 非空（同一套摘要规则）。

    实测：活跃列表里 title 为空的普通任务都是
    「整行一个块」形态，与接续点同病——空 title 在列表里读不出是哪条任务。
    """
    project = _ns()
    # 同形态样例（整行一个块、无锚词 → 规则退化到「最长段」）
    blob = _iso_insert_task(
        iso_store,
        "【待办·周末去看演出（2026-10-15 定）】",
        project,
    )
    # 「块 + 块外正文」的普通任务（无锚词，最长段 = 块外正文）
    bracketed = _iso_insert_task(iso_store, "[todo] T-910 普通待办任务", project)

    backfill_task_state(iso_store, dry_run=False)

    bt = iso_store.get_node(blob)["payload"]
    assert (bt.get("title") or "").strip(), "普通任务（整行块）回填后 title 仍为空"
    assert "周末去看演出" in bt["title"]
    kt = iso_store.get_node(bracketed)["payload"]
    assert kt.get("title") == "T-910 普通待办任务"
    # 普通任务补 title 不得顺手打 kind
    assert bt.get("kind") is None and kt.get("kind") is None


def test_backfill_title_idempotent_rerun(iso_store):
    """apply 后重跑 dry-run：changes 为空且 already == scanned（title 非空即就绪）。"""
    project = _ns()
    _iso_insert_task(
        iso_store,
        "【★接续点·2026-10-16 任务治理整理（进行中）+ 下一步外包 · 新会话先读这条】",
        project,
    )
    _iso_insert_task(iso_store, "[doing] T-911 普通任务", project, task_state="doing")

    first = backfill_task_state(iso_store, dry_run=False)
    assert first["changes"], "首次 apply 应产生变更"

    second = backfill_task_state(iso_store, dry_run=True)
    assert second["changes"] == []
    assert second["already"] == second["scanned"]


# ---------------------------------------------------------------------------
# 12. 写入层自动打标（mem_ingest / apply_task_patch）+ 状态过滤豁免
#     —— 生产 2363 / 2393 回归（2026-10-17 复盘）
# ---------------------------------------------------------------------------
#
# 根因一：kind=handoff 此前只有存量回填（backfill_task_state）会打——回填之后
#   纯 ingest 进来的接续点一律缺 kind，include_handoff=True 也捞不回；
# 根因二：_matches_filters 要求生效状态 ∈ {todo, doing, blocked}，接续点首行是
#   标记块、解析不出状态 → 三条列表（默认 / include_handoff / include_legacy）
#   全隐形。
# 本节锁死两个修复：写入路径自动打标（_is_handoff 首行判据不放宽）+ 接续点豁免
# 状态判定（豁免≠塞假状态：task_state 仍如实反映解析结果）。
# 隔离：一律走本文件的 iso_store（独立临时库）；mem_ingest 类用例经共享单例写入，
# iso_store 已把单例 db_path 重指到临时库（见本文件第 10 节的既有约定）。


def _ingest_task(content: str, **kwargs) -> int:
    """经 mem_ingest（MCP/REST/CLI 三通道的公共实现）写一个 type=task 节点。"""
    import json

    from mcp_tools import mem_ingest

    res = json.loads(mem_ingest(content, type="task", domain="task", **kwargs))
    assert res["stored"] is True, f"mem_ingest 应回执 stored=True：{res}"
    return res["node_id"]


def _iso_payload(iso, nid: int) -> dict:
    return (iso.get_node(nid) or {}).get("payload") or {}


def test_write_layer_mem_ingest_marks_handoff(iso_store):
    """①写入层自动打标：纯 ingest 进来的接续点落库即带 kind=handoff，不依赖回填。"""
    nid = _ingest_task("【★接续点·2026-10-17 写入层打标验证 · 新会话先读这条】")

    payload = _iso_payload(iso_store, nid)
    assert payload["kind"] == HANDOFF_KIND, "接续点必须由写入层打 kind=handoff（回填不该是唯一入口）"
    assert payload.get("task_state") is None, "写入层不代答状态：解析不出就如实为 None"


def test_write_layer_handoff_filtering_regression(iso_store):
    """②状态过滤豁免：默认不出现；include_handoff=True 时 task_state=None 也完整取回（2363/2393 回归口）。"""
    nid = _ingest_task("接续点 / 会话接续 / 新聊天先读 —— 2026-10-17 状态过滤豁免验证 · 无状态词")

    data = list_active_tasks(iso_store, project="")
    assert nid not in {it["id"] for it in data["results"]}, "kind=handoff 的接续点默认应被排除"

    included = {it["id"]: it for it in list_active_tasks(iso_store, project="", include_handoff=True)["results"]}
    assert nid in included, "include_handoff=True 时 task_state=None 的接续点必须能取回（根因二回归）"
    assert included[nid]["kind"] == HANDOFF_KIND
    assert included[nid]["state"] is None, "豁免不得塞假状态：行内 state 应如实为 None"


def test_write_layer_duplicate_ingest_idempotent(iso_store):
    """③幂等：重复 ingest 同一条接续点不报错、两份都正确打标、默认列表恒空且回填 0 变更。"""
    content = "【★接续点·2026-10-17 重复写入幂等验证 · 新会话先读这条】"
    first = _ingest_task(content)
    after_first = list_active_tasks(iso_store, project="", include_handoff=True)["total"]

    second = _ingest_task(content)
    after_second = list_active_tasks(iso_store, project="", include_handoff=True)["total"]

    assert isinstance(first, int) and isinstance(second, int) and first != second
    assert after_first == 1 and after_second == 2, "重复 ingest 各落一条、计数稳定（不吞第一条也不爆重复）"
    for nid in (first, second):
        assert _iso_payload(iso_store, nid)["kind"] == HANDOFF_KIND, "每次写入都必须正确打标，不得产生畸形标记"
    assert list_active_tasks(iso_store, project="")["results"] == [], "两次打标都不得把接续点漏进默认列表"

    # 回填对已打标节点是终态：apply 后重跑 0 变更（写入层与回填同源判定）
    apply_result = backfill_task_state(iso_store, dry_run=False)
    assert apply_result["handoff"] == 2
    rerun = backfill_task_state(iso_store, dry_run=True)
    assert rerun["changes"] == []
    assert rerun["already"] == rerun["scanned"] == 2


def test_write_layer_counterexample_todo_not_marked(iso_store):
    """④反例回归（保住 PR #112 的收紧）：句中含「接续点」的真待办不得被打 handoff，且仍在默认列表。"""
    nid = _ingest_task("[待办] Palimpsest「接续点 × 任务列表」解耦（走 PR，2026-10-16 立）")

    payload = _iso_payload(iso_store, nid)
    assert payload.get("kind") is None, "句中含「接续点」的真待办形态不得被打 kind=handoff"

    data = list_active_tasks(iso_store, project="")
    ids = {it["id"] for it in data["results"]}
    assert nid in ids, "真待办应照常出现在默认活跃列表"
    row = next(it for it in data["results"] if it["id"] == nid)
    assert row["state"] == "todo", "[待办] 首行解析出的状态应是 todo"


def test_write_layer_apply_task_patch_marks_handoff(iso_store):
    """⑤更新路径：apply_task_patch（PUT /memory/{node_id} 的实现）把 content 改成接续点形态 → 自动打标。"""
    project = _ns()
    nid = _iso_insert_task(iso_store, "[todo] T-830 普通待办任务", project, task_state="todo")

    apply_task_patch(iso_store, nid, {"content": "【★接续点·2026-10-17 改写成接续点形态 · 新会话先读这条】"})

    payload = _iso_payload(iso_store, nid)
    assert payload["kind"] == HANDOFF_KIND, "content 改成接续点形态后必须自动补 kind=handoff"

    # 幂等：再改一次与打标无关的字段 → kind 不被重复写、不被清掉
    apply_task_patch(iso_store, nid, {"importance": 0.7})
    payload = _iso_payload(iso_store, nid)
    assert payload["kind"] == HANDOFF_KIND
    assert payload["importance"] == 0.7

    # 可见性与 mem_ingest 路径一致：默认排除 / include_handoff 取回
    assert nid not in {it["id"] for it in list_active_tasks(iso_store, project)["results"]}
    included = list_active_tasks(iso_store, project, include_handoff=True)["results"]
    assert nid in {it["id"] for it in included}


def test_write_layer_backfill_handoff_without_state_retrievable(iso_store):
    """③回填联动：解析不出状态的接续点回填后 legacy 不置 True，include_handoff=True 完整取回；重跑幂等。

    旧逻辑会因 unknown 状态把它标 legacy=True —— kind 已把它从默认视图排除，
    legacy 再把显式取回也挡掉，接续点就永远见不到（生产 2363/2393 回归根因之二）。
    """
    project = _ns()
    handoff = _iso_insert_task(iso_store, "【★接续点·2026-10-17 回填豁免验证 · 新会话先读这条】", project)
    normal = _iso_insert_task(iso_store, "[doing] T-831 陪跑普通任务", project, task_state="doing")

    backfill_task_state(iso_store, dry_run=False)

    hp = _iso_payload(iso_store, handoff)
    assert hp["kind"] == HANDOFF_KIND
    assert hp["task_state"] == UNKNOWN_STATE, "解析不出 → unknown（回填既有约定），可见性由 include_handoff 控制"
    assert hp["legacy"] is False, "接续点不得因状态解析不出被标 legacy=True（会把显式取回也挡掉）"
    assert hp["needs_review"] is True, "unknown 态维持 needs_review 标记（可以在列表里收拾，但不影响过滤）"

    included_ids = {it["id"] for it in list_active_tasks(iso_store, project, include_handoff=True)["results"]}
    assert handoff in included_ids, "include_handoff=True 必须取回回填后的接续点（2363/2393 验收口）"
    assert normal in included_ids

    # 幂等：apply 后重跑 0 变更（在临时库上验证；生产库的回填由运维窗口单独执行）
    second = backfill_task_state(iso_store, dry_run=False)
    assert second["changes"] == []
    assert second["already"] == second["scanned"] == 2


def test_write_layer_mem_ingest_fills_title(iso_store):
    """⑥title 同步：ingest 写接续点 → 落库即带非空 title（交接列表读得出是哪条）。"""
    content = "【★接续点·2026-10-17 写入层 title 补齐验证 · 新会话先读这条】"
    nid = _ingest_task(content)

    payload = _iso_payload(iso_store, nid)
    title = (payload.get("title") or "").strip()
    assert title, "接续点落库必须带非空 title（此前只有回填会补，新写入的全是空）"
    assert "2026-10-17" in title and "title 补齐验证" in title, (
        f"title 应取自标记里的日期+主题段，不是常量回退值：{title!r}"
    )
    assert payload["kind"] == HANDOFF_KIND, "kind 打标行为不受 title 补齐影响"


def test_write_layer_preserves_existing_title(iso_store):
    """⑦不覆盖：title 已有非空值（人工/既有）→ 更新路径补 kind 但 title 保持原值。"""
    project = _ns()
    nid = _iso_insert_task(
        iso_store,
        "[todo] T-833 普通待办任务",
        project,
        title="人工拟好的标题",
        task_state="todo",
    )

    apply_task_patch(iso_store, nid, {"content": "【★接续点·2026-10-17 不覆盖已有 title 验证 · 新会话先读这条】"})

    payload = _iso_payload(iso_store, nid)
    assert payload["kind"] == HANDOFF_KIND, "更新路径照常补 kind=handoff"
    assert payload["title"] == "人工拟好的标题", "title 已有非空值时必须保持原值，不得被自动补齐覆盖"


def test_write_layer_ingest_title_idempotent(iso_store):
    """⑧幂等：同一内容 ingest 两次 → 不报错、两份 title 稳定一致（不拼接、不重复）。"""
    content = "【★接续点·2026-10-17 幂等 title 验证 · 新会话先读这条】"
    first = _ingest_task(content)
    second = _ingest_task(content)

    assert first != second, "两次 ingest 各落一条节点"
    t1 = (_iso_payload(iso_store, first).get("title") or "").strip()
    t2 = (_iso_payload(iso_store, second).get("title") or "").strip()
    assert t1 and t1 == t2, f"同内容两次 ingest 的 title 必须稳定一致：{t1!r} vs {t2!r}"
    assert t1.count("幂等 title 验证") == 1, "title 不得重复拼接（每次写入只补一次）"


def test_write_layer_apply_task_patch_fills_title(iso_store):
    """⑨apply_task_patch 路径：content 改成接续点形态 → kind 与缺失的 title 一并自动补。"""
    project = _ns()
    nid = _iso_insert_task(iso_store, "[todo] T-834 普通待办任务", project, task_state="todo")

    apply_task_patch(iso_store, nid, {"content": "【★接续点·2026-10-17 patch 补 title 验证 · 新会话先读这条】"})

    payload = _iso_payload(iso_store, nid)
    assert payload["kind"] == HANDOFF_KIND
    title = (payload.get("title") or "").strip()
    assert "2026-10-17" in title and "patch 补 title 验证" in title, f"title 应从更新后的 content 首行提取：{title!r}"

    # 幂等：再 patch 与 title 无关的字段 → title 稳定（不重复拼接、不被清掉）
    apply_task_patch(iso_store, nid, {"importance": 0.7})
    payload = _iso_payload(iso_store, nid)
    assert payload["title"] == title, "title 非空后再次更新必须保持原值"
    assert payload["importance"] == 0.7


def test_write_layer_normal_todo_gets_no_handoff_title(iso_store):
    """⑩反例：普通待办（首行不以接续点标记词开头）→ 不得被塞接续点 title。"""
    nid = _ingest_task("[待办] Palimpsest「接续点 × 任务列表」解耦（走 PR，2026-10-16 立）")

    payload = _iso_payload(iso_store, nid)
    assert payload.get("kind") is None, "真待办不得被打 kind=handoff（既有回归口，PR #112 收紧成果）"
    assert not (payload.get("title") or "").strip(), "普通待办不得被塞接续点 title（补 title 只限接续点）"


# ---------------------------------------------------------------------------
# 11. 列表新鲜度（issue #128 A 项）：fresh_days 折叠 / created_at 回退 / handoff 豁免置顶
# ---------------------------------------------------------------------------


def _iso_days_ago(days: float) -> str:
    """现在时刻往前推 days 天的 ISO 串（本地时区，与 ``_now_iso`` 同格式）。"""
    return (datetime.now(timezone.utc).astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def test_list_active_fresh_fallback_to_created_at_when_touched_null():
    """``last_touched_at`` 为 null → ``fresh_days`` 回退 ``created_at``（issue 验收第 4 条）。

    created_at 久远 → 折叠；created_at 新鲜 → 保留；两个时间戳都缺 → 视为新鲜
    （保留、不猜、不折叠）。insert_node 会吞掉传入的 created_at（占位 None），
    这里插入后用 update_payload 补写（与生产写入路径同款做法）。
    """
    project = _ns()

    def _with_created_at(content: str, age_days: float) -> int:
        nid = _insert_task(content, project, task_state="doing")
        payload = (store.get_node(nid) or {}).get("payload") or {}
        payload["created_at"] = time.time() - age_days * 86400
        store.update_payload(nid, payload)
        return nid

    stale_id = _with_created_at("[doing] T-910 created_at 超期", 40)
    fresh_id = _with_created_at("[doing] T-911 created_at 新鲜", 1)
    naked_id = _insert_task("[doing] T-912 无任何时间戳", project, task_state="doing")  # 两者都缺

    data = list_active_tasks(store, project=project)  # 默认 fresh_days=30

    ids = [it["id"] for it in data["results"]]
    assert stale_id not in ids, "created_at 40 天前 → 折叠"
    assert fresh_id in ids and naked_id in ids, "created_at 新鲜 / 两者都缺 → 保留"
    assert data["stale"]["count"] == 1
    assert data["stale"]["oldest"]["id"] == stale_id


def test_list_active_folds_stale_items_with_summary():
    """超期项折叠进 stale 汇总：count / oldest 正确，results 只留新鲜项。"""
    project = _ns()
    fresh_id = _insert_task("[doing] T-920 新鲜", project, task_state="doing")
    _insert_task("[doing] T-921 超期", project, task_state="doing", last_touched_at=_iso_days_ago(40))
    older_id = _insert_task("[doing] T-922 更老", project, task_state="doing", last_touched_at=_iso_days_ago(90))

    data = list_active_tasks(store, project=project)

    ids = [it["id"] for it in data["results"]]
    assert ids == [fresh_id], "超期项全部折叠，只留新鲜条目"
    assert data["total"] == 3, "total 是过滤后总数（含折叠项），语义不变"
    assert data["stale"]["count"] == 2
    assert data["stale"]["oldest"]["id"] == older_id
    assert data["stale"]["oldest"]["age_days"] >= 89
    assert "fresh_days=0" in data["stale"]["hint"]


def test_list_active_fresh_days_zero_expands_all():
    """``fresh_days=0`` 关闭折叠：返回全部、``stale`` 为 null（兼容旧行为）。"""
    project = _ns()
    ids = [
        _insert_task("[doing] T-930 现在的", project, task_state="doing"),
        _insert_task("[doing] T-931 40 天前", project, task_state="doing", last_touched_at=_iso_days_ago(40)),
    ]

    data = list_active_tasks(store, project=project, fresh_days=0)

    assert {it["id"] for it in data["results"]} == set(ids), "关闭折叠 → 全部可见"
    assert data["stale"] is None
    assert data["total"] == 2


def test_list_active_handoff_exempt_from_folding_and_pinned():
    """handoff 豁免折叠 + ``include_handoff=True`` 时置顶、组内按 created_at 倒序（新在前）。

    排序主键是**时间**不是 importance（否则历史接续点凭高 importance 长期压住新写的
    那条，「新会话先读这条」会读到过期上下文）。这里特意让「老的高优 / 新的低优」，
    断言新的排前面——importance 只在同刻时作次键。
    """
    project = _ns()
    h_old = _insert_task(
        "[doing] 接续点·老的高优",
        project,
        task_state="doing",
        kind=HANDOFF_KIND,
        importance=0.9,
        last_touched_at=_iso_days_ago(90),
    )
    h_new = _insert_task(
        "[doing] 接续点·新的低优",
        project,
        task_state="doing",
        kind=HANDOFF_KIND,
        importance=0.3,
        last_touched_at=_iso_days_ago(90),
    )
    # insert_node 不落 created_at，用 update_payload 补写（与生产写入同款做法）
    for nid, days in ((h_old, 30), (h_new, 1)):
        payload = dict((store.get_node(nid) or {}).get("payload") or {})
        payload["created_at"] = time.time() - days * 86400
        store.update_payload(nid, payload)
    normal = _insert_task("[doing] T-940 普通新鲜任务", project, task_state="doing")

    data = list_active_tasks(store, project=project, include_handoff=True)

    ids = [it["id"] for it in data["results"]]
    assert ids == [h_new, h_old, normal], "接续点置顶 + 组内时间倒序（新在前），普通任务保持原排序"
    assert data["stale"] is None, "接续点豁免折叠：超期也不进 stale 汇总"


def test_list_active_handoff_pin_importance_tie_breaks_by_id_desc():
    """置顶组内 importance 相同 → id 倒序兜底。"""
    project = _ns()
    first = _insert_task("[doing] 接续点·先写", project, task_state="doing", kind=HANDOFF_KIND, importance=0.8)
    second = _insert_task("[doing] 接续点·后写", project, task_state="doing", kind=HANDOFF_KIND, importance=0.8)

    data = list_active_tasks(store, project=project, include_handoff=True)

    assert [it["id"] for it in data["results"]] == [second, first]


# ---------------------------------------------------------------------------
# 12. last_touched_at 初始语义（issue #128 C 项）：mem_ingest 写 task 时初始化
# ---------------------------------------------------------------------------


def test_mem_ingest_task_initializes_last_touched_at(iso_store):
    """``mem_ingest(type='task')`` 后 ``last_touched_at`` 非空且与 ``created_at`` 同一刻。"""
    nid = _ingest_task(f"[doing] T-950 ingest 初始化验证 {uuid.uuid4().hex[:8]}")

    payload = _iso_payload(iso_store, nid)
    touched = payload.get("last_touched_at")
    assert touched, "type=task 落库必须初始化 last_touched_at（此前恒为 null，无法当新鲜度判据）"
    created = payload["created_at"]
    assert isinstance(created, (int, float))
    assert touched == _iso_from_epoch(created), "应与 created_at 同一刻、同 _now_iso 格式（秒精度 ISO）"


def test_mem_ingest_non_task_has_no_last_touched_at(iso_store):
    """非 task 类型不受影响：不写 last_touched_at。"""
    import json

    from mcp_tools import mem_ingest

    res = json.loads(mem_ingest(f"普通记忆 {uuid.uuid4().hex[:8]}", type="memory", importance=0.5))
    assert res["stored"] is True

    payload = _iso_payload(iso_store, res["node_id"])
    assert "last_touched_at" not in payload


# ---------------------------------------------------------------------------
# 13. 判据取更早者（2026-10-19 主人拍板）：last_touched 被回填刷过也要能退场
# ---------------------------------------------------------------------------


def test_list_active_fresh_uses_earlier_of_touched_and_created():
    """判据取 ``last_touched_at`` 与 ``created_at`` **更早者**。

    存量任务的 ``last_touched_at`` 会被批量回填刷成同一天（实测生产库 9 条 52 天前
    建的老任务全是 ``2026-10-08``）→ 单看它会把「建了很久的老任务」显示成「11 天前
    动过」而永不退场；取更早者 = 「建得久 或 久未动，都算旧」。
    """
    project = _ns()
    nid = _insert_task("[doing] T-960 老任务", project, task_state="doing")
    payload = (store.get_node(nid) or {}).get("payload") or {}
    payload["created_at"] = time.time() - 40 * 86400  # 40 天前建
    payload["last_touched_at"] = _iso_days_ago(1)  # 1 天前被批量回填刷过
    store.update_payload(nid, payload)

    data = list_active_tasks(store, project=project)
    assert [r["id"] for r in data["results"]] == [], "40 天前建的老任务应退场（不能因回填的 last_touched 显得新鲜）"
    assert data["stale"]["count"] == 1
    assert data["stale"]["oldest"]["id"] == nid

    expanded = list_active_tasks(store, project=project, fresh_days=0)
    assert [r["id"] for r in expanded["results"]] == [nid]


# ---------------------------------------------------------------------------
# 14. 补产胶囊不得压过手写接续点（issue #227；判据用例直接取生产节点 3778 形态）
# ---------------------------------------------------------------------------


def _capsule_content(session_hex: str) -> str:
    """补产 / session-end 胶囊的 content 形态：首行锚词 + session hash。"""
    return f"接续点 / 会话接续 local-{session_hex}\n【思路】机器从 18 轮对话抓的片段。"


def _iso_set_fields(s, nid: int, **fields) -> None:
    """insert_node 吞掉 created_at 等基础字段 → 读回后 update_payload 补写（生产同款）。"""
    payload = dict((s.get_node(nid) or {}).get("payload") or {})
    payload.update(fields)
    s.update_payload(nid, payload)


def _curated_handoff(s, project: str, created_days_ago: float) -> int:
    """手写接续点：标题可读、无 recovered 标。"""
    nid = _iso_insert_task(
        s,
        "【★接续点·2026-10-29 手写的跨会话接续 · 新会话先读这条】\n【下一步】先修湿度门禁。",
        project,
        kind=HANDOFF_KIND,
        title="2026-10-29 手写的跨会话接续",
    )
    _iso_set_fields(s, nid, created_at=time.time() - created_days_ago * 86400)
    return nid


def _recovered_capsule(s, project: str, session_hex: str, created_days_ago: float) -> int:
    """补产胶囊：3778 形态——机器打 kind=handoff + recovered=True + 更新的 created_at。"""
    nid = _iso_insert_task(s, _capsule_content(session_hex), project, kind=HANDOFF_KIND)
    _iso_set_fields(
        s,
        nid,
        recovered=True,
        recovered_at=_iso_days_ago(0),
        created_at=time.time() - created_days_ago * 86400,
    )
    return nid


def test_recovered_capsule_never_outranks_curated_handoff(iso_store):
    """issue #227 验收①：recovered=true 的胶囊不得压过手写接续点（3778 真实形态）。

    生产事故原样：3748 手写接续点（老）在场，3778 补产胶囊（新）凭 created_at
    抢走 ``tasks_active(include_handoff=true)`` 首位。修后补产胶囊必须让位——
    但仍留在 results 里（turns 不丢、不隐藏）。
    """
    s = iso_store
    project = _ns()
    curated = _curated_handoff(s, project, created_days_ago=1)
    recovered = _recovered_capsule(s, project, session_hex="8" * 20 + uuid.uuid4().hex, created_days_ago=0)

    data = list_active_tasks(s, include_handoff=True)

    ids = [it["id"] for it in data["results"]]
    assert ids.index(curated) < ids.index(recovered), f"手写接续点必须排在补产胶囊前：{ids}"
    assert ids[0] == curated, "手写接续点占首位（新会话先读这条就读到它）"
    assert recovered in ids, "补产胶囊留在列表里照常可见"
    item = next(it for it in data["results"] if it["id"] == recovered)
    assert item["recovered"] is True, "recovered 标须带到列表条目上（消费方可识别补产胶囊）"


def test_unrecovered_capsule_ranking_regression(iso_store):
    """回归（issue #227 约束）：未打 recovered 标的正常胶囊排序**零行为变化**。

    正常 session-end 胶囊（无 recovered 标、created_at 最新）照旧置顶——只有
    补产胶囊被排除出置顶组。
    """
    s = iso_store
    project = _ns()
    curated = _curated_handoff(s, project, created_days_ago=2)
    capsule = _iso_insert_task(s, _capsule_content(uuid.uuid4().hex * 2), project, kind=HANDOFF_KIND)
    _iso_set_fields(s, capsule, created_at=time.time())

    data = list_active_tasks(s, include_handoff=True)

    ids = [it["id"] for it in data["results"]]
    assert ids[:2] == [capsule, curated], f"正常胶囊仍按 created_at 置顶：{ids}"


def test_hash_only_first_line_title_degrades_to_stub(iso_store):
    """issue #227 验收②：hash-only 首行不再产出裸 hash title → ``补产胶囊 <日期> <前8位>``。"""
    ts = time.mktime((2026, 10, 29, 10, 0, 0, 0, 0, -1))  # 本地时区
    session_hex = "b7f9c2d1e08a36f5"  # 前 8 位 = b7f9c2d1
    payload = {
        "content": _capsule_content(session_hex.ljust(64, "0")),
        "created_at": ts,
    }

    title = _handoff_title(payload)

    expected_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    assert title == f"补产胶囊 {expected_date} {session_hex[:8]}", title
    assert session_hex not in title, "完整 hash 不得进 title"


def test_hash_only_title_omits_date_when_created_at_missing(iso_store):
    """created_at 缺失 → stub 省略日期段，仍可读、可区分。"""
    title = _handoff_title({"content": _capsule_content("3b593a05" + "c" * 56)})
    assert title == "补产胶囊 3b593a05", title


def test_hash_only_degrade_spares_real_curated_shapes(iso_store):
    """回归：正常手写接续点的 title 提取不受降级影响。

    「接续点 / 会话接续 / 新聊天先读 —— 2026-10-13~14 大修」这类锚词 + 日期 /
    主题的真实形态：剥完锚词剩下的是信息段，不是 16 连 hex，不触发降级。
    """
    payload = {
        "content": "接续点 / 会话接续 / 新聊天先读 —— 2026-10-13~14 大修（本地检索主序解耦）",
        "created_at": time.time(),
    }
    assert _handoff_title(payload) == "2026-10-13~14 大修（本地检索主序解耦）"


def test_mem_ingest_hash_only_capsule_title_stub(iso_store):
    """写入层（``mem_ingest`` 三通道公共实现）自动补 title 也走 stub（带 created_at 日期）。"""
    session_hex = "ab12cd34" + "e" * 56
    nid = _ingest_task(_capsule_content(session_hex))

    payload = _iso_payload(iso_store, nid)
    assert payload["kind"] == HANDOFF_KIND
    title = payload["title"]
    assert title.startswith("补产胶囊 "), title
    assert session_hex[:8] in title and session_hex not in title, title


def test_hash_only_degrade_after_leading_status_block(iso_store):
    """回归：首行前导 ``[...]`` 状态块后仍是 hash-only → 必须降级。

    ``_is_handoff`` 的判据明确允许首行带若干状态块（生产实测 3752：
    ``[完成] 接续点 / 会话接续 local-<64hex>``）。title 判据若不同口径，
    这类补产胶囊会漏降级、继续留 hash-only title——两处判据必须同口径。
    """
    session_hex = "6b7a6ced" + "f" * 56
    for prefix in ("[完成] ", "[待办] ", "[完成] [待办] "):
        payload = {"content": prefix + _capsule_content(session_hex), "created_at": time.time()}
        title = _handoff_title(payload)
        assert title.startswith("补产胶囊 "), f"{prefix!r} 形态应降级为 stub：{title}"
        assert session_hex[:8] in title and session_hex not in title, title
