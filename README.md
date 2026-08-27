
# Palimpsest v2.0

> 为 **Obsidian** 知识库注入 **AI 记忆**：轻量级 GraphRAG + 语义搜索的 MCP 服务器；也是可替换 Agent 记忆层的 **Memory Provider + Context Engine**（双插件换脑）。

Palimpsest 是一个专为 Obsidian 用户和 AI Agent 设计的轻量级记忆服务，基于 TriviumDB（向量 × 图谱 × 文档 三位一体嵌入式数据库）。它将你的 **Obsidian Vault** 和 AI 对话转化为**可检索、可关联、可演进**的结构化资产：

1. **Obsidian 原生集成**：直接扫描 Vault 目录，解析 Frontmatter（Tags/Domain），支持双向链接 `[[ ]]` 上下文切片，将纯 Markdown 笔记向量化入库。
2. **AI 对话记忆**：事件、角色、状态的结构化记忆，语义检索 + 图谱扩散双通道召回。
3. **轻量 GraphRAG**：结合向量检索与图谱扩散（RELATED_TO / REVISED_BY），为 Agent 提供深度的知识推理能力。
4. **Agent 记忆层替换**（v2.0）：通过 MemoryProvider / ContextEngine 插槽把 Agent（如 Hermes）的内置记忆换成 Palimpsest——每轮自动召回、强信号自动沉淀、压缩前图谱提炼。

对外通过 **MCP / CLI / REST 三通道**暴露能力，Hermes 等 Agent 可原生接入。

---

## 核心理念

| 能力 | 说明 |
|---|---|
| 结构化记忆 | 以事件/角色/状态为核心构建记忆层，不只是对话原文 |
| 语义检索 | 向量相似度召回（多 provider：本地 qwen3-embedding 1024 维默认 / OpenAI 兼容云端可选） |
| 图谱扩散召回 | 沿有向带权边 BFS 遍历，增强召回广度与关联解释 |
| 知识库向量化 | Markdown 切片 → kb_chunk 节点 → 与记忆同库检索 |
| 统一检索入口 | `mem_search` 一个工具查记忆 + 知识库，规则节点 ×1.3 加权 |
| 冲突检测 | 写入相似新记忆时自动标记旧记忆 outdated，建立 REVISED_BY 版本链 |
| Ingest 安全扫描 | 写入前正则扫描 API key / token / 私钥 / 身份证 / 手机号（10 条规则），命中拒绝入库——开源脱敏红线（core/secret_scan.py） |
| 容量自动合并 consolidate | 相似 memory 节点自动合并（REVISED_BY 保留、kb_chunk 永不参与、高价值保护），dry-run 预览 / --apply 执行 |
| FTS5 全文搜索 | 中文子串匹配（trigram + 2 字 LIKE 兜底），支持重建索引与即时查询（core/fts_index.py） |
| 轻量 Dashboard | 独立 Web 服务 http://127.0.0.1:8010，提供统计 / 搜索 / 最近记忆 / 合并治理预览执行 |
| **三通道对齐**（v2.0） | MCP / CLI / REST 统一语义层：REST 新增 `/mem/search` `/mem/ingest` `/mem/link` `/graph/neighbors` `/mem/router`，与 MCP 工具同一实现，行为一致 |
| **Agent 记忆层替换**（v2.0） | MemoryProvider 插槽：每轮 `prefetch` 自动召回、`sync_turn` 强信号自动沉淀、`on_session_end` 提炼、`on_pre_compress` 抽取；ContextEngine 插槽：压缩前图谱关键链提炼 |
| **5 个模型工具**（v2.0） | `palimpsest_search / ingest / link / graph / router`——Agent 可主动检索、写入、建图谱边、规则路由 |

---

## 定位与适配（诚实版）

**Palimpsest 是给「长期运行的 agent」用的语义记忆层（MCP 服务），不是给人用的产品界面。** 它的核心消费者是 agent（通过 MCP / CLI / REST 调用），前端 Dashboard 只是可选的「窗户」。

**它解决什么**：
- **agent 跨会话记忆**：事实 / 偏好 / 踩坑 / 决策理由，下次会话检索即得，不靠上下文窗口硬扛
- **知识库语义化**：Obsidian 笔记向量化入库，语义召回 + 图谱关联 + 规则加权
- **记忆治理**：安全扫描防污染（API key/身份证等拒绝入库）、容量自动合并防膨胀、outdated 清理、REVISED_BY 版本链可追溯

