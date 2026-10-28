# 完整安装选项（Install）

> README 的 Install 是**默认路径**（内置向量引擎，5 步）。本文是**所有选项**：三条向量引擎路径、硬件要求、更换模型与重嵌全库。
> 装不起来先跑 `python scripts/palimpsest_cli.py doctor`——每个失败项都会打印修复命令。

---

## 完整安装选项（含三条向量引擎路径）

### 安装（通用）

> **本项目没有发布到 PyPI。** PyPI 上的 `palimpsest` 是**另一个无关的包**（2023 年上传、此后未更新），
> `pip install palimpsest` 装到的**不是**本项目，也启动不了。请按下述步骤从源码安装。

```bash
git clone https://github.com/JiaY-77/Palimpsest.git
cd Palimpsest

# 需要 Python 3.10+
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
# 内置向量引擎（默认 EMBEDDING_PROVIDER=local 需要；不装则改用 ollama / openai 后端）
# ⚠️ llama-cpp-python 在 PyPI 没有预编译 wheel，直接 pip install 会走源码编译（要求 CMake + MSVC，
#    多数机器会失败）。走官方 CPU wheel 索引：
pip install "llama-cpp-python>=0.3.36,<0.4" \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

### 硬件要求

**没有独显也能跑。** 本地向量模型 `qwen3-embedding:0.6b`（1024 维）在纯 CPU 上的实测：走 Ollama 时端到端检索 209 ms（稳定均值）、索引约 92–296 ms/条——对「对话间隙检索一次记忆」这个使用方式毫无感知。三种硬件档位（纯 CPU / 核显 / 独显）的完整实测数据、复现方式与档位强制手段见 [`docs/HARDWARE.md`](HARDWARE.md)。

内置本地引擎（`EMBEDDING_PROVIDER=local`）走 `llama-cpp-python`，纯 CPU 单条 embedding 约 0.8 秒（Q8_0 量化，2880 条全库重嵌约 40 分钟）；检索端到端耗时尚未在同一张表里补测，需要更快就走 Ollama 路径。

**本地部署的门槛在「有没有模型文件」，不在硬件性能**——模型首次运行自动下载，之后完全离线。

### 路径 A（默认）：内置本地向量模型，零外部依赖

```bash
cp .env.example .env
# 编辑 .env：填入 DEEPSEEK_API_KEY（或改 LLM_BACKEND=ollama 走全本地对话）
```

首次启动自动下载向量模型（`Qwen3-Embedding-0.6B` Q8_0，约 610 MB，默认从 ModelScope 拉取、国内可直连，失败自动回退 HuggingFace），带 sha256 校验与断点续传；只需一次，之后完全离线、数据不出本机。

**已经装过 Ollama 的用户不必重复下载**：把 `EMBEDDING_MODEL_PATH` 指向已有文件即可（Ollama 的 `qwen3-embedding:0.6b` 与这里要的 GGUF 是同一个文件，sha256 一致）。

### 路径 B：复用已有的 Ollama（可选）

```bash
# 1. 安装并启动 Ollama（https://ollama.com）；2. 拉取向量模型
ollama pull qwen3-embedding:0.6b
# 3. .env 里设 EMBEDDING_PROVIDER=ollama
```

Ollama 常驻 11434 端口；本项目的 REST 检测到端口被 Ollama 桌面版占用时会主动让路。

### 路径 C：云端 key（无需本地模型）

```bash
# 编辑 .env：
#    EMBEDDING_API_KEY=你的云端key
#    EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM  按服务商填写
```

> **三条路径通用**：不设置 `EMBEDDING_PROVIDER` 即自动探测（有有效 `EMBEDDING_API_KEY` → 云端，否则 → 内置本地；可显式写 `local` / `ollama` / `openai` 强制）。换 provider = 换向量空间，**必须重建知识库索引**（见 [更换向量模型](#更换向量模型--重嵌全库)）。

### 启动

```bash
# 推荐：部署体检（每个失败项都会打印对应的修复命令）；startup-check 为其轻量子集
python scripts/palimpsest_cli.py doctor
python scripts/palimpsest_cli.py startup-check

# REST 服务 (:8090) —— 唯一写者，所有接入方式都经它
python -m uvicorn main:app --host 127.0.0.1 --port 8090

# CLI 示例 / 索引知识库
python scripts/palimpsest_cli.py search "架构最近发生了什么变化？"
python scripts/build_kb_index.py

