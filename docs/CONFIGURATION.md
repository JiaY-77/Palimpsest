# 配置参考

所有配置均从环境变量读取（`.env` 文件由 `python-dotenv` 自动加载），完整带注释模板见
[`.env.example`](../.env.example)。README 只列用户必配的最小集，本页是完整配置表（85 项，含
`config.py` 之外的 `KNOWLEDGE_DIR`）与「更换向量模型 / 重嵌全库」的操作流程。

## 配置

| 变量 | 默认值 | 说明 | 生效前提（Precondition） |
|---|---|---|---|
| `REST_PORT` | `8090` | FastAPI REST 服务端口 | 启动 REST 服务时 |
| `DASHBOARD_PORT` | `8010` | 监控面板服务端口 | 启动 dashboard 时 |
| `DB_PATH` | `data/mh_memory.db` | 嵌入式 TriviumDB 数据库路径 | — |
| `PALIMPSEST_API_KEY` | *（空 = 关闭）* | 可选 REST 鉴权；设置后除 `/` 外所有请求须带 Bearer / X-API-Key | 启用 REST 鉴权时 |
| `LLM_BACKEND` | `deepseek` | LLM 后端：`deepseek` 或 `ollama` | 需要 LLM 调用时 |
| `DEEPSEEK_API_KEY` | *（空）* | DeepSeek API 密钥 | `LLM_BACKEND=deepseek` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 基础地址 | `LLM_BACKEND=deepseek` |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek 模型标识 | `LLM_BACKEND=deepseek` |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Ollama OpenAI 兼容基础地址 | `LLM_BACKEND=ollama` |
| `OLLAMA_MODEL` | `deepseek-r1:7b` | 作为 LLM 的 Ollama 对话模型 | `LLM_BACKEND=ollama` |
| `EMBEDDING_PROVIDER` | *（空 = 自动探测）* | 向量后端：留空自动探测（有云端 key → `openai`，否则 → **`local`**）；显式写 `local`（内置 llama.cpp，零外部依赖）、`ollama`（本地服务、私有）或 `openai`（OpenAI 兼容云端，如 Voyage/硅基流动） | — |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | 本地 Ollama 向量模型 | `EMBEDDING_PROVIDER=ollama` |
| `OLLAMA_EMBEDDING_BASE_URL` | `http://127.0.0.1:11434` | Ollama 原生 embedding API 根地址（与 LLM 的 /v1 解耦） | `EMBEDDING_PROVIDER=ollama` |
| `OLLAMA_EMBEDDING_DIM` | `1024` | 向量维度（本地后端） | `EMBEDDING_PROVIDER=ollama` |
| `EMBEDDING_API_KEY` | *（空）* | 云端向量端点的 API 密钥 | `EMBEDDING_PROVIDER=openai` |
| `EMBEDDING_BASE_URL` | `https://api.voyageai.com/v1` | 云端向量基础地址（任意 OpenAI 兼容端点） | `EMBEDDING_PROVIDER=openai` |
| `EMBEDDING_MODEL` | `voyage-3` | 云端向量模型 | `EMBEDDING_PROVIDER=openai` |
| `EMBEDDING_DIM` | `1024` | 向量维度（云端后端） | `EMBEDDING_PROVIDER=openai` |
| `EMBEDDING_MODEL_PATH` | *（空）* | 直接指向本地 GGUF 的绝对路径，设置后**跳过下载**。也可指向 Ollama 已有的 blob（`qwen3-embedding:0.6b` 与这里要的 GGUF 是同一文件，sha256 一致） | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_MODEL_REPO` | `Qwen/Qwen3-Embedding-0.6B-GGUF` | 自动下载时使用的模型仓库 | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_MODEL_FILE` | `Qwen3-Embedding-0.6B-Q8_0.gguf` | 下载 / 缓存的文件名 | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_MODEL_SHA256` | `06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439` | 下载后的完整性校验值（不匹配则删除半成品并报错） | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_MODEL_SIZE` | `639150592` | 期望字节数（进度显示与大小校验） | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_MODELS_DIR` | *（空）* | 模型缓存目录；留空则用 `<项目根>/models` | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_DOWNLOAD_SOURCE` | `auto` | 下载源：`auto`（依次尝试 ModelScope → HuggingFace）/ `modelscope` / `huggingface` | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_AUTO_DOWNLOAD` | `1` | 缺模型时是否自动下载；设 `0` 则要求显式提供 `EMBEDDING_MODEL_PATH` | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_LOCAL_DIM` | `1024` | 本地模型的向量维度 | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_LOCAL_N_CTX` | `2048` | llama.cpp 上下文长度（调大可处理更长文本，吃内存也更多） | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_LOCAL_N_BATCH` | `512` | llama.cpp 批大小，**同时是分块池化的块长上限**（`EMBEDDING_LOCAL_CHUNK_TOKENS` 必须 ≤ 它）。**不建议调小**：调小会让块长跟着变小、已有向量全部失效，必须整库重嵌（2026-10-30 实测：2900 节点按 128 token 重切，一小时仍未跑完），而 639MB 模型是硬地板、省下的只是计算批缓冲；2GB 机器请改用进程外 embedding（llama-server / Ollama / 云端 key） | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_LOCAL_CHUNK_TOKENS` | `512` | 分块池化块长（issue #232）：超过该 token 数的 content 按 token 边界切块，逐块嵌入（块内 LAST pooling）后按块内 token 数加权平均再归一——长节点向量覆盖全文而非只有前缀（实测 cos vs 全文 0.92，旧截断仅 0.405）。**必须 ≤ `EMBEDDING_LOCAL_N_BATCH`**，否则块本身又被 `embed()` 的 `tokens[:n_batch]` 静默截断，分块白做（自检会报 error 拦下）。默认 512 = 与 n_batch 相同 | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_LOCAL_N_THREADS` | `0` | CPU 线程数；`0` = 交引擎自行决定 | `EMBEDDING_PROVIDER=local` |
| `EMBEDDING_LOCAL_N_GPU_LAYERS` | `0` | 卸载多少层到 GPU（`0` = 全部留在 CPU；`99` 通常表示全卸载）。只有装了支持 GPU 的 llama-cpp-python wheel 才生效——CPU-only wheel（`llama_supports_gpu_offload() == False`）下设 `>0` 会报错，不静默降级为纯 CPU。启动自检同样校验（error 级） | `EMBEDDING_PROVIDER=local` |
| `MEMORY_DECAY_FACTOR` | `0.95` | 月度记忆衰减（排序用，`score × importance × factor^(天/30)`）；`1.0` 关闭衰减；`kb_chunk` 节点永不衰减 | soft 模式：仅进入 ε 微调项 `recency_norm`（ε 默认 0.02 → 排序影响 ≤0.02，一年内约 0.01 量级，近乎半死参数）；hard 模式：乘性硬加权 |
| `MEMORY_RERANK_MODE` | `soft` | 重排模式：`soft` = 语义分为主线 + ε 级元数据微调（默认）；`hard` = 旧版乘性硬加权（可回退） | — |
| `SOFT_RERANK_EPS` | `0.02` | `soft` 模式的 ε：落在余弦分差区间的 15%–40%，只做 tie-break | `MEMORY_RERANK_MODE=soft` |
| `MEMORY_CONFIDENCE_EPS` | `0` | 记忆置信度的 ε 级修正强度。置信度由可观测证据按 Subjective Logic 公式算出（见 [CONFIDENCE.md](CONFIDENCE.md)），**只做 tie-break、不做过滤**——实测按置信度过滤会掉 4.81pp 召回。实测最优 `0.05` | `> 0`（默认关闭） |
| `MEMORY_CONFIDENCE_CENTER` | `0.27` | 把 belief `b` 映射到 `[-1,1]` 的中心点：`b == center` → 0 | `MEMORY_CONFIDENCE_EPS > 0` |
| `MEMORY_CONFIDENCE_SPAN` | `0.15` | 映射跨度：`b ≥ center + span` 截断到 `+1` | `MEMORY_CONFIDENCE_EPS > 0` |
| `DOMAIN_BOOST_EPS` | `0.10` | 域软加权加分（加性，作用在语义分上）：`domain_boost` 非空时对同域候选加此值 | `domain_boost` 参数非空 |
| `KB_SOFT_RERANK_MULT` | `1.5` | `kb_chunk`（知识块不老化）在 `soft` 模式下的 ε 加成倍率 | `MEMORY_RERANK_MODE=soft` |
| `DOMAIN_BIAS_WEIGHT` | `1.15` | 域偏置检索的额外权重 | `domain_bias` 参数非空 |
| `MEM_SEARCH_FRESHNESS_WEIGHT` | `0` | `mem_search` 新鲜度加权强度：`score += weight × decay(节点年龄)`，在过滤后、排序前应用（默认 0 = 关闭，行为与未引入前逐位一致）。**数据优先**：非零取值必须以 `eval/freshness_sweep.py` 在真实查询集上的输出为依据，不许拍脑袋定常数 | `> 0`（默认关闭） |
| `MEM_SEARCH_FRESHNESS_SCALE_DAYS` | `30` | 衰减时间尺度（天）：`exp` 形式为半衰期，`linear` 形式为归零天数 | `MEM_SEARCH_FRESHNESS_WEIGHT > 0` |
| `MEM_SEARCH_FRESHNESS_DECAY` | `exp` | 衰减形式：`exp`（半衰期）或 `linear`（线性归零）；其他值按 `exp` 处理 | `MEM_SEARCH_FRESHNESS_WEIGHT > 0` |
| `PIN_DOMAIN` | *（空 = 关闭）* | **常驻域**：该 domain 的 `active` 节点**每轮无条件**注入进 `/lifecycle/pre-turn`，不参与语义相似度竞争——用于「必须每轮生效」的规则（与当前消息零关键词重叠也照样到达）。空串 = 完全关闭，行为与未引入前一致 | — |
| `PIN_BUDGET_CHARS` | `3000` | 常驻块的字符预算：节点按 `importance` 倒序装入，**按节点边界截断**（绝不切半个节点）；超预算在响应的 `pin.truncated` / `pin.dropped` 里如实报告，不静默少注入 | `PIN_DOMAIN` 非空 |
| `EXPAND_MAX_EDGES_PER_NODE` | `20` | 图谱扩散时每节点最多扩散的最强边数 | 图扩散启用（`RETRIEVAL_EXPAND_DEPTH≥1` 或检索附带邻居） |
| `EXPAND_MIN_EDGE_WEIGHT` | `0.0` | 图谱扩散弱边过滤阈值（0 关闭） | 图扩散启用（`RETRIEVAL_EXPAND_DEPTH≥1` 或检索附带邻居） |
| `RRF_K` | `60.0` | 混合检索 RRF 常数 k（单侧命中也计贡献） | `mem_hybrid_search` 且 `mode=rrf` |
| `RRF_SEM_WEIGHT` | `1.0` | 混合检索 RRF 语义侧权重 | `mem_hybrid_search` 且 `mode=rrf` |
| `RRF_FTS_WEIGHT` | `0.1` | 混合检索 RRF 精确（FTS）侧权重——语义主序干净后 FTS 小幅加成 | `mem_hybrid_search` 且 `mode=rrf` |
| `RRF_FTS_EXACT_WEIGHT` | `1.5` | 精确标识符查询（query 含 commit hash / `#PR号` / `工单号`）时的 FTS 侧权重。下限由 `w/(k+q) > 1/(k+top_k)` 决定（约 `0.94`）；但 `1.0` 恰好等于语义权重会造成两者第 0 名**同分**（都是 `1/60`），胜负交给实现顺序，所以取 `1.5` 留余量——离线实测（22 条标识符 query）`1.10` 即已饱和（`recall@5` 0.356 → 0.690、`MRR@10` 0.636 → 1.000），`1.1`~`3.0` 指标完全相同。普通自然语言 query 仍走 `RRF_FTS_WEIGHT`，全局行为不变 | `mem_hybrid_search` 且 `mode=rrf` 且判定为标识符查询 |
| `TDB_TEXT_ENABLED` | `0` | **关闭（默认）**：TDB（TriviumDB）BM25 文本通道，第二路中文文本召回。`0` = 完全零成本（检索不发任何 TDB 文本调用、也不预热，线上行为与未引入该通道前逐位一致）；`1` = 混合检索（`mode=rrf`）多取一路 `TEXT BM25` 计入融合，通道状态见 `channels["tdb_text"]`（`ok`/`empty`/`degraded`，启动自检会额外预热）。背景：FTS5 trigram 对 2 字短词与 6~8 字中文长短语的召回弱于 TDB 的 CJK 2-gram（2026-10-24 生产库副本实测） | `mem_hybrid_search` 且 `mode=rrf` 且开关为 1 |
| `TDB_TEXT_WEIGHT` | `1.0` | TDB 文本通道在 RRF 融合中的权重（同为 `mode=rrf`） | `TDB_TEXT_ENABLED=1` 时 |
| `TDB_TEXT_LIMIT` | `0` | TDB 文本通道候选量；`0` = 沿用 FTS 的 `fts_limit`，不另配 | `TDB_TEXT_ENABLED=1` 时 |
| `TDB_TEXT_FTS_FLOOR` | `3` | 条件触发门槛：本次 FTS 命中数 **≥** 此值时**跳过** TDB 路（FTS 已够好，省掉整路的开库 + 载侧车开销）；**<** 此值才走 TDB，只在中文短词/长短语这类 FTS 弱场景付费。`0` = 永不跳过（等价于改动前「永远走 TDB」）。跳过时 `channels["tdb_text"]` 为 `skipped`（与 `degraded` 区分） | `TDB_TEXT_ENABLED=1` 时 |
| `TIME_INDEX_ENABLED` | `0` | **关闭（默认）**：时间索引（到期扫描），写侧日期打标 + 读侧 RRF 第五路。背景：聊天里「后天 / 下周三 / 这个月27号」这类相对日期，语义与 FTS 都抓不住（提问侧没有任何字面/语义线索）。`1` = `mem_ingest` 把日期表达解析成绝对日期存进 `payload["dates"]`（`due`=未来声明 / `mention`=当天与回顾），混合检索（`mode=rrf`）按「今天」（`today` 参数）扫出到期节点并入 RRF 融合，通道状态见 `channels["time"]`（`ok`/`empty`，级联模式为 `skipped`）。`0` = 完全零成本（写读两侧都不导入模块，payload 不出现 `dates` 键，检索输出与引入前逐位一致）。已知边界（农历 / 节日 / 节前节后不解析；级联模式 `skipped`）见 docs/USAGE.md「时间索引」一节 | `mem_ingest` 与 `mem_hybrid_search` 且 `mode=rrf` 且开关为 1 |
| `TIME_INDEX_WEIGHT` | `1.0` | 时间索引通道在 RRF 融合中的权重（同为 `mode=rrf`） | `TIME_INDEX_ENABLED=1` 时 |
| `TIME_INDEX_LIMIT` | `20` | 时间索引通道候选量上限（到期节点按 `created_at` 升序取前 N） | `TIME_INDEX_ENABLED=1` 时 |
| `TIME_INDEX_WINDOW_DAYS` | `0` | 到期日窗口（天）：`0` = 只取「今天到期」；`N` = `[today-N, today+N]` 都算到期（跨时区 / 提前一天提醒场景用） | `TIME_INDEX_ENABLED=1` 时 |
| `CHUNK_INDEX_ENABLED` | `1` | **开启（默认）**：chunk 级向量索引写入接线（`core/chunk_index.py`，独立 `node_chunks.db`）。`mem_ingest` / `POST /memory/{id}/reembed` / `PUT·PATCH /memory/{id}` 改 content 三处写入在提交后调用统一入口 `index_node_chunks`：`split_chunks` 出 >1 段才写（逐段 embed）、单段内容清掉已有 chunk 行；失败仅 warning 不阻塞主写入。`0` = 三处全部跳过（不写不删，`chunk_stats` 不变；检索侧读不到即可）。存在量长文档回填见 `scripts/backfill_chunks.py`（生产库须停服窗口） | 三处写入路径 |
| `CHUNK_SCORE_WEIGHT` | `1.0` | chunk 召回参与 `mem_search` 排序的权重（issue #295）：主向量召回之后，另取一路 `node_chunks.db` 宽召回（深度 `max(top_k×5, 50)`）按 node 聚合取最大余弦，`node_score = max(主向量分, chunk cos × 本权重)`；chunk-only 命中的节点补进候选，与主候选走同一套 outdated / tier / scope / domain / block 过滤与排序。`0` = 检索侧整条 chunk 路关闭（零开销不读表），结果与未引入前**逐位一致**（有一致性验证钉住）。表缺失 / 损坏 / 模型不匹配 → 静默降级为主向量独走，不抛异常 | `mem_search` / `mem_hybrid_search` 语义路，且开关为非 0 |
| `CHUNK_DB_PATH` | *（空 = 派生）* | chunk 库文件路径；未设置时落在 `DB_PATH` 同目录的 `node_chunks.db`。由 `core/chunk_index.py` 在运行时解析（不读 config.py，见 readme_check 登记） | 检索 / 写入 chunk 表时 |
| `RERANK_ENABLED` | `0` | **语义重排（opt-in，默认关闭；#272 / #274）**：设 `1` 后把混合检索（`mem_hybrid_search` 两条 mode）的融合候选（深度 `RERANK_POOL`）交给后端（`RERANK_BACKEND`）按「对回答问题的帮助程度」重排。离线实测（60 题判卷集、oracle 池条件）：`llm` 后端 hit@5 **0.867 → 1.000**（7~15 秒/查询，需 `DEEPSEEK_API_KEY`）；`local_ce` 后端 hit@5 **0.867 → 0.967**（约 4.8 秒/查询，零额度零出网）。两者 52 条已命中全零回退。三条红线（两后端一致）：只排序不筛选（返回列表长度与传入一致，不截短不置空）；任何异常 fail-open 回原顺序（`channels.rerank = "degraded"`）；关闭时检索路径连 `core.reranker` 模块都不导入（行为与引入前逐位一致）；详细数字见 [RETRIEVAL_QUALITY.md](RETRIEVAL_QUALITY.md)。不适用于 pre-turn 自动注入这类延迟敏感路径 | `RERANK_ENABLED=1` |
| `RERANK_BACKEND` | `local_ce` | 重排后端：`local_ce` = 本地 cross-encoder（llama-cpp-python 加载 `RERANK_LOCAL_MODEL_PATH` 指向的 GGUF，逐条打 query-doc 对、零 API 额度；#274）；`llm` = 远端 chat-completions listwise（#272 原路径，行为不变）。非法值（含空串）视同 `local_ce`，只 warn 一次，不抛异常 | `RERANK_ENABLED=1` |
| `RERANK_LOCAL_MODEL_PATH` | `models/bge-reranker-v2-m3-Q8_0.gguf` | `local_ce` 后端的模型文件：绝对路径，或相对**项目根**解析（模型文件不进仓库，指向用户本地放置的 GGUF — 同向量模型的 `models/` 约定）。文件缺失 / 依赖缺失 / 加载失败 → `channels.rerank = "degraded"` 回原顺序，且日志给出下载指引；加载失败原因会被记住，不每条查询重复试。下载与安装见下文「本地重排模型」 | `RERANK_BACKEND=local_ce` 时 |
| `RERANK_LOCAL_N_THREADS` | `0` | `local_ce` 推理线程数；`0` = 交 llama.cpp 自动决定 | `RERANK_BACKEND=local_ce` 时 |
| `RERANK_LOCAL_N_BATCH` | `4096` | `local_ce` 的 llama.cpp 物理批大小（`n_batch`）。注意与 `EMBEDDING_LOCAL_N_BATCH` 无关——那是 store 侧向量模型的旋钮 | `RERANK_BACKEND=local_ce` 时 |
| `RERANK_LOCAL_N_UBATCH` | `512` | `local_ce` 的 llama.cpp 物理 ubatch（一次送进计算的 token 上限）；`0` = 不显式传（取 llama.cpp 默认 `n_ubatch = n_batch`，即 #307 修复前的历史行为）。**修复后默认 512**：与 `EMBEDDING_PROVIDER=local` 的向量模型同进程交替使用多轮时，历史行为会在 `decode_batch` 段错误（Windows exit 139），显式压到 512 实测稳定（40 轮 × 2 次零崩溃，对照实验见 #307 PR）。打分仍逐条调用，此值只影响物理批缓冲 | `RERANK_BACKEND=local_ce` 时 |
| `RERANK_POOL` | `20` | 参与重排的候选深度：融合候选取前这么多条交给重排后端（启用时语义候选同步拉深到这里，见 [RETRIEVAL_QUALITY.md](RETRIEVAL_QUALITY.md)）。默认 20 是 `local_ce` 实测调优（#274：约 4.8 秒/查询 vs 池 40 的 10.3 秒，hit@5 差 1 题、在判官噪声地板内）；**回退旧深度：`RERANK_POOL=40`** | `RERANK_ENABLED=1` 时 |
| `RERANK_SNIPPET_CHARS` | `240` | 每条候选喂给模型的正文长度（字符）。实测（#274）：`local_ce` 降到 160 会再丢一条救回且提速有限，保持 240 | `RERANK_ENABLED=1` 时 |
| `RERANK_TIMEOUT` | `20` | 单次重排超时（秒；连接/读取同一值）。`llm` 后端专用，`local_ce` 本地推理不走网络、不受它约束 | `RERANK_ENABLED=1` 时 |
| `RERANK_MAX_TOKENS` | `8000` | 输出 token 上限（`llm` 后端专用）：默认模型是 reasoning 模型，给小值（如 4000）会被截断成空 `content`，对外表现为「模型返回空排序」→ fail-open 回原顺序 | `RERANK_ENABLED=1` 时 |
| `RERANK_REASONING_EFFORT` | `low` | 推理强度（`llm` 后端专用）：`low` 可显著缩短 reasoning（实测 13447 字符 → 约 1000）；空 = 请求体不传该字段 | `RERANK_ENABLED=1` 时 |
| `RERANK_MODEL` | *（空 = 用 `DEEPSEEK_MODEL`）* | 重排用的模型标识（`llm` 后端专用）；空则复用对话模型 | `RERANK_ENABLED=1` 时 |
| `RETRIEVAL_EXPAND_DEPTH` | `0` | 语义主序的图扩散深度：`0` = 纯语义排序（默认）；`1` = 图邻居参与语义主序（可一键回退） | 检索启用图扩散时 |
| `TIER_FACTS` | `memory,correction,decision,plan,task,review,solution,inspiration,user_intent,character_state` | 归入事实层的记忆 type（逗号分隔）；未登记的 type 一律归事实层 | 检索与注入按 tier 过滤时 |
| `TIER_LOGS` | `record,event,git_commit,thought` | 归入日志层的记忆 type（逗号分隔），默认不进检索与注入池 | 检索与注入按 tier 过滤时 |
| `DEFAULT_TIER` | *（空 = 不过滤；2.21 起默认，#266）* | **检索**的默认分层：空串 = 不过滤（全量），`facts` 只回事实层（设 `facts` 可一键回退 2.20 行为——facts 默认曾把 27% 判卷题的日志层 gold 挡在候选外），`logs` 只回日志层。prefetch 自动注入不受此键影响（用 `PALIMPSEST_PREFETCH_TIER` 单独控制，默认 facts） | 未显式指定 `tier` 时 |
| `MEM_INGEST_MAX_LENGTH` | `50000` | 单条记忆 content 最大字符数，超长拒绝写入 | `mem_ingest` 写入时 |
| `MEM_SEARCH_MAX_QUERY_CHARS` | `8000` | 检索 query 最大字符数，超长在检索入口直接拒绝（REST 422 / pre-turn fail-open 并标 `skip_reason="query_too_long"`），不再打到 embedding 模型侧报「exceeds the context length」 | `/mem/search`、`/mem/hybrid-search`、`/lifecycle/pre-turn` |
| `SEARCH_EXCERPT_CHARS` | `400` | 检索结果逐字 `excerpt` 的目标长度：无摘要生成，只选截断点——优先落句边界（。！？.? / 换行），无句读退化为词边界（空格/逗号），仍无硬截断；150 字 `summary` 语义不变（硬截断，不是摘要，不得据此断言事实完整），要全文用 `mem_get_full`。prefetch / pre-turn 注入路径仍只用 `summary`（防 prompt 膨胀） | `/mem/search`、`/mem/hybrid-search`、`/mem/retrieve`（MCP `mem_retrieve`） |
| `POLICY_MODE` | `warn` | 写入口护栏模式：`warn` = 只记日志与结果字段，不改写行为（默认）；`enforce` = 策略真正拦截；非法值回退 `warn` | `mem_ingest` 写入时 |
| `POLICY_PROTECTED_TYPES` | `rule` | 受保护 type（逗号分隔）：`enforce` 模式下不参与自动覆盖（不被标 `outdated` / 建 `REVISED_BY` 边）；`payload.protected=True` 的节点亦受保护 | `mem_ingest` 写入时的冲突检测 |
| `POLICY_TYPE_LIMITS` | `record:8000,memory:8000,task:10000,plan:12000,kb_chunk:8000,novel_chunk:12000` | 按 type 分级的软上限（形如 `task:20000,plan:80000`）；`warn` 模式只告警不拒，`enforce` 模式拒绝。默认值取自实测内容长度分布（各类型 p90 的 3-5 倍），正常写入不受影响 | `mem_ingest` 写入时 |
| `POLICY_MAX_CORE_TOTAL` | `0` | 核心记忆（facts tier）总字符上限；`0` = 不限 | — |
| `PALIMPSEST_POLICY_UPDATE` | *（空）* | 配置人工闸门：仅当为 `1` 时接受运行时策略覆盖；agent 侧工具不暴露改策略入口，策略只能改 `.env` / 重启生效 | — |
| `CONFLICT_SKIP_TYPES` | *（空）* | 跳过冲突检测的 type（逗号分隔），列出的 type 不再被标 `outdated` / 建 `REVISED_BY` 边；默认空 = 行为不变。用于 `task` 这类「累积关系」而非「同一事实被取代」的场景 | `mem_ingest` 写入时的冲突检测 |
| `CONFLICT_TIME_WINDOW` | *（空）* | 启用 bi-temporal 时间窗冲突判定：相似但世界时间窗不重叠的事实（如「十年前住北京」≠「现在住上海」）不判为互相取代。默认关 = 行为逐字节不变（高风险语义变更，验证后再开） | `mem_ingest` 写入时的冲突检测 |
| `CONFLICT_AUTO_SUPERSEDE` | *（空）* | 启用**自动相似取代**。默认关：写入绝不因相似度把旧记忆标 `outdated`——相似候选只记入 `related_ids`，取代仅由调用方显式声明（`mem_ingest` 的 `replaces` 参数）触发。开启则回退旧行为（相似度 > 0.75 → 标 `outdated`）。理由：「相似」不等于「同一事实被取代」，仅凭阈值会误吞同主题互补内容 | `mem_ingest` 写入时的冲突检测 |
| `DEDUPE_REJECT_THRESHOLD` | `0.85` | **机械去重闸门**的相似度门槛（`mem_ingest` 的 `dedupe=reject` / `merge` 判定用）。**与冲突检测的 0.4 无关**——0.4 是「话题相关」，不是「重复」。默认值由生产库全量分布实测选定（同 type 门内相似对 p99 = 0.725；>0.85 仅占 0.089%，>0.92 仅 0.024%）。闸门不做语义判断：`allow`（默认，行为不变）/ `reject`（高相似拒写并返回 `duplicate_of`）/ `merge`（合并进 `dedupe_target` 指定节点，不新建） | `mem_ingest` 写入时的机械闸门 |
| `RP_MODE` | *（空）* | **RP 专用实例开关**（`1`/`true`/`yes`/`on` 开；默认关 = 行为完全不变）。开启后两条变化：① 所有带 `domain`（或不带）的入口域锁死为 `RP_DOMAIN`——不传自动补，传别的 domain → 400 拒绝；② ingest 不再挂 `kb_chunk` 知识关联（`linked_from` 保持空、不建 REFERENCES 边） | 酒馆记忆桥独立部署（RP 域隔离） |
| `RP_DOMAIN` | `rp` | `RP_MODE` 开启时的锁死域名；空值回退 `rp` | `RP_MODE` |
| `KNOWLEDGE_DIR` | *（可选）* | 知识库根目录（待索引的 Obsidian `.md` 文件） | 使用 `kb_index` / `build_kb_index.py` 时 |

