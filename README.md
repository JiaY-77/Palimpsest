# MemoryHub

> 捕获 AI 的推理链（reasoning），将其转化为可检索、可复用、可跨端同步的结构化记忆资产。

**MemoryHub 是一个专为 AI 对话场景设计的轻量级记忆服务。** 它不存储对话原文，而是从模型的推理过程中提取结构化信息——事件、角色状态、剧情计划、用户意图——让 AI 记住“为什么这么想”，而不是只记住“说了什么”。

---

## 💡 核心理念

现有 RAG 系统只能记住对话结果（鱼），而 MemoryHub 记住的是模型的推理过程（渔网）。

| | 普通 RAG / 记忆插件 | MemoryHub |
|---|---|---|
| 存储内容 | 对话原文或摘要 | **结构化的推理链**（思考过程） |
| 因果逻辑 | ❌ 丢失 | ✅ 完整保留 |
| 角色动机 | ❌ 丢失 | ✅ 完整保留 |
| 跨会话复用 | 低（依赖原始文本） | **高（思维底片可迁移）** |
| 记忆合并 | 简单去重 | **语义级合并，保留演进痕迹** |
| 多角色隔离 | 通常不支持 | ✅ 原生支持 |

---

## 🚀 核心功能

- **记忆提取**：从 AI 回复中自动提取思考链，生成结构化记忆节点
- **语义检索**：基于向量相似度 + 图谱扩散，精准召回相关记忆
- **智能合并**：语义级去重（相似度 > 0.85 跳过，0.4–0.85 更新并建立 `REVISED_BY` 边）
- **多角色隔离**：按 `character_name` 隔离，不同角色卡记忆互不污染
- **记忆管理**：支持删除、更新 payload、更新向量的 REST API
- **批量导入**：支持酒馆导出的 JSON 聊天文件
- **角色分析报告**：基于全部记忆生成 LLM 驱动的角色心理分析
- **跨端压缩**：将聊天记录压缩为 `.memory` 文件（约 100 倍压缩比），支持跨端同步

---

## 🏗️ 技术架构

```
┌────────────────────────────────────────────────────────────────┐
│                    客户端（SillyTavern / WebUI）               │
└────────────────────────┬───────────────────────────────────────┘
                         │ HTTP
                         ▼
┌────────────────────────────────────────────────────────────────┐
│                    MemoryHub（FastAPI）                       │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│  │thinking_tracker│  │   merger    │  │      retriever      │  │
│  │  思维链解析   │  │  智能合并   │  │   语义检索+图谱扩散  │  │
│  └─────────────┘  └──────────────┘  └─────────────────────┘  │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────┐
│                     TriviumDB（嵌入式数据库）                  │
│                                                              │
│   向量（1024维 bge-m3） │  Payload（JSON）  │  有向带权图    │
│   （语义检索）         │  （文档过滤）     │  （图谱扩散）   │
└────────────────────────────────────────────────────────────────┘
```

### 技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| **向量+图+文档** | TriviumDB | 原生三位一体，单文件部署，无需外部数据库 |
| **Embedding** | bge-m3 + Ollama | 本地免费，1024 维，中文效果优秀 |
| **LLM 网关（规划中）** | LiteLLM | 统一模型调度、成本可视化、请求监控 |
| **API 服务** | FastAPI + Python | 快速开发，生态成熟 |

---

## 📦 快速开始

### 环境要求

- Python 3.10+
- Ollama（本地 Embedding）
- Docker（可选，用于 LiteLLM 网关）

### 安装

```bash
git clone https://github.com/YOUR_USERNAME/memoryhub.git
cd memoryhub
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env`，填写你的配置：

```ini
# LLM 后端选择
LLM_BACKEND=deepseek

# DeepSeek API
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# 本地 Embedding（Ollama）
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=bge-m3
```

### 启动服务

```bash
uvicorn main:app --reload
```

访问 `http://localhost:8000` 查看 API 文档。

---

## 🔌 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/extract` | 从 AI 回复中提取记忆节点 |
| `POST` | `/retrieve` | 语义检索相关记忆 |
| `POST` | `/import` | 批量导入酒馆聊天文件 |
| `GET` | `/export` | 导出所有记忆节点 |
| `GET` | `/summary` | 生成人类可读的记忆摘要 |
| `POST` | `/report` | 生成角色灵魂分析报告 |
| `DELETE` | `/memory/{id}` | 删除指定记忆节点 |
| `PUT` | `/memory/{id}` | 更新节点 payload |
| `PATCH` | `/memory/{id}/vector` | 更新节点向量 |

---

## 🗂️ 项目结构

```
memoryhub/
├── main.py                 # FastAPI 主入口
├── core/
│   ├── thinking_tracker.py # 思维链解析
│   ├── trivium_store.py    # TriviumDB 存储封装
│   ├── merger.py           # 智能合并去重
│   ├── retriever.py        # 语义检索
│   ├── importer.py         # 聊天文件导入
│   └── extractor.py        # 记忆提取（含 LLM 备用）
├── config.py               # 配置管理
├── requirements.txt
└── .env                    # 环境变量
```

---

## 📌 当前状态与后续规划

### 已完成

- ✅ 思维链解析（支持结构化 thinking 标签）
- ✅ 向量存储 + 语义检索
- ✅ 智能合并去重（语义级）
- ✅ 多角色隔离
- ✅ 记忆 CRUD API
- ✅ 聊天文件批量导入
- ✅ 角色分析报告

### 进行中 / 规划

- ⏳ **LiteLLM 集成**：统一模型网关 + 成本可视化
- ⏳ **透明代理层**：强制所有 LLM 请求输出统一格式 thinking
- ⏳ **前端可视化界面**：记忆图谱浏览 + 节点编辑
- ⏳ **预设适配优化**：通用 LLM 提取 + 正则快速通道

---

## 📄 License

Apache-2.0

---