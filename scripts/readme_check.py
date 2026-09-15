"""文档一致性检查：比对文档声称与代码实际（Palimpsest）。

检查项：
  1. MCP 工具清单   mcp_tools/*.py 的 @mcp.tool() 注册 ↔ README / README_EN 的工具表与标题数字
  2. CLI 子命令     scripts/palimpsest_cli.py 的 add_parser ↔ README / README_EN 的命令表
  3. REST 路由      main.py 的 @app.<method>(...) ↔ README / README_EN 的路由表
  4. 配置项         config.py ↔ .env.example ↔ README / README_EN 的配置表
  5. 文件引用       三份文档中的 markdown 链接 / 反引号路径 / 项目结构树条目是否真实存在
  6. 行内代码配对   文档正文（跳过代码块）每行的反引号必须成对
  7. 脱敏占位符     文档正文不应出现连续星号（脱敏遗留 / 行内代码未闭合；水平分割线除外）

设计意图：版本号同步属于「引用」层，本脚本补的是「内容」层——代码加了新工具 / 新命令 / 新配置项
而文档漏写时，靠它报出来，不靠人记。

用法：
    python scripts/readme_check.py              # 人类可读报告
    python scripts/readme_check.py --json       # 机器可读输出（与报告同一份数据）
    python scripts/readme_check.py --strict     # 警告也导致非零退出码
    python scripts/readme_check.py --root DIR   # 显式指定仓库根（默认按脚本位置推导）

退出码：0 = 通过；1 = 存在错误（或 --strict 下存在警告）；2 = 检查无法进行（仓库根不可用）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DOC_FILES = ("README.md", "README_EN.md", "CONTRIBUTING.md")
# CHANGELOG 只参与脱敏占位符检查（PLACEHOLDER_DOCS），不参与行内代码配对
# 与文件引用检查（DOC_FILES）：历史条目会合法引用已删除 / 已重命名的文件
# 与旧路径，纳入检查只会产生永远修不掉的历史噪音。
PLACEHOLDER_DOCS = (*DOC_FILES, "CHANGELOG.md")
FENCE = "```"

# 文档里合法出现、但不对应仓库内文件的引用（示例占位 / 运行时产物 / 用户本地文件）
EXEMPT_REFERENCES = {
    "data/mh_memory.db",
    "data/fts.db",
    "path/to/Palimpsest",
    ".env",
}
EXEMPT_PREFIXES = ("/tmp/", "~/", "<", "${")

# config.py 之外读取、但同属服务配置面的键 → 值 = 读取点。
# 显式登记而不是静默放过：集中到 config.py 统一读取属于后续架构工作，在那之前必须
# 写明「谁在读」，否则这些键既躲过 config.py 的键名/默认值比对，又会被误判成文档漂移。
CONFIG_READ_OUTSIDE_CONFIG_PY = {
    "KNOWLEDGE_DIR": "core/task_archive.py · mcp_tools/_common.py",
}

# 默认值不是字面量、无法与文档逐字比对的键 → 值 = 不能比对的原因（会打印在报告 info 里，避免静默跳过）
CONFIG_DERIVED_DEFAULTS = {
    "DB_PATH": "运行时解析：未给时 <项目根>/data/mh_memory.db",
    "EMBEDDING_PROVIDER": "未设置时自动探测：有可用云端 key → openai，否则 → ollama",
}

# 默认值为空串（未设置即关闭 / 未配置）时，文档侧允许的写法
CONFIG_EMPTY_MARKERS = ("空", "可选", "未设置", "empty", "optional", "unset")

GETENV_CALL_RE = re.compile(r"os\.(?:getenv|environ\.get)\(")
CONFIG_ROW_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*(.*?)\s*\|", re.MULTILINE)
CODE_STR_LITERAL_RE = re.compile(r"^[\"'](.*)[\"']$", re.DOTALL)
CODE_STR_WRAPPER_RE = re.compile(r"^str\(\s*([\d_]+)\s*\)$")


class Report:
    """单个检查项的结果容器。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.info = ""
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "info": self.info, "errors": self.errors, "warnings": self.warnings}


# ---------------------------------------------------------------------------
# 通用解析助手
# ---------------------------------------------------------------------------


