# Changelog

本项目遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)（Semantic Versioning）。

版本格式：`主版本.次版本.修订号`。发布流程见 [RELEASING.md](docs/RELEASING.md)。

## [Unreleased]

### 新增

- **长节点分块池化嵌入：彻底消除 `embed()` 的静默前缀截断**（#232）：`llama-cpp-python` 的 `embed()` 内部有 `tokens = tokens[:n_batch]`，超过 `EMBEDDING_LOCAL_N_BATCH`（512）的输入被**静默截断**，只嵌内容前缀——生产库 732/2912 个节点（25%，`novel_chunk` 119/124、`task` 148/192、`record` 212/748）的向量只覆盖前缀。现在 local 路径改走**分块池化**：按 token 边界切块（`EMBEDDING_LOCAL_CHUNK_TOKENS`，默认 512，**必须 ≤ n_batch**——块本身也要过 `embed()`，自检新增 error 级检查拦错误配置），逐块嵌入（块内保持 LAST pooling：Qwen3-Embedding 是 decoder-only 因果 LM）后按块内 token 数加权平均再 L2 归一。检索层零改动（仍是单向量契约，`embed_text` 签名与返回值不变）；短文本（≤ chunk）走单块路径，与改动前逐位一致（有测试钉住）。实测 cos vs 全文一次嵌入：旧截断 0.405 → 分块池化 0.92，峰值内存不变（1.5 GB vs 直接提 n_batch=4096 的 3.5 GB）。
  - **截断事实变成可读的审计字段**：新增 `TriviumStore.embed_text_with_audit()`，全部写入路径（`mem_ingest` / `insert_node` / `insert_node_tx` / `reembed_node` / git_ingest / memoryhub 导入 / 会话缓冲 / kb / novel / skill 索引重建）把审计沉淀进 `payload["embedding"]` 子对象——`{"embedding_mode": "chunked"|"single", "total_tokens", "embedded_tokens", "embedded_chunks", "chunk_tokens"}`，`embedded_tokens == total_tokens` 恒成立。ollama / openai provider 本次不改截断策略，只落 `{"embedding_mode": "single"}`；`rebuild_db` 路径不落 `payload.embedding` 审计字段（保持 payload 原样契约），向量本身已全文覆盖。
  - **存量修复脚本 `scripts/reembed_long_nodes.py`**：默认 dry-run（只读，打印将重嵌的节点 id / type / tokens），`--apply` 才写；按 content 现算 token 数挑选（不信旧字段），已带 chunked 审计的节点跳过（幂等，重跑 0 变更）；不自动备份，`--apply` 前先 `python scripts/backup_db.py`，生产库须停服务窗口执行（连接级排他）。生产重嵌由维护者按窗口执行。
- **Docker 一键自托管**：新增 `Dockerfile`（`python:3.12-slim`——`triviumdb` 的 wheel 要求 `<3.13`；`llama-cpp-python` 走官方 CPU wheel 索引，否则源码编译失败；非 root 用户 UID 10001；模型可在构建期预置，并借 BuildKit cache mount 跨构建复用，`docker compose build` 不再重下 610MB）、`docker-compose.yml`（`./data` 持久卷、2GB 内存上限、HEALTHCHECK、`env_file` 用 `required: false`——**没建 `.env` 也能起来**，否则 `docker compose up` 直接报错退出）与 `docs/DEPLOY.md`（三步启动 / 公网 API Key / Caddy TLS / 备份恢复 / 升级 / 故障排查 / 单实例边界）。
- **`GET /health` 健康探针**：恒 200，返回 `status` / `version` / `db` / `db_initialized`，免鉴权（与 `/` 同白名单），供容器编排与反向代理使用；**未初始化不算故障**——全新实例与「TriviumDB 尚未 flush、写只在 WAL 里」都报 `db: ok`，只用 `db_initialized` 区分，`degraded` 只留给数据目录不可写 / 探测超时等真异常。
- CI 新增 `docker.yml`：构建镜像 → 起容器 → 探活 → 真实 `ingest` / `search` / `export` 冒烟，把「部署能不能起来」变成每次 PR 都跑的门禁。

### 测试

- **`pytest` 裸跑不再收集 `data/` 下的备份副本**：CI 跑的是 `pytest tests/`，但本地直接敲 `pytest` 会递归整个仓库——`data/` 被 `.gitignore` 排除，里面若有备份副本（`data/backup_*/test_*.py`）就会被一起收集，触发 `import file mismatch` 让整个会话在 collection 阶段中断（实测踩过一次，白跑一轮）。`pyproject.toml` 增加 `[tool.pytest.ini_options] testpaths = ["tests"]`，让本地裸跑与 CI 同口径，不靠人记着加 `tests/`。

### 文档

- 钉住 #232 重嵌后的首份检索基线（`docs/RETRIEVAL_QUALITY.md` §8.8）：148 题 / 判卷 31 的完整指标与配对 bootstrap；并如实记录「31 条题的 gold 与被重嵌的 748 个长节点零交集，故重嵌前后指标逐位相同」，以长节点尾部检索实测（top10 命中 1/8 → 3/8）作为 #232 有效性的直接证据。
- 修复题集数据与文档的漂移：`eval_set_v2.json` 本地文件此前仍是 138 题 / 判卷 27（Phase-1 判词的 10 条从未写入），已按判词文档补回 4 条 live 正样本（q140–q143）+ 6 条 dropped（q144–q149），恢复为 148 题 / 判卷 31；数据文件不入库，本条记录口径以便复现。

### 修复

- **备份补上 `fts.db` + doctor 新增「FTS 索引完整性」检查**（#241）：`scripts/backup_db.py` 此前只备份 TriviumDB 整文件组、从不备份 `data/fts.db`，从备份恢复后 FTS5 索引不存在，`_ensure_schema` 的 `CREATE VIRTUAL TABLE IF NOT EXISTS` 会静默建出空表——混合检索（RRF / 级联）随之一路降级为纯语义检索，仅在日志里刷「降级」，服务看起来完全健康。现在备份附带 `fts.db`（含 `-wal` / `-shm` 侧车，存在才拷），主库组校验通过后还比对副本 `mem_fts` 行数与备份开始前的源库快照（源库没有索引则跳过说明，不算失败）；`doctor` / 启动自检新增「FTS 索引完整性」（主库节点数 vs 索引行数，经 `store._acquire()` 取 `node_count()`，不做全量遍历）：索引缺失或行数不足一半时标红并给出重建命令 `python scripts/check_fts_consistency.py --repair`。

## [2.18.0] - 2026-10-29

### 新增

- **胶囊接手留痕**（#208）：新增 `POST /memory/{node_id}/open`——胶囊首次被接手时写 `opened_at`，每次不同会话接手累加 `open_count` 并保留 `opened_by`（去重、先到先序、上限 10 个）。直接走 `update_payload` 写，content / `last_touched_at` / FTS 索引都不动——「被接手」是交接事件，不是「任务被碰过」。`tasks_active` 在有值时回报 `opened_at` / `open_count`（无值则键缺席，老胶囊形态不变）。
- **孤儿会话缓冲补产**（#215）：胶囊只在 `POST /lifecycle/session-end` 落地，进程被硬砍（计划内关机 / 崩溃）就永远产不出来。新增扫描器把静默超过阈值的活跃缓冲补产成胶囊：后台循环（启动 5 秒后首轮，此后每 `SESSION_CAPSULE_RECOVER_INTERVAL_SECONDS`）加手动端点 `POST /lifecycle/recover-orphan-capsules`（带 `dry_run` 只报告不写库）。单 session 失败只记日志计入 failed、绝不中断整批；循环自身出错不退出。缓冲新增 `updated_at`（每次追加轮次刷新），静默时长才有据可依。
- **胶囊创建按内容厚度设门槛**（#219）：实质轮 ≥ 2 且填满 ≥ 2 节才产胶囊；不过门槛时**既不改也不建**胶囊节点，但返回可观测的 `{skipped: too_thin, substantive_turns, filled_sections}` 而不是静默丢弃，同时仍然关闭缓冲（否则断线自愈扫描器会每 5 分钟重试一个永远长不厚的会话）。`POST /lifecycle/post-turn` 额外回显 `turn_index_resolved`（缓冲实际使用的轮次号）+ 请求的 `turn_index`，插件重载后能接着缓冲的最后一轮续，不再覆盖。
- **新增 `EMBEDDING_LOCAL_N_BATCH`，并明确它同时是 embedding 的截断长度**（#228）：`llama-cpp-python` 的 `embed()` 内部执行 `tokens = tokens[:n_batch]`，**超出 n_batch 的 token 被静默丢弃**——所以这个参数不是纯内存旋钮，调小它就是用检索质量换内存（`n_batch=32` 时长文本峰值 RSS 从 1534 MB 降到 962 MB，但会截断 92% 的 kb 切片）。默认值保持 `512`（历史行为不变），配置项只在需要极限压内存时下调。顺带记录一个由此暴露的既有缺陷：默认 512 下，生产库 633/2407 个节点（26%，`novel_chunk` 119/124）的向量本来就只覆盖各自内容的前 512 token，已单独立 issue #232 跟踪。`n_ctx=2048` 不动（降会截断长切片）。

- **常驻域（pin domain）：每轮无条件注入的规则块**（#176）：`PIN_DOMAIN` 指定的 domain 里 `status=active` 的节点会**无条件**拼进 `/lifecycle/pre-turn` 注入文本的**最前面**，不参与语义相似度竞争——解决「常驻规则与当前消息零关键词重叠就永远检索不到」这个硬缺口（`MEMORY.md` 每轮全量注入，但有 2200 字硬顶且已用满）。成本用**节点边界的字符预算**（`PIN_BUDGET_CHARS`，默认 3000）兜住：节点按 `importance` 倒序装入，超预算时**整条不放、绝不切半个节点**，并在响应的 `pin.truncated` / `pin.dropped` 里如实报告，不静默少注入。与语义召回去重（常驻节点同时被召回命中时只保留常驻那份）。`PIN_DOMAIN` 为空 = 完全关闭，行为与改动前逐位一致；常驻域查询异常 fail-open（跳过常驻块，召回照常）。

### 破坏性变更

- **思考骨架统一为「四问 = 胶囊四节」**（#188）：原先 code / research / default 三套骨架各有一套字段名（`change` / `failure_path` / `verify` / `deliverable` 等），而胶囊四节是 `thought` / `outcome` / `next` / `pitfalls` —— 任何单骨架轮都填不满四节。实测全库自动胶囊 3 条**全部**只有【思路】有正文、另三节恒为「（待补）」（`pending` 三项），而同期手写接续点 121 条。现四问固定为「①核心判断 / 思路 ②做了什么 ③下一步 / 待办 ④踩的坑 / 风险」，编号直接对应四节；`skeleton_kind` 仍按消息情境分类（`code` / `research` / `default`），但只作归因标记、不再影响骨架正文。
  兼容性（三条都已落地并有测试覆盖）：
  - **历史 `thought` 节点的 `thought_origin` 归因不丢**：`detect_thought_origin` 把三套旧模板的起手式并入判据（`_LEGACY_SKELETON_OPENERS`），老节点仍判为 `skeleton`；
  - **老缓冲节点仍能合成胶囊**：`compose_session_capsule` 保留旧字段名取值（`judgement` / `conclusion` / `goal`、`change` / `deliverable`、`verify` / `output`、`failure_path`）作为兜底；
  - `is_substantive_turn(...)` 的 `min_thought_chars` 参数与 `Config.SESSION_BUFFER_MIN_THOUGHT_CHARS` **已废弃**（保留签名与配置项以免破坏调用方，下个大版本删除）。

- **会话缓冲轮次不再恒为 1**（#216）：宿主不发 `turn_index`（缺省 0）时，服务端不再把 0 当「同一轮」幂等覆盖（此前每轮顶掉上一轮，缓冲只剩最后一轮），改为按 `last_turn_index + 1` 追加（新建缓冲从 1 起；显式非 0 序号保留同序号覆盖去重）；Hermes 插件 `sync_turn` 随请求上送每会话单调轮次计数（宿主的 provider 调用不传轮次号）。`<thought>` 解析不出编号小节时不再整轮按 `no_sections` 丢弃：无编号行的原生思考轮原文截断进 thought 节照收（宁粗不空），含编号行但四问全空（照抄问句没写内容）仍视为无内容；插件收侧把本轮**全部** reasoning 段按消息时间序拼接（此前只留最近一段，工具循环里前面几段的思考会漏）。
### 修复

- **补产胶囊不再压过手写接续点**（#227）：断线自愈补产的胶囊（`recovered=true`）此前会因 `created_at` 比同场次的手写接续点更新而**排在 `tasks_active(include_handoff=true)` 第一位**——新会话按约定「先读第一条」于是读到机器抓的半成品，跨会话续接被打断（生产实证：节点 3778 压过 3748）。现在 `recovered=true` 的胶囊不进 handoff 置顶组（仍留在普通任务组，轮次不丢不隐藏），正常 session-end 胶囊排序零行为变化（有回归测试）。同时 `_handoff_title` 对「首行只有锚词 + session hash」的形态不再产出 hash-only 标题，退化为 `补产胶囊 <日期> <session 前 8 位>`；前导 `[...]` 状态块（`[完成]` / `[待办]`）与该判据同口径剥离，两处对「首行装饰」的定义统一。
- **`kb_chunk` / `novel_chunk` / `git_commit` 的 `created_at` 从未写入**（#226）：`insert_node` 硬编码 `None`，存量 938 + 124 + 23 条全部无时间戳——新鲜度加权（#224）对这三类恒为「无信号」，评测的时间切分也无法放置以它们为答案的题目。现在写入路径在**调用方未提供该键**时落当前时刻（显式 `None` 视为「刻意无时间戳」并保留；顺带修掉调用方传入的 `created_at` 被 `extra_fields` 合并丢弃的暗 bug），并新增 `scripts/backfill_created_at.py` 回填存量：默认 dry-run，`--apply` 落盘，每个值带 `created_at_backfill` 审计标记（来源字段 + 原值 + 时间；`commit_time` 另标 `date-only`），无法恢复的节点只报告不编造，重跑幂等。生产库实测 1085 条全部回填，`freshness_decay` 三类从 `None` 变为实值，时间切分 tune 14→20 / held-out 4→6 / `missing_timestamp` 9→1。
- **思考骨架每轮被注入两次**（#206）：宿主把插件加载两次（插件加载器 + scoped memory provider），两次 `register()` 各挂一个 `pre_llm_call` 回调，宿主再把各回调返回的文本 `"\n\n".join` 后整段拼进 user 消息——于是每轮 user 消息里出现**两份完全相同**的骨架，白烧 token，模型还会看到两条重复的建流程指令。修在插件侧：`thought_skeleton_context` 按 `(session_id, turn_id)` 做一次性闸门（类级 dict、跨实例共享，与既有 `_skeleton_kind_by_session` 同款理由），同一轮第二个回调直接返回空串且**不再发 HTTP**；闸门判定刻意放在重置 `skeleton_kind` **之前**——否则第二个实例会把第一个实例写好的 kind 清空，收侧上报的 `skeleton_kind` 又变回空串（2026-10-17 修过的老 bug 会复现）。拿不到 `turn_id` 的调用方（旧宿主 / 非钩子调用）不做去重，行为与改动前逐位一致。
- **监控面板融合进 REST：`http://127.0.0.1:8090/dashboard`**（一个进程搞定）：面板原先只以独立进程存在（`scripts/dashboard.py` :8010），只跑 REST 的人根本没有面板。现在九个只读端点统一抽进 `server/dashboard_api.build_router`，REST 与独立进程各自 `include_router`，`dashboard.html` 一行未改。**数据源可注入**是关键：REST 进程内注入直读实现（`iter_payloads` / `compute_stats` / 既有工具函数），**不再 HTTP 调自己**——那会多占一个线程、平白多一跳，线程池紧张时还可能自锁；独立进程注入经 REST 代理的实现（单写者约束下它没有库访问权）。合并治理的**预览**同时恢复可用（REST 早有 `POST /mem/consolidate` 的 dry-run，原先连预览都返回 501 属过时保守写法），**apply 仍恒为 501**——面板是只读观察窗，破坏性操作走可审计的 CLI。
- **修复 dashboard 在文档给出的启动方式下根本起不来**（`scripts/dashboard.py`）：脚本漏了 `from _common import ...`（仓库统一的「项目根注入 sys.path」约定），于是 `python scripts/dashboard.py` 会把 `scripts/` 而非仓库根放进 `sys.path`，直接崩在 `from config import Config`：`ModuleNotFoundError: No module named 'config'`——面板从来没被真正启动过，而 `docs/INSTALL.md` 看起来一切正常。现按约定补上注入，并新增 `tests/test_scripts_bootstrap.py`：**按脚本模式的 sys.path 形态**（摘掉仓库根、只放 `scripts/`）import 入口脚本，dashboard 与 CLI 各一条——普通 pytest 里 `sys.path` 早被 conftest 处理，怎么 import 都复现不出这个 bug。
- **「老库被迫落到默认 local」改成真正的 WARNING**（#192 跟进）：原先只把提示追加进自检 detail（等于日志里的脚注）；现在是 `logger.warning` + detail 双写——启动日志里扫得到才算通知，否则用户升级后「后端被默认值换掉」这件事根本不会进入视野。
- **RemoteStore 透传 `replaces` / `meta` / `sections` / `pending`**（#197）：`client/remote_store.py` 的 `insert_node` 原本是硬编码五项白名单（`content` / `type` / `importance` / `domain` / `source`），经 HTTP 客户端路径写入的调用方**静默丢字段**——`meta` 丢意味着外部宿主绑不上自己的会话实体，`sections` 丢意味着手写胶囊与自动胶囊的形状又分裂回两种。现改为显式可选字段元组 `_OPTIONAL_INGEST_KEYS`（与服务端 `MemIngestRequest` 同步，回归用例钉着），且**只在调用方真的给了值时才带键**：不传时请求体与旧版逐键一致。
- **升级可观测性：回显 embedding provider 的来源，并补齐升级路径文档**（#192 / #193）：2.16.0 把 `EMBEDDING_PROVIDER` 默认值从 `ollama` 改成 `local`，老 `.env` 通常没写这一项——升级后会**静默换后端**，配置面上却毫无变化。现启动自检的 embedding 项回显来源（`env` / `.env 未显式设置 → 自动探测`），并在「**老库 + 靠默认值落到 local**」这个最高风险组合上追加一句点名提示（空库不打扰）。来源判定集中在新函数 `config.embedding_provider_source()`（刻意做成模块级函数而非 `Config` 属性——它不是配置项，落进 `Config` 会被配置项一致性检查当成漏登记的旋钮）。`docs/INSTALL.md` 新增「从 2.15 及更早版本升级」小节：显式写 PROVIDER、存量库必须重嵌（附 700 节点 ≈70 秒的实测对照）、停 REST → `reindex --force --yes` → 起 REST → `/doctor` 应回 9/9 的完整顺序。
- **删除 `tool_calls_count` 判据与请求字段**（#194）：宿主侧从不填这个字段（恒 0），而判据 2 放宽（#188）后空 `sections` 在更上游就被 `no_sections` 挡掉——这条判据在**任何真实链路里都到不了**，留着只会让后人以为它在工作。`PostTurnRequest.tool_calls_count`、`is_substantive_turn(tool_calls_count=...)`、`decide_session_buffer(tool_calls_count=...)` 一并删除；REST 侧老调用方若仍传该字段会被 pydantic 忽略（不报错，向后兼容）。实质轮判据现为两条：①`<thought>` 解析出的 `sections` 任一非空；②用户消息含指令动词。相关测试与 `docs/CONFIGURATION.md` 的判据说明同步更新。
- **插件写请求超时：硬编码 5 秒 → 可配、默认 30 秒**（#191）：服务端写库要**同步算 embedding**（`core/trivium_store.py:1074`），对 Ollama 的超时是 30 秒——模型冷启动、CPU 推理、并发排队（本地 embedding 串行加锁）都可能几秒到几十秒。插件却只给 5 秒，而它是 fail-open 的，于是超时的写入被**静默丢弃**，第三方部署最常见的症状正是「有时候记忆没进去」。现 `PALIMPSEST_WRITE_TIMEOUT` 可配（默认 30 秒，与服务端预算对齐；思考骨架那条热路径仍显式传 2 秒）。失败与偏慢（≥5 秒）都打 `warning` 并带耗时——原来只有 `debug`，用户侧完全无痕。
- **实质轮判据 2 放宽为「填了胶囊格子就算」**（#188）：旧规则是「`<thought>` 正文 ≥400 字 **且** 落在结论字段上」，与「每轮顺手填一次」的设计意图（每轮低成本填写、会话结束合并成胶囊）冲突——实测 `type=session` 缓冲节点全库仅 4 条、`turns` 恒为 1、自动胶囊只产 3 条，模型顺手填的短轮全被挡在门外。现四问中任填一格即进缓冲。空思考的轮仍走更早的 `no_sections` 分支；闲聊轮因为拿不到骨架（trivial 不注入骨架）本就不会产生结构化思考，因此放宽不会把噪声收回来。判据 1（`tool_calls_count`）与判据 3（用户指令动词）在 REST 链路里已不可达（空 `sections` 先被 `no_sections` 挡掉），保留其意义在直接调用 `is_substantive_turn` 的场景与未来扩展。

