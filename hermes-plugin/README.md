# Palimpsest × Hermes 插件

把 **Hermes 的记忆层换成 Palimpsest**：中文语义召回、加权知识图谱、规则路由、自动沉淀，全部跑在本地。

本目录是 **Palimpsest 的 Hermes 双插件**源码，包含两个独立插槽的实现：

| 插槽 | 类 | 作用 |
|---|---|---|
| **Memory Provider** | `PalimpsestMemoryProvider`（`__init__.py`） | 每轮自动召回相关历史记忆注入上下文；强信号自动沉淀；会话结束提炼要点 |
| **Context Engine** | `PalimpsestContextEngine`（`context_engine.py`） | 上下文压缩前，用 Palimpsest 图谱提炼关键链，喂给压缩阶段 |

## 文件结构

```
hermes-plugin/
├── __init__.py          # Memory Provider 主实现（5 个模型工具 + 生命周期 hooks）
├── context_engine.py    # Context Engine（图谱增强压缩）
├── cli.py               # hermes palimpsest status/test 子命令
├── config_schema.py     # 配置面板声明（Hermes dashboard 渲染用）
└── plugin.yaml          # 插件清单（kind=standalone，hooks 声明）
```

## 安装

前置条件：本仓库已按 README 完成 **Palimpsest 本体安装**（REST 服务 `:8090` 常驻）。

把本目录部署到 Hermes 插件目录：

```bash
# 找到 Hermes 插件目录（默认 ~/.hermes/plugins/）
mkdir -p ~/.hermes/plugins/palimpsest
cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
```

> Windows 示例：`C:\Users\<你>\AppData\Local\hermes\plugins\palimpsest\`

激活（一行一件）：

```bash
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

## 配置

通过环境变量配置（全部可选，默认指向本机 Palimpsest）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PALIMPSEST_BASE_URL` | `http://127.0.0.1:8090` | Palimpsest REST 服务地址 |
| `PALIMPSEST_DOMAIN` | `hermes` | 记忆域（节点隔离） |
| `PALIMPSEST_PREFETCH_TOP_K` | `5` | 每轮自动召回条数 |
| `PALIMPSEST_AUTO_INGEST` | `true` | 是否自动沉淀；`false` 时仅保留 5 个手动工具 |
| `PALIMPSEST_GRAPH_TOPICS` | `3` | 压缩前图谱提炼的主题数（1-5） |

## 验证

```bash
hermes palimpsest status   # 检查 REST 可达性 + 语义层端点
hermes palimpsest test     # 端到端自检：search + ingest 连通性
```

完整接入步骤见 [`docs/HERMES_INTEGRATION.md`](../docs/HERMES_INTEGRATION.md)。

## 行为细节

- **fail-open**：Palimpsest 不可达/超时/报错时，召回与压缩增强自动降级为空，绝不阻塞会话。
- **自动沉淀克制**：不是每轮都写库——只有命中强信号（纠正/偏好/决策/规则）才写入，避免低价值轮次污染记忆库。
- **trivial 过滤**：`好的` / `嗯` / `继续` 等寒暄输入跳过召回，省 HTTP 往返。
- **cron/flush 跳过**：cron 与 flush 会话不初始化记忆层（防污染）。
- **5 个模型工具**：`palimpsest_search` / `palimpsest_ingest` / `palimpsest_link` / `palimpsest_graph` / `palimpsest_router`（规则路由），agent 可主动调用。
