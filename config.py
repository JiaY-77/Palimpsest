import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ---- 通用 ----
    DB_PATH = os.getenv("DB_PATH", "data/mh_memory.db")

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

    # ---- Ollama Embedding 配置 ----
    OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    OLLAMA_EMBEDDING_DIM = int(os.getenv("OLLAMA_EMBEDDING_DIM", "1024"))

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
