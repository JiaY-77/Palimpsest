"""治理哨兵降级决策测试（scripts.palimpsest_gov_daily，issue #128 B 项）。

覆盖点：
  1. 纯函数 ``decide_stale_actions``：30 / 60 天边界（「超」= 严格大于）；
  2. 幂等：content 已带 ``[待复核]`` 前缀的不重复降级；
  3. 无 ``last_touched_at`` / 解析不出的跳过不猜；
  4. 执行器 ``run_stale_demotion``（mock ``_rest``）：降级走 ``PUT /memory/{id}``
     且 payload 形态正确（task_state=blocked + [待复核] 前缀 content）；
  5. 执行点双保险：brief 不带 content 时，取回全文已带前缀 → 不再 PUT。

全程不碰网络、不碰库（纯函数 + monkeypatch）。
"""

import json
from datetime import datetime, timedelta, timezone

from scripts import palimpsest_gov_daily as gov

# 固定时刻（无微秒）→ 秒精度 ISO 往返无损，30/60 天边界可精确断言
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def _iso(days_ago: float, plus_seconds: int = 0) -> str:
    """NOW 往前推 days_ago（+plus_seconds 秒）的 ISO 串。"""
    return (NOW - timedelta(days=days_ago, seconds=plus_seconds)).isoformat(timespec="seconds")


def _brief(nid, state="doing", touched=None, content="", **extra):
    brief = {"id": nid, "state": state, "title": f"任务{nid}", "last_touched_at": touched}
    if content:
        brief["content"] = content
    brief.update(extra)
    return brief


# ---------------------------------------------------------------------------
# 1. 纯函数：30 / 60 天边界
# ---------------------------------------------------------------------------


def test_demote_only_past_30_day_boundary():
    demotions, archive = gov.decide_stale_actions(
        [
            _brief(1, touched=_iso(29.9)),  # 未到 30 天 → 不动
            _brief(2, touched=_iso(30)),  # 恰好 30 天，「超」未成立 → 不动
            _brief(3, state="blocked", touched=_iso(30, plus_seconds=1)),  # 超 30 天 → 降级
        ],
        NOW_TS,
    )
    assert [d["id"] for d in demotions] == [3]
    assert archive == []


def test_archive_suggestions_overlap_demotions_past_60_days():
    demotions, archive = gov.decide_stale_actions(
        [
            _brief(1, touched=_iso(60)),  # 恰好 60 天 → 降级、不进归档
            _brief(2, touched=_iso(60, plus_seconds=1)),  # 超 60 天 → 既降级又建议归档
            _brief(3, touched=_iso(90)),
        ],
        NOW_TS,
    )
    assert [d["id"] for d in demotions] == [1, 2, 3], ">30 天统一降级（含 >60 天）"
    assert [a["id"] for a in archive] == [2, 3], ">60 天额外建议归档（与降级不互斥）"


def test_freshness_uses_earlier_of_touched_and_created():
    """判据取 ``last_touched_at`` 与 ``created_at`` **更早者**（2026-10-19 主人拍板）。

    存量任务的 ``last_touched_at`` 会被批量回填刷成同一天 → 单看它会把「52 天前建的
    老任务」判成「11 天前动过」而永远治不到。
    """
    demotions, archive = gov.decide_stale_actions(
        [
            # last_touched 看着新鲜（1 天前），但 created_at 是 40 天前 → 判僵尸
            _brief(1, touched=_iso(1), created_at=NOW_TS - 40 * 86400),
            # created_at 新鲜、last_touched 40 天前 → 也判僵尸（取更早者）
            _brief(2, touched=_iso(40), created_at=NOW_TS - 1 * 86400),
            # 两者都新鲜 → 不动
            _brief(3, touched=_iso(1), created_at=NOW_TS - 2 * 86400),
        ],
        NOW_TS,
    )
    assert [d["id"] for d in demotions] == [1, 2]
    assert archive == []


