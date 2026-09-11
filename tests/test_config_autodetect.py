"""
config 自动探测测试 —— 覆盖 EMBEDDING_PROVIDER 三种情形：
  1. 未设置 + 有效云端 key → "openai"
  2. 未设置 + 无 key / 占位符 → "ollama"
  3. 显式设置 → 以显式值为准
"""



class TestDetectEmbeddingProvider:
    """直接测试 _detect_embedding_provider 函数（monkeypatch env）。"""

    def test_no_key_falls_back_to_ollama(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_placeholder_key_falls_back_to_ollama(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "YOUR_API_KEY")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_empty_string_key_falls_back_to_ollama(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "   ")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_xxx_key_falls_back_to_ollama(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "xxx")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_valid_key_selects_openai(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-real-key-12345")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "openai"

    def test_explicit_provider_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-real-key-12345")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_explicit_openai_overrides_no_key(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "openai"

    def test_whitespace_key_is_placeholder(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "  changeme  ")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_skxxx_is_placeholder(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "ollama"

    def test_voyage_key_selects_openai(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "vp_abc123def456")
        from config import _detect_embedding_provider
        assert _detect_embedding_provider() == "openai"