def read_text(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def section(text: str, start: str, end: str | None) -> str:
    """返回 start 之后、end 之前的片段；end 为 None 表示到文末。找不到 start 时返回空串。"""
    idx = text.find(start)
    if idx < 0:
        return ""
    rest = text[idx + len(start) :]
    if end:
        stop = rest.find(end)
        if stop >= 0:
            rest = rest[:stop]
    return rest


def table_first_column(segment: str, pattern: str) -> set[str]:
    """按正则抽取 markdown 表格首列的匹配项。"""
    return set(re.findall(pattern, segment, re.MULTILINE))


# ---------------------------------------------------------------------------
# 1. MCP 工具清单
# ---------------------------------------------------------------------------


def actual_mcp_tools(root: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted((root / "mcp_tools").glob("*.py")):
        names |= set(re.findall(r"@mcp\.tool\([^)]*\)\s*\ndef\s+(\w+)", path.read_text(encoding="utf-8")))
    return names


def check_mcp_tools(root: Path) -> Report:
    report = Report("MCP 工具清单")
    actual = actual_mcp_tools(root)
    report.info = f"代码注册 {len(actual)} 个"
    specs = (
        ("README.md", r"^### MCP 工具（(\d+) 个）", "### CLI 命令"),
        ("README_EN.md", r"^### MCP tools \((\d+)\)", "### CLI —"),
    )
    for doc, head_re, end in specs:
        text = read_text(root, doc)
        match = re.search(head_re, text, re.MULTILINE)
        if not match:
            report.warnings.append(f"{doc}: 未找到 MCP 工具小节标题")
            continue
        documented = table_first_column(section(text, match.group(0), end), r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|")
        claimed = int(match.group(1))
        if claimed != len(actual):
            report.errors.append(f"{doc}: 标题声称 {claimed} 个，代码实际 {len(actual)} 个")
        for name in sorted(actual - documented):
            report.errors.append(f"{doc}: 代码有、文档未列 —— {name}")
        for name in sorted(documented - actual):
            report.errors.append(f"{doc}: 文档有、代码没有 —— {name}")

    contributing = read_text(root, "CONTRIBUTING.md")
    tree_match = re.search(r"mcp_tools/\s+#\s*(\d+)\s+MCP tools", contributing)
    if tree_match and int(tree_match.group(1)) != len(actual):
        report.warnings.append(
            f"CONTRIBUTING.md: 结构树写 {tree_match.group(1)} 个 MCP 工具，代码实际 {len(actual)} 个"
        )
    return report


# ---------------------------------------------------------------------------
# 2. CLI 子命令
# ---------------------------------------------------------------------------


def actual_cli_commands(root: Path) -> set[str]:
    source = (root / "scripts/palimpsest_cli.py").read_text(encoding="utf-8")
    return set(re.findall(r"add_parser\(\s*[\"']([^\"']+)[\"']", source))


def check_cli_commands(root: Path) -> Report:
    report = Report("CLI 子命令")
    actual = actual_cli_commands(root)
    report.info = f"代码注册 {len(actual)} 个"
    specs = (
        ("README.md", "### CLI 命令", "### 区块"),
        ("README_EN.md", "### CLI —", "### Blocks"),
    )
    for doc, head, end in specs:
        segment = section(read_text(root, doc), head, end)
        if not segment:
            report.warnings.append(f"{doc}: 未找到 CLI 小节（{head}）")
            continue
        documented = table_first_column(segment, r"^\|\s*`([a-z][a-z0-9]*(?:-[a-z0-9]+)*)")
        for name in sorted(actual - documented):
            report.errors.append(f"{doc}: 代码有、文档未列 —— {name}")
        for name in sorted(documented - actual):
            report.errors.append(f"{doc}: 文档有、代码没有 —— {name}")
    return report


# ---------------------------------------------------------------------------
# 3. REST 路由
# ---------------------------------------------------------------------------

ROUTE_RE = re.compile(r"@app\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']")
PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")


def normalize_route(path: str) -> str:
    """把路径占位符统一成 {}，使 /memory/{id}（文档）与 /memory/{node_id}（代码）可比。"""
    return PLACEHOLDER_RE.sub("{}", path)


def actual_routes(root: Path) -> set[tuple[str, str]]:
    source = (root / "main.py").read_text(encoding="utf-8")
    return {(method.upper(), normalize_route(path)) for method, path in ROUTE_RE.findall(source)}


def check_rest_routes(root: Path) -> Report:
    report = Report("REST 路由")
    actual = actual_routes(root)
    report.info = f"main.py 注册 {len(actual)} 条"
    specs = (
        ("README.md", "### REST API", "## 测试"),
        ("README_EN.md", "### REST API", "## Tests"),
    )
    for doc, head, end in specs:
        segment = section(read_text(root, doc), head, end)
        rows = re.findall(r"^\|\s*`(GET|POST|PUT|PATCH|DELETE)`\s*\|\s*`([^`]+)`", segment, re.MULTILINE)
        documented = {(method.upper(), normalize_route(path)) for method, path in rows}
        for method, path in sorted(actual - documented):
            report.errors.append(f"{doc}: 代码有、文档未列 —— {method} {path}")
        for method, path in sorted(documented - actual):
            report.errors.append(f"{doc}: 文档有、代码没有 —— {method} {path}")
    return report


# ---------------------------------------------------------------------------
# 4. 配置项（config.py ↔ .env.example ↔ README / README_EN）
# ---------------------------------------------------------------------------


def _iter_getenv_defaults(source: str) -> dict[str, str | None]:
    """解析 ``os.getenv("KEY"[, DEFAULT])`` → {KEY: 默认值表达式}（无默认值为 None）。

    逐字符做括号配平取实参，避免 ``str(50_000)`` 这类嵌套括号把正则截断。
    """
    found: dict[str, str | None] = {}
    for call in GETENV_CALL_RE.finditer(source):
        depth = 1
        idx = call.end()
        while idx < len(source) and depth:
            if source[idx] == "(":
                depth += 1
            elif source[idx] == ")":
                depth -= 1
            idx += 1
        args = source[call.end() : idx - 1]
        key_match = re.match(r"\s*[\"']([A-Z0-9_]+)[\"']", args)
        if not key_match:
            continue
        tail = args[key_match.end() :].lstrip()
        found.setdefault(key_match.group(1), tail[1:].strip() if tail.startswith(",") else None)
    return found


def _canon_code_default(expr: str | None) -> str:
    """源码默认值表达式 → 可比字符串（去引号、去数字分隔下划线、解开 ``str("50_000")``）。

    只对「非字符串字面量」去下划线：字符串默认值里的下划线是内容的一部分
    （如 ``"memory,user_intent,character_state"``），按数字分隔符剥掉会把
    ``user_intent`` 变成 ``userintent``，与文档永远对不上。
    """
    if expr is None:
        return ""
    expr = expr.strip().rstrip(",").strip()
    wrapper = CODE_STR_WRAPPER_RE.match(expr)
    if wrapper:
        # str("50_000") → 内层是字符串字面量，按字面量处理（不去下划线）
        return _canon_str_literal(wrapper.group(1))
    literal = CODE_STR_LITERAL_RE.match(expr)
    if literal:
        return _canon_str_literal(literal.group(1))
    # 形如 "a","b" 的元组/多值默认值：拼接各段字面量再规范化
    parts = re.findall(r"[\"']([^\"']*)[\"']", expr)
    if parts:
        return ",".join(_canon_str_literal(p) for p in parts)
    return expr.replace("_", "")


def _canon_str_literal(value: str) -> str:
    """字符串字面量内容 → 可比字符串（数字分隔下划线才剥，标识符内下划线保留）。"""
    # 仅当整体是「纯数字+下划线」时才剥下划线（50_000 → 50000）
    if re.fullmatch(r"[\d_]+", value):
        return value.replace("_", "")
    return value.strip()


def _canon_doc_default(cell: str) -> str:
    """文档默认值单元格 → 可比字符串（去掉 ``**强调**`` 与行内代码标记）。"""
    text = cell.strip()
    for mark in ("**", "*", "`"):
        if len(text) > len(mark) and text.startswith(mark) and text.endswith(mark):
            text = text[len(mark) : -len(mark)].strip()
    return text


def _compare_config_defaults(report: Report, doc: str, key: str, code_value: str, cell: str) -> None:
    """比对单个配置项的默认值：写法允许不同，值必须一致。"""
    doc_value = _canon_doc_default(cell)
    if not code_value:
        lowered = doc_value.lower()
        if any(marker in doc_value or marker in lowered for marker in CONFIG_EMPTY_MARKERS):
            return
        report.errors.append(f"{doc}: `{key}` 代码默认值为空串（未设置即关闭 / 未配置），文档却写「{doc_value}」")
        return
    if doc_value != code_value:
        report.errors.append(f"{doc}: `{key}` 默认值不一致 —— 代码 `{code_value}` / 文档 `{doc_value}`")


def check_config_keys(root: Path) -> Report:
    report = Report("配置项")
    defaults = _iter_getenv_defaults((root / "config.py").read_text(encoding="utf-8"))
    cfg = set(defaults)
    outside = set(CONFIG_READ_OUTSIDE_CONFIG_PY)
    comparable = sorted(cfg - set(CONFIG_DERIVED_DEFAULTS))
    env = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (root / ".env.example").read_text(encoding="utf-8"), re.MULTILINE))
    report.info = (
        f"config.py {len(cfg)} 个（默认值比对 {len(comparable)} · 派生值 {len(cfg) - len(comparable)}："
        f"{'、'.join(sorted(CONFIG_DERIVED_DEFAULTS))}）· .env.example {len(env)} 个"
    )

    for name in sorted(cfg - env):
        report.errors.append(f".env.example: 缺少 config.py 已支持的配置项 —— {name}")
    # .env.example 也可能攒下没人读的键：对外承诺了一个不生效的旋钮，同样是漂移
    for name in sorted(env - cfg - outside):
        report.warnings.append(
            f".env.example: 有、config.py 未读取 —— {name}"
            "（死键请删；确由 config.py 之外读取，请登记进 CONFIG_READ_OUTSIDE_CONFIG_PY）"
        )

    specs = (
        ("README.md", "## 配置", "## 更换向量模型"),
        ("README_EN.md", "## Configuration", "## Swapping models"),
    )
    for doc, head, end in specs:
        segment = section(read_text(root, doc), head, end)
        if not segment:
            report.warnings.append(f"{doc}: 未找到配置小节（{head}）")
            continue
        documented = table_first_column(segment, r"^\|\s*`([A-Z][A-Z0-9_]+)`")
        cells = dict(CONFIG_ROW_RE.findall(segment))
        for name in sorted(cfg - documented):
            report.errors.append(f"{doc}: config.py 支持、文档未写 —— {name}")
        for name in sorted(documented - cfg - outside):
            report.warnings.append(f"{doc}: 文档写了、config.py 里没有 —— {name}")
        for name in comparable:
            if name not in documented:
                continue
            cell = cells.get(name)
            if cell is None:
                report.warnings.append(f"{doc}: `{name}` 默认值单元格无法解析（表格列数异常？）")
                continue
            _compare_config_defaults(report, doc, name, _canon_code_default(defaults[name]), cell)
    return report


# ---------------------------------------------------------------------------
# 5. 文档内文件引用是否存在
# ---------------------------------------------------------------------------

LINK_RE = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s]+)\)")
INLINE_PATH_RE = re.compile(
    r"`([A-Za-z0-9_][A-Za-z0-9_./\-]*/[A-Za-z0-9_.\-]+\.(?:py|md|toml|yml|yaml|json|vbs|html|txt|cfg))`"
)
TREE_LINE_RE = re.compile(r"^([^├└]*)[├└]──\s+(.+?)\s*$")
ROOT_LINE_RE = re.compile(r"^[A-Za-z][\w.\-]*/\s*$")


