<div align="center">

# Palimpsest

**本地优先的长期记忆系统 · AI 助手的跨会话记忆底座**

**Local-first, battle-tested, memory that never disappears.**

> _Palimpsest_：拉丁语，原指「重写的羊皮纸」——旧字迹被覆写抹去，却又在岁月里重新透出。
>
> 我们把这个意象搬进记忆里：**新的事实覆盖旧的事实，但旧迹永不真正丢失**——每一次改写都通过一条有迹可循的 **版本链**（`REVISED_BY`）连接，新旧记忆可查可溯。

[![Version](https://img.shields.io/badge/Version-v1.1.1-4c6ef5.svg)](/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Storage](https://img.shields.io/badge/TriviumDB-0.8.5-2d9cdb.svg)](/)
[![LLM](https://img.shields.io/badge/Backends-DeepSeek%E2%80%A2Ollama-6f42c1.svg)](/)
[![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml)

**中文** | [English](./README_EN.md)

</div>

---

## 一句话介绍

Palimpsest 是一个 **本地优先的嵌入式长期记忆系统**，将 **语义向量检索（Vector Search）、加权知识图谱（Knowledge Graph）与全文检索（Full-Text Retrieval）** 三合一，把 AI 助手的跨会话记忆统一存放、管理、演化在一座本地数据库里。

我们的目标是成为 **AI 助手的「记忆底座」** —— 让每一次对话的收获都不再随会话关闭而烟消云散，而是**可检索、可关联、可演进**：

- 🗃️ **混合检索** —— 语义向量（cosine）与 FTS5 全文索引（`trigram` 分词，支持中文子串匹配）通过 RRF（Reciprocal Rank Fusion）或级联方式融合，每条命中都标注来源 `fts_hit` / `sem_hit`
- 🔗 **图谱扩散召回** —— 节点由 **加权边**（`RELATED_TO` / `REVISED_BY` / `CAUSES` / `REFERS_TO`）相连，BFS 沿边扩散召回；扩散按最强边截断、弱边过滤、可按域「块」隔离，防止跨域污染
- 🕸️ **社区发现** —— 内置 Leiden 聚类，一键把记忆库分成主题簇（如项目簇、人物关系簇），回答「记忆库里都有哪些圈子」
- 🔄 **冲突检测与版本链** —— 写入时与相似旧记忆比对：高相似度（score > 0.75）判为同一事实被取代，旧版标记 `outdated` 并通过 `REVISED_BY` 链向新版；中相似度只记 `related_ids` 提示相关不误标；type / domain 双隔离防跨类误标
- 🛡️ **写入前敏感扫描** —— 存储前按 **10 条正则规则**（API Key、令牌、私钥、银行卡/手机号、Bearer Token 等）扫描，命中即**拒绝写入**并报告命中的规则
- 🧹 **容量合并与记忆盘点** —— `mem_consolidate` 把近似重复节点合并（相似度 ≥ 0.85、保护高价值记忆）；`mem_stats` 统一盘点库内分布（类型/域/重要度/时间/图谱/热点），回答「库里有什么」
- ⏫ **高频记忆自动升级** —— 检索命中自动计数（`hit_count`），`promote` 把反复被用到的记忆浮出水面：升权 + 打标（dry-run 预览、幂等可逆），为人工升级知识库提供依据
- ⏳ **记忆生命周期** —— 时间衰减加权（`MEMORY_DECAY_FACTOR`，默认 0.95/月）在排序中淡化陈旧记忆而不动存储；`kb_chunk` 知识切片豁免衰减；`outdated` 旧版默认不再参与普通检索（可显式追溯）
- 📁 **任务自动归档** —— 完成任务自动移出热库，写成 markdown 归档至知识库归档目录后删除——先 `dry-run` 预览，`apply` 提交
- ✅ **启动自检** —— `startup-check` 校验关键文件、存储初始化与 FTS 索引，输出结构化报告，失败时以非零退出码结束
- ✂️ **省 token 设计** —— 检索默认只返回 **150 字摘要 + 元数据**，而非全文；完整内容按需二次拉取
- 🔐 **可选 API Key 鉴权** —— 默认关闭（localhost 本机直连）；设置 `PALIMPSEST_API_KEY` 后 REST 层要求 Bearer / X-API-Key 头，适合局域网受信部署
- 🎯 **三接口、一核心** —— MCP（stdio）、FastAPI REST、完整 CLI 三套接入共用同一套底层工具，行为永不割裂
- 🧠 **Hermes 双插件换脑** —— 把 Hermes 的记忆层整体换成 Palimpsest：Memory Provider（语义召回 + 自动沉淀）+ Context Engine（压缩前图谱提炼），一行命令激活，记忆跨会话不丢

---

## 为什么需要 Palimpsest？

### 当前 AI 助手的三类「记忆困境」

绝大多数 AI 应用同时面临三类数据能力的割裂：

| 场景 | 传统做法 | 问题 |
|---|---|---|
| 跨会话记忆 | 每次会话从零开始 | 历史经验与事实随会话关闭而丢失 |
| 知识库语义化 | 简单关键词匹配 | 无法理解语义，无法在概念间关联 |
| 记忆治理 | 无序堆积/手动清理 | 重复、过期、矛盾的信息越来越多 |

Palimpsest 用 **一个本地内核** 同时解决「检索、关联、演进」三件事，避免在向量库、文档库、图谱库之间搬运与同步。

### 「记忆不丢」的一个例子

> 你告诉助手「服务监听 8090 端口」。后来设计变更，又说「端口改为 8095」。
>
> 旧记忆并不会被粗暴覆盖——它被标记为 `outdated`，通过 `REVISED_BY` 指向新版本。任何时候版本链查询都能展开这条链，看清这个事实**如何一步步演变成今天的样子**。这就是 Palimpsest：覆而不失，改写可溯。

---

### 使用场景

#### 场景 1 · 长期陪伴 / 个人助理 agent 的跨会话记忆（Hermes 等）

把 Palimpsest 接入 agent 后，它就是你的「记忆底座」：每轮对话自动召回相关历史、把强信号记忆自动沉淀，会话结束时再提炼本轮要点；上下文压缩之前，图谱还会先提炼一次，把散落的片段织成可检索的网络。会话关闭也没关系——下次见面它依然记得住、想得起。

#### 场景 2 · 知识库语义化（Obsidian 用户）

把积累了多年的 Obsidian Vault 变成可语义检索的资产：`build_kb_index.py` 扫描全部 `.md`，按 Markdown 标题切片、向量化入库，`[[双链]]` 上下文原样保留。搜索不再是「关键词碰运气」，而是「语义相关、附带图谱邻居」。

#### 场景 3 · 创作设定库（小说 / 世界观作者）

`build_novel_index.py` 把本地的创作 Vault（角色卡、世界观、人物关系文档）整文件入库为 `domain=novel` 节点；`link_novel_relations.py` 按关系清单批量建边（师徒/血缘/阵营等）；配合社区发现与图谱查询，设定之间的关系一目了然。创作数据留在本地，不入公网。

#### 场景 4 · 记忆治理（防污染 / 防膨胀 / 可追溯）

记忆库不会越用越乱：写入前敏感扫描拦下密钥，冲突检测 + 版本链让每次改写都有迹可循，容量合并把近似重复收缩成一条，时间衰减淡化陈旧记忆，盘点与 promote 让高频记忆浮出。记忆是资产，不是垃圾场。

---

### 给 Hermes 用户：把它变成你的记忆插件

Hermes 预留了 memory provider / context engine 插槽，Palimpsest 为此提供**双插件**：**Memory Provider**（记忆读写）+ **Context Engine**（上下文压缩前提炼）。插件源码在仓库 [`hermes-plugin/`](./hermes-plugin/README.md)，含 `plugin.yaml`（`kind=standalone`）与两个 hooks：`on_session_end`（会话结束提炼要点）与 `on_pre_compress`（压缩前图谱提炼）。

部署（把插件复制到 Hermes 插件目录，然后一行一件激活）：

```bash
# 1. 复制插件到 Hermes 插件目录（默认 ~/.hermes/plugins/）
mkdir -p ~/.hermes/plugins/palimpsest
cp hermes-plugin/* ~/.hermes/plugins/palimpsest/

# 2. 激活（一行一件）
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

激活后，每轮对话都会自动发生这些事：

- **自动召回** —— 每轮经 REST `:8090` 检索相关历史（`memory.provider=palimpsest`）。
- **强信号自动沉淀** —— 高信号的事实自动写入记忆（启发式判断，不依赖 LLM）。
- **会话结束提炼** —— `on_session_end` 把本轮要点沉淀为结构化记忆。
- **压缩前图谱提炼** —— `on_pre_compress` 用 `context.engine=palimpsest-graph` 提炼图谱要点，喂给压缩阶段。
- **记忆工具集** —— `palimpsest_search` / `palimpsest_ingest` / `palimpsest_link` / `palimpsest_graph` / `palimpsest_router` 等，供 agent 主动调用。

两点注意：

- REST 服务（`:8090`）需**常驻运行**（如 `scripts/start_rest.vbs` 开机自启）。
- 自动沉淀是**启发式**判断（相似度、重要度阈值），不是 LLM 判断——它求「快、稳、不花钱」，而非「聪明」。

---

### Obsidian 用户：我们的读取思路（即使不用 Palimpsest）

> 这一节讲的是「思路」，不是广告——就算你完全不用 Palimpsest，也能照此用任何工具链复刻。

我们不把 Vault 当「文件」看待，而是当作**知识源**。读取分五步：

1. **Vault 目录即知识源** —— 递归扫描 `KNOWLEDGE_DIR` 下的全部 `.md`（自动跳过 `.obsidian` 等配置目录），每个笔记就是一个待处理文档。
2. **Frontmatter 解析** —— 读取 YAML Frontmatter，用 `tags` 驱动规则识别：含 `rule` 标签的笔记划入规则域（`domain=rule`），其余归 `kb`。
3. **按 Markdown 标题智能切片** —— 以 `##` / `###` 为边界切成 300~800 字符的块，块内**原样保留 `[[双链]]`**，让「哪篇关联哪篇」的上下文不丢。
4. **向量化入库** —— 每个切片经 embedding 编码，作为 `kb_chunk` 节点写入存储，构成可语义检索的知识资产。
5. **规则加权** —— 规则域切片检索时 **×1.3 加权**，让「该怎么做」的规则压过普通知识浮上来。

**想自己实现？** 这套流程的骨架很简单：一个向量库（sqlite-vec / chroma 皆可）+ 一个 embedding 服务就能复刻。真正的设计点有三个：

- **切片粒度** —— 太粗检索不准、太碎丢上下文。
- **规则识别** —— 用 frontmatter / 路径 / 命名约定，把「该执行的规则」和「普通笔记」分开。
- **双链保留** —— 让 `[[A]]⇄[[B]]` 的关系进入检索结果，而不是只在正文里躺着。

本思路的现成实现即 `scripts/build_kb_index.py`（全量 `--full` / 增量默认，增量按 `mtime` 对比只重建变化文件）。

---

## 快速上手

### 安装

```bash
# 1. 需要 Python 3.10+
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 本地向量模型（默认向量后端）—— 需先启动 Ollama
ollama pull qwen3-embedding:0.6b
```

### 配置

```bash
cp .env.example .env
# 编辑 .env:
#   - LLM_BACKEND=deepseek   → 填入 DEEPSEEK_API_KEY
#   - LLM_BACKEND=ollama      → 保持 OLLAMA_* 默认即可
#   - EMBEDDING_PROVIDER=ollama  (本地, 默认) 或 openai (云端, 需 EMBEDDING_API_KEY)
```

> **注意：** 更换向量后端会改变向量空间，事后必须重建知识库索引：`python scripts/build_kb_index.py`。

### 启动

```bash
# 可选：启动前自检
python scripts/palimpsest_cli.py startup-check
```

> **首次运行自检**：`startup-check` 会检查关键文件 / 存储 / FTS / 依赖 / Embedding 服务。
> 若 Embedding 项失败：本地默认 Ollama 请先启动并 `ollama pull qwen3-embedding:0.6b`；
> 若使用云端 `EMBEDDING_PROVIDER=openai`，请确认 `.env` 已配置 `EMBEDDING_API_KEY`。

```bash
# REST 服务 (:8090)
python -m uvicorn main:app --host 127.0.0.1 --port 8090

# MCP 服务（stdio —— 接入任意 MCP 客户端）
python mcp_server.py

# CLI（示例）
python scripts/palimpsest_cli.py search "架构最近发生了什么变化？"

# 监控面板 (:8010)
python scripts/dashboard.py

# 索引知识库（KNOWLEDGE_DIR 下的 Obsidian .md 文件）
python scripts/build_kb_index.py
```

Windows 下 `scripts/start_rest.vbs` 可以隐藏窗口启动 REST 服务（如开机自启），日志写入 `scripts/start_rest.log`。

**MCP 客户端接入**（通用 MCP servers 配置）：

```json
{
  "mcpServers": {
    "palimpsest": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/Palimpsest"
    }
  }
}
```

---

## 配置

所有配置均从环境变量读取（`.env` 文件由 `python-dotenv` 自动加载），完整带注释模板见 `.env.example`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REST_PORT` | `8090` | FastAPI REST 服务端口 |
| `DASHBOARD_PORT` | `8010` | 监控面板服务端口 |
| `DB_PATH` | `data/mh_memory.db` | 嵌入式 TriviumDB 数据库路径 |
| `PALIMPSEST_API_KEY` | *（空 = 关闭）* | 可选 REST 鉴权；设置后除 `/` 外所有请求须带 Bearer / X-API-Key |
| `LLM_BACKEND` | `deepseek` | LLM 后端：`deepseek` 或 `ollama` |
| `DEEPSEEK_API_KEY` | *（空）* | DeepSeek API 密钥 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 基础地址 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek 模型标识 |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI 兼容基础地址 |
| `OLLAMA_MODEL` | `deepseek-r1:7b` | 作为 LLM 的 Ollama 对话模型 |
| `EMBEDDING_PROVIDER` | `ollama` | 向量后端：`ollama`（本地、私有）或 `openai`（OpenAI 兼容云端，如 Voyage/硅基流动） |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | 本地 Ollama 向量模型 |
| `OLLAMA_EMBEDDING_BASE_URL` | `http://localhost:11434` | Ollama 原生 embedding API 根地址（与 LLM 的 /v1 解耦） |
| `OLLAMA_EMBEDDING_DIM` | `1024` | 向量维度（本地后端） |
| `EMBEDDING_API_KEY` | *（空）* | 云端向量端点的 API 密钥 |
| `EMBEDDING_BASE_URL` | `https://api.voyageai.com/v1` | 云端向量基础地址（任意 OpenAI 兼容端点） |
| `EMBEDDING_MODEL` | `voyage-3` | 云端向量模型 |
| `EMBEDDING_DIM` | `1024` | 向量维度（云端后端） |
| `MEMORY_DECAY_FACTOR` | `0.95` | 月度记忆衰减（排序用，`score × importance × factor^(天/30)`）；`1.0` 关闭衰减；`kb_chunk` 节点永不衰减 |
| `RULE_RETRIEVAL_WEIGHT` | `1.3` | 规则域知识切片的分数倍率 |
| `DOMAIN_BIAS_WEIGHT` | `1.15` | 域偏置检索的额外权重 |
| `EXPAND_MAX_EDGES_PER_NODE` | `20` | 图谱扩散时每节点最多扩散的最强边数 |
| `EXPAND_MIN_EDGE_WEIGHT` | `0.0` | 图谱扩散弱边过滤阈值（0 关闭） |
| `RRF_K` | `60.0` | 混合检索 RRF 常数 k（单侧命中也计贡献） |
| `L1_MAX_SIZE` | `5120` | L1 外部记忆文件（MEMORY.md）最大读取字节数，超过跳过 |
| `MEM_INGEST_MAX_LENGTH` | `50000` | 单条记忆 content 最大字符数，超长拒绝写入 |
| `KNOWLEDGE_DIR` | *（可选）* | 知识库根目录（待索引的 Obsidian `.md` 文件） |
| `HERMES_MEMORY_FILE` | *（空）* | 可选的外部纯文本记忆文件路径，作为额外记忆来源；留空则禁用 |

---

## 使用

### MCP 工具（16 个）— `mcp_tools/*`

| 工具 | 说明 |
|---|---|
| `mem_search` | 统一检索：记忆 / 知识库 / 两者；可选图谱邻居扩展、域偏置、块级隔离 |
| `mem_hybrid_search` | 混合检索：FTS5 + 向量；`mode=rrf`（k=60）或 `cascade`；命中标注 `fts_hit` / `sem_hit` |
| `mem_retrieve` | 语义检索，返回 150 字摘要 + 元数据（绝不返回全文） |
| `mem_get_full` | 按 ID 拉取节点完整内容 |
| `mem_ingest` | 写入新记忆——含冲突检测、`REVISED_BY` 版本链、敏感扫描、长度护栏 |
| `mem_recent` | 最近的记忆（新的在前） |
| `mem_review` | 最近 N 天的周期性回顾 + 治理候选（高价值升级 / outdated 清理 / 低价值） |
| `mem_stats` | 库级盘点：类型 / 域 / 重要度 / 时间 / 图谱分布 + 热点节点 |
| `mem_version_history` | 沿 `REVISED_BY` 链展开，查看事实演化过程 |
| `mem_consolidate` | 近似重复检测；dry-run 预览或 apply 合并 |
| `mem_communities` | Leiden 社区发现：把记忆库聚成主题簇，回答「有哪些圈子」 |
| `kb_index` | 将知识库 `.md` 文件索引为 `kb_chunk` 节点（向量化） |
| `kb_search` | 对已索引知识切片的语义搜索 |
| `graph_neighbors` | 从某节点出发对知识图谱做 BFS（关系过滤、深度 1–3、弱边过滤） |
| `mem_link` | 手动创建图边（`RELATED_TO` / `CAUSES` / `REFERS_TO`；默认双向） |
| `router_query` | 查询规则域知识切片，提取模型/配置推荐 |

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
| `review` | 最近 N 天的周期回顾 |
| `stats` | 库级盘点统计（totals/域/重要度/时间/图谱） |
| `kb "QUERY"` | 知识切片的语义搜索 |
| `consolidate` | 合并预览；`--apply` 执行合并（`--threshold 0.85`、`--max-importance 0.8`） |
| `promote` | 高频记忆升级候选；`--apply` 升权打标（`--days`、`--min-hits`） |
| `ingest-git` | 将近期 git 提交索引为 `git_commit` 节点（幂等） |
| `fts-rebuild` | 重建完整 FTS5 索引 |
| `fts-search "QUERY"` | 原始 FTS5 搜索（trigram 子串） |
| `startup-check` | 运行启动自检（失败时退出码 1） |
| `task-archive` | 归档已完成任务；`--apply` 写入 markdown 并删除节点 |

示例：

```bash
python scripts/palimpsest_cli.py ingest "服务监听 8090 端口" --domain work --importance 0.6
python scripts/palimpsest_cli.py search "8090 端口" --neighbors
python scripts/palimpsest_cli.py stats
python scripts/palimpsest_cli.py promote            # 预览高频记忆候选
python scripts/palimpsest_cli.py consolidate        # 预览合并候选
python scripts/palimpsest_cli.py consolidate --apply # 合并
```

### 区块（Blocks）

`block` 是「域分组」概念：图谱按区块隔离，扩散检索只沿同区块的边，防止跨域污染。出厂内置通用区块：`task`（任务）、`kb`（知识库）、`hermes`（助手自身记忆）、`novel`（小说创作设定）、`general`（未分类兜底）；其中 `rule` 是 `kb` 的子集（规则切片，归入 `kb` 区块）。你也可以把自己的 `domain` 当作区块使用（如 `--block myproject`）。`--block` 留空则按全量模式检索。

节点归属统一由 `payload.domain` 字段表达。写入记忆时通过 `--domain X` 或 `mem_ingest(domain=...)` 指定区块；`kb` 类型节点由知识库索引自动设置（`kb` / `rule`）。

### REST API — `main.py`，端口 8090

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 服务信息 + 版本 + 端点索引 |
| `GET` | `/export` | 导出记忆为分页 JSON 快照（默认每页 100 条，上限 500） |
| `GET` | `/summary` | 人类可读的记忆摘要（事件 / 角色状态 / 计划） |
| `GET` | `/memory/{id}` | 读取单节点完整 payload |
| `POST` | `/report` | 基于当前存储生成 LLM 分析报告 |
| `DELETE` | `/memory/{id}` | 删除记忆节点（FTS 索引同步） |
| `PUT` | `/memory/{id}` | 更新节点 payload（**合并语义**：只改传入字段，其余保留；自动同步 FTS） |
| `PATCH` | `/memory/{id}` | 部分更新节点 payload（与 PUT 同合并语义，REST 语义更精确） |
| `PATCH` | `/memory/{id}/vector` | 更新节点的向量（维度需一致） |
| `POST` | `/mem/search` | 统一检索 |
| `POST` | `/mem/hybrid-search` | FTS5 + 向量混合检索 |
| `POST` | `/mem/ingest` | 写入新记忆（含冲突检测 + 敏感扫描） |
| `POST` | `/mem/link` | 创建图边 |
| `POST` | `/mem/stats` | 库级盘点统计 |
| `POST` | `/graph/neighbors` | 某节点的图谱邻居 |
| `POST` | `/graph/communities` | Leiden 社区发现 |
| `POST` | `/mem/router` | 任务路由查询 |

> 若设置了 `PALIMPSEST_API_KEY`，除 `/` 外所有端点要求 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。

示例：

```bash
curl -X POST http://127.0.0.1:8090/mem/search \
  -H "Content-Type: application/json" \
  -d '{"query": "架构", "scope": "all", "top_k": 5}'
```

---

## 测试

```bash
# 在仓库根目录执行
python -m pytest tests/ -v
```

测试套件覆盖核心闭环：写入 → `mem_search` 命中 → `mem_get_full` 全文往返；图谱建边 → `graph_neighbors` / `mem_communities`；敏感扫描拒绝含密钥内容；混合检索 FTS 侧命中标记；冲突检测 / 版本链与 outdated 检索语义；`consolidate` / `promote` 干跑与幂等；PUT/PATCH 部分更新保留字段；并发与失败路径（脏 payload、embedding 不可用等）。

`tests/conftest.py` 在一切导入之前将 `DB_PATH` 重定向到 **临时数据库**（并隔离知识库）—— 测试套件永远不碰生产数据库；使用确定性 fake embedder，无需在线 Ollama 即可全绿。

---

## 项目结构

```
Palimpsest/
├── README.md                     # 中文主版
├── README_EN.md                  # 英文版
├── LICENSE
├── .env.example                  # 配置模板（带注释）
├── .gitignore
├── requirements.txt
├── config.py                     # 环境变量驱动配置
├── main.py                       # FastAPI REST 入口 (:8090)
├── mcp_server.py                 # MCP stdio 入口 (FastMCP)
├── dashboard.html
├── core/                         # 共享引擎，无框架依赖
│   ├── trivium_store.py          #   TriviumDB 封装（向量+图谱+文档）
│   ├── conflict.py               #   冲突检测 / 版本链（三层防误标）
│   ├── consolidator.py           #   近似重复记忆合并
│   ├── stats.py                  #   库级盘点统计（mem_stats 核心）
│   ├── promoter.py               #   高频记忆自动升级（hit_count → promote）
│   ├── fts_index.py              #   FTS5 全文索引（trigram, fts.db）
│   ├── reporting.py              #   LLM 生成的记忆报告
│   ├── secret_scan.py            #   写入前敏感扫描（10 条规则）
│   ├── startup_check.py          #   启动自检
│   ├── task_archive.py           #   完成任务自动归档
│   ├── utils.py
│   └── version.py                #   版本号来自 git tag（兜底 dev）
├── mcp_tools/                    # 16 个 MCP 工具（MCP/REST/CLI 共用）
│   ├── __init__.py
│   ├── _common.py                #   共享 store / mcp / 序列化助手
│   ├── memory.py                 #   mem_* 工具
│   ├── kb.py                     #   kb_index / kb_search
│   ├── graph.py                  #   graph_neighbors / mem_link / mem_communities
│   ├── routing.py                #   router_query
│   ├── consolidate_tool.py       #   mem_consolidate
│   └── stats_tool.py             #   mem_stats
├── scripts/                      # 运维工具
│   ├── palimpsest_cli.py         #   CLI（search/ingest/…/stats/promote）
│   ├── dashboard.py              #   监控面板 (:8010)
│   ├── build_kb_index.py         #   知识库分块 & 向量化
│   ├── build_novel_index.py      #   小说设定库整文件入库（--source 指定 vault）
│   ├── link_novel_relations.py   #   小说人物关系批量建边（dry-run/--apply）
│   ├── sync_rules.py             #   规则笔记 → 模型路由决策树
│   ├── check_kb_consistency.py   #   知识库 vs 数据库一致性检查
│   ├── check_fts_consistency.py  #   FTS 内容级对账
│   ├── export_all_data.py        #   只读 JSON 备份导出
│   ├── graph_edges.py            #   持久化知识图谱边
│   ├── migrate_domain.py         #   历史字段迁移（character_name → domain）
│   ├── rebuild_db.py             #   从导出快照重建数据库
│   ├── start_rest.vbs            #   Windows 隐藏窗口 REST 启动器
│   └── tdb_stress/               #   TriviumDB 压力测试
├── hermes-plugin/                # Hermes 双插件（Memory Provider + Context Engine）
├── tests/                        # pytest（conftest 隔离 + fake embedder，无需联网）
└── data/                         # 运行时数据库（gitignore）
    ├── mh_memory.db              #   主 TriviumDB 存储
    └── fts.db                    #   FTS5 全文索引
```

---

## 开发指南

- **虚拟环境：** 每个 checkout 单独建一个（`python -m venv venv`）并 `pip install -r requirements.txt`。
- **新增工具：** 在 `mcp_tools/` 内用共享的 `@mcp.tool()` 装饰器注册——它会立即同时出现在 MCP 服务、REST 层与 CLI 中。
- **新增核心模块：** 保持 `core/` 不引入 FastAPI/MCP；经由 `mcp_tools/` 与 `main.py` 消费。版本号由 `core/version.py` 从 git tag 获取。
- **改了 schema？** 重建 FTS 索引（`fts-rebuild`）与知识库索引（`build_kb_index.py`）；导出 / 重建工具在 `scripts/`。
- **测试：** 保持隔离——绝不让测试指向生产数据库。

提交 PR 前请先跑测试：

```bash
python -m pytest tests/ -v
```

版本发布遵循 [语义化版本](https://semver.org/lang/zh-CN/)，流程见 [RELEASING.md](docs/RELEASING.md)，历史见 [CHANGELOG.md](CHANGELOG.md)。

---

## License

[MIT](LICENSE) © JiaY-77
