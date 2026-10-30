<div align="center">

# Palimpsest

**All You Need Is Just Memory.**

**Local-first · 向量 / 全文 / 图谱 / 时间，四种索引一个内核 · MCP + REST + CLI 共用同一套工具**

[![Version](https://img.shields.io/badge/Version-v2.20.0-4c6ef5.svg)](/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Backends](https://img.shields.io/badge/Backends-DeepSeek%E2%80%A2Ollama%E2%80%A2Built--in-6f42c1.svg)](/)
[![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml)
[![LTS](https://img.shields.io/badge/LTS-v2.20-2ea44f.svg)](docs/LTS.md)

**中文** | [English](./README_EN.md)

> **v2.20.0 是首个 LTS 版本**：接口冻结、默认行为与数据格式不变，支持至 **2027-10-30**。承诺细则见 [docs/LTS.md](docs/LTS.md)。

</div>

> _Palimpsest_：拉丁语，原指「重写的羊皮纸」——旧字迹被覆写抹去，却又在岁月里重新透出。这里把同一个意象用在记忆上：**新事实覆盖旧事实，但旧迹永不真正丢失**，每次改写都沿一条可查的 **版本链**（`REVISED_BY`）连接。

---

## What it is

一个**本地记忆库**：把 AI 会话里的收获写成节点，存进本机一个数据库，之后任何会话都能**按意思**（不是按关键词）找回来。

- **不需要 API key 就能跑。** 存储、检索、图谱、版本链全部本地完成；LLM 只在生成报告时用到，可以完全不配。
- **零外部依赖。** 内置向量引擎（`llama-cpp-python` + 自带 GGUF 模型），装完即可离线运行；Ollama 只是可选的加速路径，不是必需。
- **一个进程是唯一写者。** REST `:8090` 是唯一入口，MCP / CLI / 各脚本都经它，所以不存在多进程写坏库。
- **数据就是一个目录。** `data/` 下的 `.db` + `.vec` / `.gidx` / `.pidx` 侧车 + `fts.db`，整组备份即可搬家。恢复后跑 doctor（`python scripts/palimpsest_cli.py doctor`）确认 FTS 索引完整。

## Docker（最快）

```bash
git clone https://github.com/JiaY-77/Palimpsest.git
cd Palimpsest
cp .env.example .env          # 公网部署：填入 PALIMPSEST_API_KEY（生成：openssl rand -hex 32）
docker compose up -d --build  # 首次构建会预置 610MB 向量模型并自动校验
curl -s http://127.0.0.1:8090/health
```

单实例自托管（一个库只允许一个进程写入）；内存规格、公网安全、备份与升级见 **[docs/DEPLOY.md](docs/DEPLOY.md)**。

---

## Install

**全程 5–10 分钟，不需要任何 API key。** 向量模型约 610 MB，仅首次下载。

### 1. 拉代码、装依赖

```bash
python --version                  # 需要 3.10+
git clone https://github.com/JiaY-77/Palimpsest.git
cd Palimpsest
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

> ⚠️ 本项目**没有发布到 PyPI**。`pip install palimpsest` 装到的是 2023 年上传的另一个无关包，启动不了。

**成功判据**：`pip install` 退出码 0，且 `python -c "import fastapi, triviumdb"` 无输出。

### 2. 向量引擎（默认已定，不用选）

不设置 `EMBEDDING_PROVIDER` 时自动探测：有云端 `EMBEDDING_API_KEY` → `openai`，否则 → **内置 `local`（推荐，零外部依赖）**。

```bash
cp .env.example .env

# llama-cpp-python 在 PyPI 没有预编译 wheel，直接装会走源码编译（要求 CMake + MSVC，多数机器失败）：
pip install "llama-cpp-python>=0.3.36,<0.4" \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

首次启动自动下载 `Qwen3-Embedding-0.6B` Q8_0（约 610 MB，默认 ModelScope、国内可直连，失败自动回退 HuggingFace），带 sha256 校验与断点续传。**只需一次，之后完全离线。**

**本机已有 Ollama？** 复用它省掉上面的下载（可选，不是必需）：

```bash
ollama pull qwen3-embedding:0.6b
echo "EMBEDDING_PROVIDER=ollama" >> .env
```

**成功判据**：`pip install` 无 CMake / MSVC 报错（模型在下一步首次启动时下载）。

### 3. 起服务

```bash
python scripts/palimpsest_cli.py doctor      # 部署体检：每个失败项都会直接打印修复命令
python -m uvicorn main:app --host 127.0.0.1 --port 8090
```

> 服务**禁止多 worker / 多实例**：库文件由 triviumdb 以独占写模式打开，第二个写连接会失败并报 `Database locked`，并发写入失败还可能污染文件组且不自愈。冲突是响亮的（CLI 抛 `DatabaseBusyError`、REST 返 `503`），不会静默降级。

Windows 可用 `scripts/start_rest.vbs` 隐藏窗口启动（日志写 `scripts/start_rest.log`）。

**成功判据**：`doctor` 全部 OK，服务常驻。

### 4. 验证（三条全过才算装好）

```bash
# ① 服务活着
curl -s http://127.0.0.1:8090/
# 返回 {"service":"Palimpsest","version":"v2.17.0","endpoints":[...]}

# ② 体检全绿
python scripts/palimpsest_cli.py doctor

# ③ 能写能读 —— 验的是「语义召回通不通」
python scripts/palimpsest_cli.py ingest "install smoke test"
python scripts/palimpsest_cli.py search "装好了吗"
# 返回 {"results":[{"id":1,"type":"memory","score":0.4815,"summary":"install smoke test",...}]}
```

③ 是真正的验收：**写进去是英文、问的是中文、一个字都不重合却命中了**。如果这步只能靠关键词匹配才成功，说明向量链路没通。

### 5. 接到你的 agent

MCP 客户端（推荐，与 REST 共进程）：

```json
{ "mcpServers": { "palimpsest": { "url": "http://127.0.0.1:8090/mcp/" } } }
```

Hermes（双插件：Memory Provider + Context Engine）：

```bash
mkdir -p ~/.hermes/plugins/palimpsest && cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

**成功判据**：agent 的工具列表出现 `palimpsest_search` / `palimpsest_ingest`，且一次查询能召回你自己的记忆。

> 接入面是标准协议：MCP（stdio / streamable-http）、REST、CLI 指向同一个进程与同一套底层工具，因此**多个客户端可以同时接入同一座记忆库**；多写入方的一致性规范仍在设计中，当前建议以单一主写入方为准。
>
> **三条向量引擎路径、硬件要求、换模型与重嵌全库 → [`docs/INSTALL.md`](docs/INSTALL.md)**

---

## What it solves

| 症状 | 它怎么解 | 自己验一下 |
|---|---|---|
| 换个会话它就忘了我们聊过什么 | 高信号要点写入本地库；新会话按**语义**召回，不靠关键词命中 | `mem_search "上次讨论的架构"` |
| 记忆越堆越乱，同一件事好几个版本 | 取代由**写入方显式声明**（`replaces`）；旧版标 `outdated` 并留 `REVISED_BY` 链，永不静默覆盖 | `mem_version_history <id>` |
| 有一堆 Markdown，AI 读不过来 | 按 `##`/`###` **标题**切片入库（标题＝人写下的语义边界），`[[双链]]` 转图谱边 | `build_kb_index.py` → `kb "关键词"` |
| 它记不住「当时为什么这么定」 | 节点带类型 / 重要度 / 时间维度；推理过程另存为 `thought` 节点 | `mem_fact_history <id>` |
| 记忆存在别人服务器上 | 全部在本机 `data/` 目录；冷备份整组文件即可搬家 | `python scripts/backup_db.py --keep 7` |
| 多轮之后上下文爆掉 | 检索默认只回 **150 字摘要 + 元数据**，全文按需二次拉取 | 看 `mem_retrieve` 返回体积 |

**一条设计底线**：任何节点默认**不会因为相似度高就被自动取代**。相似只作相关提示（`related_ids`），要不要取代由有完整上下文的那一方说了算——因为「我喜欢吃苹果」和「我喜欢吃苹果派」相似度很高，但根本不是同一件事。

**日常怎么用**（该记什么、`importance` 怎么给、什么时候该取代）见 **[`docs/USAGE.md`](docs/USAGE.md)**。

---

## Why not the others

| | md 文件夹 | 纯向量库 | 云托管记忆 | **Palimpsest** |
|---|---|---|---|---|
| 存什么 | 文件 | 向量 | 向量 / 文本 | 节点 + 加权边 + 向量 + 时间 |
| 排序 | 无 | 有 | 有 | 有（重要性 / 时间衰减 / 命中计数） |
| 冲突治理 | 无 | 无 | 部分 | **显式取代 + `REVISED_BY` 版本链** |
| 语义召回 | 无（grep） | 有 | 有 | 有（向量 + FTS5 + RRF 融合） |
| 关系 | 无 | 无 | 部分 | **加权图 + BFS 扩散 + Leiden 社区** |
| 版本可溯 | 无 | 无 | 少见 | **bi-temporal（`valid_at` / `invalid_at`）** |
| 数据归属 | 你的 | 你的 | 平台的 | **你的** |
| 安装成本 | 零 | 中 | 零（要账号） | **中（Python + 向量模型）** |

**三件不回避的事：**

1. **安装比 md 文件夹麻烦。** 这一点 md 方案赢，承认。装完之后才是它崩的地方——文件到几百上千个时，排序、冲突治理、语义召回、版本链四个问题会一起出现。
2. **单机单进程。** 一个库同时只允许一个写进程，不是为多端共用设计的。
3. **多写入方的一致性规范还没做。** 当前建议以单一主写入方为准。

> 机制清单、与 LangGraph Store / AutoGen Memory 的逐项对比、Markdown 在这里的位置、适用场景 → **[`docs/DESIGN.md`](docs/DESIGN.md)**

---

## 出问题怎么办

先跑体检——它会把每个失败项对应的修复命令直接打出来：

```bash
python scripts/palimpsest_cli.py doctor
```

| 症状 | 原因 | 修法 |
|---|---|---|
| `Embedding` 项失败 | 内置模型缺失 / 损坏；或 Ollama 未启动；或云端 key 未配 | 删掉模型文件重下；`ollama pull qwen3-embedding:0.6b`；或填 `EMBEDDING_API_KEY` |
| `Database locked` / REST `503` | 同一库被第二个进程打开 | 只留一个写进程，先停掉旧的 REST / CLI |
| 向量维度不一致 | 换过 embedding 模型或 provider | `reindex --force --yes` 全量重算并写入新指纹 |
| `pip install llama-cpp-python` 编译失败 | 走了源码编译路径 | 用官方 CPU wheel 索引（见 Install 步骤 2） |
| 端口被占用 | 8090 已被别的进程监听 | `uvicorn --port <其他>`，或先查出占用进程 |

**REST 是唯一写者。** CLI / dashboard / 各脚本都经 REST；stdio `mcp_server.py` 只在不跑 REST 的纯 MCP 场景使用（逃生梯），因为它会以独占写模式直接打开库文件。

体检也解决不了 → 开一条 issue：**[新建 issue](https://github.com/JiaY-77/Palimpsest/issues/new/choose)**。带上这三样，处理速度快十倍：

1. **`doctor` 的完整输出**（会隐去密钥；贴之前自己再扫一眼有没有真实 key）
2. **OS / Python 版本 / 卡在第几步**（对应 Install 的步骤编号）
3. **能复现的命令**（一条就够，贴完整调用，而不是「跑不起来」）

- Bug → [`bug.yml`](.github/ISSUE_TEMPLATE/bug.yml)　功能请求 → [`feature.yml`](.github/ISSUE_TEMPLATE/feature.yml)
- 安全漏洞**不要**开公开 issue，见 [`SECURITY.md`](SECURITY.md)
- 想改代码：先看 [`CONTRIBUTING.md`](CONTRIBUTING.md)（开发环境 / 加工具 / 测试要求 / commit 规范 / PR 流程）
- 合并门禁：`pytest` + `ruff check` + `ruff format --check` + `mypy` + `scripts/readme_check.py --strict`，五条全绿才进

---

## 配置

所有配置均从环境变量读取（`.env` 由 `python-dotenv` 自动加载），带注释模板见 `.env.example`。新用户通常只需要这几项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REST_PORT` | `8090` | FastAPI REST 服务端口 |
| `DB_PATH` | `data/mh_memory.db` | 嵌入式 TriviumDB 数据库路径 |
| `SESSION_BUFFER_MIN_THOUGHT_CHARS` | `400` | 会话缓冲只收实质轮的门槛：本轮有工具调用 / 思考正文 ≥ 此值且含结论字段 / 用户消息含指令动词，任一满足才进（详见 docs/CONFIGURATION.md） |
| `SESSION_CAPSULE_RECOVER_ENABLED` | `1` | 会话胶囊断线自愈总开关：后台每 `INTERVAL` 秒扫描一次，把静默超过阈值的活跃会话缓冲补产成胶囊（进程被硬砍、session-end 没跑时的兜底）；`0` = 不启后台循环，手动端点仍可用 |
| `SESSION_CAPSULE_RECOVER_AFTER_SECONDS` | `1800` | 孤儿判定阈值（秒）：缓冲最后活动距今超过它才补产。刻意保守——误判不丢轮次，session-end 会把全部缓冲重合成同一胶囊节点 |
| `SESSION_CAPSULE_RECOVER_INTERVAL_SECONDS` | `300` | 后台扫描间隔（秒） |
| `EMBEDDING_PROVIDER` | *（空 = 自动探测）* | 向量后端：留空自动探测（有云端 key → `openai`，否则 → `local`），或显式 `local` / `ollama` / `openai` |
| `DEEPSEEK_API_KEY` | *（空）* | 生成报告用；不配也能跑 |
| `KNOWLEDGE_DIR` | *（可选）* | 知识库根目录（待索引的 Obsidian `.md` 文件） |
| `PIN_DOMAIN` | *（空 = 关闭）* | 常驻域：该 domain 的 `active` 节点每轮**无条件**注入（不参与语义相似度竞争），用于「必须每轮生效」的规则 |
| `PIN_BUDGET_CHARS` | `3000` | 常驻块字符预算，按节点边界截断；超预算在 `pin.truncated` / `pin.dropped` 报告 |

完整配置表（77 项，含默认值与生效条件）见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。

---

## 更换向量模型

切换 embedding 模型或 provider 会改变向量空间，**必须重嵌全库**。库内记录一条**向量空间指纹**（`provider` / `model` / `dim` / `pooling` 的哈希）；启动自检、`doctor` 与 `reindex` 开跑前都会比对它——不一致时 `doctor` 该项判失败并直接给出修复命令。换模型 / 换 provider 后跑 `reindex --force --yes` 一次到位（`--yes` 跳最后那次重嵌确认；CLI 经 REST 业务端点执行，无需停 REST）：放弃断点、跳过指纹确认、全量重算，完成时写入新指纹。完整操作顺序见 [`docs/INSTALL.md`](docs/INSTALL.md)。

---

## 文档地图

README 只放「上手要用的」。其余按职责分在 `docs/`——**加东西时按这张表找位置，不要往 README 里堆**。

| 你想做什么 | 看这篇 |
|---|---|
| 装（三条向量引擎路径、硬件要求、换模型） | [docs/INSTALL.md](docs/INSTALL.md) |
| 查命令（21 个 MCP 工具 / 25 个 CLI 子命令 / 44 条 REST 路由 / 区块语义 / 索引规则） | [docs/REFERENCE.md](docs/REFERENCE.md) |
| 知道什么时候用哪个工具 | [docs/USAGE.md](docs/USAGE.md) |
| 配环境变量（77 项全表） | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| 懂设计（机制清单、与其它方案对比、Markdown 定位、适用场景） | [docs/DESIGN.md](docs/DESIGN.md) |
| 跑测试 / 读评测 / 看仓库结构 / 开发约定 | [docs/TESTING.md](docs/TESTING.md) |
| 改代码（分层约定、加工具、门禁） | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 接 Hermes（双插件七步接入） | [docs/HERMES_INTEGRATION.md](docs/HERMES_INTEGRATION.md) |
| 接 Obsidian 知识库 | [docs/OBSIDIAN.md](docs/OBSIDIAN.md) |
| 双时态 / 安全 / 记忆包 / 完整 API | [BITEMPORAL](docs/BITEMPORAL.md) · [SECURITY](SECURITY.md) · [MEMORYHUB](docs/MEMORYHUB.md) · [API](docs/API.md) |
| 检索质量与实测数据 | [RETRIEVAL_QUALITY](docs/RETRIEVAL_QUALITY.md) · [CONFIDENCE](docs/CONFIDENCE.md) |
| 长期支持承诺 / 版本规则 / 接口稳定性分级 | [LTS](docs/LTS.md) |
| 发版流程 / 变更历史 / 弃用计划 | [RELEASING](docs/RELEASING.md) · [CHANGELOG](CHANGELOG.md) · [DEPRECATIONS](docs/DEPRECATIONS.md) |

---

## License

[MIT](LICENSE) © JiaY-77
