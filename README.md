<div align="center">

# Palimpsest

**本地优先的长期记忆系统 · AI 助手的跨会话记忆底座**

**Local-first, battle-tested, memory that never disappears.**

> _Palimpsest_：拉丁语，原指「重写的羊皮纸」——旧字迹被覆写抹去，却又在岁月里重新透出。我们把这个意象搬进记忆里：**新的事实覆盖旧的事实，但旧迹永不真正丢失**——每次改写都通过一条有迹可循的 **版本链**（`REVISED_BY`）连接，新旧记忆可查可溯。

[![Version](https://img.shields.io/badge/Version-v2.5.0-4c6ef5.svg)](/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Backends](https://img.shields.io/badge/Backends-DeepSeek%E2%80%A2Ollama-6f42c1.svg)](/)
[![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml)

**中文** | [English](./README_EN.md)

</div>

---

## 一句话介绍

Palimpsest 是一个 **本地优先的嵌入式长期记忆系统**，将 **语义向量检索（Vector Search）、加权知识图谱（Knowledge Graph）与全文检索（Full-Text Retrieval）** 三合一，把 AI 助手的跨会话记忆统一存放、管理、演化在一座本地数据库里。目标是成为 **AI 助手的「记忆底座」**——让每一次对话的收获都不再随会话关闭而烟消云散，而是**可检索、可关联、可演进**：

- 🗃️ **混合检索** —— 语义向量（cosine）与 FTS5 全文索引（`trigram` 分词，支持中文子串）经 RRF 或级联融合，命中标注来源 `fts_hit` / `sem_hit`
- 🔗 **图谱扩散召回** —— 节点由**加权边**（`RELATED_TO` / `REVISED_BY` / `CAUSES` / `REFERS_TO`）相连，BFS 沿边扩散，按最强边截断、弱边过滤、可按「块」隔离防跨域污染
- 🕸️ **社区发现** —— 内置 Leiden 聚类，一键把记忆库分成主题簇，回答「记忆库里都有哪些圈子」
- 🔄 **冲突检测与版本链** —— 高相似（score > 0.75）判为同一事实被取代，旧版标 `outdated` 并经 `REVISED_BY` 链向新版；中相似只记 `related_ids`；type / domain 双隔离防跨类误标
- 🕰️ **事实时间维度（bi-temporal）** —— 事实类记忆写入时记 `valid_at`（世界时间：开始为真之时），被取代时补 `invalid_at`（不再为真）与 `expired_at`（系统标为历史）；与 `created_at`（记录时间）正交，检索结果随 `meta.times` 透出，`mem_fact_history` 可查单条事实的完整时间线与取代关系。详见 [docs/BITEMPORAL.md](docs/BITEMPORAL.md)
- 🛡️ **写入前敏感扫描** —— 按 10 条正则规则扫描：强规则（API Key / 令牌 / 私钥 / SSH Key / Bearer 等 8 条）命中即拒写，弱规则（身份证 / 手机号 2 条）仅放行并打 `secret_hint` 供审计（详见 [`SECURITY.md`](SECURITY.md)）
- 🧹 **容量合并与记忆盘点** —— `mem_consolidate` 合并近似重复（≥ 0.85、保护高价值），`mem_stats` 盘点类型 / 域 / 重要度 / 时间 / 图谱 / 热点 / tier 分层
- ⏫ **高频记忆自动升级** —— 检索命中计数（`hit_count`），`promote` 把反复被用到的记忆升权打标（dry-run 预览、幂等可逆）
- ⏳ **记忆生命周期** —— 时间衰减加权（`MEMORY_DECAY_FACTOR`）在排序中淡化陈旧记忆而不动存储；`kb_chunk` 豁免；`outdated` 旧版默认不参与普通检索
- 📁 **任务自动归档** —— 完成任务自动写成 markdown 归档至知识库归档目录后删除节点——先 `dry-run` 预览，`apply` 提交
- ✅ **部署体检** —— `doctor` 一键体检关键文件 / 存储 / FTS / 依赖 / Embedding 可达性 / 向量维度一致性 / 运行时路径字符集，每个失败项给出修复命令（`--json` 机器可读）
- ✂️ **省 token 设计** —— 检索默认只返回 **150 字摘要 + 元数据**，完整内容按需二次拉取
- 🗂️ **记忆分层（`tier`）** —— 检索侧轻量视图，不迁数据：默认只取事实层（`memory` / `correction` / `decision` / `plan` / `task` 等），把日志层（`record` / `event` / `git_commit`）从默认检索与注入池摘出；`tier="logs"` 只取日志层、`tier=""` 回到全量
- 🔐 **可选 API Key 鉴权** —— 默认关闭；设置 `PALIMPSEST_API_KEY` 后 REST 层要求 Bearer / X-API-Key，适合局域网受信部署
- 🎯 **三接口、一核心** —— MCP（stdio 与 streamable-http）、FastAPI REST、完整 CLI 共用同一套底层工具，行为永不割裂；REST 在 `/mcp` 同时暴露 MCP 端点，可与 MCP 客户端共进程运行
- 🧠 **Hermes 双插件换脑** —— Memory Provider（语义召回 + 自动沉淀）+ Context Engine（压缩前图谱提炼），一行命令激活，记忆跨会话不丢

---

## 为什么需要它 · 使用场景

绝大多数 AI 应用同时面临三类数据能力割裂：跨会话记忆每次从零开始、知识库只能关键词匹配、记忆无序堆积越用越乱。Palimpsest 用 **一个本地内核** 同时解决「检索、关联、演进」三件事，避免在向量库、文档库、图谱库之间搬运与同步。

「记忆不丢」的一个例子：你告诉助手「服务监听 8090 端口」，后来设计变更又说「端口改为 8095」。旧记忆不会被粗暴覆盖——它被标记 `outdated`，通过 `REVISED_BY` 指向新版本，版本链查询随时能展开这条链，看清这个事实**如何一步步演变成今天的样子**。这就是 Palimpsest：覆而不失，改写可溯。

它适合四类场景：**长期陪伴 / 个人助理**（Hermes 等）把每轮对话自动召回、强信号自动沉淀，压缩前图谱再提炼一次；**知识库语义化**（Obsidian）把多年 Vault 切片向量化，搜索从关键词碰运气变成语义相关 + 图谱邻居；**创作设定库**（小说 / 世界观）整文件入库并批量建边，配合社区发现看清设定关系；**记忆治理**用敏感扫描、冲突检测、容量合并、时间衰减与 `promote`，让记忆库越用越清晰。

---

## 给 Hermes 用户：把它变成你的记忆插件

Palimpsest 为 Hermes 提供**双插件**：**Memory Provider**（记忆读写）+ **Context Engine**（上下文压缩前提炼），源码在 [`hermes-plugin/`](./hermes-plugin/README.md)，含 hooks `on_session_end`（会话结束提炼要点）与 `on_pre_compress`（压缩前图谱提炼）。

```bash
mkdir -p ~/.hermes/plugins/palimpsest && cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

激活后每轮对话自动：经 REST `:8090` 检索相关历史、高信号事实启发式沉淀、会话结束沉淀要点、压缩前图谱提炼；并提供 `palimpsest_search` / `palimpsest_ingest` / `palimpsest_link` / `palimpsest_graph` 等工具供 agent 主动调用。注意：REST 服务需**常驻运行**（如 `scripts/start_rest.vbs`）；自动沉淀是**启发式**判断，求「快、稳、不花钱」而非「聪明」。

---

## Obsidian 用户：我们的读取思路

我们不把 Vault 当「文件」看待，而是当作**知识源**：递归扫描 `KNOWLEDGE_DIR` 下的 `.md`，按 Markdown 标题切片并原样保留 `[[双链]]`，再向量化入库为可语义检索的 `kb_chunk`。就算完全不用 Palimpsest，也能照此思路用任何工具链复刻；现成实现即 `scripts/build_kb_index.py`。完整思路见 [`docs/OBSIDIAN.md`](docs/OBSIDIAN.md)。

---

## 快速上手

### 安装（通用）

```bash
# 需要 Python 3.10+
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 路径 A：云端 key，最快跑起来（无需 Ollama）

```bash
cp .env.example .env
# 编辑 .env：填入云端向量 API Key + LLM Key
#    EMBEDDING_API_KEY=你的云端key        # 留空或删除 → 自动走本地 Ollama
#    EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM  按服务商填写
#    DEEPSEEK_API_KEY=你的LLMkey         (LLM_BACKEND=deepseek 时必填)
```

### 路径 B：本地 Ollama（隐私优先，数据不出本机）

```bash
# 1. 安装并启动 Ollama（https://ollama.com）；2. 拉取向量模型
ollama pull qwen3-embedding:0.6b
# 3. 复制 .env.example → .env 并填 DEEPSEEK_API_KEY（或改 LLM_BACKEND=ollama 全本地）
cp .env.example .env
```

> **两条路径通用**：不设置 `EMBEDDING_PROVIDER` 即自动探测（有有效 `EMBEDDING_API_KEY` → 云端，否则本地；可显式写 `openai` / `ollama` 强制）。换 provider = 换向量空间，**必须重建知识库索引**（见 [更换向量模型](#更换向量模型--重嵌全库)）。

### 启动

```bash
# 推荐：部署体检（每个失败项都会打印对应的修复命令）；startup-check 为其轻量子集
python scripts/palimpsest_cli.py doctor
python scripts/palimpsest_cli.py startup-check

# REST 服务 (:8090) —— 唯一写者，所有接入方式都经它
python -m uvicorn main:app --host 127.0.0.1 --port 8090

# CLI 示例 / 监控面板 (:8010) / 索引知识库
python scripts/palimpsest_cli.py search "架构最近发生了什么变化？"
python scripts/dashboard.py
python scripts/build_kb_index.py
```

> **接入方式与单进程约束（重要）**
>
> 首选 **REST 的 `/mcp`**：MCP 客户端接入 `http://127.0.0.1:8090/mcp/`，与 REST 共用同一进程；CLI / dashboard / 各脚本同样一律经 REST。stdio `mcp_server.py` 是**逃生梯**（非首选），仅用于**不跑 REST** 的纯 MCP 场景：库文件由 triviumdb 以独占写模式打开，第二个连接会失败并报 `Database locked: ...`，并发写入失败还**会污染文件组**且不自愈。因此 REST 服务禁止多 worker / 多实例，**一个库只应有一个进程访问**；冲突是响亮的——CLI 抛 `DatabaseBusyError`、REST 返回 `503`，不会静默降级。
>
> Embedding 项失败时：本地 Ollama 请先启动并 `ollama pull qwen3-embedding:0.6b`；云端请确认 `.env` 已配置 `EMBEDDING_API_KEY`。

Windows 下 `scripts/start_rest.vbs` 可隐藏窗口启动 REST 服务（如开机自启），日志写入 `scripts/start_rest.log`。

**MCP 客户端接入（推荐：HTTP，与 REST 共进程）**

```json
{ "mcpServers": { "palimpsest": { "url": "http://127.0.0.1:8090/mcp/" } } }
```

**备份与恢复**：`python scripts/backup_db.py --keep 7` 做整文件组冷备份 + 回读校验（默认保留 7 份）。备份必须**整组**（`.db` / `.vec` / `.gidx` / `.pidx` / `.flush_ok` / `.pld.*` / `.wal`，同属一个 generation），脚本拷贝后会回读校验，不通过即报错退出。

---

## 配置

所有配置均从环境变量读取（`.env` 由 `python-dotenv` 自动加载），完整带注释模板见 `.env.example`。以下是新用户必配项，完整配置表（36 项）见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REST_PORT` | `8090` | FastAPI REST 服务端口 |
| `DASHBOARD_PORT` | `8010` | 监控面板端口 |
| `DB_PATH` | `data/mh_memory.db` | 嵌入式 TriviumDB 数据库路径 |
| `PALIMPSEST_API_KEY` | *（空 = 关闭）* | 可选 REST 鉴权；设置后除 `/` 外须带 Bearer / X-API-Key |
| `LLM_BACKEND` | `deepseek` | LLM 后端：`deepseek` 或 `ollama` |
| `DEEPSEEK_API_KEY` | *（空）* | DeepSeek 密钥（`LLM_BACKEND=deepseek` 必填） |
| `OLLAMA_MODEL` | `deepseek-r1:7b` | `LLM_BACKEND=ollama` 时的对话模型 |
| `EMBEDDING_PROVIDER` | *（空 = 自动探测）* | 向量后端：留空自动探测，或 `ollama` / `openai` |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | 本地 Ollama 向量模型 |
| `EMBEDDING_API_KEY` | *（空）* | 云端向量端点密钥（配置后自动走 `openai`） |
| `KNOWLEDGE_DIR` | *（可选）* | 知识库根目录（待索引的 Obsidian `.md` 文件） |

---

## 更换向量模型 / 重嵌全库

切换 embedding 模型会改变向量空间，**必须重嵌全库**。完整操作顺序（`reindex --check` / `--dry-run` / `--yes`）与换维度的重建流程见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。

---

## 自定义索引规则

`build_novel_index.py` 与 `build_kb_index.py` 的扫描范围、节点 kind 分类、分块策略、payload domain 均来自一套**声明式 JSON 规则**（默认内置，零配置即用），可用 `--rules` 显式指定：

```bash
python scripts/build_novel_index.py --source <vault路径> --rules rules.json
python scripts/build_kb_index.py --rules rules.json
```

**加载优先级**：`--rules <path>`（路径不存在会报错）> `<vault 根>/<legacy 文件名>`（novel 脚本兼容旧约定 `.palimpsest-novel-index.json`）> `<vault 根>/.palimpsest-index.json` > 内置默认；旧格式 `.palimpsest-index.yaml` **不会解析**，仅在警告里提示改用 `.json`。顶层键为 `kind_map` / `default_kind` / `chunk_strategy` / `min_chunk_len` / `max_chunk_len` / `domain` / `include` / `exclude` / `require_frontmatter_id`，未知键只警告、非法取值报错。`kind_map` 每条支持 `dir_prefix` / `filename` / `glob`（同条可组合，多个字段 = AND），按声明顺序先匹配者胜，全不命中 → `default_kind`（默认 `"default"`，不静默归并，未命中路径出现在 `unmatched_paths`）。完整示例见 [`examples/index-rules.example.json`](examples/index-rules.example.json)。

---

## 使用

### MCP 工具（19 个）— `mcp_tools/*`

| 工具 | 说明 |
|---|---|
| `mem_search` | 统一检索：记忆 / 知识库 / 两者；可选图谱邻居扩展、域偏置、**域软加权 `domain_boost`**、块级隔离、**记忆分层 `tier`**（默认 `facts` 只回事实层，不含 `record`/`event`/`git_commit`；`""` = 不过滤） |
| `mem_hybrid_search` | 混合检索：FTS5 + 向量；`mode=rrf`（k=60）或 `cascade`；同样支持 `domain_boost` 域软加权与 `tier` 分层；命中标注 `fts_hit` / `sem_hit` |
| `mem_retrieve` | 语义检索，返回 150 字摘要 + 元数据（绝不返回全文） |
| `mem_get_full` | 按 ID 拉取节点完整内容 |
| `mem_ingest` | 写入新记忆——含冲突检测、`REVISED_BY` 版本链、敏感扫描、长度护栏 |
| `mem_recent` | 最近的记忆（新的在前） |
| `tasks_active` | 活跃任务列表（`type=task` & `status=active`）：按 `project` / 状态集过滤，`doing > blocked > todo` → 最近触碰倒序排序；默认排除 `legacy=true` 的老节点 |
| `mem_review` | 最近 N 天的周期性回顾 + 治理候选（高价值升级 / outdated 清理 / 低价值）；`tier`（默认 `facts`）作用于 `recent_ingests`：`logs` 只回日志层、`""` 不过滤 |
| `mem_stats` | 库级盘点：类型 / 域 / 重要度 / 时间 / 图谱分布 + 热点节点；`tiers` 分节按检索侧 tier 语义分组（facts / logs / unclassified）并输出实际生效的 `TIER_FACTS` / `TIER_LOGS` 清单 |
| `mem_version_history` | 沿 `REVISED_BY` 链展开，查看事实演化过程 |
| `mem_fact_history` | **单条事实的时间线**：返回其 bi-temporal 时间字段（`valid_at` / `invalid_at` / `expired_at` / `created_at`）与取代关系（它取代了谁 / 谁取代了它） |
| `mem_consolidate` | 近似重复检测；dry-run 预览或 apply 合并 |
| `mem_communities` | Leiden 社区发现：把记忆库聚成主题簇，回答「有哪些圈子」 |
| `kb_index` | 将知识库 `.md` 文件索引为 `kb_chunk` 节点（向量化） |
| `kb_search` | 对已索引知识切片的语义搜索 |
| `skill_search` | 技能语义检索：检索 Hermes 技能（`skill_chunk` 节点），返回 name / description / category / source_path |
| `graph_neighbors` | 从某节点出发对知识图谱做 BFS（关系过滤、深度 1–3、弱边过滤） |
| `mem_link` | 手动创建图边（`RELATED_TO` / `CAUSES` / `REFERS_TO`；默认双向） |
| `mem_unlink` | 删除图边（幂等：边不存在返回 `deleted=false`；用于清理误标产生的 `REVISED_BY` 边） |

### CLI 命令 — `scripts/palimpsest_cli.py`

| 命令 | 说明 |
|---|---|
| `search "QUERY"` | 统一检索（`--scope all\|memory\|kb`、`--neighbors`、`--block`） |
| `hybrid-search "QUERY"` | FTS5 + 向量混合检索（`--mode rrf\|cascade`） |
| `ingest "CONTENT"` | 写入新记忆（`--importance 0.5`、`--type memory`、`--domain`） |
| `link --source N --target N` | 创建图边（`--relation`、`--one-way`） |
| `index` | 扫描并索引知识库 |
| `graph --id N` | 某节点的图谱邻居（`--depth`、`--relation`、`--min-weight`） |
| `recent` | 最近的记忆（`--limit`、`--domain`） |
| `tasks` | 任务节点状态注册表（子命令 `active` / `backfill`） |
| `tasks active` | 活跃任务列表（`--project`、`--states todo,doing,blocked`、`--limit`） |
| `tasks backfill` | 存量任务节点回填 `task_state`（默认 dry-run 预览，`--apply` 才写库） |
| `review` | 最近 N 天的周期回顾（`--tier facts\|logs\|''` 作用于 recent_ingests） |
| `stats` | 库级盘点统计（totals/域/重要度/时间/图谱） |
| `kb "QUERY"` | 知识切片的语义搜索 |
| `consolidate` | 合并预览；`--apply` 执行合并（`--threshold 0.85`、`--max-importance 0.8`） |
| `promote` | 高频记忆升级候选；`--apply` 升权打标（`--days`、`--min-hits`） |
| `ingest-git` | 将近期 git 提交索引为 `git_commit` 节点（幂等） |
| `fts-rebuild` | 重建完整 FTS5 索引 |
| `fts-search "QUERY"` | 原始 FTS5 搜索（trigram 子串） |
| `doctor` | 部署体检：关键文件 / 存储 / FTS / 依赖 / Embedding / 向量维度一致性 / 运行时路径字符集，失败项给出修复命令（`--json` 机器可读） |
| `startup-check` | 运行启动自检（`doctor` 的轻量子集，失败时退出码 1） |
| `task-archive` | 归档已完成任务；`--apply` 写入 markdown 并删除节点 |
| `reindex` | 全库向量重嵌入（换 embedding 模型后使用；`--check` 体检、`--dry-run` 预览） |

### 区块（Blocks）

`block` 是「域分组」概念：图谱按区块隔离，扩散检索只沿同区块的边，防止跨域污染。内置 `task` / `kb` / `hermes` / `novel` / `general`，也可把自己的 `domain` 当作区块（如 `--block myproject`），留空按全量模式检索。节点归属统一由 `payload.domain` 表达：写入时通过 `--domain X` 或 `mem_ingest(domain=...)` 指定，`kb` 节点由索引自动设置。

### REST API — `main.py`，端口 8090

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 服务信息 + 版本 + 端点索引 |
| `POST` | `/mem/search` | 统一检索 |
| `POST` | `/mem/ingest` | 写入新记忆（含冲突检测 + 敏感扫描） |
| `POST` | `/graph/neighbors` | 某节点的图谱邻居 |
| `POST` | `/lifecycle/pre-turn` | **记忆策略**：每轮模型调用前决定召回哪些记忆，返回可注入 prompt 的文本 |

> 完整 26 条路由见 [`docs/API.md`](docs/API.md)。若设置了 `PALIMPSEST_API_KEY`，除 `/` 外所有端点要求 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。

---

## 测试与评测

```bash
python -m pytest tests/ -v                                 # 在仓库根执行
DB_PATH=/tmp/stress.db python -m uvicorn main:app --port 8091
python scripts/rest_stress.py --base http://127.0.0.1:8091 --seeds 200 --out report.json
venv/Scripts/python.exe eval/gen_eval_set.py --dry-run     # 需要 DEEPSEEK_API_KEY
venv/Scripts/python.exe eval/run_eval.py
```

测试套件覆盖核心闭环：写入 → `mem_search` 命中 → `mem_get_full` 全文往返；图谱建边 → `graph_neighbors` / `mem_communities`；敏感扫描拒绝含密钥内容；混合检索 FTS 侧命中标记；冲突检测 / 版本链与 outdated 检索语义；`consolidate` / `promote` 干跑与幂等；PUT/PATCH 部分更新保留字段；并发与失败路径。`tests/conftest.py` 在导入前把 `DB_PATH` 重定向到临时库，套件永不碰生产库，用确定性 fake embedder，无需在线 Ollama 即可全绿。

`scripts/rest_stress.py` 应用层压测覆盖 6 类真实场景（高频检索 / 批量写入 / 图谱建边与扩散 / 边界输入 / 读写混合长压 / 写入后召回正确性），输出 qps 与 p50/p95/p99。`eval/` 离线评测从库内真实节点反推题集，对 `fts` / `vec` / `rrf` / `cascade` 算 Recall@K、MRR@K、nDCG@K；脚本先把真库复制到 `eval/.tmp/` 只在副本上读写、并对真库前后算 SHA256 自证。配套工具：`scripts/retrieval_probe.py` / `scripts/prod_entrypoint_check.py` / `scripts/ab_snapshot_*.py`；题集与指标定义见 [`eval/README.md`](eval/README.md)。

---

## 项目结构

`main.py`（REST 唯一写者）/ `mcp_server.py`（stdio 逃生梯）/ `config.py` → `core/`（共享引擎，无框架依赖）→ `mcp_tools/`（18 个工具，MCP/REST/CLI 共用）；另有 `scripts/` 运维脚本、`eval/` 离线评测、`hermes-plugin/` 双插件、`tests/`、`docs/`、`data/`。分层约定与完整树见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 开发指南

- **虚拟环境：** 每个 checkout 单独建一个（`python -m venv venv`）并 `pip install -r requirements.txt`。
- **新增工具：** 在 `mcp_tools/` 内用共享的 `@mcp.tool()` 装饰器注册——它会立即同时出现在 MCP 服务、REST 层与 CLI 中。
- **新增核心模块：** 保持 `core/` 不引入 FastAPI/MCP；经由 `mcp_tools/` 与 `main.py` 消费。
- **改了 schema？** 重建 FTS 索引（`fts-rebuild`）与知识库索引（`build_kb_index.py`）。
- **测试：** 保持隔离——绝不让测试指向生产数据库。

提交 PR 前请先过质量门禁（与 CI 的 `lint` / `typecheck` / `test` 三个 job 一一对应）：

```bash
python -m pytest tests/ -q                         # 测试（CI 上跑 3.10 / 3.11 / 3.12）
ruff check .                                       # 静态检查（规则集钉在 pyproject.toml）
mypy                                               # 类型检查（当前覆盖 core/）
python -m pytest --cov=core --cov=mcp_tools -q     # 覆盖率基线
```

文档与代码的一致性由 `scripts/readme_check.py` 检查（MCP 工具清单 / CLI 子命令 / REST 路由 / 配置项键名与默认值 / 文件引用 / 行内代码配对），CI 的 `docs` job 以 `--strict` 跑：`python scripts/readme_check.py --strict`。

版本发布遵循 [语义化版本](https://semver.org/lang/zh-CN/)，流程见 [RELEASING.md](docs/RELEASING.md)，历史见 [CHANGELOG.md](CHANGELOG.md)，弃用 / 退役计划见 [DEPRECATIONS.md](docs/DEPRECATIONS.md)。

---

## License

[MIT](LICENSE) © JiaY-77
