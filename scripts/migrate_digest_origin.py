"""
Palimpsest 老库迁移：给历史 digest 节点补 meta.origin（issue #278）
====================================================================
背景：session-end「会话要点」/ post-turn「[对话沉淀] 用户原话」是每次会话固定
写入的对话残渣，新写入路径已带 meta.origin 打标（main.py）；库里的历史节点
没有这个标记。本脚本按 content 前缀（core/digest.py 的 DIGEST_PREFIX_RULES，
三种历史格式都覆盖）给命中节点补 ``meta.origin``：

  - ``会话要点（Palimpsest 策略引擎提炼）：`` → session_digest
  - ``会话要点（Palimpsest 插件提炼）：``   → session_digest
  - ``[对话沉淀] …``                        → dialogue_digest

幂等：
  - meta.origin 已有任何值的节点一律跳过（不覆盖人工/外部标记）；
  - content 前缀未命中的节点跳过；
  - 重复运行只做统计，不再重复改写（第二遍 0 变更）。

安全：
  - 默认 dry-run：只打印预览统计，不写任何数据；--apply 才真正写回；
  - 只补 meta.origin 字段，不改正文、不删节点、不动向量 / 边 / FTS；
  - meta 是整键替换（update_payload 为浅合并），故先读全量 payload、合并
    既有 meta 键后整包写回，不丢其他 meta 键；
  - 运行前建议先停 REST 服务或拷库副本（triviumdb 连接级排他，服务在跑会
    Database locked）；正式库请先 backup_db.py。

运行：
  venv/Scripts/python.exe scripts/migrate_digest_origin.py             # dry-run 预览
  venv/Scripts/python.exe scripts/migrate_digest_origin.py --apply     # 真正执行
可选 --db <绝对路径> 指定目标库（默认跟随 DB_PATH 环境变量 / .env）。
"""

import argparse
import os
import sys

# --db 须在 config 模块加载（import core.trivium_store）之前生效：
# Config.DB_PATH 在 import 期求值，import 后再改环境变量无效。
# 故 argparse 前置，--db 在此设置环境变量后才 import 项目模块。


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="migrate_digest_origin",
        description="老库迁移：按 content 前缀给历史 digest 节点补 meta.origin（幂等，只补字段）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行写回（默认 dry-run 只预览统计，不写任何数据）",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="",
        help="目标库绝对路径（默认跟随 DB_PATH 环境变量 / .env）；正式库建议先拷副本",
    )
    args = parser.parse_args()

    if args.db:
        os.environ["DB_PATH"] = os.path.abspath(args.db)

    import _common  # noqa: F401  导入即把项目根注入 sys.path

    from core.digest import DIGEST_ORIGIN_DIALOGUE, DIGEST_ORIGIN_SESSION

    result = migrate_digest_origin(apply=args.apply)
    action = "执行" if args.apply else "预览（dry-run，未写数据）"
    print(f"迁移模式: {action} | 库: {result['db_path']}")
    if result["preview"]:
        print("前缀命中节点（前 20 条）:")
        for p in result["preview"]:
            print(f"  id={p['node_id']}  origin={p['origin']}  content={p['content']!r}")
    elif not args.apply:
        print("（无前缀命中的待迁移节点）")
    print("-" * 40)
    for key in ("scanned", "patched", "skipped_already_marked", "by_origin"):
        print(f"{key}: {result[key]}")
    if args.apply and result["patched"]:
        print(f"提示：已补 meta.origin（{DIGEST_ORIGIN_SESSION} / {DIGEST_ORIGIN_DIALOGUE}），正文未改动。")


def migrate_digest_origin(apply: bool = False) -> dict:
    """遍历所有节点，给「content 前缀命中但无 meta.origin」的节点补标记。

    返回统计 dict：{dry_run, db_path, scanned, patched, skipped_already_marked,
    skipped_no_prefix, by_origin, preview}；by_origin 按 origin 计数；
    preview 列出前 20 条命中节点。判定与回退通道共用 core.digest 的同一份
    前缀规则（迁移判据必须是纯内容判据——已打标与否不影响「该不该补」，
    但「已有 origin」的节点一律跳过以保证幂等）。
    """
    from core.digest import prefix_digest_origin
    from core.trivium_store import TriviumStore

    store = TriviumStore()
    scanned = 0
    patched = 0
    already = 0
    no_prefix = 0
    by_origin: dict[str, int] = {}
    preview: list[dict] = []
    # 先把迭代器完全消费掉再写库：triviumdb 的迭代器在耗尽前不释放连接，
    # 循环体内再开写连接会报 Database locked（--apply 第一个节点就会炸）
    nodes = list(store.iter_payloads())
    for nid, payload in nodes:
        scanned += 1
        origin = prefix_digest_origin(payload)
        if origin is None:
            no_prefix += 1
            continue
        meta = payload.get("meta") or {}
        if meta.get("origin"):
            # 已有任何 origin 标记（含人工/外部用途）→ 不覆盖，保证幂等
            already += 1
            continue
        if len(preview) < 20:
            preview.append({"node_id": nid, "origin": origin, "content": (payload.get("content") or "")[:40]})
        if apply:
            new_payload = dict(payload)
            new_payload["meta"] = {**meta, "origin": origin}
            store.update_payload(nid, new_payload)
        by_origin[origin] = by_origin.get(origin, 0) + 1
        patched += 1

    return {
        "dry_run": not apply,
        "db_path": store.db_path,
        "scanned": scanned,
        "patched": patched,
        "skipped_already_marked": already,
        "skipped_no_prefix": no_prefix,
        "by_origin": by_origin,
        "preview": preview,
    }


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
