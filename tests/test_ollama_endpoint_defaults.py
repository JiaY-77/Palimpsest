"""Ollama 端点默认值守卫。

默认端点必须写 IPv4 字面量 ``127.0.0.1`` 而不是 ``localhost``：部分系统把
``localhost`` 优先解析为 IPv6 回环 ``[::1]``，而 Ollama 默认只监听 IPv4，
于是每个请求都要先经历一次连接超时再回落——实测单次 embedding 从数十毫秒
退化为约 2 秒，检索与写入的吞吐随之下降两个数量级。

这里锁定「默认值」这一契约（源码字面量 + 运行时取值两道）。``.env.example``
与两份 README 里的同名默认值由 ``scripts/readme_check.py`` 在 CI 的 docs job
逐字比对，不在此重复。
"""

import os
import re
from pathlib import Path

import pytest

CONFIG_PY = Path(__file__).resolve().parent.parent / "config.py"

#: 键 → 期望的默认值（必须是 IPv4 字面量）
EXPECTED_DEFAULTS = {
    "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
    "OLLAMA_EMBEDDING_BASE_URL": "http://127.0.0.1:11434",
}

_LOCALHOST_HINT = (
    "localhost 在部分系统优先解析为 IPv6 回环 [::1]，而 Ollama 默认只监听 "
    "IPv4，会让每个请求先超时再回落（单次调用由数十毫秒退化到约 2 秒）"
)


@pytest.mark.parametrize("key,expected", sorted(EXPECTED_DEFAULTS.items()))
def test_source_default_is_ipv4_literal(key, expected):
    """config.py 登记的默认值字面量必须是 IPv4 字面量。"""
    source = CONFIG_PY.read_text(encoding="utf-8")
    match = re.search(rf'os\.getenv\(\s*"{key}"\s*,\s*"([^"]*)"\s*\)', source)
    assert match, f"config.py 中未找到 {key} 的 os.getenv 字面量默认值"
    default = match.group(1)
    assert "localhost" not in default, f"{key} 默认值 {default!r} 使用了 localhost：{_LOCALHOST_HINT}"
    assert default == expected, f"{key} 默认值应为 {expected!r}，实际 {default!r}"


@pytest.mark.parametrize("key", sorted(EXPECTED_DEFAULTS))
def test_runtime_value_is_ipv4_literal(key):
    """当前进程实际解析到的端点值同样不得使用 localhost。"""
    if os.getenv(key):
        pytest.skip(f"{key} 已被环境变量覆盖，默认值断言不适用")
    import config

    value = getattr(config.Config, key)
    assert "localhost" not in value, f"{key} 解析为 {value!r}：{_LOCALHOST_HINT}"
    assert value == EXPECTED_DEFAULTS[key]