### 修复

- **插件清单里最后一条误声明的钩子清掉**（#183）：`provides_hooks` 还留着 `on_session_end`——它与 `on_pre_compress` 同类，是 `MemoryProvider` 的**方法**（会话结束时由宿主经 provider 接口调用），不是事件钩子。上一轮 #178 只清掉了 `on_pre_compress`，校验器于是换一条 WARN 继续报（`declares hook 'on_session_end' but registration did not add it`），`plugins doctor` 因此永远到不了全绿，常驻 WARN 又把真正的注册问题盖住。现 `provides_hooks` 只留 `pre_llm_call`（`register()` 唯一真正 `ctx.register_hook()` 的钩子），两个 provider 方法改为在 YAML 注释里说明。功能侧无任何变化（会话沉淀本来就正常生效）。
- **`hermes palimpsest status` 不再打印双 `v`**（#184）：REST `/` 的 `version` 字段本身就带 `v` 前缀（`v2.17.0`），CLI 又拼了一个字面 `v`，输出成 `vv2.17.0`。改为先规整（已带 `v` 就原样用，裸版本号才补前缀），这样两种上游形状都正确，也不动 REST 字段（改它会波及所有调用方）。`-N-gSHA` 后缀是 `git describe` 的正常输出，不是问题。

### 变更

- **九条管理/批处理 CLI 命令改走 REST**（#223）：`tasks-backfill` / `review` / `consolidate` / `promote` / `ingest-git` / `fts-rebuild` / `fts-search` / `reindex` / `task-archive` 原先在 CLI 进程里自建 `TriviumStore()`，与常驻 REST 抢库的独占锁。现在改为服务端业务级端点（一次请求一个操作、不暴露逐节点写），`root()` 端点索引同步补齐，`RemoteStore` 实现对应方法；dry-run 走只读请求（不开本地只读库、不推进 generation）。`ingest-git` 的 commit 解析逻辑迁进 `core/git_ingest.py`。
- **评测增加配对显著性检验与时间切分**（#217）：`eval/significance.py` 在同一批查询上配对比较两套配置——逐查询分数取自 `run_eval.py` 结果文件，均值差用双侧中心化 bootstrap（add-one 校正）检验，并按子集（all / tune / held_out）报告百分位 CI；逐查询分数落盘保证比较可复现。`eval/split_eval_set.py` 按 gold 节点 `created_at` 做时间切分，避免「在报告用的同一批查询上调参」。纯 stdlib，无新依赖。
- **重建检索评测题集**（#221）：旧题集把裸节点 id 当 gold，2026-09-10 库重建后 id 全部错位——内容级验证显示 118 条正样本里**0 条**仍指向正确节点（此前报的「54 条存活」只查了 id 存在 + 有无时间戳，没查内容）。新增 `eval/reresolve_gold.py`（在库副本上用生产 embedding 按余弦重定位，只接受无歧义命中：top1 ≥ 0.60 且 margin ≥ 0.06，阈值取自实测分布），结果 27 条改判 / 40 条隔离 / 51 条丢弃 / 20 条负样本；`split_eval_set.py --pin-manifest` 改为**按 qid 冻结**切分，新增题目不会静默打乱既有分配。
- **`reindex` 的修复命令补齐 `--yes` 与前置条件**（#185）：`--force` 只跳「指纹不一致」那一层确认，最后那次「即将重嵌 N 个节点，确认？[y/N]」由独立的 `--yes/-y` 控制——无 TTY 的脚本 / CI / agent 里会直接 `EOF when reading a line`。指纹守卫的 `FIX_CMD` 与 `doctor` 的修复提示、`README` / `README_EN` / `docs/CONFIGURATION` / `docs/INSTALL` / `docs/USAGE` 全部改为 `reindex --force --yes`；同时补上「reindex 直连库（不走 REST，本库单写者），须先停 REST、跑完再启动」这个前置条件——原先的提示让用户在服务运行时执行，只会撞 `Database locked`。bug 报告的模板也改为：服务在跑时默认贴 `curl http://127.0.0.1:8090/doctor`，需要 CLI 时先说明要停 REST。

### 测试

- 新增 `tests/test_plugin_manifest_hooks.py`（4 条）：静态解析 `plugin.yaml` 的 `provides_hooks` 与 `register()` 里的 `ctx.register_hook(<字面量>)`，断言两者**相等**（双向：多声明 → doctor WARN，少声明 → 未声明的注册），并要求 `on_pre_compress` / `on_session_end` 这两个 provider 方法两边都不出现——把 #173 的验收标准（声明 == 注册，由测试钉住）落地。另有一条防空转基线（两边都空时不得静默通过）。`requirements-dev.txt` 加 `PyYAML`（只进开发依赖，运行时完全不碰 YAML）。
- `tests/test_session_capsule.py` / `tests/test_substantive_turn.py` / `tests/test_strategy_engine.py` / `tests/test_thought_lifecycle.py` 按统一骨架与放宽后的判据重写：解析断言改为「编号 → 四节」映射；判据 2 的正样本从「450 字长思考」换成「四问短填」、反样本换成「四问全空」；新增「三情境骨架正文完全相同、`kind` 仍分类」的断言；判据 1/3 的直接调用用例保留。`tests/test_thought_origin.py` 现有用例原样通过（旧起手式走兼容判据）。



- 新增 `tests/test_pin_domain.py`（20 条）：纯函数层覆盖域/状态筛选、`importance` 倒序 + `id` 倒序、**节点边界截断不切半条**、单条上限、预算小于头部时不注入、溢出在正文里可见；端点层覆盖默认关闭、与当前消息**零语义重叠仍注入**、trivial 轮也注入、常驻块排在语义召回块之前、与召回去重、超预算报告、store 异常 fail-open、响应里 `pin` 诊断字段形状稳定。端点用例一律用 monkeypatch 注入构造数据、**不往临时库写节点**——同 session 内临时库共享，写入会污染其它模块的计数断言（实测曾把 `test_smoke::test_consolidate_dryrun` 打红）。
## [2.17.0] - 2026-10-28

### 新增

- **思维骨架重构为「胶囊字段导向」，并改为逐轮累进会话缓冲**（#166 → #180）：骨架的每个编号问题现在就是一个胶囊字段——`code` 问「改了什么 / 失败路径 / 怎么验 / 交付物」，`research` 问「要查清什么 / 已有证据 / 反例 / 结论」，`default` 问「要什么 / 已知未知 / 该不该动手 / 产出」。`post-turn` 把当轮答案解析成结构化字段，累进该 `session_id` 的缓冲节点（`type=session`，logs 层，同一 session 只一条，条目按 `turn_index` 覆盖而非重复）；`session-end` 把缓冲合并成**一条** `type=task` + `kind=handoff` 胶囊，缓冲标 `closed`，并以 `CONSOLIDATED_INTO` 建边。宿主不传 `session_id` 时行为与旧版完全一致（不建缓冲、逐轮 `thought` 节点不变）。新增 `SESSION_MEMORY_INGEST` 开关（默认开）保留旧「会话要点」路径，关掉时以 `skip_reason=session_memory_disabled` 可观测地跳过。骨架起手式、kind 白名单与 `detect_thought_origin()` 均从骨架表派生，老文案历史节点降级为 `thought_origin=unknown` 且不抛异常。胶囊响应体**有意不含 `created` 字段**——字段存在即成功信号，调用方以 `capsule.node_id` 为准。
- **记忆置信度（evidence-based confidence）作为 ε 级 tie-break**（#159）：相似度接近时用证据量做最后一级排序，不改变主排序。
- **REST 允许本地浏览器工具跨源访问**（#162）：CORS 放行本地页面工具（如 dashboard、浏览器插件）直连 REST。
- **REST `/mem/ingest` 支持可选 `meta` 自定义元数据（MCP `mem_ingest` 同步支持）**：键值均为字符串的字典，校验通过后原样写入节点 payload 的 `meta` 子字典，供外部宿主把记忆绑定到自己的会话实体上（如 `{"chat_id": "..."}`）。结构约束（不合法拒绝写入，REST 侧 422）：键 ≤8 个、键名 `^[a-zA-Z0-9_]{1,32}$`、值为字符串且 ≤256 字符（护栏集中在 `core/meta_guard.py`）。不传 / 空字典时 payload 不含 `meta` 键，payload 形状与旧行为完全一致。
- **新 REST 端点 `POST /mem/purge-by-meta`：按「域名 + 自定义元数据」精确定位并批量删除**。典型场景：外置宿主（如酒馆 / SillyTavern 记忆桥）在用户删除聊天记录时，同步清空该聊天在记忆库里写入的节点。破坏性操作三道护栏：`domain` 必填非空（缺失 / 空串 400，不存在全库级删除）；`dry_run` 默认 true（缺省只返回 `matched_count` / `ids` / 每条 80 字 `previews` 预览，不删任何节点）；匹配数 > `max_delete`（默认 50）时 409 拒绝执行（附实际匹配数 + 预览）。真实删除逐条走节点删除链（级联清理关联边 + FTS/TDB 文本通道），单条失败记入 `failed` 不中断；每次执行写一条 `type=record` 审计节点（domain 同请求、importance 0.5、content 含 meta 键值 / deleted_ids / 时间戳），审计节点本身不带 `meta`，不会被同条件的 purge 连坐删除。

### 性能

- **每个进程只保留一条长连接，取代「每操作开-关库」**（#177）：`TriviumStore` 此前每次操作都构造并关闭一个 TriviumDB 连接——连接级排他又迫使每次窗口都要过进程锁，重开时重读侧车、重走 generation 记账，成本在每次调用上重复支付。实测（一次性库、60 次连续 insert）：复用连接 **0.1 ms/op**，`open → insert → close` **63.1 ms/op**；大头不是开闭文件（4.5 ms），而是**新连接首次操作的索引加载（约 58 ms）**，库越大越明显。现按 `(库路径, 访问模式)` 在进程内注册一条共享连接，所有 `TriviumStore` 实例复用；访问锁从「跟着连接」改为「跟着操作窗口」——代理解锁不再关闭句柄。共享连接统一 `load_text_index=True`（`index_text` 是文本索引唯一写入口，未加载内存态就写 + flush 会把 `.text` 侧车洗成单条），当前文本通道默认关闭故成本可忽略，代码内注明侧车长大后需重测。同一路径请求第二种访问模式时抛 `AccessModeConflictError`（消息含原因与出路），不再被索引初始化的兜底分支静默吞掉。共享连接在 ASGI shutdown 与 `atexit` 时先 `flush()` 再关闭，flush 失败只记日志、不阻塞退出；`db_health` / `dims` 等探测方改为复用共享连接，避免「自己占着自己」误报 busy。**这是性能改造而非缺陷修复**：既有进程锁已保证并发正确性（改造前实测 20/20 命中、0 静默空结果）。

### 修复

- **插件清单的 hooks 声明与实际注册不符**（#178）：`plugin.yaml` 声明了 `on_pre_compress`——它是 MemoryProvider 的**方法**（压缩前由 provider 接口调用），不是事件 hook，导致 `plugins doctor` / `plugins validate` 报 `unknown hook 'on_pre_compress'`；而真正注册的 `pre_llm_call`（喂当轮思考骨架）却没声明。校验器比对的是 `provides_hooks` 字段（不是 `hooks`）。现改为 `provides_hooks: [on_session_end, pre_llm_call]`，provider 接口不变。

### 变更

- **embedding 进程内化后，停止为 Ollama 加守卫**（#161）：Ollama 不再是必需依赖，健康检查与看门狗不再以它为对象。

### 依赖

- **TriviumDB 0.9.0b2**（#165）：并用真实现场库（`11.4 MB` 文本侧车 / 2877 节点）复现并修复了 0.8.11 下 `flush` / `build_text_index` / `close` 全部失败且重开后锁死的问题；4 个开库点显式 `auto_build_quiver=False`（0.9.0 起 dim=1024 在 652 节点即自动构图，本库 3000+ 节点会长期处于自动构图状态，而当前检索不依赖 QuIVer）。

### 测试