**适配什么**（适合用它的场景）：
- 长期陪伴 / 个人助理 agent——跨会话记忆是刚需
- 编码 agent 做长项目——项目状态 / 踩坑 / 决策理由要记住（Git Memory 把 commit 也变成可检索事实）
- 创作 / 研究 agent——素材、设定、结论需要积累
- Obsidian 用户——原生适配（frontmatter / 双链 / 智能切片）
- **想换掉 Agent 内置记忆的用户**（v2.0）：Agent 若支持 MemoryProvider / ContextEngine 插槽，可直接把记忆层换成 Palimpsest（每轮自动召回 + 强信号自动沉淀 + 压缩前图谱提炼）——已在 Hermes 实测

**不适合什么**（别硬用）：
- 一次性任务 agent（用完即走，不需要记忆）
- 无状态工具型 agent（每次调用独立，无上下文延续）
- 记忆量极小（几十条）——MEMORY.md 或文件就够，引入服务是过度设计
- 没有资源 / 意愿维护服务的用户——需要跑 uvicorn、embedding（本地 Ollama 或云端 key）、定期治理（可自动化）

**对什么友好**：
- **MCP 生态友好**：Hermes 等 agent 通过 MCP 协议直接接入，零适配
- **本地优先**：默认 Ollama 本地 embedding，隐私可控；云端 provider 可选
- **中文友好**：FTS5 trigram 中文子串搜索（不用分词器）
- **治理友好**：CLI 全自动化（consolidate 预览/执行、周维护可脚本化），Dashboard 只作查看

**设计哲学**：纯后端为主——agent 是记忆的消费者，前端不是主体。**记忆是给 agent 用的，不是给人看的。**

**成熟度（诚实）**：单用户真实工作流中迭代（当前约 320 节点，语义检索 / 图谱 / 治理均经实战验证）；v2.0 起接口进入稳定期（MCP / CLI / REST 三通道对齐，Agent 记忆层替换已在 Hermes 实测：自动召回 / 自动沉淀 / 图谱压缩全链路跑通）。仍建议固定版本使用。

---

## 核心特性

- **Obsidian 原生适配**：完美兼容 Obsidian Vault，支持 Frontmatter 规则解析（`tags: rule`）、双链 `[[ ]]` 上下文保留及 Markdown 智能切片，让笔记无缝转化为 AI 知识。
- **轻量 GraphRAG**：语义检索结合图谱扩散召回，利用 RELATED_TO/REVISED_BY 边提供关联解释与推理能力，让笔记互联、可沿边扩散召回。
- **语义检索 + 图谱扩散双通道**：可独立检索，也可 `include_neighbors=True` 分区返回（语义区原样 + 图关联区，不互相挤占）
- **150 字摘要设计**：检索默认只返回摘要 + meta，全文按需 `mem_get_full` 取——省 token 的关键设计
- **冲突检测自动版本链**：相似记忆（score > 0.4）自动标记 outdated + REVISED_BY 链；**outdated 节点不留**（内容已被新版覆盖），复盘治理（mem_review）时全部清理
- **新记忆类型约定**：`type=decision`（技术决策理由/权衡）、`type=correction`（主人纠正）、`type=git_commit`（工程事实）
- **记忆时间衰减**：旧记忆检索权重随龄衰减（`MEMORY_DECAY_FACTOR=0.95`，约每月 5%），知识块不衰减
- **多域隔离**：`domain` 字段隔离（如 hermes / work / novel），互不污染；**无 domain 的 general 节点只在全量模式（不设 block）下可见，分区查询自动忽略它**（无标签 = 未分类，隔离场景丢弃防污染）
- **知识库统一语义层**：`kb_index` 建索引、`kb_search` 检索，规则类切片（domain=rule）内置 ×1.3 加权
- **图谱遍历 + 手动建边**：`graph_neighbors` BFS 遍历（`min_weight` 精馏过滤弱边 + 结果按 weight 降序截断，防高节点先到先得；`block` 分区块——主结果与图谱扩散都按区块过滤，起点自检拦截跨域起点，防跨域污染）、`mem_link` 手动建边（双向协议自动补反向）
- **双接口**：MCP Server（stdio）+ FastAPI REST

---

## Obsidian 集成

