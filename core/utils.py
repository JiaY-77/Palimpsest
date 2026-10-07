"""通用工具函数"""

from typing import Any


def _to_float(value: Any, default: float) -> float:
    """安全转 float，失败用默认值（payload 字段可能为字符串）"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# triviumdb 的锁错误以 RuntimeError 抛出，措辞见下（中英双版）。用**消息标记**
# 识别而非异常类型，避免把其它 RuntimeError 误判成「库被占用」。
# 四种措辞缺一不可：任一漏判都会把「库被别的进程占用」误报成「库损坏、
# 需备份恢复」，对运维的误导性极强（见 core/db_health.py 的 busy 分支）。
_DB_LOCK_MARKERS = (
    "database locked",
    "already opened",
    "数据库已锁定",
    "incompatible access mode",
)


def _is_db_locked_error(exc: BaseException) -> bool:
    """判断异常是否为 triviumdb 的「库被其他进程占用」错误（大小写不敏感）。"""
    msg = str(exc).lower()
    return any(m in msg for m in _DB_LOCK_MARKERS)


# triviumdb 的「存储 generation 损坏 / 不一致」同样以 RuntimeError + 消息抛出
# （实测：即便 triviumdb 定义了 RecoveryRequiredError 等专用异常类，损坏场景抛的
# 仍是普通 RuntimeError）。故与锁错误同理，用**消息标记**识别；英文标记作通用形式，
# 中文括注作补充。用于「损坏必须 fail-fast、不得静默降级」的判据（见 trivium_store
# 的 _init_indexes）：漏判会让「库已损坏」伪装成「一切正常」而延误恢复。
_DB_CORRUPT_MARKERS = (
    "(corrupted file)",
    "拒绝不完整",
    ".tdb/.vec generation",
    "(immutable generation invalid)",
    "(graph block generation mismatch)",
    "(property index does not match the main database generation)",
    "sidecar metadata missing or mismatched",
)


def _is_db_corrupt_error(exc: BaseException) -> bool:
    """判断异常是否为 triviumdb 的「存储 generation 损坏」错误（大小写不敏感）。"""
    msg = str(exc).lower()
    return any(m in msg for m in _DB_CORRUPT_MARKERS)