- 新增 `tests/test_singleton_connection.py`（8 条）：同进程共享连接身份（跨调用 / 跨 store 实例 / 关窗后）、并发检索零空结果（带死锁守卫）、并发写零失败且 ID 唯一、访问模式冲突双向报错且消息含约束与出路、代理 `close()` 幂等且锁释放平衡、`shutdown()` 先 flush 再 close 且之后可重建、`load_text_index=True` 与 `auto_build_quiver=False` 确实抵达构造函数。
- 多写者 / DB-busy 系列测试改锚：单例后「同进程第二个连接」不再失败，用例改为用绕过注册表的裸连接模拟**另一个进程**——跨进程独占语义不变。
- `tests/test_session_capsule.py` 按缓冲-合并链路重写（骨架解析 / 逐轮累进 / 合并胶囊 / 兼容开关 / 无 session_id 旧行为）。
- 新增 `tests/test_meta_tag_purge.py`（12 条）：meta 写入与非打标旧行为、三项结构校验拒绝（非法键名 / 值超长 / 键过多）、MCP 与 REST 同构共享写入路径、purge 的干跑缺省基线 / 精确命中删除 / 误删防护（同 meta 不同 domain 不删、同 domain 不同 meta 不删）/ 超限 409 一个不删 / 关联边级联清理 / `type=record` 审计节点且不带 `meta`。

## [2.16.2] - 2026-10-27

### 修复

- **本地 embedding 并发调用会把整个进程 abort（无异常、无 5xx）**：`_LOCAL_LLM_LOCK` 此前只护住懒加载，真正的 `_LOCAL_LLM.embed(text)` 调用在锁外。REST 的 26 个端点里有 25 个是同步 `def`（Starlette 放进线程池执行），所以请求是真并发——多个线程同时进入同一个 Llama 实例的计算图，触发 C 层 `GGML_ASSERT` 后直接 `abort()`，客户端只看到连接断开。现把推理调用纳入同一把锁（加载路径在到达前已退出自己的 `with` 块，非重入锁不会死锁）。实测：10 个并发检索全部 200（各 3 条结果）、事后服务存活、日志中 `GGML_ASSERT` 为 0；代价是串行化——单次 0.56s，10 并发墙钟 3.61s。`--workers N` 不是替代方案（库文件被 triviumdb 独占打开，多进程直接 `Database locked`）；上游已报 `abetlen/llama-cpp-python#2392`。
- **`thought` 节点类型两边都没登记，落进「未登记类型默认归 facts」的兜底**：`thought` 既不在 `TIER_FACTS` 也不在 `TIER_LOGS`，于是 388 个思维快照节点挤在默认检索 / 注入池里，而 `tier=logs` 一条都取不到——与设计正好相反。现将其并入 `TIER_LOGS`（与 record / event / git_commit 同层），并新增 `KNOWN_TYPES` 列出代码实际写入的全部类型；`tests/test_tier_registry.py` 用 ast 解析 `config.py` 的字面默认值做守护（防 CI 环境变量把默认值掩盖成假绿）。生产库实测：`tier=logs` 现在能取到 thought、默认层不再返回、不过滤通道无回归。

### 文档

- 新增 `docs/USAGE.md`：入口选择、记忆生命周期，以及「该记什么 / `importance` 怎么给 / `type` 怎么挑 / 何时声明取代」的判断手册，含四个日志层类型（record / event / thought / git_commit）与反模式清单；README 两处挂链。
- README 按「agent 优先」重排：`What it is` → `Install`（5 步，每步给成功判据，单一默认路径）→ `What it solves` → `Why not the others` → `When it breaks` → `Report an issue` → `Deep dive`；原有内容全部保留后移，无内容丢失。
- 修坏链：`docs/BITEMORAL.md` → `docs/BITEMPORAL.md`。

## [2.16.1] - 2026-10-26

### 修复

- **启动自检的「Embedding 服务可用」没有跟着 provider 分派**：`_check_embedding()` 此前只有 `openai` 和「其它」两条路，`local` 被 else 兜底当成 `ollama` 处理——于是内置引擎也在探测 `11434` 的 `/api/tags`。后果是**关掉 Ollama 后自检误报「Embedding 不可用」**，而检索实际完全正常，与 2.16.0「零外部依赖」的承诺直接冲突。现改为按 `EMBEDDING_PROVIDER` 三分支：`local` 直接实算一条向量（依赖未装 / 模型缺失 / 维度配置错都在这里如实暴露），`ollama` 保留「先探端口再实算」（端口不通时不必等满 embed 超时），`openai` 直接实算；三条路最终都以「非全零向量」为准——HTTP 可达不等于能用。
- 失败文案按 provider 分开：`local` 现在给出带 `--extra-index-url` 的完整安装命令和模型文件指引，不再把内置引擎的问题说成「Ollama 没启动」。

## [2.16.0] - 2026-10-26

### 新增

- **内置本地词嵌入后端（`EMBEDDING_PROVIDER=local`）**：用 `llama-cpp-python` 在**进程内**跑 GGUF，不再依赖 Ollama 常驻服务，`ollama` / `openai` 两个后端保留为可选。三个后端共用同一套 `embed_text()` 分发，`reindex` / `reembed` / REST 侧无需区分。
  - 模型获取：`core/model_fetch.py` 负责路径解析与首次下载——ModelScope 优先（国内直连）、HuggingFace 兜底；**不用 `hf-mirror.com`**（实测它对 `Qwen/Qwen3-Embedding-0.6B-GGUF` 返回 308 永久重定向回官方，等于白绕）。下载带 `Range` 断点续传、大小 + sha256 双校验、`.part` 原子落盘；校验不通过立即删除半成品，不留「文件在但内容坏」的坑。已装 Ollama 的用户可用 `EMBEDDING_MODEL_PATH` 直接指向已有 blob——两处是同一个文件（sha256 `06507c7b…`，639150592 B，Q8_0）。
  - **pooling 必须 `LAST`**：该模型基座是 decoder-only 因果 LM（`Qwen/Qwen3-0.6B-Base`），只有末位 token 见过整句。实测同一 GGUF 下 `last` 与 Ollama 基准 cosine **0.99942**；`mean` 0.755、`cls` 0.341——不是模型差，是取错了位置（官方 `last_token_pool` 亦如此，且需按 `attention_mask` 取最后一个**真实** token，批量推理右侧 padding 会踩空）。
  - 输出**由本实现自己做 L2 归一化**（不依赖 llama.cpp 的 `normalize` 参数，版本行为不稳），与 Ollama 输出尺度对齐；实测三条文本 local 侧模长均为 `1.000000`，与 Ollama cosine 0.99934–0.99953。
  - 换引擎不换模型：语义等价（0.999+），但两后端的读写**不可混用**（分数密集时千分之一扰动即可改排序，混用 top-5 重合仅 66%），落地方案是一次性全量 reembed（实测 2880 条约 40 分钟）。
- **向量空间指纹守卫 + `reindex --force`**：库内记录一条运维记录（`source=embedding-fingerprint`，upsert 保证全局至多一条，多余的历史残留自动清掉），标识 `space_id = sha256(provider|model|dim|pooling)[:16]`。三处比对、三种力度：启动自检只打一行 WARN 且**不改退出码**（否则升级后服务起不来）；`doctor` 第 9 项「向量空间一致性」判 **FAIL** 并直接给出修复命令；`reindex` 开跑前比对差异并要求确认（`--force` / `--yes` 跳过）。
  - **引擎版本单独记录、不进 `space_id`**：换引擎不改变向量空间（实测 cosine 0.9994），若也进哈希，则每次 `pip install --upgrade` 都判红，告警很快变成狼来了。故 `engine`（如 `llama_cpp:0.3.36`）只用于**软提示**，且任一方探测不到（`unknown`）时静默。
  - **`reindex --force`** = 放弃断点 + 跳过指纹确认 + 全量重算，完成时写入新指纹；`--restart` 的旧语义（只放弃断点）不变，`--force` 是其超集。
  - 指纹**只在零失败且未被 Ctrl+C 中断时写入**：向量只重算了一半却盖上「已完成」的章，等于让守卫失去意义。
  - 已知代价：存量库升级后首次 `doctor` 必然 FAIL（库内还没有指纹记录），按提示跑一次 `reindex --force` 即转为 PASS。
- **TriviumDB BM25 中文文本通道（`TDB_TEXT_ENABLED`，默认关）**：在 FTS5 trigram 之外增加第二条中文召回通道（TriviumDB TEXT BM25，2-gram 分词）。动机来自副本库实测：FTS5 对**短中文 query**（2 字会退化成 rowid 顺序的 LIKE）和**长短语**（6~8 字被当整句匹配，召回近乎为零）都失灵，而 TDB 2-gram BM25 在这些场景 top-3 全中。
  - 读通道上线时只认识 `rebuild()` 的快照，此后写入的记忆全都看不见——补上写侧同步，接在 FTS 已有的同一批 hook 上（ingest / update_content / delete / consolidate / task_archive / REST 删改），`type=record` 的状态日志节点也一并纳入。同步失败只告警，与 FTS 侧行为一致。
  - **性能护栏 `TDB_TEXT_FTS_FLOOR`（默认 3）**：该通道每次查询都要重开 store 并加载约 11MB 的 `.text` sidecar，实测 260~480ms，端到端从 0.9s 涨到 1.5~3.5s，而召回提升只发生在 FTS 本来就吃力的 query 上。故 FTS 命中数达到该下限时**整条通道跳过**（不 import、不查询、不连库；`0` = 永不跳过）。跳过以 `channels["tdb_text"] == "skipped"` 上报，与 `degraded`、真空结果三者可区分；每条结果另带 `meta.tdb_text_hit`，便于把贡献归因到单条而非只到通道级。
  - `TDB_TEXT_ENABLED=0`（默认）时**输出与改动前逐字节一致**——新通道不改变任何既有行为。

### 变更

- **默认 embedding provider 从 `ollama` 改为 `local`**：不设 `EMBEDDING_PROVIDER` 且无云端 key 时走内置引擎，新用户不再需要先装 Ollama。显式设置始终优先（完全向后兼容）。**存量库升级需重嵌全库**——向量空间标识变化由上面的指纹守卫在启动/体检时点名，一条 `reindex --force` 即可对齐。

### 修复

- **服务守卫在 Ollama 桌面版持有端口时让路**：Windows 桌面 app 会自己拉起并管理 `ollama serve`。在它启动的那段窗口里端口还没开始监听，守卫探测为「down」于是起了第二个 `serve` 抢走端口，导致 app 每次重试都 bind 失败、约每秒弹一次窗口。现在启动前先查桌面进程是否在场：在场则记一条 `skipped_desktop_app` 事件并**完全静默**（「app 在管它」不值得每两分钟报一次）；探测按字节匹配 `tasklist` 输出（本地化编码，无匹配时打印的是 notice 而非空），任何探测失败都返回 False——坏掉的探测永远不能阻断一次合法启动。

### 依赖

