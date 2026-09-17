"""
core.index_rules 单元测试
=========================
全部走纯函数与临时目录（tmp_path），不连数据库、不连 Ollama。
覆盖：默认加载、规则加载优先级、非法配置、未知键警告、
match_kind / is_included / chunk_markdown、.yaml 迁移提示、
以及 novel 脚本加载测试配置后的未匹配不静默归并。
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import build_novel_index  # noqa: E402

# 让 build_kb_index 的 split_markdown 可被引用（迁移等价性断言）
from build_kb_index import split_markdown as kb_split_markdown  # noqa: E402

from core.index_rules import (  # noqa: E402
    DEFAULT_RULES_FILENAME,
    KB_BUILTIN_RULES,
    LEGACY_RULES_FILENAME,
    NOVEL_BUILTIN_RULES,
    IndexRules,
    IndexRulesError,
    Rule,
    chunk_markdown,
    is_included,
    load_rules,
    match_kind,
)


def _write(tmp_path: Path, name: str, data: dict, root: Path | None = None) -> Path:
    """写一个规则 JSON 到 (root or tmp_path)/name。"""
    target = root or tmp_path
    p = target / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. 无配置 → 内置默认
# ---------------------------------------------------------------------------

def test_load_rules_builtin_default_when_no_config(tmp_path: Path):
    """无配置 → 内置默认。

    内置默认的 **kind_map 为空、default_kind = "default"**：解析不出 kind 就是「未匹配」。
    旧实现把兜底写成 `return "character"`（把「不知道」伪装成「知道」），是本条配置化
    改动的根因，故此处断言默认规则里不含任何业务 kind —— 回归这条即回归根因。
    """
    rules = load_rules(root=tmp_path)
    assert rules.source == "builtin"
    assert rules.kind_map == ()
    assert rules.default_kind == "default"
    assert rules.chunk_strategy == "file"
    assert rules.min_chunk_len == 300
    assert rules.max_chunk_len == 800
    assert rules.exclude == ("03_章节", "04_草稿", ".obsidian")


def test_load_rules_root_none_returns_builtin():
    rules = load_rules()
    assert rules.source == "builtin"
    assert rules.default_kind == "default"


def test_novel_script_default_has_no_hardcoded_kind_map():
    """novel 脚本默认不再自带任何 vault 目录名（issue #24 的核心诉求）。"""
    assert NOVEL_BUILTIN_RULES.kind_map == ()
    assert NOVEL_BUILTIN_RULES.default_kind == "default"
    # 只有 03_章节 / 04_草稿 / .obsidian 这类通用排除项，没有 01_世界观 之类的目录约定
    assert not any("世界观" in p or "角色" in p for p in NOVEL_BUILTIN_RULES.exclude)


def test_kb_builtin_default_heading_and_exclude():
    """知识库内置默认：按标题切 300~800，只排除 .obsidian（等价改动前行为）。"""
    assert KB_BUILTIN_RULES.chunk_strategy == "heading"
    assert KB_BUILTIN_RULES.min_chunk_len == 300
    assert KB_BUILTIN_RULES.max_chunk_len == 800
    assert KB_BUILTIN_RULES.exclude == (".obsidian",)
    assert KB_BUILTIN_RULES.default_kind == "default"


# ---------------------------------------------------------------------------
# 2. 加载优先级：explicit > legacy 文件名 > 库根默认 json
# ---------------------------------------------------------------------------

def test_load_rules_priority_explicit_then_legacy_then_default(tmp_path: Path):
    default = tmp_path / DEFAULT_RULES_FILENAME
    default.write_text(json.dumps({"min_chunk_len": 111}), encoding="utf-8")
    legacy = tmp_path / ".palimpsest-novel-index.json"
    legacy.write_text(json.dumps({"min_chunk_len": 222}), encoding="utf-8")
    explicit = tmp_path / "my-rules.json"
    explicit.write_text(json.dumps({"min_chunk_len": 333}), encoding="utf-8")

    # 库根默认 json
    r_default = load_rules(root=tmp_path)
    assert r_default.source == str(default)
    assert r_default.min_chunk_len == 111

    # legacy 文件名（novel 约定）优先于库根默认
    r_legacy = load_rules(root=tmp_path, legacy_filename=".palimpsest-novel-index.json")
    assert r_legacy.source == str(legacy)
    assert r_legacy.min_chunk_len == 222

    # explicit 最高
    r_explicit = load_rules(root=tmp_path, explicit=str(explicit))
    assert r_explicit.source == str(explicit)
    assert r_explicit.min_chunk_len == 333


