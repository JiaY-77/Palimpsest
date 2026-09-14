"""Tests for hermes-plugin/config_schema.py loading path and schema contract.

The plugin's config_schema.py is loaded *by path* by Hermes — it is never
package-imported.  The only permitted import is the pure-data module
``plugins.memory.config_schema``.  This file stubs that module so the
plugin config_schema can be loaded and validated without installing Hermes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub ``plugins.memory.config_schema`` (pure-data module per Hermes design).
#
# The stub is hand-written to match the signatures in Hermes'
# ``plugins/memory/config_schema.py``.  If Hermes changes those interfaces
# this stub must be kept in sync.
# ---------------------------------------------------------------------------

# 1. ``plugins`` is a package — needs __path__
_plugins = types.ModuleType("plugins")
_plugins.__path__ = []  # type: ignore[attr-defined]

# 2. ``plugins.memory`` is a sub-package
_plugins_memory = types.ModuleType("plugins.memory")
_plugins_memory.__path__ = []  # type: ignore[attr-defined]

# 3. ``plugins.memory.config_schema`` — the pure-data module
_cfg_schema_mod = types.ModuleType("plugins.memory.config_schema")

STORAGE_FLAT_JSON = "flat_json"

@dataclasses.dataclass(frozen=True)
class ProviderField:
    key: str
    label: str
    kind: str = "text"
    default: str = ""
    description: str = ""
    placeholder: str = ""
    options: tuple = ()
    env_key: str | None = None
    aliases: tuple = ()
    env_fallbacks: tuple = ()
    inline: bool = False
    group: str = ""
    info: str = ""
    scope: str = "host"


@dataclasses.dataclass(frozen=True)
class ProviderConfigSchema:
    name: str
    label: str
    storage: str = STORAGE_FLAT_JSON
    docs_url: str = ""
    fields: tuple = ()


_cfg_schema_mod.STORAGE_FLAT_JSON = STORAGE_FLAT_JSON  # type: ignore[attr-defined]
_cfg_schema_mod.ProviderField = ProviderField  # type: ignore[attr-defined]
_cfg_schema_mod.ProviderConfigSchema = ProviderConfigSchema  # type: ignore[attr-defined]

sys.modules.setdefault("plugins", _plugins)
sys.modules.setdefault("plugins.memory", _plugins_memory)
sys.modules.setdefault("plugins.memory.config_schema", _cfg_schema_mod)

# ---------------------------------------------------------------------------
# Load hermes-plugin/config_schema.py by file path
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
_CONFIG_SCHEMA_PATH = _REPO / "hermes-plugin" / "config_schema.py"

_spec = importlib.util.spec_from_file_location(
    "hermes_config_schema", _CONFIG_SCHEMA_PATH
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

CONFIG_SCHEMA = _mod.CONFIG_SCHEMA

# Read plugin __init__.py source so we can cross-reference field keys.
_INIT_SRC = (_REPO / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Valid kinds allowed by Hermes for ProviderField
# ---------------------------------------------------------------------------

_ALLOWED_KINDS = {"text", "select", "secret", "bool", "number", "json"}


class TestConfigSchemaContract:
    """Validate the schema object satisfies Hermes interface requirements."""

    def test_schema_loads(self):
        assert CONFIG_SCHEMA is not None

    def test_schema_name(self):
        assert CONFIG_SCHEMA.name == "palimpsest"

    def test_schema_storage(self):
        assert CONFIG_SCHEMA.storage == STORAGE_FLAT_JSON

    def test_fields_non_empty(self):
        assert len(CONFIG_SCHEMA.fields) > 0

    def test_all_field_keys_and_labels_are_non_empty_strings(self):
        for f in CONFIG_SCHEMA.fields:
            assert isinstance(f.key, str) and f.key, f"key must be non-empty: {f!r}"
            assert isinstance(f.label, str) and f.label, f"label must be non-empty: {f!r}"

    def test_field_keys_are_unique(self):
        keys = [f.key for f in CONFIG_SCHEMA.fields]
        assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"

    def test_field_kind_is_valid(self):
        for f in CONFIG_SCHEMA.fields:
            assert f.kind in _ALLOWED_KINDS, f"field {f.key!r} has invalid kind {f.kind!r}"

    def test_field_default_is_str(self):
        for f in CONFIG_SCHEMA.fields:
            assert isinstance(f.default, str), (
                f"field {f.key!r} default must be str, got {type(f.default)}"
            )

    def test_at_least_one_inline_field(self):
        assert any(f.inline for f in CONFIG_SCHEMA.fields), "no inline=True field found"

    def test_docs_url_points_to_palimpsest_repo(self):
        assert CONFIG_SCHEMA.docs_url.startswith(
            "https://github.com/"
        ), f"docs_url unexpected: {CONFIG_SCHEMA.docs_url}"
        assert "Palimpsest" in CONFIG_SCHEMA.docs_url

    def test_every_field_key_appears_in_init_source(self):
        """Schema-declared keys must match what __init__.py actually reads.

        Keys are checked case-insensitively because __init__.py references
        them via UPPER_SNAKE env-var names (e.g. ``PALIMPSEST_PREFETCH_TOP_K``).
        """
        src_lower = _INIT_SRC.lower()
        for f in CONFIG_SCHEMA.fields:
            assert f.key.lower() in src_lower, (
                f"field key {f.key!r} not found in hermes-plugin/__init__.py"
            )
