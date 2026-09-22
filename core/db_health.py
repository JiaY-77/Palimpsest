"""数据库健康探针。

为什么需要
----------
TriviumDB 对库文件是严格排他的，跨进程并发写入会让写入方失败，并在文件组
留下残留（`.tmp` / `.wal`），进而导致 generation 校验失败 —— 库会从「可读写」
退化为「读不动」，且不会自愈。**一次写入失败就足以污染整库**。

因此写入失败必须立刻被检验并告警：若库已不健康，运维需要拿到明确信号
（而不是「下一个请求又失败了」），及时从冷备份恢复。

本模块只做探测，不修改任何数据。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def check_db_health(db_path: str | None = None, dim: int | None = None) -> dict:
    """探测库能否被正常打开，返回 ``{"ok", "node_count", "error"}``。

    只在响应/日志里使用，不抛异常（探测本身失败也要给出结构化结果）。
    """
    import triviumdb

    from config import Config

    path = db_path or Config.DB_PATH
    dim_val = dim or int(getattr(Config, "OLLAMA_EMBEDDING_DIM", 1024))

    result: dict = {"ok": False, "node_count": None, "error": "", "busy": False}
    # 先做存在性检查：triviumdb 在路径不存在时会直接创建新空库 ——
    # 探针是只读操作，绝不能有「建出一个空库」这种副作用。
    if not os.path.exists(path):
        result["error"] = f"数据库文件不存在：{path}"
        return result
    try:
        db = triviumdb.TriviumDB(path, dim=dim_val)
        try:
            result["node_count"] = db.node_count()
            result["ok"] = True
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 —— 探测失败即「不健康」，原样回报原因
        msg = f"{type(exc).__name__}: {exc}"
        result["error"] = msg
        # 区分两类失败：库被别的进程占用（busy，非损坏）vs 文件组本身损坏。
        # 前者重试即可，后者必须走备份恢复 —— 混在一起会误导运维。
        low = msg.lower()
        result["busy"] = ("locked" in low or "已锁定" in msg
                          or "incompatible access mode" in low)
    return result


def health_hint(health: dict) -> str:
    """把探测结果转成一句人话告警（供日志与响应体共用）。"""
    if health.get("ok"):
        return ""
    if health.get("busy"):
        return (
            "数据库被其他进程占用（非损坏）。确认只有一个进程访问该库"
            "（Palimpsest 的 REST 与 MCP 应共用同一进程，见 issue #32），然后重试。"
            f"原因：{health.get('error', '未知')}"
        )
    return (
        "数据库当前不可用，写入失败可能已污染文件组，且不会自愈。"
        "请立即用 scripts/backup_db.py 的产物做恢复，并在恢复前避免任何写入重试。"
        f"原因：{health.get('error', '未知')}"
    )