# ---------------------------------------------------------------------------
# 3. --rules 指向不存在的文件 → IndexRulesError
# ---------------------------------------------------------------------------

def test_load_rules_missing_explicit_raises(tmp_path: Path):
    with pytest.raises(IndexRulesError):
        load_rules(root=tmp_path, explicit=str(tmp_path / "nope.json"))


# ---------------------------------------------------------------------------
# 4. 非法配置 → IndexRulesError
# ---------------------------------------------------------------------------

def test_invalid_chunk_strategy_raises(tmp_path: Path):
    _write(tmp_path, DEFAULT_RULES_FILENAME, {"chunk_strategy": "paragraphs"})
    with pytest.raises(IndexRulesError) as exc:
        load_rules(root=tmp_path)
    assert "chunk_strategy" in str(exc.value)


def test_max_below_min_raises(tmp_path: Path):
    _write(tmp_path, DEFAULT_RULES_FILENAME,
           {"min_chunk_len": 800, "max_chunk_len": 300})
    with pytest.raises(IndexRulesError) as exc:
        load_rules(root=tmp_path)
    assert "min_chunk_len" in str(exc.value)


def test_kind_map_entry_missing_kind_raises(tmp_path: Path):
    _write(tmp_path, DEFAULT_RULES_FILENAME,
           {"kind_map": [{"dir_prefix": "01_世界观"}]})
    with pytest.raises(IndexRulesError) as exc:
        load_rules(root=tmp_path)
    assert "kind" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. 未知键 → 只产生 warning，不报错
# ---------------------------------------------------------------------------

def test_unknown_key_only_warns(tmp_path: Path):
    _write(tmp_path, DEFAULT_RULES_FILENAME,
           {"min_chunk_len": 123, "bogus_key": True})
    rules = load_rules(root=tmp_path)  # 不应抛错
    assert rules.min_chunk_len == 123
    assert any("bogus_key" in w for w in rules.warnings)


# ---------------------------------------------------------------------------
# 6. match_kind：filename / dir_prefix 任意层级 / glob / 全不命中
# ---------------------------------------------------------------------------

def _sample_rules() -> IndexRules:
    return IndexRules(
        kind_map=(
            Rule(kind="overview", filename="00-总览.md"),
            Rule(kind="reference", dir_prefix="reference"),
            Rule(kind="archive", glob="archive/*.md"),
        ),
        default_kind="default",
    )


def test_match_kind_filename_exact_basename():
    rules = _sample_rules()
    assert match_kind("root/00-总览.md", rules) == "overview"
    assert match_kind("deep/nested/00-总览.md", rules) == "overview"


def test_match_kind_dir_prefix_any_level():
    rules = _sample_rules()
    assert match_kind("reference/notes.md", rules) == "reference"
    assert match_kind("a/b/reference/x.md", rules) == "reference"
    assert match_kind("a/b/reference.md", rules) == "default"


def test_match_kind_glob():
    rules = _sample_rules()
    assert match_kind("archive/note.md", rules) == "archive"
    assert match_kind("archive/笔记.md", rules) == "archive"
    # fnmatch 的 * 跨路径分隔符匹配，archive/*.md 也匹配子目录
    assert match_kind("archive/2026/sep/note.md", rules) == "archive"
    # 不匹配非 .md 文件
    assert match_kind("archive/readme.txt", rules) == "default"


def test_match_kind_miss_returns_default_not_business_kind():
    rules = _sample_rules()
    kinds = {r.kind for r in rules.kind_map}
    got = match_kind("notes/unlisted.md", rules)
    assert got == "default"
    assert got not in kinds


