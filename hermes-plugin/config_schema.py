"""Declarative config surface for the Palimpsest memory provider.

Loaded by path (never package-imported) so the web dashboard can render
a config panel without pulling in the agent runtime.

Per Hermes design, each provider's config_schema.py is loaded by path — it is
never package-imported. The only permitted import is the pure-data module
``plugins.memory.config_schema`` (STORAGE_FLAT_JSON, ProviderConfigSchema,
ProviderField). This module therefore must be importable at Hermes runtime;
failing to import it outside Hermes is expected. That constraint is exercised
and guarded by ``tests/test_plugin_config_schema.py``.
"""

from plugins.memory.config_schema import (
    STORAGE_FLAT_JSON,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="palimpsest",
    label="Palimpsest Memory Provider",
    storage=STORAGE_FLAT_JSON,
    docs_url="https://github.com/JiaY-77/Palimpsest/blob/main/docs/HERMES_INTEGRATION.md",
    fields=(
        ProviderField(
            key="base_url",
            label="REST 地址",
            default="http://127.0.0.1:8090",
            description="Palimpsest REST 服务地址",
            inline=True,
        ),
        ProviderField(
            key="domain",
            label="记忆域",
            default="hermes",
            description="Palimpsest 记忆域",
            inline=True,
        ),
        ProviderField(
            key="prefetch_top_k",
            label="召回条数",
            default="3",
            kind="number",
            description="每轮自动召回的 top_k（1-10）",
        ),
        ProviderField(
            key="prefetch_neighbors",
            label="附带图邻居",
            default="false",
            kind="bool",
            description="召回时附带一跳图邻居（记忆域图边稀疏，默认关闭降噪）",
        ),
        ProviderField(
            key="prefetch_min_score",
            label="召回最低分",
            default="0.3",
            kind="number",
            description="低于该相关度的召回结果直接丢弃（默认 0.3，调高更省上下文）",
        ),
        ProviderField(
            key="prefetch_tier",
            label="召回分层",
            default="facts",
            description="只注入该分层的记忆：facts（默认）/ logs / 空串 = 不过滤",
        ),
        ProviderField(
            key="auto_ingest",
            label="自动沉淀",
            default="true",
            kind="bool",
            description="命中强信号（纠正/偏好/决策/规则）时自动写入 Palimpsest",
        ),
    ),
)