# ---------------------------------------------------------------------------
# 2. 幂等 + 不猜
# ---------------------------------------------------------------------------


def test_idempotent_already_reviewed_prefix_skipped():
    demotions, archive = gov.decide_stale_actions(
        [
            _brief(1, touched=_iso(40), content="[待复核] [doing] 老任务"),  # 已降级过
            _brief(2, touched=_iso(40), content="[doing] 另一条"),  # 未降级
            _brief(3, touched=_iso(90), content="[待复核] [doing] 更老"),  # 60+：前缀不影响建议
        ],
        NOW_TS,
    )
    assert [d["id"] for d in demotions] == [2], "已带 [待复核] 前缀的不再降级（幂等）"
    assert [a["id"] for a in archive] == [3], "60+ 只建议归档，与是否已降级无关"


def test_missing_last_touched_at_skipped_not_guessed():
    demotions, archive = gov.decide_stale_actions(
        [
            _brief(1, touched=None),  # 无判据
            _brief(2, touched="不是时间的东西"),  # 解析不出
            _brief(3, state="todo", touched=_iso(90)),  # 非 doing/blocked → 不归哨兵管
        ],
        NOW_TS,
    )
    assert demotions == [] and archive == [], "无判据 / 非 doing|blocked 一律跳过，不猜"


# ---------------------------------------------------------------------------
# 3. 执行器：mock _rest，断言 PUT /memory/{id} 的 payload 形态
# ---------------------------------------------------------------------------


def test_demotion_puts_expected_payload(monkeypatch):
    calls = []

    def fake_rest(method, path, body=None, timeout=300):
        calls.append((method, path, body))
        if method == "GET" and path.startswith("/tasks/active"):
            return True, json.dumps(
                {"results": [_brief(7, touched=_iso(40), content="[doing] 写周报", title="写周报")]}
            )
        if method == "GET" and path == "/memory/7":
            payload = {"type": "task", "content": "[doing] 写周报", "task_state": "doing"}
            return True, json.dumps({"id": 7, "payload": payload})
        return True, json.dumps({"status": "ok"})

    monkeypatch.setattr(gov, "_rest", fake_rest)
    records = []
    gov.run_stale_demotion(records)

    puts = [(path, body) for method, path, body in calls if method == "PUT"]
    assert len(puts) == 1, f"应恰好一次 PUT，实际调用：{calls}"
    path, body = puts[0]
    assert path == "/memory/7"
    assert body["task_state"] == "blocked"
    assert body["content"] == "[待复核] [doing] 写周报"
    assert any("已降级" in r for r in records), "降级动作要进 records（watchdog 可见）"


def test_demotion_idempotent_at_execution_point(monkeypatch):
    """执行点双保险：brief 不带 content（生产里就是没有），全文已带前缀 → 不 PUT。"""
    calls = []

    def fake_rest(method, path, body=None, timeout=300):
        calls.append((method, path, body))
        if method == "GET" and path.startswith("/tasks/active"):
            return True, json.dumps({"results": [_brief(8, touched=_iso(40))]})
        if method == "GET" and path == "/memory/8":
            payload = {"type": "task", "content": "[待复核] [doing] 已降级过", "task_state": "blocked"}
            return True, json.dumps({"id": 8, "payload": payload})
        return True, json.dumps({"status": "ok"})

    monkeypatch.setattr(gov, "_rest", fake_rest)
    gov.run_stale_demotion([])

    assert [c for c in calls if c[0] == "PUT"] == [], "全文已带 [待复核] 前缀 → 不得重复降级"


def test_rest_unreachable_recorded(monkeypatch):
    def fake_rest(method, path, body=None, timeout=300):
        return False, "ConnectionRefusedError: eof"

    monkeypatch.setattr(gov, "_rest", fake_rest)
    records = []
    gov.run_stale_demotion(records)

    assert len(records) == 1 and "不可达" in records[0]
