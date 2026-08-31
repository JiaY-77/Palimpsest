# -*- coding: utf-8 -*-
"""
Palimpsest 一次性迁移：character_name → domain（消除记忆领域二义性）
====================================================================
背景（2026-08-29 设计原则：记忆领域无二义性）：
  新写入统一用 payload.domain（character_name 为兼容镜像，读侧一律走 node_domain）；
  老数据只有 character_name 没有 domain。本脚本把「无 domain 但有 character_name」的
  节点复制 character_name → domain，使两字段一致，后续读侧统一走 node_domain。

幂等：
  - 已有 domain 的节点跳过（domain 优先，不论 character_name 是否有值）
  - 无 character_name 的节点跳过（未分类，保持无 domain）
  - 重复运行只做统计，不再重复改写

安全：
  - 默认 dry-run：只打印预览统计，不写任何数据
  - --apply 才真正执行 store.update_payload 写回
  - 只改 payload，不动向量 / 边 / 索引

运行：
  venv/Scripts/python.exe scripts/migrate_domain.py            # dry-run 预览
  venv/Scripts/python.exe scripts/migrate_domain.py --apply    # 真正执行
"""
import argparse
import sys

import _common  # noqa: E402,F401  导入即把项目根注入 sys.path

from core.trivium_store import TriviumStore  # noqa: E402


def run(apply: bool = False) -> dict:
    """遍历所有节点，把「无 domain 但有 character_name」复制 domain 写回。

    返回统计 dict：
      {dry_run, db_path, migrated, skipped_already_have_domain,
       skipped_no_character_name, preview}
    preview：dry-run 时列出前 20 个待迁移节点（id + character_name）。
    """
    store = TriviumStore()
    migrated = 0
    already = 0
    no_cn = 0
    preview: list[dict] = []
    for nid, payload in store.iter_payloads():
        if payload.get("domain"):
            already += 1
            continue
        cn = payload.get("character_name")
        if not cn:
            no_cn += 1
            continue
        payload["domain"] = cn
        if apply:
            store.update_payload(nid, payload)
        if len(preview) < 20:
            preview.append({"node_id": nid, "character_name": cn})
        migrated += 1

    return {
        "dry_run": not apply,
        "db_path": store.db_path,
        "migrated": migrated,
        "skipped_already_have_domain": already,
        "skipped_no_character_name": no_cn,
        "preview": preview,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="migrate_domain",
        description="Palimpsest 一次性迁移：无 domain 但有 character_name 的节点复制 domain 写回",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行写回（默认 dry-run 只预览统计，不写任何数据）",
    )
    args = parser.parse_args()

    result = run(apply=args.apply)
    action = "执行" if args.apply else "预览（dry-run，未写数据）"
    print(f"迁移模式: {action} | 库: {result['db_path']}")
    if not args.apply and result["preview"]:
        print("预览待迁移节点（前 20 条）:")
        for p in result["preview"]:
            print(f"  id={p['node_id']}  character_name={p['character_name']!r} -> domain")
    elif not args.apply and result["migrated"] == 0:
        print("（无待迁移节点）")
    print("-" * 40)
    for key in ("migrated", "skipped_already_have_domain", "skipped_no_character_name"):
        print(f"{key}: {result[key]}")
    if args.apply and result["migrated"]:
        print("注意：character_name 保持原样（兼容镜像），读侧已统一走 node_domain。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)