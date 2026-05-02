# MemoryHub — 为角色扮演而生的 AI 记忆引擎

> 让 AI 不再忘记的「第二大脑」。
> 基于结构化思维链的长期记忆系统。

---

## 一句话介绍

MemoryHub 是一个专为 SillyTavern 设计的**独立记忆后端服务**。
它通过捕获大语言模型的内部思考过程（思维链），构建结构化的角色扮演记忆图谱，
并通过 API 提供记忆的存储、合并、检索与上下文注入能力。

---

## 为什么需要 MemoryHub？

### 现有记忆方案的局限

| 方案 | 核心问题 |
|------|----------|
| 纯摘要 | 因果链丢失，只剩「A 做了 B」 |
| 记忆表格 | 角色脸谱化，越聊越笨 |
| 分层总结机 | 压缩过程中主观评价和动机被洗掉 |
| 纯向量 RAG | 检索零散，无法回溯完整叙事线 |

### MemoryHub 的创新

MemoryHub 不记忆 AI 的「输出文本」，而是记忆 AI 的「思考过程」。
它直接从 DeepSeek 的原生思维链（`reasoning_content`）和预设立场（preset）中
提取结构化的记忆节点和边，构建一张可追溯、可修正、可演化的记忆图谱。

**核心优势**：
- **记住「为什么」而不是「什么」**：保留角色的动机、情感和因果链
- **非破坏性更新**：修正记忆时不删除旧版本，保留 `REVISED_BY` 边
- **独立后端服务**：与酒馆解耦，数据可跨会话持久化
- **合并去重**：自动识别重复记忆，避免数据库膨胀

---

## 技术架构

```
┌──────────────────────────┐
│  SillyTavern（前端）     │
│  Service Plugin (计划中)  │
└─────────┬────────────────┘
          │ HTTP POST /retrieve  /extract
          ▼
┌──────────────────────────┐
│  MemoryHub（FastAPI）     │
│                          │
│  ├── extractor.py        │  ← 记忆提取入口
│  ├── thinking_tracker.py │  ← 思维链解析
│  ├── merger.py           │  ← 合并去重
│  ├── retriever.py        │  ← 记忆检索
│  └── trivium_store.py    │  ← 存储封装
└─────────┬────────────────┘
          │
          ▼
┌──────────────────────────┐
│  TriviumDB               │
│  向量 + 图 + 文档        │
│  三合一嵌入式数据库      │
└──────────────────────────┘
```

---

## 核心模块

| 模块 | 文件 | 职责 | 状态 |
|------|------|------|:--:|
| 思维链解析 | `thinking_tracker.py` | 解析 `<thinking>` 块，提取剧情/角色/计划/意图 | ✅ |
| 记忆提取 | `extractor.py` | 统一提取入口，优先思维链，备用 LLM 分析 | ✅ |
| 合并去重 | `merger.py` | 新记忆与旧记忆比对，自动跳过重复 | ✅ |
| 记忆检索 | `retriever.py` | 关键词匹配检索，返回相关性排序 | ✅ |
| 存储封装 | `trivium_store.py` | TriviumDB 节点/边操作、索引管理 | ✅ |
| API 服务 | `main.py` | FastAPI 端点 `/extract`, `/retrieve` | ⬜ 待接入 |
| 核心验证 | `verify_core_loop.py` | 集成测试：提取 → 合并 → 存储 → 检索 | ✅ |

---

## 快速上手

### 1. 环境要求

- Python 3.12+
- DeepSeek API Key（或兼容 OpenAI API 的后端）

### 2. 安装

```bash
git clone https://github.com/JiaY-77/memoryhub.git
cd memoryhub
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install triviumdb fastapi uvicorn openai python-dotenv
```

### 3. 配置 `.env`

```
LLM_BACKEND=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_MODEL=deepseek-v4-flash
DB_PATH=data/mh_memory.db
```

### 4. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### 5. 运行核心验证

```bash
python verify_core_loop.py
```

预期输出：第一次运行创建 10 个记忆节点，第二次运行全部显示「与已有记忆重复，已跳过」。

---

## 设计哲学

> **记住渔网，而不是记住鱼。**

传统记忆系统试图记住 AI 说了什么（「鱼」）。
MemoryHub 记住的是 AI 为什么这么说（「渔网」）——它的思考路径、角色动机、因果关系。

这条思考路径可以被任何一个 AI 模型用来重演角色的决策逻辑，
即使换了模型、换了角色卡，角色的内核也不会丢失。

---

## 路线图

- [x] **V0.9** (2026-05-02)  
  思维链捕获 + 结构化提取 + 合并去重 + 存储 + 检索，完整闭环验证通过

- [ ] **V1.0**  
  接入 SillyTavern 服务端插件，实现自动提取和注入

- [ ] **V1.1**  
  管理面板（Element Plus 前端），支持可视化记忆图谱

- [ ] **V1.2**  
  真实 Embedding 语义检索

- [ ] **V2.0**  
  记忆遗忘策略、矛盾处理、多角色记忆隔离

---

## License

Apache-2.0