def is_exempt(reference: str) -> bool:
    return reference in EXEMPT_REFERENCES or reference.startswith(EXEMPT_PREFIXES)


def tree_references(text: str) -> set[str]:
    """解析项目结构树代码块：按「│   」前缀深度维护目录栈，拼出文件相对路径。"""
    references: set[str] = set()
    stack: list[str] = []
    in_fence = False
    root_seen = False
    for line in text.splitlines():
        if line.lstrip().startswith(FENCE):
            in_fence = not in_fence
            stack = []
            root_seen = False
            continue
        if not in_fence:
            continue
        match = TREE_LINE_RE.match(line)
        if match:
            prefix, raw = match.group(1), match.group(2)
            name = raw.split("#", 1)[0].strip()
        elif ROOT_LINE_RE.match(line.strip()):
            if root_seen or stack:  # 树根只认一次
                continue
            root_seen = True
            continue
        else:
            continue
        if not name:
            continue
        depth = len(prefix) // 4
        if name.endswith("/"):
            stack = stack[:depth]
            stack.append(name.rstrip("/"))
            continue
        references.add("/".join([*stack[:depth], name]))
    return references


def check_file_references(root: Path) -> Report:
    report = Report("文档文件引用")
    checked = 0
    for doc in DOC_FILES:
        text = read_text(root, doc)
        references: set[str] = set()
        references |= set(LINK_RE.findall(text))
        references |= set(INLINE_PATH_RE.findall(text))
        references |= tree_references(text)
        for reference in sorted(references):
            clean = reference.strip().removeprefix("./").rstrip("/")
            if not clean or clean.startswith(("http", "#")) or is_exempt(clean):
                continue
            checked += 1
            if "*" in clean:  # 通配引用（如 scripts/ab_snapshot_*.py）：有匹配即算存在
                if not list(root.glob(clean)):
                    report.errors.append(f"{doc}: 通配引用无匹配文件 —— {reference}")
                continue
            if not (root / clean).exists():
                report.errors.append(f"{doc}: 引用了不存在的路径 —— {reference}")
    report.info = f"检查 {checked} 处引用"
    return report


