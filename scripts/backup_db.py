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

除 TriviumDB 主库组之外，本脚本还备份同一目录的 fts.db（FTS5 全文索引，
core/fts_index.py）及其 -wal / -shm 侧车（存在才拷），拷入备份目录根、文件名
不变。回读校验通过后还会比对 fts.db 行数与备份开始前的源库行数（issue #241：
fts.db 不进备份，恢复后 FTS 静默变空、混合检索悄悄降级为纯语义）。源库没有
fts.db（全新部署、索引从未建立）时跳过并说明，不算失败。

退出码：0 成功；1 备份或校验失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
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
# FTS 索引侧车（SQLite WAL 模式伴随文件）。
_FTS_SIDE_SUFFIXES = ("-wal", "-shm")


def collect_group(db_path: str) -> list[str]:
    """收集当前库的整文件组（属于同一 generation 的全部文件）。"""
    directory = os.path.dirname(db_path) or "."
    stem = os.path.basename(db_path)
    files = []
    for name in sorted(os.listdir(directory)):
        if not name.startswith(stem):
            continue
        tail = name[len(stem) :]
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


def collect_fts_files(fts_path: str) -> list[str]:
    """收集 FTS 索引组：fts.db 本体 + -wal / -shm 侧车（存在才进）。"""
    files = []
    if os.path.isfile(fts_path):
        files.append(fts_path)
    for suffix in _FTS_SIDE_SUFFIXES:
        side = fts_path + suffix
        if os.path.isfile(side):
            files.append(side)
    return files


def fts_row_count(path: str) -> int:
    """只读统计 fts.db 的 mem_fts 行数（文件/表异常向上抛，由调用方定夺）。

    用 mode=ro 打开：校验绝不顺手建文件，也不阻塞正在写索引的进程。
    """
    import pathlib

    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return int(conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0])
    finally:
        conn.close()


def prune_backups(target_dir: str, keep: int) -> list[str]:
    """滚动清理：按名称排序（时间戳前缀）保留最近 keep 份。"""
    entries = sorted(
        d for d in os.listdir(target_dir) if d.startswith("backup_") and os.path.isdir(os.path.join(target_dir, d))
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

    # FTS 索引组（issue #241）：备份开始前先对源库取一次行数快照，避免备份
    # 期间的写入让「副本 vs 源」比较误判。源库没有 fts.db（全新部署、索引
    # 从未建立）→ 跳过并说明，不算失败，退出码仍按主库组结果。
    from core.fts_index import _db_path as _fts_db_path

    fts_path = _fts_db_path()
    fts_files = collect_fts_files(fts_path)
    source_fts_rows: int | None = None
    if not fts_files:
        print("[backup] 源库无 fts.db（FTS 索引尚未建立）——跳过 FTS 索引备份")
    else:
        try:
            source_fts_rows = fts_row_count(fts_path)
        except Exception as exc:  # noqa: BLE001 —— 行数读不出只跳过 FTS 校验，不阻断主库备份
            print(f"[backup] 读取源 fts.db 行数失败（{exc}），本次不比对 FTS 行数", file=sys.stderr)

    target_dir = args.target or os.path.join(os.path.dirname(db_path), "backups")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(target_dir, f"backup_{stamp}")
    os.makedirs(dest, exist_ok=True)

    copied = []
    for src in group:
        shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
        copied.append(os.path.basename(src))
    print(f"[backup] 已备份 {len(copied)} 个文件 -> {dest}")

    if fts_files:
        for src in fts_files:
            shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
        print(
            f"[backup] 已附带 FTS 索引组 ({len(fts_files)} 个文件: {', '.join(os.path.basename(p) for p in fts_files)})"
        )

    if not args.no_verify:
        try:
            count = verify_group(os.path.join(dest, os.path.basename(db_path)))
            print(f"[backup] 回读校验通过：{count} 节点")
        except Exception as exc:  # noqa: BLE001 —— 校验失败即视为备份不可用
            print(f"[backup] 回读校验失败：{exc}", file=sys.stderr)
            print("[backup] 快照可能不是同一 generation —— 请检查备份期间是否有写入", file=sys.stderr)
            return 1

        # 主库组校验通过后再校验 FTS 副本（issue #241）
        if source_fts_rows is not None:
            dest_fts = os.path.join(dest, os.path.basename(fts_path))
            try:
                if not os.path.isfile(dest_fts):
                    raise FileNotFoundError("FTS 索引副本缺失（fts.db 未拷入备份目录）")
                fts_rows = fts_row_count(dest_fts)
                if fts_rows != source_fts_rows:
                    print(
                        f"[backup] FTS 索引校验不一致：源 {source_fts_rows} 行 vs 副本 {fts_rows} 行"
                        "（备份期间有写入？可用 python scripts/check_fts_consistency.py --repair 重建）",
                        file=sys.stderr,
                    )
                    return 1
                print(f"[backup] FTS 索引校验通过：{fts_rows} 行")
            except Exception as exc:  # noqa: BLE001 —— FTS 校验失败同样不可交付
                print(f"[backup] FTS 索引校验失败：{exc}", file=sys.stderr)
                return 1

    for name in prune_backups(target_dir, args.keep):
        print(f"[backup] 清理旧备份：{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
