"""题集自洽校验（issue #240）——baseline 的第 0 步。

背景：``eval_set_v2.json`` 曾静默漂移成 138 题 / 判卷 27，而 README 与
``docs/RETRIEVAL_QUALITY.md`` §8.3/§8.4 记录的是 148 / 31——根因不是那次
漏写，而是没有任何东西能发现漂移：题集是 git-ignored 本地数据，CI 看不到。
本脚本对题集做纯读校验，漂移就地 loud fail，不产出貌似合理的基线。

用法：

    python eval/check_eval_set.py                    # 校验默认题集
    python eval/check_eval_set.py --expect 148 --expect-judged 31

对外两层：``check(path)`` 纯函数返回违规消息列表（空 = 通过，不抛异常给
调用方），``main()`` 是 CLI（违规退出码 1，每条违规单独一行）。

不做的校验：qid 连续性。当前题集 q1..q149 断 q139（历史断号），实现
「连续」必然误报，故只查唯一性不查连续性。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

VALID_STATUSES = {"live", "reresolved", "quarantined", "dropped"}
_DEFAULT_EVAL_SET = Path(__file__).resolve().parent / "eval_set_v2.json"


def _load(eval_set_path: str | os.PathLike) -> tuple[object, str | None]:
    """读入题集。返回 (数据, None) 或 (None, 错误消息)。纯读。"""
    path = Path(eval_set_path)
    if not path.is_file():
        return None, f"文件不存在: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        # ValueError 同时覆盖 json.JSONDecodeError 与 UnicodeDecodeError
        return None, f"无法解析 JSON: {e}"


def check(eval_set_path: str | os.PathLike) -> list[str]:
    """校验题集自洽，返回违规消息列表（空列表 = 通过）。纯读，不抛异常给调用方。

    不变量（违反即各出一条消息，带不变量名与具体数字/qid 便于定位）：
    1. 文件存在且能解析 JSON；
    2. 顶层有 ``items``(list) 与 ``status_counts``(dict)；
    3. 每个 item 的 ``status`` ∈ {live, reresolved, quarantined, dropped}；
    4. len(items) == sum(status_counts.values())；
    5. 从 items 重算的计数 == 存储的 status_counts（逐 key 报差异）；
    6. qid 唯一（不查连续性——历史断号 q139，见模块 docstring）；
    7. quarantined / dropped 不得携带非空 gold_ids。
    """
    data, err = _load(eval_set_path)
    if err is not None:
        return [err]
    messages: list[str] = []

    items = data.get("items") if isinstance(data, dict) else None
    counts = data.get("status_counts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        messages.append(f"顶层缺 items(list)（实际值类型 {type(items).__name__}），逐项校验跳过")
        return messages
    if not isinstance(counts, dict):
        messages.append(f"顶层缺 status_counts(dict)（实际值类型 {type(counts).__name__}）")

    recomputed: dict[str, int] = {}
    seen_qids: dict[str, int] = {}
    dup_qids: list[str] = []
    bad_status: list[str] = []
    gold_blocked: list[str] = []

    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            messages.append(f"items[{idx}] 不是 dict（实际值类型 {type(it).__name__}）")
            continue
        qid = str(it.get("qid", ""))
        status = it.get("status")
        if status in VALID_STATUSES:
            recomputed[status] = recomputed.get(status, 0) + 1
        else:
            bad_status.append(f"{qid}(status={status!r})")
        seen_qids[qid] = seen_qids.get(qid, 0) + 1
        if seen_qids[qid] == 2:
            dup_qids.append(qid)
        if status in {"quarantined", "dropped"} and it.get("gold_ids"):
            gold_blocked.append(qid)

    if bad_status:
        messages.append(f"status 枚举违规: {', '.join(bad_status)}（合法值: live/reresolved/quarantined/dropped）")
    if dup_qids:
        messages.append(f"qid 重复: {', '.join(dup_qids)}")
    if gold_blocked:
        messages.append(f"quarantined/dropped 不得带 gold_ids: {', '.join(gold_blocked)}")

    if isinstance(counts, dict):
        bad_vals = {str(k): v for k, v in counts.items() if not isinstance(v, int) or isinstance(v, bool)}
        if bad_vals:
            messages.append(
                f"status_counts 含非整数值: {', '.join(f'{k}={v!r}' for k, v in bad_vals.items())}，计数核对跳过"
            )
        else:
            total_stored = sum(counts.values())
            if total_stored != len(items):
                messages.append(
                    f"items 数与 status_counts 之和 不一致: len(items)={len(items)} / sum(status_counts)={total_stored}"
                )
            for key in sorted(set(counts) | set(recomputed)):
                stored = counts.get(key, 0)
                actual = recomputed.get(key, 0)
                if stored != actual:
                    messages.append(f"status_counts 与实算不符: {key}: 存储 {stored} / 实算 {actual}")
    return messages


def _count_judged(data: object) -> tuple[int, int] | None:
    """(len(items), 判卷数=gold_ids 非空项数)。items 不可用返回 None。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    items = data["items"]
    judged = sum(1 for it in items if isinstance(it, dict) and it.get("gold_ids"))
    return len(items), judged


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval-set self-consistency check (issue #240), pure read")
    parser.add_argument(
        "--eval-set", default=str(_DEFAULT_EVAL_SET), help="eval set JSON (default: eval/eval_set_v2.json)"
    )
    parser.add_argument("--expect", type=int, default=None, help="若给出，校验 len(items) == N")
    parser.add_argument("--expect-judged", type=int, default=None, help="若给出，校验判卷数（gold_ids 非空项数）== N")
    args = parser.parse_args()

    messages = check(args.eval_set)
    data, _ = _load(args.eval_set)
    counts: dict = data.get("status_counts", {}) if isinstance(data, dict) else {}
    counted = _count_judged(data)
    if args.expect is not None:
        if counted is None:
            messages.append("--expect 无法核对: 题集 items 不可用")
        elif counted[0] != args.expect:
            messages.append(f"--expect 不符: 期望 len(items) == {args.expect}，实际 {counted[0]}")
    if args.expect_judged is not None:
        if counted is None:
            messages.append("--expect-judged 无法核对: 题集 items 不可用")
        elif counted[1] != args.expect_judged:
            messages.append(f"--expect-judged 不符: 期望判卷数 == {args.expect_judged}，实际 {counted[1]}")

    if messages:
        for m in messages:
            print(f"[check_eval_set] {m}")
        return 1
    summary = " ".join(f"{k}={v}" for k, v in counts.items())
    print(
        f"[check_eval_set] OK: items={counted[0] if counted else '?'} judged={counted[1] if counted else '?'} {summary}".rstrip()
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