Palimpsest 原生支持 **Obsidian** 工作流，将你的笔记 Vault 直接转化为 AI 的长期记忆层：

- **Vault 即知识库**：无需迁移，直接将本地 Obsidian 目录（纯 Markdown 笔记）作为数据源，通过 `kb_index` / `scripts/build_kb_index.py` 一键扫描切片并向量化入库。
- **规则笔记驱动**：解析笔记 Frontmatter，若 `tags` 包含 `rule`，系统自动识别为规则类文档，设置 `domain=rule` 并在检索时内置 **×1.3 加权**。
- **动态路由同步**：运行 `scripts/sync_rules.py`，将规则笔记实时同步到模型路由决策树 JSON。修改 Obsidian 笔记即可更新 Agent 的路由决策逻辑。
- **深度语义切片**：按 Markdown 标题（## / ###）智能切片，完美保留双链 `[[ ]]` 上下文；通过图谱边（`RELATED_TO` / `REVISED_BY`）将笔记互联，支持沿边扩散召回。
- **轻量 GraphRAG 体验**：语义检索结合图谱扩散，为 Obsidian 用户提供「AI 可检索的记忆/知识层」，让 Agent 能够理解笔记间的隐式关联。
- **MCP 无缝接入**：通过 MCP Server（stdio）或 FastAPI REST 接口，Agent（如 Hermes）可直接读取并调用你的 Obsidian 知识网络。

---

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Client（Hermes 等 Agent）             │
└────────────────────────────┬────────────────────────────────┘
                             │ MCP（stdio）
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    Palimpsest（Python）                        │
│                                                              │
│   mcp_server.py（FastMCP）        main.py（FastAPI REST）    │
│                                                              │
│   core/  trivium_store（存储封装）· merger（冲突合并）        │
│          retriever（检索）· extractor（提取）· importer       │
│          thinking_tracker（思维链）· secret_scan（安全扫描）  │
│          fts_index（FTS5 全文搜索）                           │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│               TriviumDB（嵌入式，单文件 data/mh_memory.db）  │
│                                                              │
│   向量（1024 维）│  Payload（JSON）│  有向带权图              │
└─────────────────────────────────────────────────────────────┘
```

### 技术选型

| 组件 | 选型 | 说明 |
|---|---|---|
| 存储引擎 | TriviumDB（Rust 嵌入式） | 向量 + 图 + 文档三位一体，单文件部署，Apache-2.0 |
| Embedding | Ollama `qwen3-embedding:0.6b` | 本地免费，1024 维，中文效果优秀 |
| MCP | FastMCP | stdio 方式，被 Hermes 等 MCP 客户端拉起 |
| REST | FastAPI + Uvicorn | 端口 8090（8000 让给 SillyTavern）；v2.0 起含统一语义层端点（/mem/search /mem/ingest /mem/link /graph/neighbors /mem/router） |
| Dashboard | 独立 HTTP 服务 | 端口 8010，统计 / 搜索 / 合并治理面板 |
| LLM 后端 | DeepSeek / Ollama 可配 | 记忆提取与报告生成用 |

---

## Agent 记忆层融合（v2.0，以 Hermes 为例）

Palimpsest v2.0 不只是「给 Agent 用的 MCP 服务」——它可以通过 Agent 的记忆插槽**直接替换内置记忆层**。Hermes 已实测：

```bash
# 1. 安装插件到 Agent 插件目录（standalone 插件）
#    $HERMES_HOME/plugins/palimpsest/（plugin.yaml / __init__.py / context_engine.py / cli.py）

# 2. 激活两个槽位
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest     # 记忆层：自动召回 + 自动沉淀
hermes config set context.engine palimpsest-graph # 压缩层：压缩前图谱提炼

