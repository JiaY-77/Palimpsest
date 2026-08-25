
# Palimpsest

> 为 **Obsidian** 知识库注入 **AI 记忆**：轻量级 GraphRAG + 语义搜索的 MCP 服务器。

Palimpsest 是一个专为 Obsidian 用户和 AI Agent 设计的轻量级记忆服务，基于 TriviumDB（向量 × 图谱 × 文档 三位一体嵌入式数据库）。它将你的 **Obsidian Vault** 和 AI 对话转化为**可检索、可关联、可演进**的结构化资产：

1. **Obsidian 原生集成**：直接扫描 Vault 目录，解析 Frontmatter（Tags/Domain），支持双向链接 `[[ ]]` 上下文切片，将纯 Markdown 笔记向量化入库。
2. **AI 对话记忆**：事件、角色、状态的结构化记忆，语义检索 + 图谱扩散双通道召回。
3. **轻量 GraphRAG**：结合向量检索与图谱扩散（RELATED_TO / REVISED_BY），为 Agent 提供深度的知识推理能力。

对外通过 **MCP Server** 暴露工具，Hermes 等 Agent 可通过 MCP 协议直接“理解”并调用你的 Obsidian 知识库。

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

---

## 核心特性

- **Obsidian 原生适配**：完美兼容 Obsidian Vault，支持 Frontmatter 规则解析（`tags: rule`）、双链 `[[ ]]` 上下文保留及 Markdown 智能切片，让笔记无缝转化为 AI 知识。
- **轻量 GraphRAG**：语义检索结合图谱扩散召回，利用 RELATED_TO/REVISED_BY 边提供关联解释与推理能力，让笔记互联、可沿边扩散召回。
- **语义检索 + 图谱扩散双通道**：可独立检索，也可 `include_neighbors=True` 分区返回（语义区原样 + 图关联区，不互相挤占）
- **150 字摘要设计**：检索默认只返回摘要 + meta，全文按需 `mem_get_full` 取——省 token 的关键设计
- **冲突检测自动版本链**：相似记忆（score > 0.4）自动 outdated + REVISED_BY 链，保留演进痕迹
- **记忆时间衰减**：旧记忆检索权重随龄衰减（`MEMORY_DECAY_FACTOR=0.95`，约每月 5%），知识块不衰减
- **多域隔离**：`domain` 字段隔离（如 hermes / work / novel），互不污染
- **知识库统一语义层**：`kb_index` 建索引、`kb_search` 检索，规则类切片（domain=rule）内置 ×1.3 加权
- **图谱遍历 + 手动建边**：`graph_neighbors` BFS 遍历（`min_weight` 精馏过滤弱边 + 结果按 weight 降序截断，防高节点先到先得）、`mem_link` 手动建边（双向协议自动补反向）
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
│          thinking_tracker（思维链）                           │
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
| REST | FastAPI + Uvicorn | 保留传统集成方式 |
| LLM 后端 | DeepSeek / Ollama 可配 | 记忆提取与报告生成用 |

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

**REST 服务（可选）**：

```bash
uvicorn main:app --reload
```

访问 `http://localhost:8000` 查看 API 文档。

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

---

## 项目结构

```
Palimpsest/
├── main.py                 # FastAPI REST 入口
├── mcp_server.py           # MCP Server（主入口，11 个工具）
├── config.py               # 配置管理（.env）
├── core/
│   ├── trivium_store.py    # TriviumDB 存储封装
│   ├── merger.py           # 冲突检测 / 智能合并（REVISED_BY）
│   ├── retriever.py        # 语义检索 + 图谱扩散
│   ├── extractor.py        # 记忆提取（LLM 后端可配）
│   ├── importer.py         # 聊天文件导入
│   └── thinking_tracker.py # 思维链解析
├── scripts/
│   ├── build_kb_index.py   # 知识库向量化
│   ├── sync_rules.py       # 规则同步
│   ├── check_kb_consistency.py
│   └── migrate_soul_logs.py
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
| 4 | 记忆生命周期（时间衰减已实现；盘点 / 高频升级待启动） | 待启动 |
| 5 | 完备化（远期） | 规划中 |

---

## 踩坑备忘（给使用者的提醒）

- **换 embedding 必须全量重建向量**：维度匹配 ≠ 向量空间匹配（跨模型余弦可为负值）
- **MCP 2.0 兼容**：FastMCP 需用 mcp 1.29.x（2.0 移除了顶层 FastMCP）
- **图谱边持久化**：kb 重建会导致 kb_chunk id 变化，图谱边可能丢失（幂等建边脚本为优先修复方向）