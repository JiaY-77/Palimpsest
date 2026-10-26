# 配置参考

所有配置均从环境变量读取（`.env` 文件由 `python-dotenv` 自动加载），完整带注释模板见
[`.env.example`](../.env.example)。README 只列用户必配的最小集，本页是完整配置表（43 项，含
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
| `EMBEDDING_LOCAL_N_THREADS` | `0` | CPU 线程数；`0` = 交引擎自行决定 | `EMBEDDING_PROVIDER=local` |
| `MEMORY_DECAY_FACTOR` | `0.95` | 月度记忆衰减（排序用，`score × importance × factor^(天/30)`）；`1.0` 关闭衰减；`kb_chunk` 节点永不衰减 | soft 模式：仅进入 ε 微调项 `recency_norm`（ε 默认 0.02 → 排序影响 ≤0.02，一年内约 0.01 量级，近乎半死参数）；hard 模式：乘性硬加权 |
| `MEMORY_RERANK_MODE` | `soft` | 重排模式：`soft` = 语义分为主线 + ε 级元数据微调（默认）；`hard` = 旧版乘性硬加权（可回退） | — |
| `SOFT_RERANK_EPS` | `0.02` | `soft` 模式的 ε：落在余弦分差区间的 15%–40%，只做 tie-break | `MEMORY_RERANK_MODE=soft` |
| `DOMAIN_BOOST_EPS` | `0.10` | 域软加权加分（加性，作用在语义分上）：`domain_boost` 非空时对同域候选加此值 | `domain_boost` 参数非空 |
| `KB_SOFT_RERANK_MULT` | `1.5` | `kb_chunk`（知识块不老化）在 `soft` 模式下的 ε 加成倍率 | `MEMORY_RERANK_MODE=soft` |
| `DOMAIN_BIAS_WEIGHT` | `1.15` | 域偏置检索的额外权重 | `domain_bias` 参数非空 |
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
| `RETRIEVAL_EXPAND_DEPTH` | `0` | 语义主序的图扩散深度：`0` = 纯语义排序（默认）；`1` = 图邻居参与语义主序（可一键回退） | 检索启用图扩散时 |
| `TIER_FACTS` | `memory,correction,decision,plan,task,review,solution,inspiration,user_intent,character_state` | 归入事实层的记忆 type（逗号分隔）；未登记的 type 一律归事实层 | 检索与注入按 tier 过滤时 |
| `TIER_LOGS` | `record,event,git_commit` | 归入日志层的记忆 type（逗号分隔），默认不进检索与注入池 | 检索与注入按 tier 过滤时 |
| `DEFAULT_TIER` | `facts` | 检索与注入的默认分层；`logs` 只回日志层，空串 = 不过滤（全量历史通道） | 未显式指定 `tier` 时 |
| `MEM_INGEST_MAX_LENGTH` | `50000` | 单条记忆 content 最大字符数，超长拒绝写入 | `mem_ingest` 写入时 |
| `MEM_SEARCH_MAX_QUERY_CHARS` | `8000` | 检索 query 最大字符数，超长在检索入口直接拒绝（REST 422 / pre-turn fail-open 并标 `skip_reason="query_too_long"`），不再打到 embedding 模型侧报「exceeds the context length」 | `/mem/search`、`/mem/hybrid-search`、`/lifecycle/pre-turn` |
| `POLICY_MODE` | `warn` | 写入口护栏模式：`warn` = 只记日志与结果字段，不改写行为（默认）；`enforce` = 策略真正拦截；非法值回退 `warn` | `mem_ingest` 写入时 |
| `POLICY_PROTECTED_TYPES` | `rule` | 受保护 type（逗号分隔）：`enforce` 模式下不参与自动覆盖（不被标 `outdated` / 建 `REVISED_BY` 边）；`payload.protected=True` 的节点亦受保护 | `mem_ingest` 写入时的冲突检测 |
| `POLICY_TYPE_LIMITS` | `record:8000,memory:8000,task:10000,plan:12000,kb_chunk:8000,novel_chunk:12000` | 按 type 分级的软上限（形如 `task:20000,plan:80000`）；`warn` 模式只告警不拒，`enforce` 模式拒绝。默认值取自实测内容长度分布（各类型 p90 的 3-5 倍），正常写入不受影响 | `mem_ingest` 写入时 |
| `POLICY_MAX_CORE_TOTAL` | `0` | 核心记忆（facts tier）总字符上限；`0` = 不限 | — |
| `PALIMPSEST_POLICY_UPDATE` | *（空）* | 配置人工闸门：仅当为 `1` 时接受运行时策略覆盖；agent 侧工具不暴露改策略入口，策略只能改 `.env` / 重启生效 | — |
| `CONFLICT_SKIP_TYPES` | *（空）* | 跳过冲突检测的 type（逗号分隔），列出的 type 不再被标 `outdated` / 建 `REVISED_BY` 边；默认空 = 行为不变。用于 `task` 这类「累积关系」而非「同一事实被取代」的场景 | `mem_ingest` 写入时的冲突检测 |
| `CONFLICT_TIME_WINDOW` | *（空）* | 启用 bi-temporal 时间窗冲突判定：相似但世界时间窗不重叠的事实（如「十年前住北京」≠「现在住上海」）不判为互相取代。默认关 = 行为逐字节不变（高风险语义变更，验证后再开） | `mem_ingest` 写入时的冲突检测 |
| `CONFLICT_AUTO_SUPERSEDE` | *（空）* | 启用**自动相似取代**。默认关：写入绝不因相似度把旧记忆标 `outdated`——相似候选只记入 `related_ids`，取代仅由调用方显式声明（`mem_ingest` 的 `replaces` 参数）触发。开启则回退旧行为（相似度 > 0.75 → 标 `outdated`）。理由：「相似」不等于「同一事实被取代」，仅凭阈值会误吞同主题互补内容 | `mem_ingest` 写入时的冲突检测 |
| `KNOWLEDGE_DIR` | *（可选）* | 知识库根目录（待索引的 Obsidian `.md` 文件） | 使用 `kb_index` / `build_kb_index.py` 时 |

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
- **`reindex` 开跑前**：比对并打印差异；`--force` / `--yes` 时继续，否则交互确认
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
python scripts/palimpsest_cli.py reindex --force

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
