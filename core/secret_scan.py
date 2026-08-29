"""敏感信息扫描：写入前检测 API Key / Token / 私钥，命中强规则拒绝入库；
个人信息（身份证/手机号）为弱规则，命中仅放行并打标记（secret_hint）供审计。"""

import re


# 强规则：命中 = 拒绝入库
STRONG_RULES: list[tuple[str, str]] = [
    ("openai_key", r"sk-[A-Za-z0-9_-]{20,}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("google_key", r"AIza[0-9A-Za-z_-]{20,}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("aws_key", r"AKIA[0-9A-Z]{16}"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("ssh_key", r"ssh-rsa AAAA[0-9A-Za-z+/]+"),
    ("bearer", r"Bearer [A-Za-z0-9._~+/=-]{20,}"),
]

# 弱规则：命中 = 警告放行（打 secret_hint 标记，不拒绝入库）
WEAK_RULES: list[tuple[str, str]] = [
    ("id_card", r"(?<![0-9])[0-9]{17}[0-9Xx](?![0-9])"),
    ("phone", r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])"),
]

# 兼容别名：全量规则 = 强 + 弱
SECRET_RULES: list[tuple[str, str]] = STRONG_RULES + WEAK_RULES


def scan_secret(text: str) -> list[str]:
    """扫描文本中的敏感信息，返回所有命中的规则名列表（空列表=干净）。

    兼容现有调用：扫描 STRONG + WEAK 全部规则。"""
    return _scan(text, SECRET_RULES)


def scan_secret_classified(text: str) -> dict:
    """分类扫描：返回 {"strong": [...], "weak": [...]}。

    strong 命中 = 应拒绝入库；weak 命中 = 警告放行。"""
    return {
        "strong": _scan(text, STRONG_RULES),
        "weak": _scan(text, WEAK_RULES),
    }


def _scan(text: str, rules: list[tuple[str, str]]) -> list[str]:
    hits: list[str] = []
    for name, pattern in rules:
        if re.search(pattern, text):
            hits.append(name)
    return hits


class SecretScanError(Exception):
    """强规则敏感信息扫描未通过异常"""

    def __init__(self, rules: list[str]) -> None:
        self.rules = rules
        msg = "内容包含敏感信息，拒绝入库。命中规则：" + "、".join(rules)
        super().__init__(msg)
