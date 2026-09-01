# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.3 特性测试 —— 撕裂恢复子进程（仅供 test_storage.py 调用）。

用法：
  python _tdb_hardkill_child.py --child-hardkill <path> <count>

行为：
  以默认（read_write）打开库 → 插入 <count> 条 → os._exit(23) 硬杀
  （绕过干净 close / WAL 刷盘，模拟进程被强杀）。父进程据此校验恢复。

约束（压测工具管理铁律）：
  · 顶层仅在被「作为脚本执行」时运行（__main__ 守卫），被 import 时不做事；
  · 进程计数守卫：同库同时只允许一个写入实例，避免互相踩踏（破坏测试并发隔离）。
"""
import os
import sys
import time

import triviumdb  # noqa: E402


def _child_hardkill(path: str, count: int) -> int:
    lock_path = path + ".hardkill.lock"
    if os.path.exists(lock_path):
        return 42  # 已有实例正在写 → 拒绝（防止并发踩踏）
    try:
        with open(lock_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
        for i in range(count):
            db.insert([0.001 * (i % 800) + 0.1] * 8, {"num": i})
        time.sleep(0.2)  # 给 WAL 写盘留一点时间，模拟真实写到一半被强杀
        os._exit(23)
    finally:
        # 正常情况下到不了这里（os._exit 已退出），仅防御性清理
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except OSError:
            pass
    return 0


def _main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--child-hardkill":
        return _child_hardkill(sys.argv[2], int(sys.argv[3]))
    sys.stderr.write("usage: _tdb_hardkill_child.py --child-hardkill <path> <count>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
