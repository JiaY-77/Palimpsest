"""检索侧的小型判定工具（core 层，便于单测与复用）。

当前仅含「精确标识符查询」判定：混合检索在 query 含精确标识符（commit
hash / PR 号 / 工单号）时抬高 FTS 通道权重（见 config.RRF_FTS_EXACT_WEIGHT）。
判定必须保守——判不了 → False，让普通查询走原有权重，不改全局行为。
"""

from __future__ import annotations

import re

# commit hash：7+ 位十六进制 token，且**必须含数字**。
# 含数字约束防误伤真英文词——defaced / decade 等单词全部由 [0-9a-f] 字母构成，
# 但真实 short hash（7 位）含数字的概率约 97%，约束几乎不损失召回。
_HEX_TOKEN_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b")

# PR / issue 号：# 后 1-5 位数字收词边界（6 位以上按一般长数字处理，不算 PR 号）
_PR_TOKEN_RE = re.compile(r"#\d{1,5}\b")

# 工单号：JIRA 风格大写前缀-数字（如 ABC-123 / OPS-4567）
_TICKET_TOKEN_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")

_EXACT_TOKEN_RES = (_HEX_TOKEN_RE, _PR_TOKEN_RE, _TICKET_TOKEN_RE)


def is_exact_identifier_query(query: str) -> bool:
    """query 是否为「精确标识符查询」（commit hash / PR 号 / 工单号）。

    命中任一标识符型 token 即 True（如 ``d60240b fix ...`` / ``PR #131`` /
    ``修复 WEB-1234``）。纯中文、纯自然语言 query 一律 False；空 query /
    解析不出 → False（保守：宁可漏判走原权重，不可误判偷改全局行为）。

    注意：``[0-9a-f]{7,}`` 建议正则加了「含数字」约束——``defaced`` 这类
    纯字母英文词不触发 FTS 权重抬高；real hash（``d60240b``）不受影响。
    """
    if not query or not query.strip():
        return False
    return any(rx.search(query) for rx in _EXACT_TOKEN_RES)
