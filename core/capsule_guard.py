"""手写胶囊结构字段（sections / pending）护栏。

供写入入口（``mcp_tools.memory.mem_ingest``，REST ``/mem/ingest`` 转调同一
实现）在落 payload 前校验可选的 ``sections`` / ``pending``，使手写胶囊与
自动胶囊（``core.strategy.compose_session_capsule`` 产出）形状一致：
sections 固定四节、pending 标注待补节。契约与 meta_guard 同构：结构非法
即拒绝写入（不猜、不静默修正），校验失败的输入绝不落库。

契约（节名与顺序见 ``CAPSULE_SECTION_ORDER``，与 strategy 侧的
``_CAPSULE_SECTION_ORDER`` 同源）：
  - sections 的键必须在四节白名单内，值必须是 str
  - pending 的元素必须是四节白名单之一
  - pending 不得包含 sections 中值非空白的键（矛盾输入直接拒，不猜）
  - 未传 pending 而传了 sections → 自动算 pending = sections 缺失或值为
    空白的键（按固定顺序），与自动胶囊「空节进 pending」同口径
"""

CAPSULE_SECTION_ORDER = ("thought", "outcome", "next", "pitfalls")
_CAPSULE_SECTIONS = frozenset(CAPSULE_SECTION_ORDER)


def validate_capsule_fields(
    sections: dict[str, str] | None,
    pending: list[str] | None,
) -> tuple[bool, str, list[str] | None]:
    """校验可选胶囊字段；返回 ``(ok, error, pending_resolved)``。

    ``sections`` / ``pending`` 均为 ``None``（未传）视为合法，此时
    ``pending_resolved`` 为 ``None``（调用方不落任何键，payload 形状与
    旧行为完全一致）。校验通过时 ``pending_resolved`` 是最终落库的
    pending：显式传入的原样返回（含空列表，尊重调用方声明，不做自动
    推断覆盖）；未传而 ``sections`` 非空时自动算（缺失或值为空白的节，
    固定顺序）；``sections`` 也未传有效内容时为 ``None``（不落 pending 键）。
    """
    if sections is None and pending is None:
        return True, "", None
    if sections is not None:
        if not isinstance(sections, dict):
            return False, "sections 必须是键值均为字符串的字典", None
        for key, value in sections.items():
            if key not in _CAPSULE_SECTIONS:
                return False, f"非法 sections 键「{key}」：限 thought / outcome / next / pitfalls", None
            if not isinstance(value, str):
                return False, f"sections 值必须是字符串：键「{key}」拿到 {type(value).__name__}", None
    if pending is not None:
        if not isinstance(pending, list):
            return False, "pending 必须是字符串数组", None
        for item in pending:
            if not isinstance(item, str) or item not in _CAPSULE_SECTIONS:
                return False, f"非法 pending 元素「{item}」：限 thought / outcome / next / pitfalls", None
            if isinstance(sections, dict) and isinstance(sections.get(item), str) and sections[item].strip():
                return False, f"pending 与 sections 矛盾：节「{item}」在 sections 中非空却又列为待补", None
    if pending is None and sections:
        pending = [name for name in CAPSULE_SECTION_ORDER if name not in sections or not sections[name].strip()]
    return True, "", pending