## 本地重排模型（`RERANK_BACKEND=local_ce`）

`local_ce` 后端在进程内跑一个 cross-encoder 给候选打相关分，全程不出网、不耗任何 API 额度。
上手机会成本只有两样：

1. **Python 依赖**：`llama-cpp-python` 在 PyPI 没有预编译 wheel，直接 `pip install` 会走源码编译（多数机器没有 MSVC 工具链）。从官方 CPU wheel 索引装：

   ```bash
   pip install "llama-cpp-python>=0.3.36,<0.4" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
   ```

   需要支持 rank 池化的版本（≥ 0.3.36，`llama_cpp.LLAMA_POOLING_TYPE_RANK` 可用）。

2. **模型文件**：下载 `bge-reranker-v2-m3`（BAAI，官方权重约 0.6B 参数）的 **Q8_0 GGUF**（约 635 MB），放到 `models/`（已在 `.gitignore`，不进仓库），保持默认的 `RERANK_LOCAL_MODEL_PATH=models/bge-reranker-v2-m3-Q8_0.gguf` 指向即可。GGUF 量化版可在 HuggingFace 搜 `bge-reranker-v2-m3 GGUF` 找社区转换（如 gpustack 组织）。

两样都缺/缺一样时**检索不会坏**：后端检测到不可用会把 `channels.rerank` 标成 `degraded`、按原顺序返回候选，日志给出上述指引，且不会每条查询重复探测。

