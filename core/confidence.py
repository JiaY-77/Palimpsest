"""记忆置信度：Subjective Logic 证据映射（可选，默认关闭）。

为什么是「证据映射」而不是「主观打分」
--------------------------------------
置信度必须**可复现**：同一条记忆、同样的证据，任何时候算出的分数都相同。
主观打分（「这条给 3 分」）做不到——同一个 agent 两次写同一件事可能给不同分，
序数量表的档间边界也说不清。

所以这里只做一件事：从 payload 与正文里提取**可观测证据**，按固定公式算分。
判断被压缩到一次性的规则配置（见下方权重表），而不是逐条决策。

公式（Subjective Logic，Jøsang）
--------------------------------
    b = r / (r + s + W)
    d = s / (r + s + W)
    u = W / (r + s + W)

``r`` / ``s`` 是支持 / 反驳证据的加权和；``W`` 是 non-informative prior 权重。
它保证「没有任何证据」时 ``b = d = 0, u = 1``（完全不确定），而不是 0/0 或 1——
历史节点在引入本模块前没有证据记录，正是靠这一条自动落到「未知」，
不需要给它们编一个默认分数。

为什么不进排序权重
------------------
置信度是**微调信号**：实测把它当主排序权重（或做硬门槛）会让召回掉 4.8 个百分点。
它只适合做 ε 级 tie-break ——「几个候选语义分相近时，把有证据的排前面」。
见 ``docs/CONFIDENCE.md`` 的实测数据。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# 证据规则：一次性配置。改动这里等于改所有记忆的分数，请同步更新 docs/CONFIDENCE.md
# ---------------------------------------------------------------------------

# 正文里可提取的支持证据：(名称, 正则, 权重)
_CONTENT_SUPPORT: tuple[tuple[str, re.Pattern[str], float], ...] = (
    # commit hash / PR 号：最硬的证据，可回溯到具体提交。
    # 7-40 位 hex 且**至少含一个字母**——git 短 hash 是 7 位，而纯 7 位数字
    # （如日期 2026102）不该被当成提交号。
    (
        "commit",
        re.compile(r"\b(?=[0-9a-f]{7,40}\b)[0-9a-f]*[a-f][0-9a-f]*\b|\bPR\s*#\d+\b", re.IGNORECASE),
        1.0,
    ),
    # 文件路径：能指回具体文件
    (
        "path",
        re.compile(r"[A-Za-z]:[\\/][^\s，。；、）)\]]{3,}|(?<![\w/])/(?:home|mnt|tmp|d)/[^\s]{3,}"),
        0.8,
    ),
    ("url", re.compile(r"https?://[^\s，。）)]+"), 0.8),
    # 内部节点引用：能指回另一条记忆
    ("node", re.compile(r"node\s*\d+|节点\s*\d+", re.IGNORECASE), 0.6),
    # 实测 / 验证声明（抽样审计精确率 92%）
    ("tested", re.compile(r"实测|跑出来|测试通过|复跑|验证过|exit code|全绿|全过|断言"), 0.7),
)

# 自我指涉的不确定表达。
# 注意：**不要**加「可能 / 也许 / 大概 / 或许」——实测这类词在技术文档里
# 绝大多数是在描述客观可能性（「未来可能 16G」「状态可能不一致」），
# 拿它当「这条记忆不确定」的信号，精确率只有 2/12 = 17%。
_HEDGE = re.compile(r"待确认|待验证|存疑|未验证|无法确认|尚无定论|不确定是否|没有证据|缺乏证据|暂未核实")

_SUPPORT_SOURCE_PATH = 0.9  # payload 带 source_path（kb_chunk / skill_chunk）
_SUPPORT_GIT_COMMIT = 1.0  # type=git_commit 且带提交信息
_PENALTY_SUPERSEDED = 1.0  # status=outdated：已被明确取代
_PENALTY_HEDGE = 0.3
_PRIOR_WEIGHT = 2.0


def extract_evidence(payload: dict[str, Any]) -> tuple[float, float, list[str]]:
    """从 payload 提取证据，返回 ``(支持权重 r, 反驳权重 s, 命中的证据名)``。"""
    content = str(payload.get("content") or "")
    r = 0.0
    kinds: list[str] = []

    if payload.get("source_path"):
        r += _SUPPORT_SOURCE_PATH
        kinds.append("source_path")
    if payload.get("type") == "git_commit" and (
        payload.get("commit_hash") or payload.get("commit_time") or payload.get("commit")
    ):
        r += _SUPPORT_GIT_COMMIT
        kinds.append("git_commit")
    for name, pattern, weight in _CONTENT_SUPPORT:
        if pattern.search(content):
            r += weight
            kinds.append(name)

    s = 0.0
    if payload.get("status") == "outdated":
        s += _PENALTY_SUPERSEDED
        kinds.append("superseded")
    if _HEDGE.search(content):
        s += _PENALTY_HEDGE
        kinds.append("hedged")

    return r, s, kinds


def confidence_of(payload: dict[str, Any]) -> dict[str, Any]:
    """返回 ``{b, d, u, evidence}``；三者满足 ``b + d + u == 1``。"""
    r, s, kinds = extract_evidence(payload)
    denom = r + s + _PRIOR_WEIGHT
    return {
        "b": r / denom,
        "d": s / denom,
        "u": _PRIOR_WEIGHT / denom,
        "evidence": kinds,
    }


def normalized(payload: dict[str, Any], center: float, span: float) -> float:
    """把 ``b`` 映射到 ``[-1, 1]``：``b == center`` 时为 0，超出 ``span`` 截断。

    用固定线性映射而不是 z-score，是为了不依赖全库统计量——检索时不必先算
    一遍均值 / 标准差，同一条记忆在任何库规模下得分一致。
    """
    if span <= 0:
        return 0.0
    b = confidence_of(payload)["b"]
    return max(-1.0, min(1.0, (b - center) / span))
