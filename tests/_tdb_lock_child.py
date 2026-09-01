# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.3 特性测试 —— 跨进程写锁子进程（仅供 test_concurrency.py 调用）。

用法：
  python _tdb_lock_child.py --writer <path>
  python _tdb_lock_child.py --reader <path>

行为：
  --writer：尝试以 read_write 打开 <path>，成功后写入 1 条并打印 OPEN/WRITE_OK，
            随后退出（干净 close）。
  --reader：尝试以 read_only 打开 <path>，成功则打印 OPEN，稍作停留后退出。

约束（压测工具管理铁律）：
  · 顶层仅在被作为脚本执行时运行（__main__ 守卫），被 import 时不做事；
  · 进程计数守卫：同库路径的并发写入由本子进程通过 lock 文件互斥，防止重复实例踩踏。
"""
import os
import sys
import time

import triviumdb  # noqa: E402


def _writer(path: str) -> int:
    lock = path + ".childpid"
    if os.path.exists(lock):
        return 41  # 已有写入实例 → 拒绝
    try:
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
        print("OPEN")
        db.insert([0.1] * 8, {"num": 1})
        db.close()
        print("WRITE_OK")
        return 0
    finally:
        try:
            if os.path.exists(lock):
                os.remove(lock)
        except OSError:
            pass


def _reader(path: str) -> int:
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False,
                             access_mode="read_only")
    print("OPEN_READ")
    time.sleep(0.5)
    db.close()
    return 0


def _main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--writer":
        return _writer(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "--reader":
        return _reader(sys.argv[2])
    sys.stderr.write("usage: _tdb_lock_child.py --writer|--reader <path>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