`local_ce` 的两个 llama.cpp 批参数（#307）：`RERANK_LOCAL_N_BATCH`（默认 4096）是物理批大小，`RERANK_LOCAL_N_UBATCH`（默认 512）是一次送进计算的 token 上限——打分是逐条调用，这两个值只影响底层批缓冲，不影响语义。**`RERANK_LOCAL_N_UBATCH` 保持 512（或至少显式设一个小于 `n_batch` 的值）**：当 `EMBEDDING_PROVIDER=local` 时，store 的向量模型实例与 reranker 的 cross-encoder 实例同进程交替使用，历史上不设 `n_ubatch`（默认等于 `n_batch=4096`）会在重复多轮后于 llama.cpp `decode_batch` 触发段错误（Windows exit 139），进程直接死。`RERANK_LOCAL_N_UBATCH=0` 可显式还原历史行为（不推荐）。另：某轮打分若所有候选分数完全相同（模型输出失去区分度），该轮不采用重排、按原序返回，`channels.rerank` 标 `rerank_degenerate`。

## 更换向量模型 / 重嵌全库

### 为什么要重嵌？

不同的 embedding 模型产生不同的向量空间——**跨模型的向量不可混用**。如果切换了 `EMBEDDING_PROVIDER`、`OLLAMA_EMBEDDING_MODEL` 或 `EMBEDDING_MODEL`，必须对库中所有节点重新生成向量（重嵌），否则新旧向量空间互相排斥，检索质量会急剧下降。