# ---------------------------------------------------------------------------
# 6. 行内代码（反引号）配对
# ---------------------------------------------------------------------------


def check_inline_code(root: Path) -> Report:
    report = Report("行内代码配对")
    scanned = 0
    for doc in DOC_FILES:
        in_fence = False
        for lineno, line in enumerate(read_text(root, doc).splitlines(), 1):
            if line.lstrip().startswith(FENCE):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            scanned += 1
            if line.count("`") % 2 == 1:
                report.errors.append(f"{doc}:{lineno}: 反引号未配对 —— {line.strip()[:90]}")
    report.info = f"扫描 {scanned} 行正文"
    return report


# ---------------------------------------------------------------------------
# 7. 脱敏占位符残留
# ---------------------------------------------------------------------------

STAR_RUN_RE = re.compile(r"\*{3,}")


def check_placeholder_marks(root: Path) -> Report:
    """连续三个以上星号：要么是脱敏留下的占位符，要么是行内代码反引号未闭合。

    反引号奇偶检查只覆盖「奇数」那一类；脱敏把行内的 key 占位符写成连续星号时，
    反引号数恰好仍是偶数，渲染却是错的——这一项专门补这个形态。
    纯星号行是 markdown 水平分割线，跳过。
    """
    report = Report("脱敏占位符残留")
    hits = 0
    for doc in PLACEHOLDER_DOCS:
        for lineno, line in enumerate(read_text(root, doc).splitlines(), 1):
            if not STAR_RUN_RE.search(line):
                continue
            if STAR_RUN_RE.sub("", line).strip() == "":
                continue
            hits += 1
            report.errors.append(f"{doc}:{lineno}: 出现连续星号（脱敏遗留 / 行内代码未闭合）—— {line.strip()[:90]}")
    report.info = f"命中 {hits} 处"
    return report


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_checks(root: Path) -> list[Report]:
    return [
        check_mcp_tools(root),
        check_cli_commands(root),
        check_rest_routes(root),
        check_config_keys(root),
        check_file_references(root),
        check_inline_code(root),
        check_placeholder_marks(root),
    ]


