# 将 Palimpsest 接入 Hermes（换脑指南）

> 本文档面向 **Hermes 用户**：把 Hermes 的默认记忆层替换为 Palimpsest，实现中文语义召回、知识图谱、规则路由与自动沉淀。全文分七步，**每步都有验证方法**，照做即可完成接入。
>
> 适用版本：Palimpsest v1.0.0 · Hermes 任意支持 memory provider / context engine 插槽的版本。

---

## 总览

接入后，Hermes 的每一轮对话都会发生这些事：

| 环节 | 触发时机 | 作用 |
|---|---|---|
| 自动召回 | 每轮对话前 | 检索 Palimpsest 中的相关历史记忆，注入上下文 |
| 自动沉淀 | 每轮对话中 | 检测到强信号（纠正/偏好/决策/规则）时写入记忆库 |
| 会话提炼 | 会话结束时 | 把本轮要点整理成一条结构化记忆 |
| 压缩前提炼 | 上下文压缩前 | 用 Palimpsest 图谱提炼关键链，喂给压缩阶段 |

---

## 第一步：安装 Palimpsest 本体

### 1.1 克隆仓库

```bash
git clone https://github.com/JiaY-77/Palimpsest.git
cd Palimpsest
```

### 1.2 创建虚拟环境并安装依赖

要求 **Python 3.10+**：

```bash
python -m venv venv
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 1.3 准备本地向量模型（默认方案）

Palimpsest 默认使用本地 Ollama 做向量化（数据不出本机）：

```bash
ollama pull qwen3-embedding:0.6b
```

### 1.4 创建配置文件

```bash
cp .env.example .env
```

**验证第一步**：

```bash
python scripts/palimpsest_cli.py startup-check
```

看到 `✓` 的项越多越好；`Embedding` 项若失败，多半是 Ollama 未启动或模型未拉取（回到 1.3）。

---

## 第二步：启动 Palimpsest REST 服务（常驻）

Hermes 插件通过 REST 与 Palimpsest 通信，服务需**持续运行**在 `127.0.0.1:8090`。

### 2.1 前台启动（验证用）

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8090
```

看到 `Uvicorn running on http://127.0.0.1:8090` 即成功。

### 2.2 常驻方案（生产用，二选一）

- **Linux / macOS**：用 systemd / launchd 或 `nohup python -m uvicorn main:app --host 127.0.0.1 --port 8090 &`
- **Windows**：仓库自带 `scripts/start_rest.vbs`（隐藏窗口启动），可放入「启动」文件夹实现开机自启，日志写 `scripts/start_rest.log`

**验证第二步**（新开终端）：

```bash
curl http://127.0.0.1:8090/
```

应返回 JSON，含 `"service": "Palimpsest"` 与 `endpoints` 列表（含 `/mem/search`、`/mem/ingest`）。

---

## 第三步：部署 Hermes 插件

插件源码在本仓库的 `hermes-plugin/` 目录（含 `plugin.yaml`、`__init__.py`、`context_engine.py` 等）。

### 3.1 复制到 Hermes 插件目录

Hermes 的插件目录默认是 `~/.hermes/plugins/`（若设置了 `$HERMES_HOME` 则为其下 `plugins/`）：

```bash
mkdir -p ~/.hermes/plugins/palimpsest
cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
```

> Windows 示例：`C:\Users\<你的用户名>\AppData\Local\hermes\plugins\palimpsest\`

### 3.2 激活插件并切换记忆层

```bash
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

> 三条命令含义：
> - `plugins enable palimpsest` —— 启用插件本身
> - `memory.provider palimpsest` —— 让 Hermes 使用 Palimpsest 作为记忆读写后端
> - `context.engine palimpsest-graph` —— 让上下文压缩使用图谱增强版本

**验证第三步**：

```bash
hermes palimpsest status
```

若 REST 已启动，应显示 `✓ Palimpsest REST ... 正常` 且语义层端点齐备。

---

## 第四步：配置向量后端（二选一）

Palimpsest 的向量化支持两种后端，在 `.env` 中切换。

### 方案 A：本地 Ollama（默认，数据不出本机）

```env
EMBEDDING_PROVIDER=ollama
OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b
OLLAMA_EMBEDDING_DIM=1024
```

**数据安全提示**：所有记忆与知识切片均在本机向量化，不上传任何内容。

### 方案 B：云端 OpenAI 兼容接口（需 API Key）

