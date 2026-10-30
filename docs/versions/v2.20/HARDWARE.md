# 硬件要求与本地 embedding 性能

> 本地 embedding 到底需要什么机器？本文给出三种硬件档位的实测数据，证明**没有独显不是使用门槛**。

## 结论先行

`qwen3-embedding:0.6b`（本地默认向量模型）在**纯 CPU** 与**核显**上均可正常使用：

- **端到端检索三档体感接近**：纯 CPU 209 ms、核显 120 ms、独显 160 ms（稳定均值）——**差别远小于单条嵌入的差距**，因为延迟大头在检索与序列化，不在向量化。这个量级对「对话间隙调用一次记忆检索」毫无感知。
- **索引是一次性成本**：纯 CPU 约 92–296 ms/条，索引 1000 条约 5 分钟；独显约 35–44 ms/条，1000 条约 1 分钟。即便最弱档位也完全可接受。
- **无需任何 GPU**：纯 CPU 档位的实测数据已覆盖「最差情况」。
- **长处体现的场景**：单条长文本嵌入时差距最大（纯 CPU 1259 ms vs 独显 65 ms，19 倍）；若工作负载是大批量长文档索引，独显收益明显。

**真正的门槛不是硬件性能，而是「安装 Ollama + 拉取模型」这一步的操作成本。** 如果不想装 Ollama，请直接走 README 的「路径 A：云端 key」——用云端 embedding API，本机零额外依赖。

## 测试环境

| 项 | 值 |
|---|---|
| CPU | Intel Core Ultra 9 285H（16 线程） |
| 内存 | 31.4 GiB |
| 核显 | Intel Arc 140T（16 GB 共享内存） |
| 独显 | NVIDIA GeForce RTX 5060 Laptop（8 GB） |
| Ollama | 0.40.3 |
| 模型 | `qwen3-embedding:0.6b`（Q8_0，595.78M 参数，1024 维） |
| 操作系统 | Windows 11 |

> 三种档位在同一台机器上通过环境变量切换，使用同一模型、同一测试文本，因此数据可直接横向对比。

## 实测数据

### 单条嵌入延迟

| 文本长度 | 纯 CPU | 核显（Intel Arc 140T） | 独显（RTX 5060） |
|---|---|---|---|
| 短（~20 字） | **58 ms** | **104 ms** | **30 ms** |
| 中（~150 字） | **304 ms** | **91 ms** | **38 ms** |
| 长（~600 字） | **1259 ms** | **229 ms** | **65 ms** |
| 冷启动（含模型加载） | **0.93 s** | **6.0 s** | **1.5 s** |

> 短文本上纯 CPU 快于核显，是因为核显有固定的提交开销；文本变长后核显的并行优势才体现出来。「文本越长、核显越划算」是本组数据里最清晰的一条规律。
>
> 长文本是差距最大的场景：纯 CPU 1259 ms vs 独显 65 ms（**19 倍**）；但即便如此，1.26 秒对后台索引仍完全可用。

### 端到端检索延迟

从 Palimpsest REST API（`POST /mem/search`）发起查询，包含向量化 + 检索 + 序列化的完整链路：

| 档位 | 典型延迟 | 稳定均值 |
|---|---|---|
| 纯 CPU | 112–316 ms | **209 ms** |
| 核显 | 99–290 ms | **~120 ms** |
| 独显 | 95–335 ms | **160 ms** |

> 端到端延迟的差异远小于单条嵌入的差异——因为**延迟大头在检索与序列化，不在向量化**。三档稳定均值都在 120–210 ms 区间，体感差别很小。

### 批量索引

| 场景 | 纯 CPU | 核显 | 独显 |
|---|---|---|---|
| 批量 8 条（短） | 92 ms/条 | 99 ms/条 | **35 ms/条** |
| 混合长度 30 条 | 296 ms/条 | — | **44 ms/条** |
| 连续 20 条短文本 | **17.9 条/秒** | — | **33.9 条/秒** |

**规模推算（纯 CPU，最差情况）**：

| 索引量 | 纯 CPU | 独显 |
|---|---|---|
| 1,000 条 | ≈ 5 分钟 | ≈ 1 分钟 |
| 5,000 条 | ≈ 25 分钟 | ≈ 4 分钟 |

索引是一次性操作（仅在建立知识库索引时需要）。即便在最弱的纯 CPU 档位，5,000 条文档切片的全量索引约 25 分钟——对一次性操作而言完全可接受。

## 如何强制指定运行档位

Ollama 自动选择后端。若需**验证**或**强制**某个档位，用以下环境变量（设置后重启 Ollama）：

### 纯 CPU（无 GPU 环境即为此档，也可强制）

```bash
OLLAMA_VULKAN=false
CUDA_VISIBLE_DEVICES=-1
```

### 核显（无独显机器通常自动如此）

```bash
OLLAMA_IGPU_ENABLE=1
GGML_VK_VISIBLE_DEVICES=1   # Vulkan 层只暴露核显
CUDA_VISIBLE_DEVICES=-1     # 同时挡掉 CUDA 后端
```

> **注意**：单独设 `CUDA_VISIBLE_DEVICES=-1` **不足以**强制用核显。Ollama 0.40.3 默认通过 Vulkan 使用 GPU，会主动丢弃核显（日志提示 `dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1`），此时仍会落在独显上。

### 确认当前档位

```bash
ollama ps
```

`PROCESSOR` 列会显示 `100% CPU` 或 `100% GPU`。更精确的判断看 Ollama 服务启动日志中的 `inference compute` 行：

- 核显：`type=iGPU`
- 独显：`type=discrete`
- 无此行：纯 CPU

## 复现方式

```bash
# 1. 设置档位环境变量（见上），重启 Ollama
# 2. 确认档位
ollama ps

# 3. 单条延迟
time curl -s http://127.0.0.1:11434/api/embed \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-embedding:0.6b","input":"测试文本"}' > /dev/null

# 4. 端到端检索（需 REST 服务在 :8090 运行）
curl -s http://127.0.0.1:8090/mem/search \
  -H "Content-Type: application/json" \
  -d '{"query":"测试查询","top_k":3,"scope":"memory"}'
```

## 换更小的模型？

若机器确实极弱，可换用更小的向量模型。**换模型 = 换向量空间，必须重建全部索引**（见 [CONFIGURATION.md](CONFIGURATION.md) 的「更换向量模型」一节）。

本项目未测试比 `qwen3-embedding:0.6b` 更小的模型——0.6B 已是常见嵌入模型中较小的一档，且在纯 CPU 上已足够可用。

## 相关文档

- [README「快速上手」](../README.md#快速上手) — 云端 vs 本地两条路径
- [CONFIGURATION.md](CONFIGURATION.md) — embedding provider 配置与换权重嵌
- [OBSIDIAN.md](OBSIDIAN.md) — 知识库索引的具体流程
