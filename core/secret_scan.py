"""敏感信息扫描：记忆写入前检测 API Key / Token / 私钥 / 个人信息，命中拒绝入库。"""

import re


SECRET_RULES: list[tuple[str, str]] = [
    ("openai_key", r"sk-[A-Za-z0-9_-]{20,}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("google_key", r"AIza[0-9A-Za-z_-]{20,}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("aws_key", r"AKIA[0-9A-Z]{16}"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("ssh_key", r"ssh-rsa AAAA[0-9A-Za-z+/]+"),
    ("id_card", r"(?<![0-9])[0-9]{17}[0-9Xx](?![0-9])"),
    ("phone", r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])"),
    ("bearer", r"Bearer [A-Za-z0-9._~+/=-]{20,}"),
]


def scan_secret(text: str) -> list[str]:
    """扫描文本中的敏感信息，返回命中的规则名列表（空列表=干净）。"""
    hits: list[str] = []
    for name, pattern in SECRET_RULES:
        if re.search(pattern, text):
            hits.append(name)
    return hits


class SecretScanError(Exception):
    """敏感信息扫描未通过异常"""

    def __init__(self, rules: list[str]) -> None:
        self.rules = rules
        msg = "内容包含敏感信息，拒绝入库。命中规则：" + "、".join(rules)
        super().__init__(msg)
