"""写入口约束护栏（write-guard policy）。

统一在写入口（`mcp_tools.memory.mem_ingest`，REST `/mem/ingest` 转调同一实现）
上做策略校验，防三类问题：

  1. 只读节点保护 —— 受保护节点（type=rule 或 payload.protected=True）不应被
     自动冲突检测标 outdated / 覆盖；
  2. 配置人工闸门 —— agent 不得自行修改写策略配置，须人工显式放行；
  3. 分级上限 —— 单节点体积、按 type/domain 的更细上限。

模式（`Config.POLICY_MODE`）：
  - ``warn``（默认）：所有策略只记 ``logger.warning`` + 结果字段，**不改任何
    写行为**；攒一阶段数据后再显式开 ``enforce``。
  - ``enforce``：策略真正生效（拒绝 / 跳过受保护节点）。

本模块为纯函数优先：不 import store、不做任何写操作、不在导入期做耗时/网络
操作。只读 ``config.Config`` 静态值。
"""

import logging

from config import Config

logger = logging.getLogger(__name__)

MODE_WARN = "warn"
MODE_ENFORCE = "enforce"


def get_policy_mode() -> str:
    """当前策略模式；非法值回退 ``warn``（最保守，不改行为）。"""
    mode = str(getattr(Config, "POLICY_MODE", MODE_WARN) or "").strip().lower()
    if mode not in (MODE_WARN, MODE_ENFORCE):
        return MODE_WARN
    return mode


def is_protected(payload: dict | None) -> bool:
    """节点是否受保护：``type == "rule"`` 或 ``payload["protected"] is True``。

    受保护节点不参与自动覆盖（enforce 模式下跳过标 outdated / 建边）。
    """
    if not payload:
        return False
    if payload.get("protected") is True:
        return True
    ptype = payload.get("type")
    if not isinstance(ptype, str):
        return False
    protected_types = getattr(Config, "POLICY_PROTECTED_TYPES", frozenset({"rule"})) or frozenset()
    return ptype.strip().lower() in {t.lower() for t in protected_types}


def _parse_type_limits(raw: str) -> dict[str, int]:
    """解析 ``POLICY_TYPE_LIMITS``（形如 ``"task:20000,plan:80000"``）。"""
    limits: dict[str, int] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        key, _, val = item.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not key or not val.isdigit():
            continue
        limits[key] = int(val)
    return limits


def check_ingest(content: str, node_type: str, domain: str) -> dict:
    """入口校验：空内容 / 超长 / 分级上限。

    返回 ``{"ok": bool, "warnings": list[str], "error": str | None}``。

    - 空内容 / 超长：无论何种模式都硬拒（沿用既有 MEM_INGEST_MAX_LENGTH 语义）。
    - 分级上限（``POLICY_TYPE_LIMITS``）：warn 模式只记 warning 不拒；
      enforce 模式拒绝。
    """
    warnings: list[str] = []
    stripped = (content or "").strip()
    if not stripped:
        return {"ok": False, "warnings": warnings, "error": "内容不能为空"}
    max_len = int(getattr(Config, "MEM_INGEST_MAX_LENGTH", 50_000))
    if len(stripped) > max_len:
        return {
            "ok": False,
            "warnings": warnings,
            "error": f"内容超长：{len(stripped)} 字符，上限 {max_len} 字符",
        }

    # 分级上限：按 type 可覆盖的更细上限
    limits = _parse_type_limits(getattr(Config, "POLICY_TYPE_LIMITS", "") or "")
    type_key = (node_type or "").strip().lower()
    if type_key in limits:
        limit = limits[type_key]
        if len(stripped) > limit:
            msg = f"内容超过 type={type_key} 的软上限：{len(stripped)} > {limit} 字符"
            if get_policy_mode() == MODE_ENFORCE:
                return {"ok": False, "warnings": warnings, "error": msg}
            warnings.append(msg)
            logger.warning("policy(warn): %s", msg)

    return {"ok": True, "warnings": warnings, "error": None}


def check_protected_overwrite(old_payload: dict | None) -> str | None:
    """受保护节点将被覆盖时返回告警文案；非受保护返回 ``None``。"""
    if not is_protected(old_payload):
        return None
    ptype = (old_payload or {}).get("type", "?")
    return f"试图覆盖受保护节点（type={ptype}）"
