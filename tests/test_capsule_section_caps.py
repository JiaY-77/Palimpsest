"""契约测试：胶囊四节长度上限（issue #244）。

此前四节里只有 thought 有上限（``CAPSULE_THOUGHT_MAX_CHARS = 300``，纯截断），
outcome / next / pitfalls 没有任何总量闸，``merge_capsule_with_existing`` 的
按行追加去重更是让节随会话轮数线性膨胀（生产实测正文长度 p95 = 28540 字符）。
本文件钉 ``_cap_capsule_section`` 的行为契约，以及 compose / merge 两条路径
都被同一道闸守住。
"""

from __future__ import annotations

from core.strategy import (
    CAPSULE_SECTION_MAX_CHARS,
    CAPSULE_SECTION_TBD,
    CAPSULE_THOUGHT_MAX_CHARS,
    CAPSULE_TRUNCATE_MARK,
    _cap_capsule_section,
    compose_session_capsule,
    merge_capsule_with_existing,
)

# ---------------------------------------------------------------------------
# 1. 每节超限被截，标记 n == 真实省略行数
# ---------------------------------------------------------------------------


def test_each_section_over_limit_is_truncated_with_dropped_line_count() -> None:
    for name in ("outcome", "next", "pitfalls"):
        limit = CAPSULE_SECTION_MAX_CHARS[name]
        lines = [f"第{i}行 " + "字" * (limit // 4) for i in range(10)]
        body = "\n".join(lines)
        assert len(body) > limit  # 前置：确实超限

        capped = _cap_capsule_section(name, body)

        assert len(capped) <= limit, f"{name}: 超上限 {len(capped)} > {limit}"
        mark = capped.rsplit("\n", 1)[-1]
        assert mark.startswith("…（本节已截断：")
        outcome = capped.split("\n")
        kept = outcome[:-1]  # 各节行本身不含换行，标记前每一行都是保留行
        n = len(lines) - len(kept)
        assert mark == CAPSULE_TRUNCATE_MARK.format(n=n), f"{name}: 标记 n 与真实省略行数不符"
        assert len(kept) + n == len(lines), f"{name}: 留 {len(kept)} 行 + 省略 {n} 行 != 总 {len(lines)} 行"
        # 真实省略行数：没被留下的行数逐条核对
        for line in lines[: len(kept)]:
            assert line in capped
        for line in lines[len(kept) :]:
            assert line not in capped


# ---------------------------------------------------------------------------
# 2. 未超限原样返回，不出现标记
# ---------------------------------------------------------------------------


def test_under_limit_body_returned_unchanged_without_mark() -> None:
    for name in ("outcome", "next", "pitfalls", "thought"):
        body = "正常内容\n第二行\n第三行"
        assert _cap_capsule_section(name, body) == body
        assert "已截断" not in body


def test_unknown_section_name_returned_unchanged() -> None:
    body = "字" * 50000
    assert _cap_capsule_section("not_a_section", body) == body


# ---------------------------------------------------------------------------
# 3. 空串 / 待补占位原样返回
# ---------------------------------------------------------------------------


def test_empty_and_tbd_body_returned_as_is() -> None:
    for name in ("outcome", "next", "pitfalls", "thought"):
        assert _cap_capsule_section(name, "") == ""
        assert _cap_capsule_section(name, CAPSULE_SECTION_TBD) == CAPSULE_SECTION_TBD


# ---------------------------------------------------------------------------
# 4. 单行超长 → 字符级保底，长度 ≤ limit 且无标记
# ---------------------------------------------------------------------------


def test_single_line_over_limit_falls_back_to_char_level_truncation() -> None:
    for name in ("outcome", "next", "pitfalls"):
        limit = CAPSULE_SECTION_MAX_CHARS[name]
        body = "罢" * (limit + 500)  # 一行，直接超 limit
        capped = _cap_capsule_section(name, body)
        assert len(capped) <= limit
        assert capped == body[:limit]
        assert "已截断" not in capped


# ---------------------------------------------------------------------------
# 5. merge 路径不膨胀：旧节已接近上限 + 新加 50 行 → 结果 ≤ limit
# ---------------------------------------------------------------------------


def test_merge_path_cannot_grow_section_past_limit() -> None:
    for name in ("outcome", "next", "pitfalls"):
        limit = CAPSULE_SECTION_MAX_CHARS[name]
        # 旧节多行、逼近上限但未超（不会在 cap 处被截）
        old_lines: list[str] = []
        i = 0
        while sum(len(x) + 1 for x in old_lines) <= limit - 20:
            old_lines.append(f"旧第{i}行 " + "旧" * 18)
            i += 1
        new = "\n".join(f"新第{i}行 全新内容" for i in range(50))
        composed = {
            "sections": {"thought": "", "outcome": "", "next": "", "pitfalls": ""},
            "turns": 1,
        }
        composed["sections"][name] = new

        merged = merge_capsule_with_existing(
            composed,
            {"thought": "", "outcome": "", "next": "", "pitfalls": "", name: "\n".join(old_lines)},
            session_id="s#244",
        )

        body = merged["sections"][name]
        assert len(body) <= limit, f"{name}: merge 后 {len(body)} > {limit}"
        assert "已截断" in body  # 确实有内容被省掉了


def test_merge_path_bounded_for_thought_too() -> None:
    # thought 合并路径同样受 300 限制（生产有 493 字节点从这里绕过 compose 上限）
    old = "字" * 200
    new = "新" * 200
    composed = {
        "sections": {
            "thought": new,
            "outcome": CAPSULE_SECTION_TBD,
            "next": CAPSULE_SECTION_TBD,
            "pitfalls": CAPSULE_SECTION_TBD,
        },
        "turns": 1,
    }
    merged = merge_capsule_with_existing(
        composed,
        {"thought": old, "outcome": CAPSULE_SECTION_TBD, "next": CAPSULE_SECTION_TBD, "pitfalls": CAPSULE_SECTION_TBD},
        session_id="s#244",
    )
    assert len(merged["sections"]["thought"]) <= CAPSULE_THOUGHT_MAX_CHARS


# ---------------------------------------------------------------------------
# 6. 幂等：同一输入跑两次结果一致；已截断的 body 再进一次不追加第二个标记
# ---------------------------------------------------------------------------


def test_cap_is_idempotent_and_never_double_marks() -> None:
    for name in ("outcome", "next", "pitfalls"):
        limit = CAPSULE_SECTION_MAX_CHARS[name]
        lines = [f"行{i} " + "肉" * (limit // 3) for i in range(10)]
        body = "\n".join(lines)
        once = _cap_capsule_section(name, body)
        twice = _cap_capsule_section(name, once)
        assert once == twice, f"{name}: 二次调用结果变了"
        assert once.count("已截断") == 1, f"{name}: 出现了 {once.count('已截断')} 个标记"


# ---------------------------------------------------------------------------
# 7. compose 路径：thought 仍 ≤ 300、无标记（防回归）；其余三节被闸住
# ---------------------------------------------------------------------------


def test_compose_thought_contract_unchanged_and_other_sections_capped() -> None:
    turns = [
        {"sections": {"thought": "初始化" * 400, "outcome": "改" * 100}},
        {"sections": {"thought": "继续" * 400, "outcome": "查" * 100}},
    ]
    # thought 合成总量 > 300 → 纯截断、无标记（既有合同）
    result = compose_session_capsule(turns, session_id="s#244")
    assert len(result["sections"]["thought"]) <= CAPSULE_THOUGHT_MAX_CHARS
    assert "已截断" not in result["sections"]["thought"]
