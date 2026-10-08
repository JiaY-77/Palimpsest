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
"""

from __future__ import annotations

import uuid

import pytest
from corpus_real_cases import EXPECTED_NONE, REAL_CORPUS

from core.task_state import (
    TASK_STATES,
    apply_task_patch,
    backfill_task_state,
    list_active_tasks,
    parse_task_state,
)
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

    data = list_active_tasks(store, project=project)
    ids = [it["id"] for it in data["results"]]

    # doing > blocked > todo；done / canceled 不在默认三态里
    assert ids == [doing, blocked, todo]
    assert done not in ids and canceled not in ids
    assert data["total"] == 3


def test_list_active_sort_by_last_touched_desc_within_state():
    project = _ns()
    older = _insert_task("[doing] T-410 旧", project, task_state="doing", last_touched_at="2026-01-01T00:00:00+08:00")
    newer = _insert_task("[doing] T-411 新", project, task_state="doing", last_touched_at="2026-06-01T00:00:00+08:00")

    data = list_active_tasks(store, project=project)
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

    row = next(it for it in list_active_tasks(store, project=project)["results"] if it["id"] == nid)
    assert set(row) == {
        "id",
        "task_key",
        "title",
        "state",
        "project",
        "next_action",
        "aliases",
        "last_touched_at",
        "importance",
    }
    assert row["task_key"] == "T-450"
    assert row["next_action"] == "补测试"
    assert row["aliases"] == ["450"]
    assert row["importance"] == 0.7


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
# 8. 真实存量语料回归（姐姐复核硬伤：真实语料 3/18 → 必须 ≥13/18）
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
    """18 条真实语料至少 13 条解析出非 None（姐姐要求的验收线）。"""
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
    assert parse_task_state("T073：doctor 命令 → [待办，2026-09-10 主人指令「计入小帕」]") == "todo"
    assert parse_task_state("普通描述文字 [阻塞] 尾部") == "blocked"


def test_parse_long_block_with_spaces_and_punctuation():
    """超长 / 含空格 / 含全角标点的标记块能被整块取出并匹配。"""
    assert parse_task_state("[待办，2026-09-10 主人指令「计入小帕」] content") == "todo"
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
    assert parse_task_state("T057：→ [挂账，T055 换脑落地后实施]（主人 2026-08") == "blocked"
    # doing 在前：本任务进行中，后面的「挂起待续」指别的事项
    assert parse_task_state("【任务·技能库瘦身（进行中）2026-09-21 挂起待续】正文") == "doing"
    # 仅「落地」出现（本任务进展描述）→ doing（弱词仍兜底）
    assert parse_task_state("T055：→ [第二步入落地]（换脑已激活") == "doing"
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
    # ISO8601 带日期前缀，粗略校验格式
    assert str(log_pl["created_at"]).startswith("20")
