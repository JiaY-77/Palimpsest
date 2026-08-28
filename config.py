import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ---- 服务端口（2026-08-26：8000 让给酒馆 SillyTavern，小帕 REST 用 8090；dashboard 独立 8010）----
    REST_PORT = int(os.getenv("REST_PORT", "8090"))
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8010"))

    # ---- 通用 ----
    DB_PATH = os.getenv("DB_PATH", "data/mh_memory.db")

    # ---- 记忆时间衰减（记忆生命周期）----
    # 检索排序时旧记忆自然降权（不改存储）：
    #   有效分 = 余弦分 × importance × MEMORY_DECAY_FACTOR^(距创建天数/30)
    # 0.95 ≈ 每月衰减 5%（保守）；设为 1.0 完全关闭衰减；kb_chunk 知识块不衰减
    MEMORY_DECAY_FACTOR = float(os.getenv("MEMORY_DECAY_FACTOR", "0.95"))

    # ---- 检索权重（魔法数字配置化）----
    RULE_RETRIEVAL_WEIGHT = float(os.getenv("RULE_RETRIEVAL_WEIGHT", "1.3"))
    DOMAIN_BIAS_WEIGHT = float(os.getenv("DOMAIN_BIAS_WEIGHT", "1.15"))

    # ---- LLM 后端选择 ----
    # 可选: "ollama" 或 "deepseek"
    LLM_BACKEND = os.getenv("LLM_BACKEND", "deepseek")

    # ---- DeepSeek 配置 ----
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # ---- Ollama 配置（备用）----
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")

    # ---- Embedding 配置（2026-08-25 开源多选择：本地隐私优先，云端精度可选）----
    # EMBEDDING_PROVIDER: "ollama"（默认本地）| "openai"（OpenAI 兼容云端：Voyage/OpenAI/硅基流动等）
    # 注意：换 provider = 换向量空间，必须全量重建知识库索引（scripts/build_kb_index.py）
    EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama")
    # 本地 Ollama Embedding
    OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    OLLAMA_EMBEDDING_DIM = int(os.getenv("OLLAMA_EMBEDDING_DIM", "1024"))
    # 云端 OpenAI 兼容 Embedding API（默认 Voyage，可换任意 OpenAI 兼容端点）
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.voyageai.com/v1")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "voyage-3")
    EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

    # ---- 根据后端动态取得当前使用的配置 ----
    @classmethod
    def get_llm_config(cls):
        if cls.LLM_BACKEND == "deepseek":
            return {
                "base_url": cls.DEEPSEEK_BASE_URL,
                "api_key": cls.DEEPSEEK_API_KEY,
                "model": cls.DEEPSEEK_MODEL,
            }
        else:  # ollama
            return {
                "base_url": cls.OLLAMA_BASE_URL,
                "api_key": "ollama",  # Ollama 不需要真 key
                "model": cls.OLLAMA_MODEL,
            }