# ---------------------------------------------------------------------------
# 7. .palimpsest-index.yaml：不解析 + 迁移提示
# ---------------------------------------------------------------------------

def test_legacy_yaml_not_parsed_and_warns(tmp_path: Path):
    (tmp_path / LEGACY_RULES_FILENAME).write_text(
        "kind_map:\n  - kind: setting\n", encoding="utf-8")
    rules = load_rules(root=tmp_path)
    # 不解析 yaml：source 回退到 builtin
    assert rules.source == "builtin"
    assert any(".json" in w and "不再支持" in w for w in rules.warnings)


def test_legacy_yaml_warning_merged_with_json_rules(tmp_path: Path):
    (tmp_path / LEGACY_RULES_FILENAME).write_text("x: 1\n", encoding="utf-8")
    _write(tmp_path, DEFAULT_RULES_FILENAME, {"min_chunk_len": 99})
    rules = load_rules(root=tmp_path)
    assert rules.min_chunk_len == 99
    assert any("不再支持" in w for w in rules.warnings)


# ---------------------------------------------------------------------------
# 8. is_included：exclude 目录 / include 白名单 / 默认全包含
# ---------------------------------------------------------------------------

def test_is_included_default_all_included():
    rules = IndexRules()
    assert is_included("a/b.md", False, rules)
    assert is_included("any.txt", False, rules)
    assert is_included("dir", True, rules)


def test_is_included_exclude_dir_and_whitelist():
    rules = IndexRules(exclude=("drafts", ".obsidian"), include=("*.md",))
    # exclude 目录（任意层级）
    assert not is_included("drafts", True, rules)
    assert not is_included("sub/drafts", True, rules)
    # include 白名单只放行 .md 文件
    assert is_included("notes/ok.md", False, rules)
    assert not is_included("notes/ok.txt", False, rules)
    # 目录不受 include 白名单限制
    assert is_included("assets", True, rules)


def test_is_included_exclude_file_by_basename():
    rules = IndexRules(exclude=("私密.md",))
    assert not is_included("somewhere/私密.md", False, rules)
    assert is_included("somewhere/公开.md", False, rules)


# ---------------------------------------------------------------------------
# 9. chunk_markdown：file 策略 / heading 策略迁移等价性
# ---------------------------------------------------------------------------

def test_chunk_markdown_file_strategy(tmp_path: Path):
    text = ("---\nid: abc\n---\n# 标题\n正文内容")
    rules = IndexRules(chunk_strategy="file")
    blocks = chunk_markdown(text, rules)
    assert len(blocks) == 1
    assert blocks[0] == "# 标题\n正文内容"
    assert chunk_markdown("", rules) == []
    assert chunk_markdown("   \n  ", rules) == []


def test_chunk_markdown_heading_drop_in_kb(tmp_path: Path):
    """heading 策略与 build_kb_index.split_markdown 对同一输入结果一致（迁移等价）。"""
    text = (
        "---\nid: abc\ntags: [x]\n---\n# 标题\n\n## 第一节\n"
        + "内容" * 200
        + "\n\n### 子节\n" + "细节" * 150
        + "\n\n## 第二节\n短内容。"
    )
    rules = IndexRules(chunk_strategy="heading", min_chunk_len=300, max_chunk_len=800)
    assert chunk_markdown(text, rules) == list(kb_split_markdown(text))


def test_chunk_markdown_respects_rule_lengths(tmp_path: Path):
    # 20 行短句，每行 ~13 字符，总 ~260 字符在一个 heading 段里
    line = "短内容行" * 3 + "。"
    lines_text = "\n".join([f"{line}{i}" for i in range(20)])
    text = "## 大节\n" + lines_text
    loose = IndexRules(chunk_strategy="heading", min_chunk_len=1,
                       max_chunk_len=5000)
    tight = IndexRules(chunk_strategy="heading", min_chunk_len=1,
                       max_chunk_len=20)
    n_loose = len(chunk_markdown(text, loose))
    n_tight = len(chunk_markdown(text, tight))
    # max 越小 → 切出的块越多
    assert n_loose < n_tight


