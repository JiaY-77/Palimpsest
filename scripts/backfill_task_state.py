"""存量 task 节点的 ``task_state`` 回填脚本。

用途：``core/task_state.py`` 引入 ``payload.task_state`` 之前，任务的状态只写在
``content`` 首行的方括号标记里（如 ``[doing] T-123 ...``）。本脚本一次性把存量
``type=task`` 节点扫一遍：

  - 打 ``payload.legacy = True``（标记为「老节点」，``/tasks/active`` 默认不列出）；
  - 从 content 首行解析状态写入 ``payload.task_state``；
  - 解析失败 → ``task_state="unknown"`` + ``needs_review=True``（交人工复核）。

**默认 dry-run**，只打印统计与将变更的节点，不写库；``--apply`` 才落盘。
幂等：已回填（``legacy`` + 合法 ``task_state``）的节点会被跳过，重跑输出 0 变更。

用法（在仓库根目录，或用 REST 之外的本地库）::

    python scripts/backfill_task_state.py                # 预览
    python scripts/backfill_task_state.py --apply         # 执行
    python scripts/backfill_task_state.py --json          # 机器可读输出

CLI 等价入口：``python scripts/palimpsest_cli.py tasks backfill [--apply]``。
"""

import argparse
import json
import os
import sys

# 允许直接以 `python scripts/backfill_task_state.py` 运行（把仓库根加进 sys.path）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填存量 task 节点的 payload.task_state")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览不落盘）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（默认人类可读）")
    args = parser.parse_args(argv)

    # 惰性 import：避免仅 `--help` 就初始化库
    from core.task_state import backfill_task_state
    from core.trivium_store import TriviumStore

    store = TriviumStore(read_only=not args.apply)  # dry-run 只读，不与 REST 争写
    result = backfill_task_state(store, dry_run=not args.apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    mode = "dry-run（未写库）" if result["dry_run"] else "已写库"
    print(f"任务状态回填 —— {mode}")
    print("=" * 48)
    print(f"扫描 type=task 节点 : {result['scanned']}")
    print(f"  已回填跳过        : {result['already']}")
    print(f"  解析成功          : {result['parsed']}")
    print(f"  解析失败→unknown  : {result['unknown']}（需人工复核 needs_review）")
    print(f"将变更 / 已变更节点 : {len(result['changes'])}")
    for c in result["changes"]:
        mark = "!" if c["needs_review"] else " "
        print(f"  [{mark}] id={c['id']:<6} {c['task_state']:<9} {c['title']}")
    if result["dry_run"] and result["changes"]:
        print("\n这是预览。加 --apply 才会真正写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