### 向量空间指纹

库内会记录一条运维记录（`source=embedding-fingerprint`，全局唯一一条），内容是当前向量空间的标识：

```
space_id = sha256(provider | model | dim | pooling) 的前 16 位
```

- **启动自检**（`startup-check`）：不一致只打一行 WARN，**不改变退出码**（升级后服务仍能起）
- **`doctor`**：作为第 9 项「向量空间一致性」——不一致判 **FAIL** 并直接给出修复命令
- **`reindex` 开跑前**：比对并打印差异；`--force` / `--yes` 跳过这一层确认，否则交互确认
- **`reindex` 正式开跑前**：还有一次「即将重嵌 N 个节点，确认？」——只有 `--yes` / `-y` 能跳过。`--force` 不管这一步，无 TTY 的脚本 / CI / agent 里会直接 `EOF when reading a line`
- **`reindex` 正常跑完且零失败**：写入 / 更新指纹（Ctrl+C 中断或部分失败时**不写**——向量只重算了一半，写指纹等于谎报）

指纹里另存**引擎版本**（`engine`，如 `llama_cpp:0.3.36`），但它**不进 `space_id`**：引擎升级不改变向量空间，只记一条软提示（`doctor` 显示 WARN 而非 FAIL）。若引擎版本探测不到则记为 `unknown` 并静默——避免告警变成狼来了。

