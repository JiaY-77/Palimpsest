"""存量 ``created_at`` 回填脚本（issue #226）。

``kb_chunk`` / ``novel_chunk`` / ``git_commit`` 三类节点的 ``payload.created_at``
此前硬编码为 ``None``，存量全部缺字段：``freshness_decay`` 对这些层静默失效、
``eval/split_eval_set.py`` 的时间切分无法放置（missing_timestamp）。

主人口径（2026-10-29 拍板）：**接受近似回填，但每个回填值必须标明来源**，
不伪装精确。证据字段优先级：

  ============  ==========================  ============================
  type          证据字段                     精度说明
  ============  ==========================  ============================
  kb_chunk      payload.source_mtime         epoch 秒，源文档 mtime
  novel_chunk   payload.source_mtime         epoch 秒，源文档 mtime
  git_commit    payload.commit_time          **仅日期粒度**（YYYY-MM-DD，
                                             git log --date=short），回填为
                                             该日本地零点
  ============  ==========================  ============================

证据字段不可恢复的节点**不回填不编造**，只在报告里计入 unrecoverable。

每个被回填节点追加审计标记::

    payload["created_at"]        = <epoch 秒数值>
    payload["created_at_backfill"] = {
        "source": "<证据字段名>",
        "value":  <证据字段原值>,
        "backfilled_at": "<ISO8601 本地时间>",
        # commit_time 来源时另带 "precision": "date-only"
    }

**默认 dry-run**：只读打开（``TriviumStore(read_only=True)``），打印按类型
统计与来源分布，不写库；``--apply`` 才落盘。幂等：已带 ``created_at`` 的
节点跳过，重跑输出 0 变更。

用法（在仓库根目录）::

    python scripts/backfill_created_at.py                # 预览
    python scripts/backfill_created_at.py --apply        # 执行
    python scripts/backfill_created_at.py --json         # 机器可读输出

生产库在 REST 服务运行期间无法写（连接级排他）——``--apply`` 须由维护者在
停服务窗口执行，或先用 ``DB_PATH=<副本>`` 前缀在整组拷贝的副本上演练。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

# 允许直接以 `python scripts/backfill_created_at.py` 运行（把仓库根加进 sys.path）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 三类目标节点与其证据字段（按优先级排序；首个可转换的字段生效）
TARGET_TYPES: dict[str, tuple[str, ...]] = {
    "kb_chunk": ("source_mtime",),
    "novel_chunk": ("source_mtime",),
    "git_commit": ("commit_time",),
}
# 证据字段精度标注：commit_time 只到日期，回填值是「该日零点」的近似
_FIELD_PRECISION = {"commit_time": "date-only"}


def _epoch_from_value(field: str, value) -> float | None:
    """把证据字段原值转成 epoch 秒；转不了返回 None（不计入可回填）。"""
    if value is None:
        return None
    try:
        if field == "commit_time":
            # git_ingest 用 --date=short，只有 "YYYY-MM-DD"；解析失败（历史
            # 数据可能是别的形态）宁可标 unrecoverable 也不猜
            dt = time.strptime(str(value), "%Y-%m-%d")
            return float(time.mktime(dt))
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def backfill_created_at(store, dry_run: bool) -> dict:
    """扫描并（可选）回填三类节点的 created_at。返回统计报告 dict。"""
    # 先把遍历结果物化再写：iter_payloads 是单连接生成器，边遍历边写会
    # 在同一连接内制造读/写窗口重叠（锁库）——issue #167 教训
    scanned: dict[str, int] = dict.fromkeys(TARGET_TYPES, 0)
    already: dict[str, int] = dict.fromkeys(TARGET_TYPES, 0)
    recoverable: dict[str, dict[str, int]] = {t: {} for t in TARGET_TYPES}
    unrecoverable: dict[str, int] = dict.fromkeys(TARGET_TYPES, 0)
    applied: dict[str, int] = dict.fromkeys(TARGET_TYPES, 0)
    now_iso = datetime.now().isoformat(timespec="seconds")
    changed: list[tuple[str, int, dict]] = []  # (type, nid, 修改内容)

    for nid, payload in store.iter_payloads():
        if not isinstance(payload, dict):
            continue
        ntype = payload.get("type")
        if ntype not in TARGET_TYPES:
            continue
        scanned[ntype] += 1
        if payload.get("created_at") is not None:
            already[ntype] += 1
            continue

        picked = None  # (field, 原值, epoch)
        for field in TARGET_TYPES[ntype]:
            raw = payload.get(field)
            epoch = _epoch_from_value(field, raw)
            if epoch is not None:
                picked = (field, raw, epoch)
                break
        if picked is None:
            unrecoverable[ntype] += 1
            continue

        field, raw, epoch = picked
        audit: dict = {
            "source": field,
            "value": raw,
            "backfilled_at": now_iso,
        }
        precision = _FIELD_PRECISION.get(field)
        if precision:
            audit["precision"] = precision
        patch = {"created_at": epoch, "created_at_backfill": audit}
        changed.append((ntype, nid, patch))
        recoverable[ntype][field] = recoverable[ntype].get(field, 0) + 1

    if not dry_run:
        for _ntype, nid, patch in changed:
            # update_payload 浅合并：只动 created_at / created_at_backfill，
            # content/type 等其余字段原样保留
            store.update_payload(nid, patch)
        for ntype, _nid, _patch in changed:
            applied[ntype] += 1

    return {
        "dry_run": dry_run,
        "scanned": scanned,
        "already": already,
        "backfillable_by_source": recoverable,
        "backfillable_total": {t: sum(recoverable[t].values()) for t in TARGET_TYPES},
        "unrecoverable": unrecoverable,
        "applied": applied,
        "total_changed": len(changed),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填三类节点的 payload.created_at（默认 dry-run 预览）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览不落盘；生产库须停服务窗口执行）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（默认人类可读）")
    args = parser.parse_args(argv)

    # 惰性 import：避免仅 --help 就初始化库
    from core.trivium_store import TriviumStore

    store = TriviumStore(read_only=not args.apply)  # dry-run 只读，不与 REST 争写
    result = backfill_created_at(store, dry_run=not args.apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    mode = "dry-run（未写库）" if result["dry_run"] else "已写库"
    print(f"created_at 存量回填（issue #226） —— {mode}")
    print("=" * 56)
    print(f"{'类型':<12}{'存量':>6}{'已有跳过':>10}{'可回填':>8}{'无法恢复':>10}{'已写':>6}")
    for t in TARGET_TYPES:
        n = result["backfillable_total"][t]
        a = result["applied"][t] if not result["dry_run"] else 0
        print(f"{t:<12}{result['scanned'][t]:>6}{result['already'][t]:>10}{n:>8}{result['unrecoverable'][t]:>10}{a:>6}")
        for field, cnt in sorted(result["backfillable_by_source"][t].items()):
            tail = "" if field != "commit_time" else "（date-only，回填为该日零点）"
            print(f"    来源 {field}: {cnt}{tail}")
    total = result["total_changed"]
    print("-" * 56)
    print(
        f"{'合计':<12}{sum(result['scanned'].values()):>6}{sum(result['already'].values()):>10}"
        f"{sum(result['backfillable_total'].values()):>8}{sum(result['unrecoverable'].values()):>10}"
        f"{sum(result['applied'].values()) if not result['dry_run'] else 0:>6}"
    )
    if result["dry_run"]:
        print(f"\ndry-run 未写库；确认统计无误后加 --apply 执行（共 {total} 个节点将被回填）。")
        print("审计标记 created_at_backfill 会记录每个回填值的来源与原值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