- **新增可选依赖 `llama-cpp-python>=0.3.36,<0.4`（extras：`local-embed`）**：不装也能用 `ollama` / `openai` 后端。⚠️ 它在 PyPI **没有预编译 wheel**，`pip install` 会走源码编译（要求 CMake + MSVC，多数机器失败）；须走官方 CPU wheel 索引 `--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu`（README 已写明）。上限钉在 `<0.4`：`pooling_type` 常量与 `embed()` 签名属可变更的 API 面，大版本升级须先在隔离环境验证向量一致性（同文本新旧引擎 cosine ≥0.999）再放宽。
- **triviumdb 回退并钉死 `0.8.10`**：`0.8.11` 的文本索引校验（`src/storage/file_format.rs::read_deferred_text_index` 用主库记录的 `.text` 长度 / CRC 校验磁盘文件）与本版「`load_text_index=false` 时 `flush()` 保留已有 `.text` 文件」的写入语义冲突，会导致**整库写操作全部失败**——报 `ImmutableArtifactError: 未加载的文本索引缺失、损坏或与主数据不匹配`，而读取、`node_count()`、`/doctor` 全部正常，因此升级时的冒烟检查（含 `/doctor` 八项全绿）发现不了。实测同一库、同一时刻：`0.8.10` 读写全部正常，`0.8.11` 任一写操作（`insert` 之后的 `flush()`、`build_text_index()`）必失败，可来回切换 100% 复现。上游已报 [YoKONCy/TriviumDB#72](https://github.com/YoKONCy/TriviumDB/issues/72)——`0.9.0-beta.1` 仍复现，且一次失败的 `flush()` 会留下不一致的文件组、使库**无法再次打开**。恢复写入的方式是把 pin 退回 `0.8.10`；上游修复前不要升级。`0.8.10` 下依赖 `>=0.8.11` 文本索引语义的 `tests/test_tdb_text_channel.py` 保持原有 skip 行为。

### 文档

- **安装反馈闭环 + PyPI 同名警示**：install 段改为从 `git clone` 开始（此前没说源码从哪来），明说 PyPI 上的 `palimpsest` 是**另一个项目**（2023 年的 YAML 角色卡阅读器，此后未更新），否则 `pip install palimpsest` 会静默装错；新增「装不起来怎么办」把 `doctor` 放在第一位，再引导去开 issue 并附完整输出。issue 模板把 `doctor` 输出设为必填——它逐项列出失败原因和修复命令，比「起不来」有用得多。
- 新增 `scripts/github_traffic_snapshot.py`：GitHub 只保留 14 天流量且不暴露访问者身份，故改为每天本地累积一条 JSON 记录。

### 测试

- **检索探针脚本入库**（`eval/` 与 `scripts/`）：6 个只读探针 + Gate 0 报告——实体锚定图谱覆盖审计（结论：图太稀疏，多跳扩散吃不到增量，应先做写边）、冲突阈值 0.75→0.85 回放对照、真实标识符扫描、标识符上下文抽验、日期误判损失量化、跨类型节点抽样。脚本里硬编码的本机路径改为从 `__file__` 派生，任何 clone 都能跑。

## [2.15.0] - 2026-10-22

### 新增

- **精确标识符查询专用 FTS 权重（`RRF_FTS_EXACT_WEIGHT`，默认 `1.5`）**：query 含 commit hash（7 位以上且含数字的十六进制 token）/ `#PR号` / `工单号` 时，本次混合检索的 FTS 侧权重由 `RRF_FTS_WEIGHT`（`0.1`）提升到该值。原因：这类 query 的语义通道天然失灵（hash 与目标节点的中文正文不相似），而 RRF 下 FTS-only 命中的上限是 `w/(k+q)`——`0.1` 时永远排在语义候选之后（实测：目标节点在 FTS 通道排第 2 名，最终 `top_k=60` 全 MISS；`w=0.5` 同样出局，下限约 `0.94`）。**为什么不是 1.0**：`1.0` 恰好等于语义权重 `RRF_SEM_WEIGHT`，会让 FTS 第 0 名与语义第 0 名拿到完全相同的分数（都是 `1/(K+0)`），胜负取决于实现顺序——离线 22 条标识符 query 实测 `w=1.0` 时 `recall@5` 0.356 / `MRR@10` 0.636，`w=1.05` 时 `MRR@10` 即达 1.000，`w=1.10` 时 `recall@5` 0.690 并**完全饱和**（`1.1`~`3.0` 指标相同）；取 `1.5` 是为留余量抵抗库分布与 embedding 版本漂移。判定保持保守（`core/search.py::is_exact_identifier_query`），纯自然语言 query 行为完全不变；`/mem/hybrid-search` 的 `channels` 现透出 `exact_query` / `fts_weight`，便于线上排查走了哪条路。

### 修复

- **会话接续点排序改按时间，不再按 `importance`**：`_order_handoff_first` 此前以 `importance` 为主键，历史接续点（写入时往往被给了更高 importance）会长期压住新写入的那条——`tasks_active(include_handoff=true)` 的首条因此可能是过期上下文，「新会话先读这条」的语义随之失效。现改为 `created_at` 降序主序 → `importance` → `id` 兜底；`created_at` 缺失时以 `id` 作时间代理，兼容存量节点。
- **`PUT` / `PATCH /memory/{id}` 统一走任务写路径**：此前 `PATCH` 直连 `store.update_payload`，绕过 `apply_task_patch`——`type=task` 节点的状态变更因此不补 `last_touched_at`、不写 `type=record` 日志节点、不建 `LOGS` 边，属「状态无痕变更」。现两个端点合并到 `_apply_payload_update`，响应字段与 PUT 完全一致，`PUT` 原行为不变。

### 文档

- **记忆包（MemoryHub）使用文档**：新增 [`docs/MEMORYHUB.md`](docs/MEMORYHUB.md)，说明格式定义（`manifest` / `nodes` / `edges`）、三步用法（只读解开 → 预览决策 → 显式选中入籍）、六条契约**及其理由**、敏感扫描两档规则与三条如实边界（不加密 / 不去重 / 不校验语义）。配套可直接跑的示例包 `docs/examples/memoryhub-sample.json`（6 节点 / 3 边，纯虚构内容，其中一条刻意放了假 key 用于演示敏感命中）与生成脚本 `scripts/gen_memoryhub_sample.py`——指纹由仓库自身的 `pack_fingerprint` 计算，保证示例与真实包格式一致。`README.md` / `README_EN.md` 的 MCP 工具表下方补了指引。

### 测试

- **融合权重 A/B 评测框架（`eval/fusion_ab.py`）**：每题**只采集一次**两路原始排名（语义 top-N 带分数、FTS top-N 带 bm25 rank），多套融合方案在内存中对同一份排名重排——避免「每个方案各走一遍线上检索」把 embedding 调用、FTS 降级状态、库的瞬时数据引入 A/B 差值。配套 `eval/gen_fusion_set.py`（gold 口径 = FTS 字面枚举 ∩ 当前可见，不依赖排序）与 `eval/probe_exact_misjudge.py`（标识符判定的误判探针）。题集的人工部分含私有内容与具体 node id，**不入库**，仓库只保留脱敏模板 `eval/fusion_set_manual.example.json`。同时标注 `eval/eval_set.json` 的 gold 已整体失效（抽查 5 条，1 条 gold 节点已不存在，其余语义错配），后续评测统一走新流程。

### 变更

- **`triviumdb` 0.8.9 → 0.8.10**：零迁移（主文件 v9 / flush marker v3 / `.quiver` v1 均不变，无需导出重建）。上游本版内容：TQL 递归防护（标量表达式与嵌套解析共用 128 层预算，覆盖括号、函数参数、一元运算与嵌套子查询）；QuIVer 构图连通性（同签名近邻挤占时保留其他签名候选，收尾连接入口不可达区域）；发布中断恢复（写新事实前持久化旧代独立备份与 TPUB v1 撤销日志，可写打开先恢复未提交发布再回放 WAL）；WebUI 图探索与 AI Copilot。同步 `README_EN.md` / `docs/TDB-能力盘点.md` 的版本引用；`tests/test_storage.py` 的 `package_version` 断言改为与安装版本动态比对（钉字面量会让每次依赖升级都假红，而该断言的意图是「`storage_info` 报的是真装的那版」）。
- **`LocalStore` / `RemoteStore` 与 store 协议对齐**：补齐两个实现对 `protocols.StoreProtocol` 的契约一致性（内部重构，对外接口与行为不变）。

## [2.14.0] - 2026-10-19

### 新增

- **活跃任务列表的新鲜度维度（`fresh_days`，默认 30 天）**：`GET /tasks/active`、MCP `tasks_active`、CLI `tasks active` 新增 `fresh_days` 参数。判据时间取 `last_touched_at` 与 `created_at` 的**更早者**——存量任务的 `last_touched_at` 会被批量回填刷成同一天，单看它会让建了很久的老任务显示成「刚动过」而永不退场。超过窗口的条目不再逐条返回，汇总进返回体 `stale`（`count` / `oldest` / `hint`）；传 `fresh_days=0` 展开全部（兼容旧行为）。真实库实测：默认返回从 19 条降到 10 条（折叠 9 条，最老 52 天）。
- **会话接续点在活跃列表里置顶**：`include_handoff=true` 时，`kind=handoff` 的条目按 `importance` 倒序排在所有普通任务之前（此前与普通任务混排，会被状态优先级压到后面）。接续点同时豁免新鲜度折叠。
- **治理哨兵自动降级僵尸任务**：`scripts/palimpsest_gov_daily.py` 迁入仓库（`PROJECT_DIR` / `VENV_PYTHON` 改为从脚本自身位置派生，不再写死绝对路径）。`doing` / `blocked` 超 30 天未动 → 自动降级（`task_state=blocked` + content 前缀 `[待复核]`，经 `PUT /memory/{id}` 复用 `apply_task_patch`，自动写 `type=record` 日志 + `LOGS` 边，可回溯）；超 60 天**额外**输出归档建议清单（不执行删除，归档仍由人拍板）。幂等：已带 `[待复核]` 前缀的不重复降级。

### 修复

- **`last_touched_at` 初始语义**：`mem_ingest` 写 `type=task` 时把 `last_touched_at` 初始化为 `created_at` 同一刻（ISO，与 `apply_task_patch` 同格式），只在缺失时补、不覆盖已有值。此前该字段只在状态变更时才写，写入后从未变更过的任务恒为 `null`，无法用作新鲜度判据。

### 变更

- **`triviumdb` 0.8.8 → 0.8.9**：零迁移（存储格式与 0.8.8 一致）。QuIVer 向量内核换为无训练 SM-L2（默认启用）；无索引 `FIND ... LIMIT` 早停修复。同步 `README_EN.md` / `docs/TDB-能力盘点.md` / `tests/test_storage.py` 的版本引用。
- `list_active_tasks` 返回体新增 `stale` 字段（无超期项时为 `null`）；brief 条目新增 `created_at` 字段。

## [2.13.0] - 2026-10-18

### 新增

- **检索 query 上限护栏（`MEM_SEARCH_MAX_QUERY_CHARS`，默认 `8000`）**：`/mem/search`、`/mem/hybrid-search` 超长 query 直接 422 拒绝，`/lifecycle/pre-turn` 内部召回 fail-open 但明确标 `skip_reason="query_too_long"`（不静默吞）；阈值与写入侧 policy 同源（`core/policy.check_search_query`），不在端点写魔数。embedding 错误判别同步拆开：模型侧明确报「exceeds the context length」→ 新异常 `EmbeddingInputTooLongError`（REST 422「输入超出 embedding 模型可处理长度」），连接失败 / 超时仍为 `EmbeddingUnavailableError`（503、文案不变）——超长输入不再被误译成「Ollama 没启动」。
- **post-turn 同轮去重**：同一 `(session_id, turn_index)` 的重复提交不落第二条 `type=thought`，返回 `stored:false` + `skip_reason:"duplicate"`；判重依据落在节点 payload（`source` + `turn_index`）——库内判定，跨进程 / 重启窗口依然有效；`turn_index=0`（宿主未提供真实序号）不判重，老宿主行为不变。
- **post-turn 可选 `domain` 字段**：宿主可把本轮思考底片 / 用户原话沉淀落进自己的域（老宿主不传 → 隐式 `general`，行为不变）。
- **思维底片来源标记（`skeleton_kind` / `thought_origin`）**：落库的 `type="thought"` 节点 payload 新增两个互补字段，回答「这条思考从哪来」。`skeleton_kind`（**事实字段**，宿主上报）：本轮注入的思考骨架类型（`""` 未注入 / `code` / `research` / `default`），宿主插件随 `POST /lifecycle/post-turn` 上报、白名单外归空串、每轮重置（本轮未走给侧则上报空串，不残留上一轮的值）；`thought_origin`（**启发式字段**，本体判定）：按落库文本开头是否为骨架引导行判定 `skeleton` / `native` / `unknown`——起手式直接从骨架模板派生（不另立一套），全/半角标点归一后比对。**诚实边界**：宿主侧无法区分「骨架引导出的产物」与「模型原生思考」，两字段并存的贡献是**交叉核对**——kind 说「注入了什么」、origin 说「文本长得像什么」，不一致即可查出「注入了骨架但模型没照写」，单独任何一个都不声称精确归因。任何写入通道写 thought 节点都自动带 `thought_origin`（新节点无需回填）；存量节点用 `python scripts/backfill_thought_origin.py` 回填（默认 dry-run、`--apply` 才写、复跑幂等 0 变更；`skeleton_kind` 为给侧上报的事实字段，存量无从得知，不回填）。
- **Hermes 插件侧接入（`hermes-plugin`，纯传输零判定）**：给侧注册 `pre_llm_call` 钩子——每轮 LLM 调用前向 `/lifecycle/pre-turn` 取骨架，**包一句 `<thought>` 指令前缀后返回**（注入落在 user 消息末尾，裸骨架会被当「参考资料」忽略，实测须指令引导模型才按骨架产出；包装在传输侧——骨架文本本体侧另有 REST 直调等读取方，指令只属于注入场景）。收侧 `sync_turn` 从 `messages` 里**本轮**（最后一条 `user` 之后）assistant 消息的 `reasoning` 键（Hermes 已把正文 `<thought>` 剥进去）原样还原成 `<thought>` 块拼回 `assistant_message`，让本体既有抽取直接生效；`reasoning` 无法区分「骨架引导产物」与「模型原生思考」，仅以骨架 kind 做亲和性观察（日志），不声称精确归因。全链 fail-open（不可达 / 超时 / 空骨架 → 本轮不注入，绝不打断回复）。新增配置 `PALIMPSEST_THOUGHT_SKELETON`（默认开）/ `PALIMPSEST_THOUGHT_TIMEOUT`（默认 2.0 秒）。

### 修复

- **hybrid-search 不再把 embedding 失败伪装成「空结果」**：embedding 类异常向上抛出，与 `/mem/search` 同状态码同语义（503 / 422）；非 embedding 的真·局部降级保留结果并附程序可读的契约字段（`degraded: true` + `errors: [...]`），FTS 通道降级同理，不再只靠 `hint` 字符串。
- **隐式 `general` 节点按域取不可达**：`mem_recent(domain="general")` 此前走 TQL 精确匹配显式字段，漏掉 `domain` 缺省为空串的节点（与 `/mem/search` 的归一口径结论相反）。现改为按 `node_domain()` 归一口径筛选，同节点两条按域查询路径结论一致；其余 domain 走原 TQL 快路径不变。
- **pre-turn 早退分支补回 `session_id` / `turn_index` 回显**：trivial / slash 命令 / 纯空白 / 过短输入的响应与正常分支结构一致。
- **思维预设层在真实宿主里两头都不生效**（给侧从未注入、收侧从未落库；全链 fail-open 所以全程静默）：①给侧闸门写在 `self._enabled` 上，但宿主会把插件加载**两次**（插件加载器一次 + scoped memory provider 一次），`initialize()` 只调后者，而 `pre_llm_call` 钩子挂在前者上——`_enabled` 恒为 `False`，骨架**永远**不注入（实测：`register()` 两次得到两个实例 id，钩子调用里的 `self_id` 正是那个从未 initialize 的）。现给侧闸门只看功能开关 + cron 轮跳过（`platform=cron`），不依赖实例初始化状态。②收侧取思考时**只看最后一条** assistant 消息的 `reasoning`，而一个回合里模型有多条 assistant 消息（工具循环），写思考的往往不是最后那条（末尾正式回答常没有 `reasoning`）→ 整轮思考被丢掉（实测：注入已生效、框架也已把思考写进前面几条 assistant 的 `reasoning`，落库仍 0 条）。现从末尾往前扫**本轮**（最后一条 `user` 之后）assistant 消息，取最近一段非空 `reasoning`（兼容 `reasoning` / `reasoning_content`）。两处均有回归测试钉住。
- **会话接续点（`kind=handoff`）在活跃任务列表里隐形**：两个根因——①`kind` 此前**只有**存量回填（`tasks backfill`）会打，回填之后经 `mem_ingest` 纯写入的接续点一律缺标记，`include_handoff=true` 也捞不回；②活跃任务过滤要求生效状态 ∈ `{todo, doing, blocked}`，接续点正文首行是标记块、解析不出状态（`task_state=None`），连显式取回也被一并挡掉（生产实测两条接续点在默认 / `include_handoff` / `include_legacy` 三条列表全隐形）。现改为：**任何写入路径**（`mem_ingest`——MCP / REST / CLI 三通道同一实现——与 `apply_task_patch`，即 `PUT /memory/{node_id}`）对 `type=task` 且 content 首行以「接续点」标题词开头的节点**自动打 `kind=handoff`**（幂等；判定复用 `_is_handoff` 首行判据，不放宽 PR #112 收紧成果——句中含「接续点」的真待办不会被误标）；`kind=handoff` 的节点在活跃任务过滤中**豁免状态判定**——显式 `include_handoff=true` 时无论 `task_state` 是否可解析都能完整取回，且不塞假状态（解析不出仍如实为 `None`）；存量回填同步修正：接续点不再因状态解析不出被标 `legacy=True`（旧逻辑会把显式取回也挡掉；修正后 `tasks backfill` 重跑幂等 0 变更）。
- **接续点写入层自动补 `title`**：`kind=handoff` 写入层打标落地后，新写入的接续点仍只有存量回填（`tasks backfill`）会补 `title`——经 `mem_ingest` 纯写入的接续点在 `include_handoff=true` 的交接列表里 `title` 为空、读不出是哪一条。现 `mem_ingest` 与 `apply_task_patch` 在打标的同一判定（`_is_handoff` 首行判据）上，于 **`title` 为空时**用 `_handoff_title`（PR #112 验收过的提取逻辑，不改动）自动补一行摘要；**已有非空值不覆盖**（尊重人工与既有值，与回填「title 非空即就绪」判据一致）。

## [2.12.0] - 2026-10-17

### 新增

- **思维预设层（thought skeleton / 思维底片）**：让「思考」成为一等公民——宿主每轮模型调用前来取一份思考骨架拼进 prompt，引导模型把结构化思考写在正文里；每轮结束后本体把正文里的 `<thought>...</thought>` 段抽出来落库。思考从此可检索、可比对、可复用，且不依赖任何模型的原生 CoT 字段（写在正文 → 所有模型通用）。
  - **pre-turn 给侧（`thought_skeleton`）**：`POST /lifecycle/pre-turn` 新增返回 `thought_skeleton` / `thought_skeleton_kind`——按当前轮情境**动态**挑选（写代码给失败路径骨架、调研给证据/反例骨架、普通轮给通用骨架、闲聊/过短不给），比静态预设高一级。请求侧新增 `thought_skeleton` 开关（宿主自己另有预设时可关）。骨架判定全在本体（`core/strategy.decide_thought_skeleton`），服务端不接 LLM，只在固定模板里挑。
  - **post-turn 收侧（`thought`）**：`POST /lifecycle/post-turn` 从 `assistant_message` 抽取 `<thought>...</thought>` 段，写 `type="thought"`、`tier="logs"`（过程记录层；升格 facts/rule 留作将来的显式治理动作，`thought` 刻意不进 `POLICY_PROTECTED_TYPES`，保持可修可删），`source` 带生命周期来源标识（`lifecycle:post-turn[:session_id]`）。importance 分两档可解释：基础 0.5；思考里写下决断信号（被定下的判断 / 根因 / 纠正）→ 0.7。
  - **抽取边界行为（均有测试钉住）**：段在正文任意位置都抽得到；多段取第一段完整闭合段（一轮一条底片，`segments` 记总数）；嵌套不递归解析（取第一个闭合段，`nested=True` 供观测）；未闭合不落库（`unclosed`）；空段 / 过短不落库（`empty` / `too_thin`）；超长截断。**不命中不落库但响应可观测**（`thought.found` / `thought.skip_reason`），入库失败同理（`ingest_failed` + error 透传），不静默吞。
  - **与用户原话沉淀互不阻断**：post-turn 的两条落库路径（强信号 `record` 与 `thought`）相互独立，任一路径的入库失败不影响另一条落库；`tier_distribution` 从「只数用户原话一条」升级为**汇总本轮全部写入**（混合轮 `{"logs": 2}`，thought-only 轮 `{"logs": 1}`）。

## [2.11.0] - 2026-10-16

### 新增

- **MemoryHub 记忆包（导出 / 读取 / 入籍）**：新增一种自包含 JSON 记忆包，让记忆可以在库与库、实例与实例之间流转——**只要有本仓库，就能把包解开读取**，先当故事听，再挑着入籍。
  - **格式**：顶层 `format: "memoryhub"` + `format_version: 1`，含 `manifest`（名称、生产者、来源库、节点 / 边计数、故事背景，以及节点与边的稳定 sha256 指纹）与 `nodes` / `edges`。
  - **导出**：`scripts/export_all_data.py --memoryhub [--name ...] [--story ...]`（写完立即回读校验格式与节点数）。
  - **读取（只读）**：MCP `mem_unpack` / `POST /memoryhub/unpack` / CLI `memoryhub read`——按顺序校验格式与版本，**脏包一律友好拒绝**（JSON 不合法、`format` 不符、版本不受支持、`nodes` / `edges` 非列表，都返回带 `code` 的错误对象而非抛异常），逐条扫描敏感规则与空内容记入 `issues`，最后生成人读摘要（故事 + 每节点预览）。
  - **入籍（写回本库）**：MCP `mem_import` / `POST /memoryhub/import` / CLI `memoryhub import`——**默认只读预览**（`select` 为空时一个字节都不写，只返回可选清单与 `decision_log`）；**须显式选中**才入籍；**不沿用原 `node_id`**（新库自行分配，原 id 记入 `payload.origin_id`，来源包指纹 / 名称记入 `payload.origin_pack`）；**边只保留两端都入籍的**；**不做冲突检测**——入籍是纯新增，绝不取代本库已有记忆（避免误标 `outdated`）；逐节点原子，单条失败收集进 `failed` 后继续，不让一条坏节点毁掉整批。
  - 权责分层保持既有约定：判定（选哪些节点、payload 如何改写）归本体 `core/strategy.decide_import`，执行落库归 `core/memoryhub.apply_import`，MCP / REST / CLI 三通道只做薄壳。
- **写入判定观测（`write_judgment` 分节）**：`mem_ingest` 每次成功写入后把判定数字（`related_ids` / `replaces` / `outdated_ids` 的计数）追加到库同目录的 `judgment_metrics.jsonl` 观测文件，`mem_stats` 三入口（CLI / REST / MCP）新增 `write_judgment` 分节按 30 天窗汇总（`writes` / `explicit_replace_rate` / `related_hit_rate` / `high_similar_unreplaced_rate` / `outdated_in_window` / `by_type`），用于回看「写入方有没有做写入前判定」。**观测数据不进记忆库、不含记忆正文**（只记计数与元信息，不建节点、不进检索池）；观测写入失败静默降级，绝不影响写入主流程。
- **新增两个只读治理 REST 端点**（`POST /mem/consolidate`、`GET /doctor`）：供定时治理任务经 HTTP 调用，避免在常驻服务之外另开数据库连接。

### 文档

- **同步 2.9.0 的冲突语义变更到用户文档**：`README.md` / `README_EN.md` 的「冲突检测与版本链」特性条目、场景示例与对比表仍在描述**已废弃的默认行为**（「相似度 > 0.75 自动取代」），`docs/BITEMPORAL.md` 的写入时序与时间窗判定一节亦未标明该路径默认关闭。现统一改为「取代由写入方显式声明（`replaces`），默认不因相似度自动取代」，并把 `replaces` 补进两份 README 与 `docs/API.md` 的 `mem_ingest` / `/mem/ingest` 条目；`config.py` 的过时注释一并修正。仅文档，无行为变更。

## [2.10.1] - 2026-10-14

### 修复

- **知识关联回填会建出错误边**：`payload.linked_from` 存的是**写入当时**的切片 id，而索引重建会重新分配 id——实测生产库 **40% 的旧 id 已漂移**（其中 119 条直接指向非切片节点）。建边器「源 7」原先直接信任这些 id，回填时会建出「记忆 → 毫不相干节点」的错边。现改为**复核**：目标须**仍是切片类型**（`kb_chunk`/`novel_chunk`/`skill_chunk`）**且**原判据仍成立（源 / 目标自带向量的 cosine > 0.35）才建边。实测生产库候选从 297 降为 **165**，132 条漂移被拒；回填后 165 条 REFERENCES 边**全部指向 `kb_chunk`**。这也恢复了建边器「绝不依赖节点 id」的既有原则。

## [2.10.0] - 2026-10-14

### 新增

- **「记忆 → 知识切片」关联边（`REFERENCES`）**：`mem_ingest` 的知识关联检测此前只把命中的切片 id 写进 `payload.linked_from`（写进去之后无任何读取方，返回节点时还会被剥掉）——图谱上从无对应边，「记忆 → 知识切片」完全不可达，**知识切片不会被记忆引用**（实测生产库该类边为 0；图谱里 `kb_chunk` 只与其它 `kb_chunk` 相连）。现在：①写入时即时落成 `REFERENCES` 边（best-effort、同三元组幂等，失败仅告警）；②`scripts/build_structural_edges.py` 新增「源 7 知识关联」，历史数据用 `--apply` 回填、重建索引时自动补边。图检索与邻居扩散由此可到达知识切片。

## [2.9.1] - 2026-10-13

### 修复

- **显式取代（`replaces`）对非白名单类型无效**：`replaces` 此前在「类型白名单」检查**之后**处理，导致 `record` / `correction` / `event` 等类型即使显式声明也无法被取代（白名单本意只约束**自动相似判定**，却把调用方的显式指令一并挡掉）。现将显式取代提前到白名单之前——类型白名单与 `CONFLICT_SKIP_TYPES` 只作用于自动路径。

## [2.9.0] - 2026-10-13

### 新增

- **显式取代（`mem_ingest` 的 `replaces` 参数）**：取代改由调用方显式声明——`replaces="<id>,<id>"`（兼容中文逗号）列出的旧节点无条件标 `outdated` 并建 `REVISED_BY` 修订链，**不论相似度**。语义判断由写入方（agent / LLM）给出，不经阈值推断。REST `/mem/ingest` 同步支持 `replaces` 字段。

### 变更

- **冲突检测默认不再自动取代**：「相似」不等于「同一事实被取代」——中文同主题文本相似度天然偏高，实测同一会话分批沉淀的互补内容会被阈值互相标 `outdated`（越认真沉淀越容易被吞）。现默认只把相似候选记入 `related_ids`（话题相关提示），**不标 `outdated`**；需要取代时显式声明（`replaces`）。旧「相似度 > 0.75 自动取代」行为由 `CONFLICT_AUTO_SUPERSEDE` 开关控制（**默认关闭**，可一键回退）。

### 修复

- **`kb_search` 知识库检索恒返回空**：知识库切片已索引却搜不出来——`kb_search` 只取 `top_k` 个原始候选再后置过滤 `kb_chunk`，当记忆/技能节点在语义上更接近查询时会占满候选窗口，知识库切片被全部过滤。改为把类型过滤下推到检索层（`payload_filter`），与 `skill_search` 同款修法（该修法此前只应用于 skill 侧，kb 侧遗漏）。

## [2.8.0] - 2026-10-11

### 新增

- **结构化建边器（图谱自动成边）**：新增 `scripts/build_structural_edges.py`，从六个来源为图谱补边——①`source_path` 同源分组（`PART_OF`）②`chunk_index` 顺序（`NEXT`）③技能间相关性（`SKILL_RELATED_TO`）④小说关系（`RELATED_TO`，解析关系文档）⑤Markdown 双链（`WIKI_LINK`）⑥**会话/时序链（`FOLLOWS`）**与**名称提及（`MENTIONS`）**。支持 `--apply` / `--rollback`（批次清单落在 `data/edge_batches/`，可精确回滚）与 `--batch-id`；同一次运行内以单连接批量读写（实测 apply 从 4m40s 降至 **0.9s**）。**只读 dry-run 是默认行为**——先看预测影响面再决定是否落库。
- **建边自动化：索引重建后自动补边**：`build_kb_index.py` / `build_skill_index.py` 收尾自动调用建边器（可用 `PALIMPSEST_BUILD_EDGES=0` 关闭）。此前索引重建会新建节点但不连边，「重建完图是散的」需要人工记得补跑。
- **`FOLLOWS` 会话链与 `MENTIONS` 名称提及两类新边源**：会话链按 `created_at` 切分（间隔 ≤ `SESSION_GAP_SECONDS`，默认 1800s）为相邻节点建有向边，**限同 domain**（跨域的时间相邻只是巧合，实测 135 条跨域对无语义关联）；无 `created_at` 的节点回退 `commit_time`（`git_commit` 类型只有日期字符串，此前整类节点均为孤岛）。`MENTIONS` 由名称词典匹配而生。落地后生产库孤立率 **60.6% → 16.6%**（有边节点 609 → 1291）。
- **治理可视化面板（Dashboard）**：新增三块只读面板——**冲突链**（被取代节点与其修订链）、**遗忘曲线**（按 `0.95^(days/30) × importance` 排序的活跃记忆）、**图谱规模**（节点数 / 有边节点 / 孤立率 / 平均出度 / 边标签分布）。走既有 `GET /export` 分页 + 30 秒内存缓存，不改动写入路径。
- **检索融合预留第三路（实体锚定路）**：`_rrf_fuse()` 扩展为可接受第三个排名列表与权重（`ent_ids` / `w_ent`），**默认关闭**（`w_ent=0`），行为与两路版本逐位一致；同时把散落的「outdated / as_of 可见性判据」抽为 `_visible()`，供后续各检索路共用。为实体锚定检索预留接口，不含行为变更。

### 修复

- **会话链的收录范围与时间戳回退**：初版会话链只收录日志类节点，导致 `memory`（92% 无出边）与 `correction`（86%）几乎全为孤岛；改为收录除知识库切片外的全部带时间戳节点。另加**同 domain 约束**（时间相邻 ≠ 相关）与 `commit_time` 回退（`git_commit` 无 `created_at`）。见上「新增」第三条。
- **写策略的分级上限此前从未生效**：`POLICY_TYPE_LIMITS` 默认值为空，护栏的第三项（按 type 分级上限）实际处于关闭状态，warn 模式上线后零告警。现按实测内容长度分布配置默认值（`record`/`memory` 8000、`task` 10000、`plan`/`novel_chunk` 12000、`kb_chunk` 8000；取各类型 p90 的 3–5 倍），实测生产库 1551 个节点**零超限**。`POLICY_MODE` 仍为 `warn`——本次只让此前静默的策略项变为可观测。

## [2.7.0] - 2026-10-10

### 新增

- **写入口约束护栏（write-guard policy）**：在统一写入口（`mem_ingest`，REST `/mem/ingest` 转调同一实现）上加一层策略校验，防三类问题：①**只读节点保护**——受保护节点（`type` 属 `POLICY_PROTECTED_TYPES`，默认 `rule`，或 `payload.protected=True`）不被自动冲突检测标 `outdated` / 建 `REVISED_BY` 边；②**按 type 分级的软上限**（`POLICY_TYPE_LIMITS`）；③**配置人工闸门**（`PALIMPSEST_POLICY_UPDATE`）。默认 `POLICY_MODE=warn`——只记日志与结果字段 `policy_warnings`，**不改变任何写行为**（攒证据后再显式开 `enforce`）。见 `core/policy.py`。
- **`as_of` 历史视图查询（bi-temporal 第二阶段）**：`mem_search` / `mem_retrieve` / `mem_hybrid_search` / `mem_recent` 新增 `as_of` 参数（数值时间戳），按时间点回看「那一刻它认为什么为真」。判据 `valid_at <= as_of AND (invalid_at 缺失 或 invalid_at > as_of)`，**以时间窗为准、不看当前 `status`**——当时为真、现已被标 `outdated` 的节点会被返回（这正是历史视图的价值）。`as_of` 为空时行为完全不变。见 [`docs/BITEMPORAL.md`](docs/BITEMPORAL.md)。
- **时间窗冲突判定（bi-temporal 第三阶段，默认关闭）**：相似度 > 0.75 的两条事实若世界时间窗**不重叠**（如「十年前住北京」≠「现在住上海」），不再判为「同一事实被取代」。由 `CONFLICT_TIME_WINDOW` 控制，**默认 `false`**（高风险语义变更，验证后再开）。附带 `scripts/replay_time_window.py` 可在既有库上只读评估影响面。

### 修复

- **`test_bitemporal` 的 `valid_at` 断言不再假设单调时钟**：`time.time()` 在 Windows 底层为 `GetSystemTimeAsFileTime`，分辨率约 15.6ms 且不保证单调，两次取钟可能出现极小的回退（实测全量跑时出现 2.4e-7s 步退），令断言偶发假红。现按系统时钟分辨率留容差。

## [2.6.1] - 2026-10-10

### 修复

- **`mem_fact_history` 补上 REST 路由与包导出**：2.6.0 引入的 `mem_fact_history` 只注册了 MCP 工具，缺 `mcp_tools/__init__.py` 的公共导出与 REST 路由 —— 前者让 `main.py` 按名导入失败，后者让 HTTP 调用返回 404，违背「三接口、一核心」（MCP / REST / CLI 共用同一套底层工具）。现补 `POST /mem/fact-history`（复用 MCP 实现，不复制逻辑）、包导出与 `root()` 端点索引，并用 `tests/test_fact_history_route.py` 锁定「REST 与 MCP 返回同构」「路由已入端点索引」两条契约。

## [2.6.0] - 2026-10-10

### 新增

- **事实时间维度（bi-temporal）：记忆开始记录「事实何时为真」**：此前 Palimpsest 只记 `created_at`（记录何时写入系统）。知识有时效性——事实会变真、也会不再为真，只有系统时间就无法回答「三个月前它认为这条配置是什么」。现引入与系统时间正交的**世界时间**维度，仅作用于事实类节点（`memory` / `task` / `plan`）：写入时自动补 `valid_at`（事实开始为真，缺省=写入时刻，显式传入不被覆盖）；高相似冲突命中、旧节点被标 `outdated` 时补 `invalid_at`（=取代者的 `valid_at`）与 `expired_at`（=本次系统标记时刻，仅首次写入）。`kb_chunk` 与历史留痕类型豁免。检索结果新增 `meta.times` 透出时间字段；新增 MCP 工具 `mem_fact_history`——沿 `REVISED_BY` 出边与**反向边**（`TriviumStore.get_incoming_edges` 新封装）返回单条事实的完整时间线与取代关系（它取代了谁 / 谁取代了它）。详见 [`docs/BITEMPORAL.md`](docs/BITEMPORAL.md)。
  分期说明：本次为**第一阶段「存字段 + 反向索引」**；`as_of` 历史视图查询与时间窗冲突判定属后续阶段，不在本 PR 范围（不改动现有冲突判定语义，历史节点无字段走原行为）。
- **混合检索通道降级留痕**：RRF / 级联融合在某路检索不可用时本就静默退回另一路，但「FTS 通道挂了」与「确实没命中」在结果上无法区分——检索质量下降不留痕迹。现 `core.fts_index.search_fts_status()` 返回 `(rows, status)`，把通道状态分为 `ok`（查询正常执行，空结果即真无命中）/ `empty`（空查询）/ `degraded`（索引文件不存在或查询抛异常）；`search_fts()` 保留为只取 rows 的向后兼容包装。`_hybrid_rrf` / `_hybrid_cascade` 改用前者，FTS 降级时记 warning 并在对外结果里新增 `channels` 字段（如 `{"semantic": "ok", "fts": "degraded"}`），调用方可据此判断降级。
- **`doctor` 新增「运行时路径字符集」预检（第 8 项）**：同类项目在中文/非 ASCII 用户名路径下会因底层图库扩展创建文件失败而开箱即崩（且报错误导为 "Access is denied"）。Palimpsest 实测在该场景正常，但仍做一次预检——关键路径（数据目录 / FTS 索引目录）含非 ASCII 字符时只**提示风险**（`ok=True`），仅当路径实测不可写才判失败并给出「改用英文目录」的修复方向。

### 修复

- **技能索引在技能目录缺失时会清空全库技能节点**：`scripts/build_skill_index.py` 的 `_skill_files()` 对不存在的目录返回空列表，`build()` 随即以空 `known_paths` 执行孤儿清理，把库中**全部** `skill_chunk` 节点当作「源文件已删除」删光——一次 `HERMES_HOME` 指向不存在的目录即可静默清空技能检索。现在技能目录缺失一律 fail-fast（抛 `SkillsDirNotFoundError`，CLI 以退出码 2 结束并提示 `--skills-dir` / `HERMES_HOME` / `HERMES_PROFILE`），仅在目录真实存在并通过扫描时才执行孤儿清理；「目录存在但没有 SKILL.md」仍按合法空目录处理。
  `tests/test_skill.py` 新增回归（目录缺失不删任何节点）与「空目录仍可清理孤儿」的对照用例。
- **技能目录解析改为 profile 感知**：此前技能目录恒取 `<HERMES_HOME>/skills`。档案模式下 Hermes 会把 `HERMES_HOME` 指向 `<root>/profiles/<name>`，此时该式等价于档案目录、行为正确；但若启动器只钉了 `HERMES_PROFILE` 而 `HERMES_HOME` 停在根，则会误指向根下的技能目录。现新增 `resolve_skills_dir()`，按 `--skills-dir` → `<root>/profiles/<profile>/skills`（存在时）→ `<HERMES_HOME>/skills` → 平台默认 `<root>/skills` 的顺序解析，取第一个存在者。`tests/test_skill.py` 覆盖各档顺序与「全部缺失则报错」。

## [2.5.0] - 2026-10-09

### 新增

- **记忆策略引擎下沉本体 + 宿主无关的 lifecycle 协议**：此前「什么值得记、记哪一层、什么时候召回、怎么去重提炼」的判定住在 Hermes 接入插件（`hermes-plugin/__init__.py`）里——那是产品核心资产，却寄居在某个宿主的插件接口内：换个宿主智能即丢失，能力上限也被该宿主的钩子协议框住。现将决策整体收进 `core/strategy.py`（强信号正则、近似重复阈值、importance 分档、分层归属、trivial/长度门槛等），并暴露一组**宿主无关**的 lifecycle 端点：`POST /lifecycle/pre-turn`（决定召回什么，返回可注入 prompt 的文本）、`/lifecycle/post-turn`（决定是否沉淀 / 写什么 / 写哪层）、`/lifecycle/session-end`（会话要点提炼 + 去重 + 写入 facts 层）、`/lifecycle/pre-compress`（压缩前抽取，不写库）。每个决策都返回 `decision_log`，让「为什么召这些、为什么没写那条」可观测——判断力因此可审计、可评估，而不只是藏在代码里。
  落地要点：①分层语义保持不变（自动抓到的用户原话片段 → logs 层 `type=record`；提炼后的要点 → facts 层 `type=memory`），历史修复（#46 scope 默认 memory、#47 会话要点写 facts、#48 明确指令正则）随之下沉；②`is_trivial_prompt` 此前借自宿主的 `agent.memory_provider`，现于本体自实现，`core/strategy.py` 不依赖任何宿主模块；③Hermes 适配器压薄为纯传输（读配置、拼 payload、转发、注入结果），不再含任何阈值/正则；④MCP 工具保留冷路径（模型主动用记忆），lifecycle 协议覆盖热路径（记忆自动找到模型），二者共享同一策略引擎与存储层。测试：决策行为迁移至 `tests/test_strategy_engine.py`，适配器传输由 `tests/test_hermes_plugin_adapter.py` 锁定（含「决策符号不得再出现在适配器」的边界守卫），端到端契约由 `tests/test_lifecycle_endpoints.py` 覆盖（含 10 轮模拟会话的分层断言）。
- **压缩前图谱增强也收进本体（`POST /lifecycle/context-enhance`）**：压缩引擎适配器（`hermes-plugin/context_engine.py`）此前自己挑主题、**逐个主题发 HTTP** 查 `/mem/search`、再拼装注入文本——同属「记忆的智能」却留在宿主侧，同样受宿主钩子形态牵制。现新增 `POST /lifecycle/context-enhance`：宿主只提交「将要压缩的消息 + 保护段参数 + 耗时预算」，主题选取（`core.strategy.extract_graph_topics`）、图谱检索（**在本进程内完成**，省掉每主题一次 HTTP 往返）、注入文本排版（`core.strategy.format_graph_enhancement`）全部在本体发生，并返回 `decision_log`。适配器随之压薄为「读配置 → 转发 → 把 `enhancement_text` 合并进 `memory_context`」，不再含任何阈值/模板（有边界守卫测试防止回流）；主题数上限由 `core.strategy.clamp_max_topics` 统一收敛。
- **服务守护脚本 `scripts/service_guard.py`（防重复拉起）**：README 要求 REST 服务常驻运行，但仓库此前只提供 `start_rest.vbs`（一次性启动），没有守护——服务崩掉后不会自动恢复。新增的守护脚本守 REST 与 Ollama 两项，且**在拉起前先确认目标端口无人监听、上一次启动已过宽限期**。这道检查是必需的而非可选的：TriviumDB 以独占方式打开库文件，而 Palimpsest 采用「每操作开-关库」模式，因此两个 REST 进程同时运行会争抢库文件；抢输的写入被中途打断，会让 storage generation 不一致（`.flush_ok` 与 `.vec`/`.pld` 对不上），库从可读写退化为读不动，且不可原地修复（只能从备份恢复）。一个只看 HTTP 的守护恰好会制造这一场景——服务**正在启动**（加载 embedding、建索引）时 HTTP 无响应，被判为「已死」而再次拉起，而旧进程其实还活着。
  实现上有两处必须留意：①端口探测**先于**HTTP 探测执行——HTTP 探测会阻塞至超时并在监听 backlog 上占位，先跑它会让端口探测自身超时（实测的 false-negative，会反过来触发重复拉起）；②端口探测用 `connect_ex` 并在 `finally` 中立即关闭套接字——留下半开连接同样会占用 backlog 槽位，让后续探测误判。`tests/test_service_guard.py` 锁定端口/HTTP 判定、启动宽限期与事件记录三组契约（含用只监听不响应的桩复现「启动中」场景）。

### 修复

- **存储 generation 损坏时 fail-fast（不再静默降级）**：`TriviumStore._init_indexes()` 此前把一切异常都当「索引创建失败」静默吞掉——包括 triviumdb 的「存储 generation 损坏」（如 `.flush_ok` 与 `.vec` 不匹配）。后果是库实际已不可读写（`stats` 全空、写入回滚），`startup-check` 却五项全绿，只有 `doctor` 的向量维度 / 迁移两项才暴露，损坏因此被长期忽视、延误恢复。现新增 `core.utils._is_db_corrupt_error()`（消息标记识别，与既有锁判据 `_is_db_locked_error` 同构），`_init_indexes()` 遇损坏一律 fail-fast，与「库被其他进程占用」的处理一致。`tests/test_corrupt_error_failfast.py` 锁定该契约（含真实损坏措辞识别与「与损坏无关的索引错误仍降级」基准）。
- **写入失败路径补回归测试（`tests/test_partial_write_rollback.py`）**：此前「事务失败不留半状态」与「失败后触发健康探测」两条契约无测试覆盖。新增三例：正常写入落库（对照组）、事务中途抛异常后节点未落库且返回 `stored: False`、失败路径确实调用 `check_db_health` 并回报 `db_healthy`。测试复用 `conftest.py` 的全局临时库，不自行改写 `Config.DB_PATH`——后者是无效的，因为 `mcp_tools._common.store` 是导入时构造的模块级单例，其路径在 import 那一刻已固定。

## [2.4.1] - 2026-10-06

### 修复

- **MCP 端点 `/mcp` 无尾斜杠时返回 307，导致按文档接入的客户端连不上**：`/mcp`（不带尾斜杠）由 Starlette 的 `Mount` 307 重定向到 `/mcp/`，而多数 MCP 客户端（含 Hermes）不跟随重定向——于是按注释与 README 给出的接入方式（`url = http://127.0.0.1:8090/mcp`）连接会直接失败，表现为 503。新增 `_NormalizeMcpPath` ASGI 中间件，在路由匹配前把裸 `/mcp` 就地改写为 `/mcp/`，两种写法均直达 MCP 子应用、不再产生重定向。中间件经 `app.add_middleware(...)` 挂载而非包装 `app` 对象——后者会使其后的 `@app.exception_handler` / `@app.get` 注册全部静默失效。
  `tests/test_mcp_path_normalization.py` 锁定该契约（`/mcp` 与 `/mcp/` 均须 200；断言刻意使用 `follow_redirects=False`，否则测试客户端会自动跟随 307 导致回归「假过」）。

## [2.4.0] - 2026-10-03

### 新增

- **存储协议与客户端实现（`protocols.Store` / `client.RemoteStore` / `server.LocalStore`）**：引入依赖倒置点，为「一份数据、一个写者、多条接入方式」铺路。`protocols.Store` 只定义接口；`server.LocalStore` 是服务端对 `TriviumStore` 的包装（REST 进程专用），`client.RemoteStore` 内部走 REST HTTP（CLI / dashboard / 脚本用）。`core/` 从此只依赖协议，不再感知具体实现，也避免与 REST 形成循环依赖。`RemoteStore` 放在 `client/`（不是 `core/`）是架构纪律
- **CLI 读命令改走 REST（不再自己开库）**：`search` / `hybrid-search` / `recent` / `graph` / `kb` / `stats` / `ingest` / `link` 原先直接调用 `mcp_tools` 的工具函数，而那些函数内部使用 `mcp_tools._common.store` —— 一个模块级 `TriviumStore`。结果是 CLI 每跑一次读命令就在本进程打开一次记忆库，与常驻 REST 争抢同一份库文件；triviumdb 对库文件是连接级排他的，偶发抢不到窗口的失败正是「库偶尔坏」的成因。现在这些命令一律经 REST（`PALIMPSEST_BASE_URL` 可覆盖，默认 `http://127.0.0.1:8090`）；REST 端点内部复用的仍是同一套检索实现，故结果逐字段一致。`mcp_tools` 同时改为命令内**惰性 import** —— 它的 `_common` 在模块级就执行 `store = TriviumStore()`，仅 import 便会建出 `data/` 目录
- **`POST /mem/recent` 端点**：CLI `recent` 此前无对应端点，新增之（复用 `mcp_tools.memory.mem_recent`，不复制逻辑）

### 文档

- **明确 MCP 接入优先级（HTTP 优先，stdio 为逃生梯）**：两份 README 的「启动」一节原先把 `python mcp_server.py` 与 REST / CLI 平铺为并列选项，容易被误解为常规用法。现改为显式三层优先级：① REST 的 `/mcp`（首选，与 REST 共用进程）② CLI / dashboard / 脚本（同样经 REST）③ stdio `mcp_server.py`（**仅限完全不跑 REST 的场景**，⚠️ 与 REST 同跑会争抢同一库）。stdio 代码予以保留，定位为「REST 出问题时的独立救援通道」，而非并列入口

### 变更

- **dashboard 改为纯客户端（不再自己打开数据库）**：`scripts/dashboard.py` 原先在模块级 `TriviumStore()` 常驻并直接全表遍历，等于在 REST 之外多出一个写者——与 REST 争库，正是「库老是坏」的成因之一。现所有数据一律经主 REST 服务获取（`PALIMPSEST_BASE_URL` 可覆盖，默认 `http://127.0.0.1:8090`）；对外 `/api/*` 接口与 `dashboard.html` 保持不变。REST 不可达时返回 `502` 并附排查指引；合并功能因 REST 尚无对应业务端点，显式返回 `501` 说明原因（不再静默返回空）。注意：`/api/mem/search` 由本地 FTS 全文检索改为 REST 语义检索，排序语义随之变化

### 修复

- **Hermes 插件：修正写入层归属与召回范围（三处同根因）**：插件 v1.0.0 的写入层归属与读取过滤和分层设计不一致，三处一并修正。
  1. `sync_turn` 自动沉淀从前写 `type=memory`（facts 层），且用一套含 `启动`/`安排`/`计划`/`方案`/`优先` 等操作动词的宽关键词表匹配——安装日志、构建输出、后台进程通知都会被误判为“重要信号”而落库。现：改用独立的「明确指令」正则（只认记住/纠正/偏好/规则类），并写入 `type=record`（logs 层）——自动抓到的是未经加工的用户原话，本就不是事实。落库 payload 同时记录 `matched_keyword` / `match_pos`，并在正文超 300 字时加截断标记，使每条自动写入都可事后审计。
  2. `on_session_end` 从前写 `type=record`（logs 层），而 `prefetch` 默认 `tier="facts"`——会话要点只写不读，静默失效。现：改写 `type=memory`（facts 层），与读取侧对齐（提炼后的结论本就该回到上下文）。
  3. `prefetch` 从前用 `scope="all"`，导致 `kb_chunk` 混入注入池并挤占 `top_k` 名额（`kb_chunk` 的 `domain` 恒为 `"kb"`，对 `domain` 过滤免疫，只有 `scope` 能挡住）。现：新增 `PALIMPSEST_PREFETCH_SCOPE` 配置项，默认 `memory`（只召回记忆节点）；需要把知识库切片一并注入时显式设为 `all`。
  三处遵循同一原则：**自动抓取的对话片段 → logs 层；提炼后的结论 → facts 层；知识库切片由 `scope` 隔离**。

- **库被其他进程占用时 fail-fast（不再静默降级）**：`TriviumStore._acquire()` 遇到 triviumdb 的连接级排他错误时，改抛带库路径与处理指引的 `DatabaseBusyError`；`_init_indexes()` 不再把它当作「索引创建失败」静默吞掉——此前正因如此，「偶发写失败 → 文件组残留 → 库从可读写变读不动」会被伪装成一切正常。CLI / 脚本经既有顶层兜底输出明确指引，REST 侧新增 `503` 处理器（`detail` = 记忆库被其他进程占用）

- **Ollama 端点默认值改用 IPv4 字面量**：`OLLAMA_BASE_URL` / `OLLAMA_EMBEDDING_BASE_URL` 的默认值由 `http://localhost:11434` 改为 `http://127.0.0.1:11434`（含 `.env.example` 与两份 README 配置表）。部分系统把 `localhost` 优先解析为 IPv6 回环 `[::1]`，而 Ollama 默认只监听 IPv4，导致每个请求都要先经历一次连接超时再回落——实测单次 embedding 由数十毫秒退化为约 2 秒，检索与写入吞吐随之下降约两个数量级

- **FTS 索引与主库同步（三处同根因）**：store 层的写方法此前不负责同步自己的派生状态——FTS5 全文索引（独立于主库的 SQLite 文件）的清理责任被推给每个调用方，而调用方会漏。三处一并修正：
  1. `TriviumStore.delete_node` 只删主库节点、不清 FTS，`scripts/build_*_index.py` 三个调用点从未清理（仅靠全量 `fts_rebuild` 掩盖）。现：`delete_node` 内部同步清理 FTS。
  2. `consolidate` 经事务写入合并节点（只带向量），调用方未补索引——合并出的记忆**能被语义检索命中、却对全文检索不可见**。现：`consolidate()` 公开入口统一为新合并节点补 FTS，所有调用方自动获得。
  3. 改 `content` 后 FTS 仍指向旧文本。现：新增 `TriviumStore.update_content`，改内容时同步 FTS 并置 `vector_stale` 标记——**不**内联重算向量（重嵌是网络调用，不该塞进写路径；重嵌仍走显式 `POST /memory/{id}/reembed`）。
  FTS 失败一律非致命（仅告警，可 `fts-rebuild` 兜底），与既有 REST 端点行为一致。

### 测试

- **补齐写路径与共存的回归测试**：新增 `tests/test_fts_write_consistency.py`（delete / merge / content-update 三条路径的 FTS 终态一致）与 `tests/test_multi_writer_cli_rest.py`（dry-run 只读不写、`consolidate(dry_run=True)` 不新增节点、库被占用时抛带指引的 `DatabaseBusyError`、以及 #52 的漂移本身——改 content 后向量仍为旧值）。锁的**时序**不做断言（triviumdb 开窗行为非确定），只断言无论谁赢得竞态都必须成立的性质，避免 flaky。

## [2.3.0] - 2026-09-19

### 新增

- **Hermes 技能语义索引与检索**：`scripts/build_skill_index.py` 扫描技能目录（默认 `$HERMES_HOME/skills`，`--skills-dir` 可覆盖）下的每个 `SKILL.md`，以一个 `type=skill_chunk` 节点入库（frontmatter `name` / `description`，`category` 取相对父目录），增量按 `source_mtime`、孤儿清理、幂等；新增 MCP / REST 工具 `skill_search`（`POST /skill/search`）按语义检索技能

## [2.2.0] - 2026-09-17

### 新增

- **索引规则可配置（`core/index_rules.py`）**：`build_novel_index.py` 与 `build_kb_index.py` 的扫描范围、目录 → `kind` 映射、分块策略、payload `domain` 不再写死在代码里，改由一套声明式 JSON 规则驱动。加载优先级：`--rules <path>` > `<库根>/<legacy 文件名>`（novel 脚本兼容 `.palimpsest-novel-index.json`）> `<库根>/.palimpsest-index.json` > 内置默认。规则文件字段：`kind_map`（支持 `dir_prefix` / `filename` / `glob` 三种匹配，按声明顺序先匹配者胜）、`default_kind`、`chunk_strategy`（`file` / `heading` / `chars`）、`min_chunk_len` / `max_chunk_len`、`domain`、`include` / `exclude`、`require_frontmatter_id`。未知键只告警（向前兼容），类型或取值非法直接报错
- **未匹配路径显式可见**：`kind` 落到 `default_kind` 的文件会单列进统计新字段 `unmatched_paths` 并打印在摘要里。此前 `_kind_of()` 用 `return "character"` 兜底，把「不知道」伪装成「知道」——某库角色卡迁移目录后 77 张卡被误判为 `setting` 而脚本无任何提示，正是这条静默兜底造成的
- **`--require-frontmatter-id`（novel 脚本）**：只索引 frontmatter 含 `id:` 键的文件，其余跳过并计入未匹配
- **`examples/index-rules.example.json`**：不含任何个人目录名的规则示例，演示全部字段与三种匹配方式
- README / README_EN 新增「自定义索引规则 / Custom index rules」一节

### 变更

- 两个索引脚本的内置默认不再包含任何具体库的目录名（`01_世界观/` 之类的约定从开源仓库移除），只保留通用行为：novel 脚本「整文件 + 排除 `03_章节` / `04_草稿` / `.obsidian`」，kb 脚本「按 `##`/`###` 标题切 300~800 字符 + 排除 `.obsidian`」
- `scripts/build_kb_index.py` 的 `split_markdown()` 实现迁至 `core/index_rules.chunk_markdown()`；公开函数名与签名保留（新增可选 `rules` 参数），默认行为不变
- 返回统计新增 `unmatched_paths`（novel 脚本另加 `rules_source` / `rules_warnings`），既有键保持不变

### 测试

- 新增 `tests/test_index_rules.py`：27 条，覆盖默认加载、加载优先级（含 legacy 文件名与 `.yaml` 迁移提示）、非法配置与未知键、三种匹配方式、`is_included` 白/黑名单、`chunk_markdown` 与 `split_markdown` 的迁移等价性、内置默认可被调用方覆盖、以及「未匹配不静默归并」的回归断言

## [2.1.0] - 2026-09-16

### 新增

- **`mem_review` 支持 `tier` 参数**：复盘盘点的 `recent_ingests` 现在受 `tier` 视图约束，与 `mem_search` 同款判定（`facts` 默认 / `logs` / `""` 不过滤）。CLI 新增 `review --tier`。`tier=""` 保持改动前的输出不变
- **`mem_stats` 新增 `tiers` 分节**：按检索侧 tier 语义输出 type 分布（`facts` / `logs` / `unclassified`），并回显本次生效的 `facts_types` / `logs_types` / `default_tier`，让「哪些 type 落在哪层」自解释。`totals` 保持不变
- **`_apply_merge` 新增 `per_pair_commit` 开关**（默认 `False`）：默认整批单事务保持不变；置 `True` 时每对合并独立提交。该开关暂未通过 `consolidate()` 对外暴露
- **`docs/DEPRECATIONS.md`**（新增）：登记字段 / 接口的弃用与退役计划。首条为 `character_name`（`domain` 的历史兼容镜像），含四阶段退役步骤与验证命令

### 修复

- **`eval/run_eval.py` 四条检索路径口径统一**：`_run_fts` / `_run_vec` 此前绕过共享过滤链，`tier` 与 `outdated` 语义与 `_run_rrf` / `_run_cascade` 不一致，四种模式不可比。现四条路径共用同一套 `scope` / `tier` / `outdated` 判定，`_run_vec` 不再直调 `store.search_similar`；新增 `--tier` 暴露口径
- **应用生命周期改用 `lifespan`**：替换已弃用的 `@app.on_event("startup")`；全局 `TriviumStore` 改为懒加载（`_get_store()`），import `main` 不再作为副作用打开数据库、建索引；启动自检经 `asyncio.to_thread` 移出事件循环

### 文档

- **`readme_check.py` 三项增强**：① `config.py` 中默认值非字面量的 `os.getenv` 键必须登记，否则报 warning；② 新增 `root()` 端点索引一致性检查（与真实路由比对）；③ 文档区块标记缺失时由静默假绿改为显式 warning
- **`main.py` `root()` 的 `endpoints` 列表补全**：此前缺 `/`、`/summary`、`/report`、`/memory/{node_id}/vector`、`/graph/communities`、`/mem/stats`
- **`eval/README.md` 说明题集版本**：明确 `eval_set.json` 为当前基准，`eval_set_v1_149.json` / `manual_set.json` 为历史/手工题集且均不入库
- **`core/doctor.py` 的修复建议不再硬编码模型名**：改从 `Config.OLLAMA_EMBEDDING_MODEL` 读取（带兜底），换 embedding 模型后建议不再误导

### 技术债与整洁度

- 记忆分层常量（`TIER_FACTS` / `TIER_LOGS` / `DEFAULT_TIER`）移至 `mcp_tools/memory.py` 顶部——此前定义在 `mem_review` 之后，模块导入时求值默认参数会取不到
- `core/reporting.py` 的 `Config` 导入提到模块级；`openai` 保持函数内延迟导入并注明原因
- `mcp_tools/memory.py` 的 import 块恢复连续，去掉 8 处 `# noqa: E402`；删除两处 `if include_outdated is None` 死代码
- `core/conflict.py` 的 `_similar_hits` docstring 修正为与代码一致（召回 `max(9, 10)` 取前 3）
- `eval/pool_filter.py` 的 `should_write_output` 补上此前被忽略的两个参数检查：已有题集而新题集为空时拒绝覆盖，避免失败运行清空已存题目
- `core/dims.py` 注明对 triviumdb「打开已有库忽略 `dim` 入参」这一未文档化行为的依赖与风险
- `core/doctor.py` 的全量遍历检查注明保持理由（triviumdb 等值索引无法表达「字段缺失」判断）

### 测试

- 新增 `tests/test_eval_mode_parity.py`（四模式口径一致性）、`tests/test_readme_check.py` 扩充（三项新检查的漂移注入）、secret-scan 边界用例 6 组（邻位数字 / 全角数字 / `Bearer` 长度阈值 / 强弱规则同时命中 / `secret_hint` 形状）、`mem_review` 的 tier 过滤、`mem_stats` 的 `tiers` 分节、`_apply_merge` 的整批回滚与逐对提交

## [2.0.0] - 2026-09-15

### 破坏性变更

- **移除规则域（rule domain）**：整个规则域机制连同其对外接口一并删除——MCP 工具 `router_query`、REST 端点 `/mem/router`、Hermes 插件的 `palimpsest_router` 工具、配置键 `RULE_RETRIEVAL_WEIGHT` 与内置 `×1.3` 加权、`domain_bias="rule"` 取值、`DEFAULT_BLOCKS` 中的 `rule` 区块及 `domain_in_block` 的 rule→kb 兼容分支、`build_kb_index.py` 的规则类文档标记（知识切片统一为 `domain=kb`）、`scripts/sync_rules.py` / `scripts/check_kb_consistency.py`。**迁移指南**：调用方改用 `mem_search` / `mem_hybrid_search` 配合 `scope` / `domain` / `domain_bias` / `domain_boost`；原 `domain_bias="rule"` 传空串即可。生产库 `domain=rule` 节点为 0，故未提供兼容垫片
- **检索默认只回事实层**：`mem_search` / `mem_hybrid_search` 新增 `tier` 参数并默认 `facts`，日志层（`record` / `event` / `git_commit`）不再出现在默认结果中。**迁移指南**：需要旧行为的调用方显式传 `tier=""`；要专门查日志传 `tier="logs"`
- **Hermes 插件注入默认收敛**：`include_neighbors` 注入默认由开改为关；`PALIMPSEST_PREFETCH_TOP_K` 默认 `5` → `3`。需要旧行为的部署显式设 `PALIMPSEST_PREFETCH_NEIGHBORS=true` 与 `PALIMPSEST_PREFETCH_TOP_K=5`

### 新增

- **记忆分层（tier 过滤）**：检索侧新增 `tier` 视图，把日志层（`record` / `event` / `git_commit`，约占活跃节点四成）从默认检索与自动注入池摘出，提升检索精度、削减注入噪音。**不改存储、不迁数据**，仅在 `_mem_search_impl` / `_hybrid_search_impl` 的后置过滤加判定（与 `scope` / `domain` / `block` 并列）。取值：`facts`（默认，只回事实层）/ `logs`（只回日志层）/ `""`（不过滤，等价于此前行为——显式历史通道）。`kb_chunk` / `novel_chunk` 不入本体系（走既有 `scope` 隔离）；未登记 `type` 一律保守归 `facts`，不静默丢结果。全链路透传：MCP 工具 → REST 路由 → CLI `--tier` → Hermes 插件。语义侧与 FTS-only 侧同时受约束（否则日志层会从 FTS 路漏回）
- **注入降噪三项**（Hermes 插件）：`include_neighbors` 注入默认关（记忆域图近乎无边，原为硬编码 `True` 纯空转）；`PALIMPSEST_PREFETCH_TOP_K` 默认 `5` → `3`；注入最低相关度门槛提为可配 `PALIMPSEST_PREFETCH_MIN_SCORE`（默认 `0.3`，与原硬编码一致）。另新增 `PALIMPSEST_PREFETCH_NEIGHBORS` / `PALIMPSEST_PREFETCH_TIER` 开关

### 修复

- **任务归档幂等**：归档不再重复或漏删——归档文件 frontmatter 写入 `node_id` 作幂等键，重跑复用已有归档并补齐残留节点；写盘改为 `.tmp` → `fsync` → `os.replace` 原子替换（此前「写临时文件 → 删节点 → 重命名」在重命名前崩溃会丢归档）
- **`mem_communities` 未绑定变量**：`db` 在异常路径下未赋值即使用，触发 `UnboundLocalError`（全仓唯一漏 `if db is not None` 的位置）
- **`/report` 阻塞事件循环**：改为 `AsyncOpenAI` + `await`，全库扫描挪进 `asyncio.to_thread`，避免长报告卡住整个 REST 服务
- **`core/version.py` 版本缓存**：修正缓存后打 tag 不刷新的问题（指纹退化到 HEAD-only 时旧值残留）

### 重构

- **拆分 4 个 >100 行函数**（`core/trivium_store.search_similar` 137 行 · `mcp_tools/memory.mem_ingest` 125 · `core/stats.compute_stats` 119 · `core/consolidator._apply_merge` 100）：按单一职责抽出 helper（候选过期过滤 / 衰减重排 / block 过滤 / 统计累加器 / 事务写入 / 单对合并），主函数退回编排。**行为不变**，且不是靠「测试绿」自证：每处都在**生产库副本**上做了跨版本等价比对（`compute_stats` 输出、`mem_ingest` 两次写入（其中一次触发冲突检测）、`consolidate` 的 dry-run 与真实合并，重构前后零差异）；端到端检索评测 40 题 × 4 模式排序 **160/160 一致**（唯一差异是浮点末位 ≤2e-08，同版本重跑可复现，来自 embedding 服务而非本次改动）
- **消除 `core → scripts` 反向依赖**：`core/doctor.py` 等 6 处改为不引用 `scripts/`，依赖方向回归单向

### 工程化

- **引入 ruff 静态质量门禁**：`pyproject.toml` 新增 `[tool.ruff]`（显式写 `select` 钉住规则集，避免随 ruff 版本漂移；中文项目下忽略 `RUF001/002/003` 的全角标点误报），CI 新增独立 `lint` job，`requirements-dev.txt` 与 CI 均钉 `ruff==0.16.7`。此前仓库从未有过 lint / 类型 / 覆盖率门禁（`git log --all -S "tool.ruff"` 为空）
- **存量问题清零**（635 处 → 0）：自动修复 + 人工处理。`BLE001`（宽泛 except）与 `S110`（try-except-pass）逐处写明「为什么这里吞异常是设计」的理由；`try/except/pass` 关闭连接改为 `contextlib.suppress`；`raise` 补 `from e` / `from None` 标明异常链；未使用的导入、循环变量、模糊变量名等一并清理。全部 `# noqa` 重新生效（无失效指令）
- **引入 mypy 类型检查**（非严格起步，当前覆盖 `core/`）：目标是让已有注解变成真约束而非一步到位 strict。配套补 `core/conflict.py` 两处列表标注、`types-requests` 类型存根（此前 `requests` 报 import-untyped）
- **覆盖率基线**（`pytest-cov`，暂不设 `--cov-fail-under` 门槛）：`core/` + `mcp_tools/` 合计 **76%**（1770 语句 / 416 未覆盖）；最低为 `mcp_tools/routing.py` 22%、`mcp_tools/kb.py` 28%、`core/reporting.py` 5%。CI 输出报告但不阻断，先用数据定位缺口
- **新增 `scripts/readme_check.py` 文档一致性检查**：对齐 MCP 工具清单 / CLI 子命令 / REST 路由 / 配置项（`config.py` ↔ `.env.example` ↔ 两份 README）/ 文档内文件引用 / 行内代码配对，支持 `--json` 与 `--strict`；`docs/RELEASING.md` 已将其列入发版前置检查
- **文档一致性检查进 CI**（新增 `docs` job，`--strict`）：此前这个检查只活在发版清单里，PR 上无人拦截——唯一约束是模板里一个复选框，属自律而非门禁。现改为纯标准库的独立 job（无需安装依赖），文档与代码漂移在 PR 上直接红
- **配置项默认值纳入比对**：`check_config_keys` 原先只比对键名是否存在，改了代码默认值而文档没跟着改会静默漂移。现解析 `config.py` 的 `os.getenv(..., DEFAULT)` 字面量（逐字符括号配平取实参，`str(50_000)` 这类嵌套不会被正则截断）并与两份 README 表格第二列逐项比对；默认值为空串的键（API Key 类）要求文档写成空值标记；`.env.example` 增加反向校验（有、但代码不读 → 报警）。派生默认值（`DB_PATH` / `EMBEDDING_PROVIDER`）与 `config.py` 之外读取的键（`KNOWLEDGE_DIR`）在新代码里显式登记原因，不再静默跳过；配套新增 `tests/test_readme_check.py` 覆盖漂移注入与 `--strict` 退出码
- **静默异常留痕**：`mcp_tools/graph.py` 的「读节点 payload 失败按空处理」「读边数失败按 0 计」两处补 `logger.debug`——此前静默吞掉，出问题时没有任何线索。（其余静默点复核后确认无需补：关连接失败集中在 `contextlib.suppress`，业务失败路径本就有 `logger.warning/error`）

### 重构

- **文档（rule 退役配套）**：两份 README / CONTRIBUTING / hermes-plugin README 同步刷新，配置表新增「生效前提（Precondition）」列
- **拆分 4 个 >100 行函数**（`core/trivium_store.search_similar` 137 行 · `mcp_tools/memory.mem_ingest` 125 · `core/stats.compute_stats` 119 · `core/consolidator._apply_merge` 100）：按单一职责抽出 helper（候选过期过滤 / 衰减重排 / block 过滤 / 统计累加器 / 事务写入 / 单对合并），主函数退回编排。**行为不变**，且不是靠「测试绿」自证：每处都在**生产库副本**上做了跨版本等价比对（`compute_stats` 输出、`mem_ingest` 两次写入（其中一次触发冲突检测）、`consolidate` 的 dry-run 与真实合并，重构前后零差异）；端到端检索评测 40 题 × 4 模式排序 **160/160 一致**（唯一差异是浮点末位 ≤2e-08，同版本重跑可复现，来自 embedding 服务而非本次改动）

### 文档

- **文档与代码一致性刷新**：`README.md` / `README_EN.md` 补上此前只进了代码、没进文档的三块——`domain_boost` 域软加权、`eval/` 离线检索评测框架、质量门禁（ruff / mypy / 覆盖率）；修复英文版 CLI 表漏掉的 `reindex` 条目与两版 API Key 说明中未配对的行内代码；`.env.example` 补齐 9 个 `config.py` 已支持但模板缺失的配置项；项目结构树补 `eval/`、评测脚本与仓库根文件；`CONTRIBUTING.md` 去掉会漂移的硬编码测试数 / 工具数并新增质量门禁段；PR 模板同步检查项
- `docs/refactor_plan.md` 更新状态（原文写 P1/P2「待启动」，实际已完成）：`mcp_server.py` 882 → 49 行、`generate_report` 抽入 `core/reporting.py`、数据访问层补全完成、大函数拆分完成；`scripts/` 瘦身仍归 P3，并新增「静态质量门禁」章节

## [1.2.0] - 2026-09-11

### 新增

- **域软加权 `domain_boost`**（`mem_search` / `mem_hybrid_search` 新增可选参数）：命中节点的域与检索域一致时按加性权重提升排序，跨域候选仍保留兜底；域判定正确时与硬过滤等效，判定错误时只降权不过滤。118 题 / 同库副本 / 同候选口径 A/B：R@5 **+6.78pp**、R@1 **+9.32pp**、MRR **+8.90pp**，逐题零损失
- **`reindex` 全库向量重嵌入**（`scripts/reindex.py` + CLI `reindex` 子命令）：换 embedding 模型后一键重建所有节点向量。支持 `--check` 体检（provider / 模型 / 实测维度 / 库实际维度 / 节点分布只读报告）、`--dry-run` 预览、`--only` / `--skip` 按 payload.type 过滤、`--resume` 断点续跑 / `--restart` 从头重嵌；状态文件跟随所操作的库（`<库目录>/reindex_state_<库文件名>.json`），不污染项目 `data/`。
  - **维度红线**：预检比对「实测 provider 维度 vs **库的实际维度**」，不一致时一个字节都不写、退出码 `2`、打印换库指引（导出 → 重建 → 切换配置）
  - **退出码**：`2` 维度不匹配 · `3` 库被占用（提示先停 REST :8090 / MCP）· `4` embedding 服务不可用且未写入任何向量 · `1` 中途失败（已写部分）
  - 结束报告四类计数（重嵌 / 跳过（缺 content）/ 修复空向量 / 失败）+ 抽样节点重嵌前后向量对比；重嵌文本严格取 `payload["content"]`（与入库口径一致），不拼接元数据
- **`doctor` 部署体检命令**（`core/doctor.py` + CLI `doctor` 子命令）：复用 `startup-check` 的 5 项检查 + 向量维度一致性检查，共 6 项；每项失败给出可直接复制的修复命令，降低部署门槛
- **离线检索质量评测框架**（`eval/`）：题集抽样（自动过滤机器生成节点）+ 4 种检索模式基线 + 报告落盘，让检索改动的收益/回归可量化（此前只有主观感受）
- **`scripts/retrieval_probe.py`**：内置已验证探针的检索基线工具，用于跨版本 / 跨 embedding 模型对比
- **embedding provider 自动探测**：云 / 本地两条部署路径自动识别，配套 quickstart 文档

### 修复

- **语义主序与图扩散解耦**（`core/trivium_store.search_similar`）：图扩散结果不再直接参与主排序，仅在语义分数相同或缺失时兜底，消除图跳数对语义排序的覆盖。真实库 A/B：`mem_search` R@5 **0.4831 → 0.7458**
- **检索候选过滤 outdated 节点**：被新版取代的旧事实不再进入候选集
- **Hermes 插件不再灌入原始工具转录与重复会话要点**（`hermes-plugin`）：此前 agent 对话被整段写入记忆库，污染检索池
- **`startup-check` 自动创建数据目录**：数据目录缺失时不再直接判失败，行为与首次部署一致
- **FTS 长查询改写**（整句 trigram → 3-gram OR）：长自然语言查询（>8 字符）改写为 3-gram 均匀采样 OR 查询，实测 recall@10 从 0.0000 提升至 0.5116；端到端 rrf recall@5 从 0.3333 提升至 0.4884，融合通道从「负资产」变回「正贡献」。短查询（≤8 字符）行为不变

### 移除

- **L1 嗅探通道**（`memory_file_hits`）——零消费方，MEMORY.md 每轮已注入上下文，属重复开销

### 依赖

- **triviumdb 0.8.7 → 0.8.8**：上游修复我们上报的 #54 的一半——**有序索引路径**的 `FIND ... LIMIT` 早停恢复并反超 0.8.6（同机同库 / 50k 节点：0.8.8 78.3k/s vs 0.8.6 66.7k/s）。存储格式不变（WAL v3 / payload v9），**零迁移**；升级后真实库（880 节点）`mem_search` 冒烟通过。
  - **仍存在的回归（本版复测确认）**：**无索引**路径的 `FIND ... LIMIT` 仍比 0.8.6 慢约 25-28x（1.8k/s vs 50k/s）。根因已定位——不是早停丢失，而是「短路能力」丢失：0.8.7 起 planner 把「无可用索引 + LIMIT → 惰性 `FullNodeScan`」的短路判定从 `plan_filter` 开头挪到其之后，兜底路径会先 `all_node_ids()` 物化全库 NodeId（~200KB）再丢弃，每次查询白付约 0.58ms 固定成本；另有 3 处 `EXPLAIN` 输出与实际执行不同源、且无条件宣称优化生效的问题。
  - 上层记忆服务检索走 `search()` / payload_filter 路径，**不在受影响范围**。
  - 已同步上游：issue [#58](https://github.com/YoKONCy/TriviumDB/issues/58) + PR [#59](https://github.com/YoKONCy/TriviumDB/pull/59)（短路判定前置 + 物化计数器 + 回归测试）。

- **triviumdb 0.8.6 → 0.8.7**：上游修复我们上报的 #49（v9 存储 bulk payload 读路径比 0.8.5 慢 80-437x）。存储格式不变（WAL v3 / payload v9），**零迁移**，无需导出重建。同机同库 A/B（50k 节点 / 1024 维，0.8.6 建的库逐字节复制后两版各读）：

  | 场景 | 0.8.6 | 0.8.7 | |
  |---|---|---|---|
  | FIND 复合零命中（无索引，全扫确认） | 3.370s | 0.0142s | ~237x |
  | FIND 复合零命中（composite 索引） | 3.813s | ~0s | 索引快速缺失判定恢复 |
  | MATCH WHERE + COUNT 全扫 | 4.897s | 0.0641s | ~76x |
  | MATCH RETURN n（2000 行 payload 读） | 0.445s | 0.0132s | ~34x |
  | FIND 复合真命中（索引） | 0.0013s | ~0s | 持平 |

  图/算法侧同步恢复：pagerank TQL 3.995s → 0.212s（~19x）、leiden 1.049s → 0.885s、search_advanced 认知管线 31.8/s → 120.0/s（~3.8x）、老 API expand3 8.3/s → 24.5/s（~3x）。0.8.7 附带能力：TQL 单跳边变量一等投影（`MATCH (a)-[r]->(b) RETURN r`）、服务端图探索 API、投影列元数据。

- 已知遗留（0.8.7 新发现，非本项目主路径）：无索引 / 位图路径的 `FIND ... LIMIT` 早停比 0.8.6 慢约 20x（50k 节点等值 LIMIT 10：51k/s → 2.35k/s），系 PR #50「修 composite 零命中 + 去重复 payload 读」移除 planner 的 limit 早返回所致。上层记忆服务检索走 `search()`/payload_filter 路径，不受影响；同库 A/B 探针为本地一次性脚本（未入库）。→ 0.8.8 只修了有序索引路径，无索引路径仍未修（见上）。

- **triviumdb 0.8.5 → 0.8.6**：上游大版本（tiered payloads + composable analytics + 服务端加固 + 发布治理）。存储格式 v7 → v9（payload 迁至 generation-scoped mmap sidecar `.pld.<gen>`，flush marker v3），打开旧库自动兼容、flush/close 时自动升级（MINIMUM_SUPPORTED_VERSION 仍为 5，早于 0.7.0 的文件需手动迁移）。本地零手工迁移：备份 `data/backup_20260906` → 副本冒烟（v7 打开/flush 升 v9/重开验证）→ 真实库由服务打开自动升级。我们提的 #39（SEARCH VECTOR 科学计数法解析）与 #40（FIND 范围/复合谓词慢）上游已标 solved 并验证：科学计数法 30/30 全过；复合谓词同库 A/B 2.0ms→0.6ms（约 3.3x）。测试断言随格式更新（storage_info database_format_current 7→9）

### 文档

- **检索质量评测资料**（`eval/docs/`）：6 篇探针结论文档，含 4 条否证结论及口径判据（更换 embedding、索引期 small-to-big 分块、无上下文查询改写、自动域推断）——「否证也留档」省未来弯路

### 测试

- 全量 **316 passed + 2 xfailed + 2 xpassed**（依赖 0.8.5→0.8.8 连续三次升级后无新增失败；版本断言与存储格式断言随依赖更新：`package_version` 0.8.5→0.8.8、`database_format_current` 7→9）

## [1.1.1] - 2026-09-05

### 安全

- **可选 API Key 鉴权**：默认不启用（localhost 本机直连，保持原行为）；设置 `PALIMPSEST_API_KEY` 后，除 `/` 健康检查外所有请求须带 `Authorization: Bearer YOUR_API_KEY` 或 `X-API-Key: YOUR_API_KEY`，否则 401（`secrets.compare_digest` 防时序攻击）。适用于局域网/受信网络部署；公网部署应配 HTTPS 反向代理
- **`/export` 分页**：不再一次返回全部记忆；默认每页 100 条（上限 500），返回 `page` / `page_size` / `total_pages`
- **`GET /memory/{id}` 剥离内部字段**：`secret_hint` / `linked_from` / `linked_kb_ids` / `superseded` 不再随 payload 返回
- **报错不再泄漏内部异常**：DELETE/PUT/PATCH/向量端点与 embedding 错误统一固定提示语，`str(exc)` 细节只进日志

### 修复

- **outdated 检索语义**：被新版本取代的旧记忆（`status=outdated`）不再参与普通检索——`mem_search` / `mem_retrieve` / `mem_hybrid_search`（RRF 与级联）默认只回当前有效节点，图谱邻居区同样过滤；旧版保留库中可追溯，显式 `include_outdated=True` 或 `mem_version_history` / REVISED_BY 边可查历史
- **`/export` 排序容错**：脏 `importance`（非数值）不再触发排序类型错误（`_to_float` 兜底）

### 测试

- 新增安全加固回归（鉴权双态 / 分页 / 内部字段剥离 / 固定报错）与 outdated 语义测试（同内容两次写入触发 REVISED_BY → 默认只见新版、显式通道两版可见）；全量 **161 passed + 2 xfailed + 2 xpassed**（无 Ollama 环境同样全绿）

## [1.1.0] - 2026-09-05

### 依赖

- **triviumdb 0.8.3 → 0.8.5**：上游修复 pagerank panic（#31）与 5000 行硬截断（#32），MATCH 现可全量返回（含 LIMIT pushdown）；TQL 聚合/标量 RETURN 扁平化（COUNT/SUM/AVG/COLLECT 别名直映射值，不再嵌套 payload）。本地零迁移升级（存储格式 v7 兼容）。`mem_recent` 空 domain 路径随之统一走 TQL（不再绕道 iter_payloads），排序仍留 Python 保持 `(created_at, id)` 双键倒序语义

### 新增

- **`mem_communities` 社区发现工具**（leiden 聚类）：按 `min_community_size` 过滤、按规模降序截断 `top_k`；提供 MCP 工具 + `POST /graph/communities` REST 端点；配套测试

### 修复

- **字符串布尔反转**：Hermes 插件 `include_neighbors` / `bidirectional` 参数曾被 `bool("false") == True` 反转成相反语义，现改为字符串比较判定
- **`mem_ingest` 输入护栏**：空内容与超长内容（默认上限 50,000 字符，可配置）在嵌入/扫描前拒绝写入，REST 返回 422 + 友好提示（防超大 payload 拖垮库）
- **TQL domain 注入面**：`mem_recent` domain 白名单校验（仅 `[a-z0-9_-]`），非法输入安全降级为全遍历过滤，不崩溃、不注入
- **CLI 全文泄漏**：`fts-search` 不再打印全文 content，只输出 node_id + 截断摘要；带 secret 标记的节点隐藏内容
- **`float()` 裸转统一为防御性 `_to_float`**：memory / graph / kb 多处 score/weight 读取，脏 payload 不再 TypeError
- **图遍历与建边**：BFS 改 `deque`（消除 `list.pop(0)` O(n)）；`mem_link` 主边与返回统一大写 relation（防同关系两种 label）
- **合并去重**：consolidator 同批次重复合并（如 (A,B)+(A,C) 把 A 标脏两次）用 `seen_ids` 拦截
- **FTS 同步可感知**：`sync_node` 返回 bool（成功/失败），失败仍 warning 不抛、不破坏调用契约
- **CLI 健壮性**：顶层 try/except 友好报错 + 失败退出码非 0；`cmd_ingest` 删除重复的 FTS 索引写入（`mem_ingest` 内部已同步）
- **压缩链路护栏**：Hermes 插件图谱增强加总耗时预算（默认 8s，可配置），后端不可达时整体 fail-open，不再白等

### 工程化

- 启动自检、防御惯例、输入校验等按审计建议收敛；`MEM_INGEST_MAX_LENGTH` / `PALIMPSEST_GRAPH_TIMEOUT` 新增环境变量配置（魔法数字配置化）

### 测试

- 套件含 0.8.5 适配断言（聚合扁平化、MATCH 全量）、`mem_communities` 测试、`mem_recent` 行为锁定回归；全量 **143 passed + 2 xfailed + 2 xpassed**（无 Ollama 环境同样全绿）

## [1.0.1] - 2026-08-31

### 修复

- **`PUT /memory/{id}` 数据丢失**：原实现为整包替换，部分更新会把 `content` / `type` / `importance` / `domain` 等字段清空（内部生产事故路径）。现改为**合并语义**（只更新传入字段，其余保留）；新增 `PATCH /memory/{id}` 端点（REST 部分更新语义）；更新后自动同步 FTS 全文索引，杜绝幽灵命中
- **Embedding 静默降级**：embedding 服务不可用时原实现静默返回全零向量（检索排序被污染且无告警）。现改为 **fail-fast**——抛出 `EmbeddingUnavailableError`，REST 层返回 503 并附修复指引；新增 `OLLAMA_EMBEDDING_BASE_URL` 配置项（与 LLM 的 `OLLAMA_BASE_URL` 解耦），`startup-check` 同步使用该配置
- **FTS 内容漂移巡检**：`check_fts_consistency` 从「只比对节点 id」升级为**内容级对账**（逐节点比对 content），发现并修复了存量漂移；新增 `sync_node` 统一 FTS 同步入口（PUT/PATCH/DELETE 复用）；空内容节点不再误报缺失
- **并发与边界加固**：`mem_ingest` 节点 id 分配加进程内锁（防并发同 id 覆盖）；`mem_review` 对脏 payload（非数值 importance）安全兜底；FTS 查询含双引号时走 LIKE 兜底（不再被 FTS5 语法吞掉）；知识库根环境变量统一 `KNOWLEDGE_DIR`（兼容回退 `KNOWLEDGE_ROOT`）；KB 索引增量 upsert 先写向量后写 mtime（中途崩溃下次增量可自愈）

### 新增

- `GET /memory/{id}` 端点：读取单节点完整 payload（REST 读能力补齐）
- 确定性 fake embedder 注入测试基建：测试套件不再依赖在线 Ollama，CI 不再在每台 runner 安装 Ollama / 拉取模型

### 工程化

- 清理 7 处冗余 `os.chdir`（配置已绝对路径化）；删除 `--db-path` 死选项；代码注释/文档中的个人化术语清零

### 测试

- 失败路径测试：PUT/PATCH 部分更新保留字段、缺失节点报错、embedding 失败抛错、GET 端点、含引号查询、脏 payload、内容漂移对账；套件由 51 条增至 **61 条**（无 Ollama 环境同样全绿）

## [1.0.0] - 2026-08-29

第一个正式开源版本。此前内部迭代版本（v0.x / v1.x / v2.x）不对外发布，1.0 起为对外稳定基线。

### 核心能力

- **混合检索**：语义向量（cosine）+ FTS5 全文索引（trigram，中文子串匹配），RRF（k=60）或级联两种融合模式，命中来源 `fts_hit` / `sem_hit` 透明标注
- **知识图谱召回**：节点间有向加权边（`RELATED_TO` / `REVISED_BY`），BFS 沿边扩散；弱边过滤、分区块隔离、每节点扩散条数上限
- **冲突检测与版本链**：写入时与相似旧记忆比对，被覆盖记录标记 `outdated` 并通过 `REVISED_BY` 链向新版；多层防误标（阈值/type 隔离/domain 隔离）
- **写入前敏感扫描**：强规则（API Key / token / 私钥 / Bearer 等 8 条）拒绝入库；弱规则（身份证 / 手机号 2 条）放行并打 `secret_hint` 标记
- **容量自动合并**：`mem_consolidate` 相似记忆 dry-run 预览 / apply 合并（高价值保护、`REVISED_BY` 保留）
- **任务自动归档**：完成任务写 markdown 归档到知识库 `05_任务归档/` 后删除节点
- **记忆生命周期**：时间衰减加权（`MEMORY_DECAY_FACTOR`），陈旧记忆检索降权
- **150 字摘要设计**：检索默认只返回摘要 + 元数据，全文按需拉取（省 token）
- **三接口一核心**：MCP（stdio）/ REST（:8090）/ CLI（15 子命令），共用同一套 `mcp_tools` 实现

### 架构与工程化

- **分层**：`core/`（存储与算法）→ `mcp_tools/`（MCP 工具层）→ `main.py` / `mcp_server.py`（入口）
- **单连接遍历**：`iter_payloads` / `iter_nodes` 一次数据库连接完成遍历（原 N+1 次开关，200 节点 3.68s → 0.022s，约 165x）
- **事务化写入**：`mem_ingest` 与 `consolidate` 均在单事务内原子提交/回滚，杜绝半状态
- **启动自检**：`startup-check` 5 项（关键文件 / 存储 / FTS / 依赖 / Embedding 服务）
- **依赖锁定**：requirements.txt 全版本锁定；`mcp==1.29.0`（2.x 移除顶层 FastMCP）
- **可安装**：pyproject.toml，`pip install -e .` 后 `palimpsest-cli` 命令可用
- **CI**：GitHub Actions，Python 3.10/3.11/3.12 矩阵，自动装 Ollama + embedding 模型 + pytest
- **测试**：51 条（冒烟 16 + 核心算法单测 31 + 事务 4），临时库隔离不碰正式库
- **数据一致性**：FTS 索引失败显式记录；`check_fts_consistency.py` 巡检（`--repair` 全量重建）
- **路径安全**：归档文件名防路径遍历（`..` 前缀兜底）

### 配置与使用

- **配置化常量**：图谱扩散、RRF、L1 嗅探等阈值全部可经环境变量调整
- **区块（Blocks）**：出厂内置 `task` / `kb`（含 `rule`）/ `hermes` / `general`；节点归属统一 `payload.domain` 字段（`character_name` 退役）
- **双语 README**：中文主版 + 英文版，含语言切换、CI 徽章
- **友好引导**：依赖缺失时输出中英双语安装指引（非 traceback）；未知区块提示（不拦截自定义 domain）

### 修复

- 消除全局 `os.chdir` 副作用（DB_PATH 绝对化，import 不再改进程工作目录）
- 消除 L1 魔法 ID（`-1`）——记忆文件命中改为独立字段 `memory_file_hits`
- 清理代码注释中的任务编号与个人化术语
- TriviumDB 0.8.2 升级（WAL v2→v3 迁移，导出→重建→验证→换配套→FTS 重建）

## 更早版本

更早版本（v0.x / v1.x / v2.x）为内部迭代版本，未对外发布，不在此记录。

[Unreleased]: https://github.com/JiaY-77/Palimpsest/compare/v2.17.0...HEAD
[2.18.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.18.0
[2.17.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.17.0
[2.16.2]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.16.2
[2.16.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.16.1
[2.16.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.16.0
[2.15.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.15.0
[2.14.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.14.0
[2.13.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.13.0
[2.12.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.12.0
[2.11.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.11.0
[2.10.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.10.1
[2.10.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.10.0
[2.9.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.9.1
[2.9.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.9.0
[2.8.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.8.0
[2.6.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.6.1
[2.6.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.6.0
[2.5.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.5.0
[2.4.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.4.1
[2.4.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.4.0
[2.3.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.3.0
[2.2.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.2.0
[2.1.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.1.0
[2.0.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.0.0
[1.2.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.2.0
[1.1.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.1.1
[1.1.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.1.0
[1.0.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.0.1
[1.0.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.0.0
