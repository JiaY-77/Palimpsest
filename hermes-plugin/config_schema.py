"""Declarative config surface for the Palimpsest memory provider.

Loaded by path (never package-imported) so the web dashboard can render
a config panel without pulling in the agent runtime.
"""

from plugins.memory.config_schema import (
    ProviderConfigSchema,
    ProviderField,
    STORAGE_FLAT_JSON,
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
            default="5",
            kind="number",
            description="每轮自动召回的 top_k（1-10）",
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