```env
EMBEDDING_PROVIDER=openai
EMBEDDING_API_KEY=你的_API_KEY
EMBEDDING_BASE_URL=https://api.voyageai.com/v1   # 或任意 OpenAI 兼容端点
EMBEDDING_MODEL=voyage-3
EMBEDDING_DIM=1024
```

**数据安全提示**：选择云端方案前，请确认你的组织允许将记忆内容发送至该服务商；涉及敏感数据时优先使用本地 Ollama。

> **注意**：更换向量后端会改变向量空间，**必须重建知识库索引**（见第六步），否则检索结果会错乱。

---

## 第五步：配置 Hermes 侧环境变量（可选）

插件默认即可工作，以下环境变量按需覆盖（在 Hermes 的运行环境中设置）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PALIMPSEST_BASE_URL` | `http://127.0.0.1:8090` | Palimpsest REST 地址 |
| `PALIMPSEST_DOMAIN` | `hermes` | 记忆域（多 agent 隔离用） |
| `PALIMPSEST_PREFETCH_TOP_K` | `5` | 每轮自动召回条数 |
| `PALIMPSEST_AUTO_INGEST` | `true` | 自动沉淀开关；`false` 时只保留手动工具 |
| `PALIMPSEST_GRAPH_TOPICS` | `3` | 压缩前图谱提炼的主题数（1–5） |

**验证第五步**：无需专门验证，配置项由第四步/第六步的验证间接覆盖。

---

## 第六步：把 Obsidian 知识库建成可检索资产（可选但推荐）

若你使用 Obsidian，可以让 Palimpsest 把整库 `.md` 变成可语义检索的知识源。

### 6.1 指定知识库目录

在 `.env` 中：

```env
KNOWLEDGE_DIR=/绝对/路径/到/你的/知识库
```

### 6.2 构建索引

```bash
python scripts/build_kb_index.py          # 增量（只重建变化的文件）
python scripts/build_kb_index.py --full   # 全量重建（换向量后端后必做）
```

**验证第六步**：

```bash
python scripts/palimpsest_cli.py kb search "你的知识库里的任意主题"
```

应返回相关切片（`kb_chunk`），每条带相关度分与来源文件。

---

## 第七步：端到端验证（验收清单）

| # | 验证项 | 命令 / 操作 | 预期结果 |
|---|---|---|---|
| 1 | REST 服务存活 | `curl http://127.0.0.1:8090/` | 返回 JSON，含 `service: Palimpsest` |
| 2 | 插件已激活 | `hermes plugins list`（或对应命令） | 列表含 `palimpsest` |
| 3 | 记忆后端已切换 | `hermes config get memory.provider` | 输出 `palimpsest` |
| 4 | 上下文引擎已切换 | `hermes config get context.engine` | 输出 `palimpsest-graph` |
| 5 | 插件自检 | `hermes palimpsest status` | `✓ REST 正常`，语义层端点齐备 |
| 6 | 端到端连通 | `hermes palimpsest test` | search + ingest 均通，输出 `端到端 OK` |
| 7 | 自动召回生效 | 新开会话，问一个与旧会话相关的问题 | 上下文出现 `[Palimpsest 记忆注入]` 片段 |
| 8 | 自动沉淀生效 | 会话中说一句带「记住 / 以后 / 偏好」的话 | 可在库中检索到该条（`palimpsest search` 或 dashboard） |
| 9 | 会话提炼生效 | 正常结束会话 | 库中出现 `会话要点` 类型的记录 |
| 10 | 知识库检索生效（可选） | `palimpsest_cli.py kb search "主题"` | 返回 `kb_chunk` 切片 |

> 第 7–9 项需要真实对话触发，属运行期验证；第 1–6 项为静态验证，接入完成即可通过。

---

## 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `palimpsest status` 显示 REST 不可达 | REST 服务未启动 | 回到第二步，确认 `:8090` 常驻 |
| 检索结果为空 | 记忆库为空，或 Embedding 后端未就绪 | 先写入几条记忆；`startup-check` 检查 Embedding |
| 换向量后端后检索错乱 | 未重建索引 | 执行 `build_kb_index.py --full` |
| 自动沉淀没有触发 | 内容未命中强信号词 | 属正常（启发式克制设计）；或用 `palimpsest_ingest` 手动写入 |
| 插件命令不存在 | 插件未启用或未复制到目录 | 回到第三步，确认目录与 `plugins enable` |

---

## 相关文档

- 插件细节与配置：`hermes-plugin/README.md`
- 安装与配置总览：仓库根目录 `README.md`
- 维护与发布：`docs/RELEASING.md`
