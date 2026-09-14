"""
rule 域退役护栏测试
==================
rule 规则型知识域（router_query / palimpsest_router / /mem/router /
RULE_RETRIEVAL_WEIGHT / domain=rule / 相关脚本）已整体退役。
这些断言守护「退役后零残留」：任何人把规则域相关符号加回来，测试立即红。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_router_query_not_exported():
    """工具包不再导出 router_query。"""
    import mcp_tools

    assert not hasattr(mcp_tools, "router_query")
    assert "router_query" not in mcp_tools.__all__


def test_rule_retrieval_weight_removed():
    """配置项 RULE_RETRIEVAL_WEIGHT 已删除（连同 ×1.3 内置加权）。"""
    from config import Config

    assert not hasattr(Config, "RULE_RETRIEVAL_WEIGHT")


def test_routing_module_deleted():
    """mcp_tools/routing.py 文件已删除。"""
    assert not (REPO_ROOT / "mcp_tools" / "routing.py").exists()


def test_rule_block_removed():
    """内置区块不再包含 rule；domain_in_block 不再有 rule→kb 兼容分支。"""
    from core.trivium_store import DEFAULT_BLOCKS, domain_in_block

    assert "rule" not in DEFAULT_BLOCKS
    assert domain_in_block("rule", "kb") is False


def test_sync_scripts_deleted():
    """规则同步与一致性检查脚本已删除。"""
    assert not (REPO_ROOT / "scripts" / "sync_rules.py").exists()
    assert not (REPO_ROOT / "scripts" / "check_kb_consistency.py").exists()


def test_rule_domain_not_in_env_example():
    """.env.example 不再暴露 RULE_RETRIEVAL_WEIGHT（对用户承诺一个死旋钮即漂移）。"""
    env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RULE_RETRIEVAL_WEIGHT" not in env


def test_main_has_no_router_endpoint():
    """REST 不再提供 /mem/router 端点。"""
    main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert '"/mem/router"' not in main_src
    assert "RouterQueryRequest" not in main_src