# 监控面板：**已融合进 REST**，只跑上面那条 uvicorn 就够了
#   http://127.0.0.1:8090/dashboard
# 想把面板单独暴露在另一个端口（如做反代 / 只给局域网看）时才需要：
#   python scripts/dashboard.py          # :8010，与 REST 内面板同一份路由
```

> **接入方式与单进程约束（重要）**
>
> 首选 **REST 的 `/mcp`**：MCP 客户端接入 `http://127.0.0.1:8090/mcp/`，与 REST 共用同一进程；CLI / 各脚本同样一律经 REST。监控面板也已并入 REST（`/dashboard`），**不需要**再单独起一个进程——它只是把同一个只读视图换个端口暴露时才需要 `scripts/dashboard.py`。stdio `mcp_server.py` 是**逃生梯**（非首选），仅用于**不跑 REST** 的纯 MCP 场景：库文件由 triviumdb 以独占写模式打开，第二个连接会失败并报 `Database locked: ...`，并发写入失败还**会污染文件组**且不自愈。因此 REST 服务禁止多 worker / 多实例，**一个库只应有一个进程访问**；冲突是响亮的——CLI 抛 `DatabaseBusyError`、REST 返回 `503`，不会静默降级。
>
> Embedding 项失败时：本地 Ollama 请先启动并 `ollama pull qwen3-embedding:0.6b`；云端请确认 `.env` 已配置 `EMBEDDING_API_KEY`。

Windows 下 `scripts/start_rest.vbs` 可隐藏窗口启动 REST 服务（如开机自启），日志写入 `scripts/start_rest.log`。

### 装不起来怎么办

先跑体检——它会把每个失败项和对应的修复命令直接打出来：

```bash
python scripts/palimpsest_cli.py doctor
```

最常见的三类原因：

- **Embedding 项失败**：本地 Ollama 没启动或缺模型 → `ollama pull qwen3-embedding:0.6b`；云端请确认 `.env` 里 `EMBEDDING_API_KEY` 已填。
- **Python 版本 / 激活命令**：需要 Python 3.10+；Windows 上激活虚拟环境是 `venv\Scripts\activate`，不是 `source venv/bin/activate`。
- **端口或库占用**：REST 默认 8090，被占用换 `--port`；一个库同时只允许一个进程访问（多开会被 `Database locked` 拒掉）。

体检也解决不了 → 到 [Issues](https://github.com/JiaY-77/Palimpsest/issues/new/choose) 开一条，**把 `doctor` 的完整输出贴进去**（输出会隐去密钥；贴之前自己再扫一眼有没有真实 key）。报清楚「OS / Python 版本 / 卡在哪一步」，比一句「跑不起来」有用得多。

**MCP 客户端接入（推荐：HTTP，与 REST 共进程）**

```json
{ "mcpServers": { "palimpsest": { "url": "http://127.0.0.1:8090/mcp/" } } }
```

**备份与恢复**：`python scripts/backup_db.py --keep 7` 做整文件组冷备份 + 回读校验（默认保留 7 份）。备份必须**整组**（`.db` / `.vec` / `.gidx` / `.pidx` / `.flush_ok` / `.pld.*` / `.wal`，同属一个 generation），脚本拷贝后会回读校验，不通过即报错退出。

## 从 2.15 及更早版本升级

升级路径上有三个**静默**陷阱（都不报错，只让你觉得「功能坏了」）：

### 1. `EMBEDDING_PROVIDER` 必须显式写

2.16.0 起默认值从 `ollama` 改成 `local`（进程内 llama.cpp）。老 `.env` 通常**没有**这一项，升级后会静默切到内置后端——缺 `llama-cpp-python` 或 GGUF 模型文件时 embedding 直接不可用，而配置文件上**看不出任何变化**。

```bash
grep EMBEDDING_PROVIDER .env    # 没输出 → 补上你原本在用的那个（ollama / openai）
```

启动自检会回显来源：看到 `provider=local，.env 未显式设置 → 自动探测` 就说明它在用默认值；老库还会多一句 WARN。

### 2. 存量库必须重跑一次 reindex

向量空间指纹是 2.16.0 才引入的，存量库没有这条记录 → `/doctor` 第 9 项「向量空间一致性」**必然 FAIL**（与有没有换过 provider 无关）。

规模参考（免得被另一处「2880 条约 40 分钟」劝退）：

| 节点数 | 环境 | 实测耗时 |
|---|---|---|
| 700 | Ollama 本地，模型热态 | **约 70 秒**（≈0.1 秒/条） |

### 3. 正确顺序

```bash
# 1) 确认 provider（见第 1 条）
# 2) 停 REST 服务——reindex 直连库，服务在跑会报 Database locked
# 3) 全量重嵌。--yes 不可省：--force 只跳「指纹确认」，不跳最后那次「确认？」
python scripts/palimpsest_cli.py reindex --force --yes
# 4) 起 REST 服务
# 5) 体检应回到 9/9
curl -s http://127.0.0.1:8090/doctor
```

## 更换向量模型 / 重嵌全库

切换 embedding 模型或 provider 会改变向量空间，**必须重嵌全库**。库内会记录一条**向量空间指纹**（`provider` / `model` / `dim` / `pooling` 的哈希）；启动自检、`doctor` 与 `reindex` 开跑前都会比对它——不一致时 `doctor` 该项判失败并直接给出修复命令。换模型 / 换 provider 后跑 `reindex --force --yes` 一次到位：放弃断点、跳过指纹确认、全量重算，完成时写入新指纹。完整操作顺序（`--check` / `--dry-run` / `--yes`）与换维度的重建流程见 [`docs/CONFIGURATION.md`](CONFIGURATION.md)。
