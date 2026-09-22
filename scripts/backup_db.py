#!/usr/bin/env python
"""Palimpsest 数据库整文件组冷备份。

为什么必须整组
--------------
TriviumDB 把同一份数据分散在多个文件里：主文件、向量块、图索引、payload
sidecar（按 generation 命名）、以及 generation 凭据（.flush_ok）。这些文件
必须属于同一个 generation 才能被打开。只拷其中一部分得到的快照会「读得动、
写不动」（打开时报 `拒绝不完整的 .tdb/.vec generation`），无法用于恢复。

用法
----
    python scripts/backup_db.py                 # 备份 + 校验，默认保留 7 份
    python scripts/backup_db.py --keep 14
    python scripts/backup_db.py --target D:/palimpsest_backups
    python scripts/backup_db.py --no-verify     # 跳过回读校验（不推荐）

退出码：0 成功；1 备份或校验失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from config import Config  # noqa: E402

# 参与整组备份的侧车后缀；payload sidecar（.pld.<generation>）用前缀单独匹配。
_SIDE_SUFFIXES = (".vec", ".gidx", ".pidx", ".flush_ok", ".wal")
# 干扰文件：历史备份、旧格式、中断残留 —— 一律不进备份。
_SKIP_MARKERS = (".bak", ".pre-", ".prev_attempt", ".tmp")


def collect_group(db_path: str) -> list[str]:
    """收集当前库的整文件组（属于同一 generation 的全部文件）。"""
    directory = os.path.dirname(db_path) or "."
    stem = os.path.basename(db_path)
    files = []
    for name in sorted(os.listdir(directory)):
        if not name.startswith(stem):
            continue
        tail = name[len(stem):]
        if any(marker in tail for marker in _SKIP_MARKERS):
            continue
        if tail == "" or tail in _SIDE_SUFFIXES or tail.startswith(".pld."):
            files.append(os.path.join(directory, name))
    return files


def verify_group(db_path: str) -> int:
    """回读校验：确认快照能被独立打开，返回节点数。

    用默认（read_write）模式打开副本：只读/immutable 模式对 sidecar 有额外
    要求（如 .manifest.json），拿它做验收会误判。校验对象是副本，写入副作用
    只落在副本上。
    """
    import triviumdb

    dim = int(getattr(Config, "OLLAMA_EMBEDDING_DIM", 1024))
    db = triviumdb.TriviumDB(db_path, dim=dim)
    try:
        return db.node_count()
    finally:
        db.close()


def prune_backups(target_dir: str, keep: int) -> list[str]:
    """滚动清理：按名称排序（时间戳前缀）保留最近 keep 份。"""
    entries = sorted(
        d for d in os.listdir(target_dir)
        if d.startswith("backup_") and os.path.isdir(os.path.join(target_dir, d))
    )
    removed = []
    for name in entries[:-keep] if keep > 0 else []:
        shutil.rmtree(os.path.join(target_dir, name))
        removed.append(name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Palimpsest 整文件组冷备份")
    parser.add_argument("--target", default="", help="备份目录（默认 <数据目录>/backups）")
    parser.add_argument("--keep", type=int, default=7, help="保留份数，默认 7")
    parser.add_argument("--no-verify", action="store_true", help="跳过回读校验")
    args = parser.parse_args()

    db_path = Config.DB_PATH
    if not os.path.exists(db_path):
        print(f"[backup] 找不到数据库：{db_path}", file=sys.stderr)
        return 1

    group = collect_group(db_path)
    if not group:
        print(f"[backup] 未匹配到任何文件组成员：{db_path}", file=sys.stderr)
        return 1

    target_dir = args.target or os.path.join(os.path.dirname(db_path), "backups")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(target_dir, f"backup_{stamp}")
    os.makedirs(dest, exist_ok=True)

    copied = []
    for src in group:
        shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
        copied.append(os.path.basename(src))
    print(f"[backup] 已备份 {len(copied)} 个文件 -> {dest}")

    if not args.no_verify:
        try:
            count = verify_group(os.path.join(dest, os.path.basename(db_path)))
            print(f"[backup] 回读校验通过：{count} 节点")
        except Exception as exc:  # noqa: BLE001 —— 校验失败即视为备份不可用
            print(f"[backup] 回读校验失败：{exc}", file=sys.stderr)
            print("[backup] 快照可能不是同一 generation —— 请检查备份期间是否有写入",
                  file=sys.stderr)
            return 1

    for name in prune_backups(target_dir, args.keep):
        print(f"[backup] 清理旧备份：{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