# 3. 自检
hermes palimpsest status
hermes palimpsest test
```

生效后的行为（客观描述）：
- **每轮自动召回**：用户消息触发 `prefetch()` → `/mem/search`（含图谱邻居）→ 相关历史记忆注入上下文；寒暄类消息跳过（省一次 HTTP）
- **每轮自动沉淀**：`sync_turn()` 只命中强信号（纠正 / 偏好 / 决策 / 规则）才写入，普通轮次不写——避免库被低价值内容污染
- **会话提炼**：`on_session_end()` 把含强信号的消息提炼成一条要点
- **压缩保护**：`on_pre_compress()` / `compress()` 压缩前用小帕图谱提炼关键链，合并进压缩 prompt 的 memory_context
- **5 个模型工具**：Agent 可主动 `palimpsest_search / ingest / link / graph / router`（检索 / 写入 / 建边 / 图谱 / 规则路由）

**边界（诚实）**：
- 依赖 Palimpsest REST :8090 常驻（服务挂掉时插件 fail-open——Agent 会话不受影响，只是失去记忆增强）
- 自动沉淀是启发式（正则强信号），不是 LLM 判断——复杂场景建议用工具主动写入
- 当前为单机单用户验证（约 320 节点）；多 Agent 共享 / 大规模并发未压测

---

## 快速开始

### 环境要求

- Python 3.10+
- Ollama（`ollama pull qwen3-embedding:0.6b`）
- 网络可访问 GitHub（TriviumDB 以 git 依赖安装）

### 安装

```bash
git clone https://github.com/JiaY-77/palimpsest.git
cd Palimpsest
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env`（或直接设置环境变量）：

```ini
# 数据库文件
DB_PATH=data/mh_memory.db

# Embedding（多 provider：本地 ollama 默认，隐私优先；云端 OpenAI 兼容可选）
# EMBEDDING_PROVIDER=ollama | openai
EMBEDDING_PROVIDER=ollama
OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b
OLLAMA_EMBEDDING_DIM=1024
# 云端 OpenAI 兼容 Embedding（Voyage / OpenAI / 硅基流动等；EMBEDDING_PROVIDER=openai 时生效）
# EMBEDDING_API_KEY=
# EMBEDDING_BASE_URL=https://api.voyageai.com/v1
# EMBEDDING_MODEL=voyage-3
# EMBEDDING_DIM=1024

# L1 嗅探（可选）：mem_search 一体化检索 MEMORY.md——命中查询词置顶返回（<5KB 内存缓存）
# HERMES_MEMORY_FILE=C:\path\to\MEMORY.md

# LLM 后端（deepseek / ollama）
LLM_BACKEND=deepseek
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash

# 记忆时间衰减（0.95 ≈ 每月衰减 5%，1.0 关闭）
MEMORY_DECAY_FACTOR=0.95
```

### 启动

**MCP Server（主入口，stdio 方式）**：

```bash
venv\Scripts\python.exe mcp_server.py
```

由 MCP 客户端（如 Hermes）配置后拉起；工具即 `mem_*` / `kb_*` / `graph_neighbors` / `mem_link` 等 11 个。

**Dashboard（轻量 Web 面板）**：

```bash
python scripts/dashboard.py
```

访问 `http://127.0.0.1:8010`（`Config.DASHBOARD_PORT`），提供统计 / 搜索 / 最近记忆 / 合并治理预览执行。

**CLI 子命令（12 个）**：

```bash
palimpsest_cli search / ingest / link / index / graph / recent / review / kb / consolidate / ingest-git / fts-rebuild / fts-search
```

**REST 服务（可选）**：

```bash
uvicorn main:app --reload
```

默认端口 **8090**（`Config.REST_PORT`，8000 让给 SillyTavern），访问 `http://localhost:8090` 查看 API 文档。

v2.0 统一语义层端点（与 MCP 工具同一实现）：
- `POST /mem/search` — 语义检索（scope=memory/kb/all，含图谱邻居）
- `POST /mem/ingest` — 写入记忆（冲突检测 + 安全扫描）
- `POST /mem/link` — 手动建图谱边
- `POST /graph/neighbors` — 图谱邻居查询
- `POST /mem/router` — 任务路由查询（规则推荐）

---

## MCP 工具列表

| 工具 | 功能 |
|---|---|
| `mem_search` | 统一检索入口：scope=memory/kb/all 混合检索；rule 切片 ×1.3 加权；`include_neighbors=True` 分区返回图关联区 |
| `mem_retrieve` | 语义检索记忆（150 字摘要 + meta，不返回全文） |
| `mem_get_full` | 按 id 取完整记忆 |
| `mem_ingest` | 写入新记忆（冲突检测：相似旧记忆 outdated + REVISED_BY 链） |
| `mem_recent` | 最近记忆列表 |
| `mem_review` | 复盘盘点：近 N 天新增记忆 / 高价值候选 / outdated / 低价值清理候选（复盘=记忆治理，2026-08-25） |
| `mem_version_history` | 版本历史查询（沿 REVISED_BY 修订链） |
| `kb_index` | 知识库文件索引（扫描 Knowledge 目录所有 .md，切片向量化） |
| `kb_search` | 知识库语义检索（kb_chunk 节点，含 domain=rule 规则类切片） |
| `router_query` | 任务路由查询（查规则类知识切片，提取推荐模型/配置） |
| `graph_neighbors` | 图谱邻居查询（BFS 遍历，relation 过滤 / depth 1-3 / limit） |
| `mem_link` | 手动建边（RELATED_TO / CAUSES / REFERS_TO，双向协议自动补反向） |

