"""存量 thought 节点的 ``thought_origin`` 回填脚本。

背景：``type="thought"`` 节点（思维底片）payload 现带两个来源标记字段——
``thought_origin``（启发式：按文本开头形态判 skeleton / native / unknown）与
``skeleton_kind``（事实：本轮注入的骨架类型，由宿主上报）。本脚本把
``thought_origin`` 判据引入之前落库的存量节点补齐：

  - 只处理 ``type=thought`` 且**缺** ``thought_origin`` 键的节点；
  - 判定复用 ``core.strategy.detect_thought_origin``（与写入路径同一判据，
    不另立一套）；
  - ``skeleton_kind`` 是给侧上报的事实字段，存量节点**无从得知**——不回填
    （缺着比编造诚实）。

**默认 dry-run**，只打印统计与将变更的节点，不写库；``--apply`` 才落盘。
幂等：已带 ``thought_origin``（键存在即算，已有值不覆盖）的节点被跳过，
重跑输出 0 变更。

用法（在仓库根目录，或用 REST 之外的本地库）::

    python scripts/backfill_thought_origin.py                # 预览
    python scripts/backfill_thought_origin.py --apply         # 执行
    python scripts/backfill_thought_origin.py --json          # 机器可读输出
"""

import argparse
import json
import os
import sys

# 允许直接以 `python scripts/backfill_thought_origin.py` 运行（把仓库根加进 sys.path）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填存量 thought 节点的 payload.thought_origin")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览不落盘）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（默认人类可读）")
    args = parser.parse_args(argv)

    # 惰性 import：避免仅 `--help` 就初始化库
    from core.strategy import backfill_thought_origin
    from core.trivium_store import TriviumStore

    store = TriviumStore(read_only=not args.apply)  # dry-run 只读，不与 REST 争写
    result = backfill_thought_origin(store, dry_run=not args.apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    mode = "dry-run（未写库）" if result["dry_run"] else "已写库"
    dist = result["distribution"]
    print(f"思维底片来源标记回填 —— {mode}")
    print("=" * 48)
    print(f"扫描 type=thought 节点 : {result['scanned']}")
    print(f"  已带标记跳过        : {result['already']}")
    print(f"  skeleton（骨架照写） : {dist.get('skeleton', 0)}")
    print(f"  native（原生思考）   : {dist.get('native', 0)}")
    print(f"  unknown（无法判定） : {dist.get('unknown', 0)}")
    print(f"将变更 / 已变更节点    : {result['changed']}")
    for c in result["changes"]:
        print(f"    id={c['id']:<6} thought_origin={c['thought_origin']}")
    if result["dry_run"] and result["changed"]:
        print("\n这是预览。加 --apply 才会真正写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
