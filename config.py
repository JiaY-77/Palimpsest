import os
from dotenv import load_dotenv

load_dotenv()

# 项目根 = config.py 所在目录（直接运行时为仓库根，pip 安装后为 site-packages 下包目录）。
# DB_PATH 等相对路径一律以它为基准解析为绝对路径，不再依赖/修改进程当前工作目录
# （此前依赖 mcp_tools 里 os.chdir 切换到项目根来解析相对路径，作为包安装后会污染
# 调用方工作目录——现已改为纯绝对路径解析，彻底消除该副作用）。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve_db_path() -> str:
    """解析 DB_PATH：环境变量给绝对路径直接用；给相对路径（非盘符/非 / 开头）
    则基于项目根解析为绝对路径；未给时默认 <项目根>/data/mh_memory.db。"""
    env = os.getenv("DB_PATH", "")
    if env and os.path.isabs(env):
        return env
    if env:
        return os.path.join(PROJECT_ROOT, env)
    return os.path.join(PROJECT_ROOT, "data", "mh_memory.db")


class Config:
    # ---- 服务端口（2026-08-26：8000 让给酒馆 SillyTavern，Palimpsest REST 用 8090；dashboard 独立 8010）----
    REST_PORT = int(os.getenv("REST_PORT", "8090"))
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8010"))

    # ---- 通用 ----
    # DB_PATH 为绝对路径（相对路径/默认值均已基于项目根解析），任何 cwd 下取用都正确
    DB_PATH = _resolve_db_path()

    # ---- 记忆时间衰减（记忆生命周期）----
    # 检索排序时旧记忆自然降权（不改存储）：
    #   有效分 = 余弦分 × importance × MEMORY_DECAY_FACTOR^(距创建天数/30)
    # 0.95 ≈ 每月衰减 5%（保守）；设为 1.0 完全关闭衰减；kb_chunk 知识块不衰减
    MEMORY_DECAY_FACTOR = float(os.getenv("MEMORY_DECAY_FACTOR", "0.95"))

    # ---- 检索权重（魔法数字配置化）----
    RULE_RETRIEVAL_WEIGHT = float(os.getenv("RULE_RETRIEVAL_WEIGHT", "1.3"))
    DOMAIN_BIAS_WEIGHT = float(os.getenv("DOMAIN_BIAS_WEIGHT", "1.15"))

    # ---- 图谱扩散精馏（魔法数字配置化）----
    # 每节点最多扩散的最强边数（防高节点全量扩散撑爆计算/污染结果）
    EXPAND_MAX_EDGES_PER_NODE = int(os.getenv("EXPAND_MAX_EDGES_PER_NODE", "20"))
    # 扩散时弱边过滤阈值（默认 0.0 不启用）
    EXPAND_MIN_EDGE_WEIGHT = float(os.getenv("EXPAND_MIN_EDGE_WEIGHT", "0.0"))

    # ---- L1 MEMORY.md 嗅探（魔法数字配置化）----
    # 超过该大小（字节）的 MEMORY.md 不读入内存缓存
    L1_MAX_SIZE = int(os.getenv("L1_MAX_SIZE", str(5 * 1024)))

    # ---- mem_ingest 内容上限（魔法数字配置化）----
    # 单条记忆 content 最大字符数，超长拒绝写入（防超大 payload 拖垮库）
    MEM_INGEST_MAX_LENGTH = int(os.getenv("MEM_INGEST_MAX_LENGTH", str(50_000)))

    # ---- 混合检索 RRF（魔法数字配置化）----
    # Reciprocal Rank Fusion 标准 k 值：单侧命中也算贡献
    RRF_K = float(os.getenv("RRF_K", "60.0"))

    # ---- LLM 后端选择 ----
    # 可选: "ollama" 或 "deepseek"
    LLM_BACKEND = os.getenv("LLM_BACKEND", "deepseek")

    # ---- 可选 API Key 鉴权 ----
    # 默认空 = 不启用鉴权（localhost 本机直连，保持现状）。
    # 设置后，除 / 健康检查外所有请求须带 Authorization: Bearer <key> 或
    # X-API-Key: <key>，否则 401。适用于局域网/受信网络部署；
    # 公网部署必须配 HTTPS 反向代理（API Key 仅做校验，不做加密传输）。
    API_KEY = os.getenv("PALIMPSEST_API_KEY", "")

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
    # Ollama 原生 embedding API 根地址（/api/embeddings），与 LLM 用的
    # OLLAMA_BASE_URL（OpenAI 兼容 /v1 路径）解耦。
    OLLAMA_EMBEDDING_BASE_URL = os.getenv("OLLAMA_EMBEDDING_BASE_URL", "http://localhost:11434")
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
