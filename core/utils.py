# -*- coding: utf-8 -*-
"""通用工具函数"""

from typing import Any


def _to_float(value: Any, default: float) -> float:
    """安全转 float，失败用默认值（payload 字段可能为字符串）"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default