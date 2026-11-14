"""
core.digest —— 对话残渣（digest）识别与来源标记（issue #278）
============================================================
session-end「会话要点」/ post-turn「[对话沉淀] 用户原话」是每次会话固定写入的
对话残渣：语义极泛（任何查询都有一定相似度）+ 数量随会话数线性增长，在候选
窗口里天然占位，把结论型记忆挤出去。

本模块三件事：
  1. 来源标记常量与识别判据（meta.origin 打标 / 历史 content 前缀兜底）；
  2. 检索降权：digest 类节点默认不进检索候选窗口（include_digest=True 是
     显式审计/回溯通道）；
  3. 写入侧打标：lifecycle 两条 digest 写入路径带 ``meta.origin``。

设计约束：
  - 只降级可达性，不删除任何历史节点、不改正文；
  - 识别不依赖迁移脚本是否已跑：老节点未打 meta.origin 时按 content 前缀
    兜底判定（检索过滤即时生效）；
  - ``meta.origin`` 是通用元数据键（外部宿主也可用），本模块只认值命中
    DIGEST_ORIGINS 的条目，不劫持其他用途。
"""

# ---- 来源标记值（meta.origin 的 digest 取值）----
DIGEST_ORIGIN_SESSION = "session_digest"  # session-end「会话要点」提炼结论
DIGEST_ORIGIN_DIALOGUE = "dialogue_digest"  # post-turn「[对话沉淀] 用户原话」

DIGEST_ORIGINS = (DIGEST_ORIGIN_SESSION, DIGEST_ORIGIN_DIALOGUE)

# ---- 历史 content 前缀 → origin（老库迁移与运行时兜底共用同一份判据）----
# 库里的历史节点有三种写法（issue 原文核查）：两种「会话要点」老格式 +
# 「[对话沉淀] 用户:」。「会话要点」两种前缀同归 session_digest；
# 「[对话沉淀]」打头的一律归 dialogue_digest（含 用户/assistant 变体）。
DIGEST_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("会话要点（Palimpsest 策略引擎提炼）：", DIGEST_ORIGIN_SESSION),
    ("会话要点（Palimpsest 插件提炼）：", DIGEST_ORIGIN_SESSION),
    ("[对话沉淀]", DIGEST_ORIGIN_DIALOGUE),
)


def prefix_digest_origin(payload: dict) -> str | None:
    """只按 content 前缀判定 digest 来源（不看 meta.origin）。

    返回 DIGEST_ORIGIN_* 或 None。老库迁移脚本专用（迁移判据必须是纯内容
    判据——已打标与否不影响「该不该补」）。
    """
    content = str((payload or {}).get("content") or "")
    if not content:
        return None
    for prefix, origin in DIGEST_PREFIX_RULES:
        if content.startswith(prefix):
            return origin
    return None


def digest_origin_of(payload: dict) -> str | None:
    """节点是否为 digest 类：meta.origin 打标优先，content 前缀兜底。

    - meta.origin 命中 DIGEST_ORIGINS → 直接采信（新写入路径与迁移后的节点）；
    - 否则按 content 前缀兜底（未迁移的老节点也能被检索过滤即时识别）。
    返回 DIGEST_ORIGIN_* 或 None（非 digest）。
    """
    origin = str(((payload or {}).get("meta") or {}).get("origin") or "")
    if origin in DIGEST_ORIGINS:
        return origin
    return prefix_digest_origin(payload)


def is_digest_payload(payload: dict) -> bool:
    """检索侧过滤判据：该 payload 是否属于默认不可达的 digest 类节点。"""
    return digest_origin_of(payload) is not None