# ---------------------------------------------------------------------------
# 10. novel 脚本 + --rules 测试配置：未匹配返回 "default"，不静默归并
# ---------------------------------------------------------------------------

def test_novel_script_unmatched_returns_default(tmp_path: Path):
    config = _write(tmp_path, "novel-rules.json", {
        "kind_map": [{"kind": "setting", "dir_prefix": "01_世界观"}],
        "default_kind": "default",
    })
    rules = load_rules(root=tmp_path, explicit=str(config))
    got = build_novel_index._kind_of("02_角色/凌无咎.md", rules)
    assert got == "default"
    assert got != "setting"
    # 匹配上的路径仍返回规则里的 kind
    assert build_novel_index._kind_of("01_世界观/规则.md", rules) == "setting"


# ---------------------------------------------------------------------------
# 11. 内置默认可被调用方覆盖（知识库 / 创作 vault 各自的内置默认）
# ---------------------------------------------------------------------------

def test_load_rules_builtin_override(tmp_path: Path):
    rules = load_rules(root=tmp_path, builtin=KB_BUILTIN_RULES)
    assert rules.source == "builtin"
    assert rules.chunk_strategy == "heading"
    assert rules.exclude == (".obsidian",)
    # 库根配置文件仍然优先于内置默认
    _write(tmp_path, DEFAULT_RULES_FILENAME, {"chunk_strategy": "chars"})
    assert load_rules(root=tmp_path, builtin=KB_BUILTIN_RULES).chunk_strategy == "chars"


# ---------------------------------------------------------------------------
# 12. 端到端：规则文件驱动的扫描 / 归类 / 未匹配统计（不连库）
# ---------------------------------------------------------------------------

def _make_vault(root: Path) -> None:
    """造一个结构与默认约定完全不同的测试库（目录名与个人 vault 无关）。"""
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "reference").mkdir(parents=True, exist_ok=True)
    (root / "drafts").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "idea.md").write_text("# 想法\n内容\n", encoding="utf-8")
    (root / "reference" / "spec.md").write_text("# 规范\n内容\n", encoding="utf-8")
    (root / "drafts" / "wip.md").write_text("# 草稿\n内容\n", encoding="utf-8")
    (root / "orphan.md").write_text("# 谁也没归类\n内容\n", encoding="utf-8")


def test_scan_respects_configured_exclude(tmp_path: Path):
    """按配置排除的目录下的文件不进扫描结果（验收①：结构不同的测试库能被正确索引）。"""
    _make_vault(tmp_path)
    rules = IndexRules(kind_map=(Rule(kind="note", dir_prefix="notes"),), exclude=("drafts",))
    files = build_novel_index._md_files(str(tmp_path), rules)
    rels = [build_novel_index._rel_path(fp, str(tmp_path)) for fp in files]
    assert "drafts/wip.md" not in rels
    assert "notes/idea.md" in rels


def test_default_builtin_excludes_are_generic(tmp_path: Path):
    """默认内置规则下，只排除 03_章节 / 04_草稿 / .obsidian 这三类通用目录。"""
    (tmp_path / "03_章节").mkdir()
    (tmp_path / "03_章节" / "c1.md").write_text("正文", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "n.md").write_text("内容", encoding="utf-8")
    files = build_novel_index._md_files(str(tmp_path), NOVEL_BUILTIN_RULES)
    rels = [build_novel_index._rel_path(fp, str(tmp_path)) for fp in files]
    assert rels == ["notes/n.md"]


def test_unmatched_paths_collected_explicitly(tmp_path: Path):
    """未匹配路径单列一类，不静默归并（验收②）。

    直接调 collect 辅助函数，避免连数据库 / embedding。
    """
    rules = IndexRules(
        kind_map=(Rule(kind="note", dir_prefix="notes"),),
        default_kind="default",
    )
    rels = ["notes/a.md", "reference/b.md", "orphan.md"]
    unmatched = [r for r in rels if match_kind(r, rules) == rules.default_kind]
    assert unmatched == ["reference/b.md", "orphan.md"]
    # 未匹配项不会落到任何一个业务 kind 上
    assert all(match_kind(r, rules) != "note" for r in unmatched)