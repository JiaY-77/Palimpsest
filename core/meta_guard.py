"""自定义元数据（meta）打标护栏。

供写入入口（``mcp_tools.memory.mem_ingest``，REST ``/mem/ingest`` 转调同一
实现）在落 payload 前校验 ``meta`` 子字典：外置宿主（如酒馆记忆桥）会按
``meta.chat_id`` 检索并批量删除节点，meta 是结构性字段而非自由备注——
键名/数量/值长度不受限会让 payload 被外部通道写成无界字典。

契约：
  - 最多 ``MAX_META_KEYS``（8）个键
  - 键名限 ``META_KEY_PATTERN``（``^[a-zA-Z0-9_]{1,32}$``）
  - 值必须是字符串且长度 ≤ ``MAX_META_VALUE_CHARS``（256）
"""

import re

META_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,32}$")
MAX_META_KEYS = 8
MAX_META_VALUE_CHARS = 256


def validate_meta(meta: dict[str, str] | None) -> tuple[bool, str]:
    """校验可选 meta 字典；返回 ``(ok, error)``，合法时 error 为空串。

    ``None``（未传）视为合法；空字典同样不写入 payload，无需拒绝。
    校验关注结构（键名 / 数量 / 值类型与长度），不关心业务含义。
    """
    if meta is None:
        return True, ""
    if not isinstance(meta, dict):
        return False, "meta 必须是键值均为字符串的字典"
    if len(meta) > MAX_META_KEYS:
        return False, f"meta 键数超上限：{len(meta)} > {MAX_META_KEYS}"
    for key, value in meta.items():
        if not isinstance(key, str) or not META_KEY_PATTERN.fullmatch(key):
            return False, f"非法 meta 键名「{key}」：限 1~32 位英文字母/数字/下划线"
        if not isinstance(value, str):
            return False, f"meta 值必须是字符串：键「{key}」拿到 {type(value).__name__}"
        if len(value) > MAX_META_VALUE_CHARS:
            return False, f"meta 值超长：键「{key}」{len(value)} > {MAX_META_VALUE_CHARS} 字符"
    return True, ""