### 推荐操作顺序

```bash
# 1. 体检：确认 provider / 模型 / 维度正确，embedding 服务可用
python scripts/palimpsest_cli.py reindex --check

# 2. 预览：查看将要重嵌哪些节点
python scripts/palimpsest_cli.py reindex --dry-run

# 3. 正式执行（默认断点续跑，Ctrl+C 中断后可自动续跑）
python scripts/palimpsest_cli.py reindex --yes

# 换了模型 / provider 之后：一条命令到位（放弃断点 + 跳过指纹确认 + 全量重算 + 写新指纹）
# `--yes` 不可省：它跳的是「即将重嵌 N 个节点」那次确认（`--force` 不管这一步）
# 另：reindex 经 REST 业务端点在服务端执行（issue #168），REST 在跑即可，无需停服
python scripts/palimpsest_cli.py reindex --force --yes

# 4. 验证：跑一次检索冒烟
python scripts/palimpsest_cli.py search "测试" --top-k 3
```

常用选项：

| 选项 | 说明 |
|---|---|
| `--only memory,record` | 只重嵌指定类型 |
| `--skip kb_chunk,novel_chunk` | 跳过指定类型 |
| `--batch 128` | 每 128 个节点打印进度 |
| `--restart` | 忽略断点，从头重嵌 |
| `--force` | 接受当前向量空间并全量重算：放弃断点 + 跳过指纹确认，完成后写入新指纹（`--restart` 的超集） |

### 换维度（新模型输出维度不同）

如果新模型的输出维度与当前库不一致（如从 1024 维换到 768 维），**不能直接重嵌**——必须新建库。流程如下：

```bash
# 1. 导出
python scripts/export_all_data.py

# 2. 重建（新库）
python scripts/rebuild_db.py

# 3. 修改 .env 中对应维度配置
# OLLAMA_EMBEDDING_DIM=768   或   EMBEDDING_DIM=768

# 4. 重建知识库索引
python scripts/build_kb_index.py --full

# 5. 如有小说设定库
python scripts/build_novel_index.py --source <vault路径> --full
```