---

## 图谱协议

| 边类型 | 语义 | 建立方式 | 方向 |
|---|---|---|---|
| `REVISED_BY` | 版本修订链（新 → 旧） | `mem_ingest` 自动 | 单向 |
| `RELATED_TO` | 关联 | `mem_link` 手动 | 无向（双向建边自动补反向） |
| `CAUSES` | 因果（预留） | `mem_link` 手动 | 无向 |
| `REFERS_TO` | 引用（预留） | `mem_link` 手动 | 无向 |

约定：**自环禁止**；weight 统一 round 6 位；双向建边协议绕开 get_edges 只返回出边的限制（先查存在则跳过，不重复建）。

---

## 脚本工具（scripts/）

| 脚本 | 功能 |
|---|---|
| `build_kb_index.py` | 知识库切片 + 向量化入库（kb_chunk 节点） |
| `sync_rules.py` | 规则笔记 → 决策树 JSON 同步（幂等） |
| `check_kb_consistency.py` | 笔记 / JSON / 向量库三方一致性检查 |
| `migrate_soul_logs.py` | SOUL 历史日志迁移（增量模式） |
| `dashboard.py` | 轻量 Web Dashboard（统计/搜索/最近记忆/合并治理），端口 8010 |

---

## 项目结构

```
Palimpsest/
├── main.py                 # FastAPI REST 入口
├── mcp_server.py           # MCP Server（主入口，11 个工具 + 12 个 CLI 子命令）
├── config.py               # 配置管理（.env）
├── core/
│   ├── trivium_store.py    # TriviumDB 存储封装
│   ├── merger.py           # 冲突检测 / 智能合并（REVISED_BY）
│   ├── retriever.py        # 语义检索 + 图谱扩散
│   ├── extractor.py        # 记忆提取（LLM 后端可配）
│   ├── importer.py         # 聊天文件导入
│   ├── thinking_tracker.py # 思维链解析
│   ├── secret_scan.py      # Ingest 安全扫描（10 条正则规则）
│   └── fts_index.py        # FTS5 全文搜索（trigram + LIKE 兜底）
├── scripts/
│   ├── build_kb_index.py   # 知识库向量化
│   ├── sync_rules.py       # 规则同步
│   ├── check_kb_consistency.py
│   ├── migrate_soul_logs.py
│   └── dashboard.py        # 轻量 Web Dashboard（8010）
├── data/                   # 数据库（mh_memory.db）
├── requirements.txt
└── .env                    # 环境变量
```

---

## 阶段路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 项目复活 / 基础链路 | 完成 |
| 1 | 统一语义层（mem_search 唯一入口 / rule 加权） | 完成 |
| 2 | 知识库向量化（kb_index / kb_search / sync_rules） | 完成 |
| 3 | 图谱能力（graph_neighbors / mem_link / 分区返回 / 双向建边） | 完成 |
| 4 | 记忆生命周期（时间衰减已实现；盘点 / 高频升级待启动） | **P0-P3 已落地**：安全扫描 / 自动合并 / Git Memory / FTS5 / Dashboard |
| 5 | 完备化（远期） | 规划中 |
| 6 | **Agent 记忆层融合（v2.0）**：REST 三通道对齐 + MemoryProvider / ContextEngine 双插件 | **完成（2026-08-27）**：Hermes 实测自动召回 / 自动沉淀 / 图谱压缩全链路 |

---

## 踩坑备忘（给使用者的提醒）

- **换 embedding 必须全量重建向量**：维度匹配 ≠ 向量空间匹配（跨模型余弦可为负值）
- **MCP 2.0 兼容**：FastMCP 需用 mcp 1.29.x（2.0 移除了顶层 FastMCP）
- **图谱边持久化**：kb 重建会导致 kb_chunk id 变化，图谱边可能丢失（幂等建边脚本为优先修复方向）