def print_human(reports: list[Report], root: Path) -> None:
    errors = sum(len(report.errors) for report in reports)
    warnings = sum(len(report.warnings) for report in reports)
    print(f"Palimpsest 文档一致性检查 —— 仓库根：{root}")
    print("=" * 64)
    for report in reports:
        if report.errors or report.warnings:
            print(f"\n[ERR] {report.name}（{report.info}）")
            for item in report.errors:
                print(f"   x {item}")
            for item in report.warnings:
                print(f"   ! {item}")
        else:
            print(f"[OK ] {report.name}（{report.info}）")
    print("\n" + "=" * 64)
    print(f"错误 {errors} · 警告 {warnings}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Palimpsest 文档一致性检查（文档 vs 代码）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（与人类可读报告同一份数据）")
    parser.add_argument("--strict", action="store_true", help="警告也导致非零退出码")
    parser.add_argument("--root", type=Path, default=None, help="仓库根目录（默认按脚本位置推导）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    if not (root / "README.md").is_file() or not (root / "main.py").is_file():
        print(f"[ERR] 仓库根不可用：{root}", file=sys.stderr)
        return 2

    reports = run_checks(root)
    errors = sum(len(report.errors) for report in reports)
    warnings = sum(len(report.warnings) for report in reports)

    if args.json:
        payload = {
            "root": str(root),
            "checks": [report.as_dict() for report in reports],
            "error_count": errors,
            "warning_count": warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_human(reports, root)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
