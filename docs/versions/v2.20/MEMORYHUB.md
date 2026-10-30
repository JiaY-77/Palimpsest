# 记忆包：把记忆拿出来、看懂、再挑着带走

> 适用于 Palimpsest v2.14 及以后。示例包见 [`examples/memoryhub-sample.json`](examples/memoryhub-sample.json)。

## 这一篇解决什么问题

记忆系统最容易变成新的孤岛：写进去容易，**拿出来难**。换一个 agent、换一台机器、想把某段经验交给同事看一眼，往往只能一条条复制粘贴——顺序丢了、关系丢了，还得担心把 API Key 一起粘出去。

记忆包（MemoryHub pack）是 Palimpsest 对这个问题的答案：**一个自包含的 JSON 文件**，装着节点、关系、以及一份给人读的「故事」。它有三个刻意的设计：

1. **先读后写**：解开一个包不需要写入任何东西，可以先当故事看。
2. **挑着收**：默认一个字节都不入籍，必须显式点名哪几条才写进你的库。
3. **敏感先说**：解开时就把疑似凭据/隐私的位置标出来，不靠你事后回想。

## 一个记忆包长什么样

```json
{
  "format": "memoryhub",
  "format_version": 1,
  "manifest": {
    "name": "示例记忆包：一个虚构的小团队 AI 助手",
    "kind": "memory_pack",
    "created_at": 1773446400.0,
    "producer": "palimpsest (sample)",
    "origin_db": "sample.db",
    "fingerprint": "759a26d37fad6191",
    "node_total": 6,
    "edge_total": 3,
    "story": "这是一个虚构的四人小团队的 AI 助手记忆。……"
  },
  "nodes": [
    {
      "node_id": 101,
      "payload": {
        "type": "memory",
        "content": "用户偏好：这个项目统一用 TypeScript，不允许 any 逃逸；……",
        "importance": 0.7,
        "domain": "demo",
        "status": "active"
      }
    }
  ],
  "edges": [
    { "source_id": 102, "target_id": 101, "label": "RELATED_TO", "weight": 1.0 }
  ]
}
```

几个字段值得单独说明：

| 字段 | 作用 |
|---|---|
| `format` / `format_version` | 校验用。值不是 `memoryhub` 或不支持的版本会被直接拒绝，不会「尽力解析」 |
| `manifest.story` | 给人读的一段话：这个包讲的是谁、什么事。这是「先当故事听」的入口 |
| `manifest.fingerprint` | 节点+边的稳定指纹。同一个包重复导出指纹不变，方便核对「是不是同一份」 |
| `manifest.origin_db` | 这个包从哪个库导出的，只作溯源，不影响导入 |
| `nodes[].node_id` | **只在包内有效**。导入时不会沿用，新库自己分配 id |
| `edges[]` | 只描述包内节点之间的关系 |

## 三步走

下面所有命令都用仓库自带的示例包，可以直接跟着跑。

### 第一步：只看不写

```bash
python scripts/palimpsest_cli.py memoryhub read docs/examples/memoryhub-sample.json
```

这条命令**不写任何东西**。它输出包的 manifest、每个节点的预览，以及敏感扫描结果：

```json
{
  "ok": true,
  "manifest": { "name": "示例记忆包：一个虚构的小团队 AI 助手", "node_total": 6, "edge_total": 3, "...": "..." },
  "nodes": [
    { "node_id": 101, "type": "memory", "importance": 0.7, "domain": "demo", "preview": "用户偏好：这个项目统一用 TypeScript，……" },
    { "node_id": 105, "type": "memory", "importance": 0.4, "domain": "demo", "preview": "部署备注：本地测试时曾把 key 写进代码里，形状像 sk-dem...xxxx （示例占位，不是真凭据）" }
  ],
  "issues": [
    { "node_id": 105, "kind": "secret", "rules": ["openai_key"] }
  ],
  "issue_counts": { "secret": 1, "empty_content": 0, "malformed_node": 0 },
  "stats": { "node_total": 6, "edge_total": 3, "type_counts": { "memory": 2, "correction": 1, "task": 1, "record": 1, "plan": 1 } }
}
```

注意 `issues` 里那条：示例包的第 5 个节点是**故意**写的假 Key（用于演示扫描），它被识别成 `openai_key`。

### 第二步：预览「如果导入会发生什么」

`import` 不带 `--select` 时同样是只读的：

```bash
python scripts/palimpsest_cli.py memoryhub import docs/examples/memoryhub-sample.json
```

```json
{
  "ok": true,
  "mode": "preview",
  "decision_log": {
    "mode": "preview",
    "node_total": 6,
    "selected": 0,
    "to_import": 0,
    "skipped_total": 6,
    "skip_reasons": { "not_selected": 6 }
  },
  "hint": "在 select 里填入要入籍的 node_id（逗号分隔）后重新调用"
}
```

`skip_reasons` 是这一步的关键：它告诉你**每条为什么没被收**。常见取值：

| 原因 | 含义 |
|---|---|
| `not_selected` | 你没点名选它（预览模式全都会是这个） |
| `secret_requires_confirm` | 命中强敏感规则，需要显式加 `--include-secrets` 才放行 |
| `empty_content` | 内容为空，没有收录价值 |
| `not_active` | 包里该节点已是 `outdated`（被取代的旧版本），不往新库搬 |
| `not_found` | `--select` 里的 id 在包内不存在 |

### 第三步：点名收下

```bash
python scripts/palimpsest_cli.py memoryhub import docs/examples/memoryhub-sample.json --select 101,102,106
```

```json
{
  "ok": true,
  "mode": "import",
  "imported": [
    { "origin_id": 101, "node_id": 1 },
    { "origin_id": 102, "node_id": 2 },
    { "origin_id": 106, "node_id": 3 }
  ]
}
```

`imported` 把「包里的 id」映射到「你库里的新 id」。这里故意跳过了 104（一条日志层记录）和 105（含假 Key 的那条）。

想收 105 也不是不行，但必须明说：

```bash
python scripts/palimpsest_cli.py memoryhub import docs/examples/memoryhub-sample.json --select 105
# → skipped: [{"origin_id": 105, "reason": "secret_requires_confirm"}]

python scripts/palimpsest_cli.py memoryhub import docs/examples/memoryhub-sample.json --select 105 --include-secrets
# → 收下
```

## 六条不可变的约定

这些是记忆包的契约，不是实现细节——换实现也应该保持：

1. **不沿用原 id**：新库自己分配，原 id 记进 `payload.origin_id`，来源包记进 `payload.origin_pack`。所以同一个包可以重复导入，不会撞 id。
2. **默认零写入**：`read` 和不带 `--select` 的 `import` 都不会改库。
3. **不做冲突检测、不取代已有记忆**：导入是纯新增。它不会把你库里相似的旧记忆标成 `outdated`——那两个决定应该由你（或你的 agent）显式做，不该被一次导入顺手改掉。
4. **边只保留两端都入籍的**：上例里 `103 → 101` 这条边没进来，因为你没选 103。留半边悬空的边只会污染图谱。
5. **非 active 节点不搬**：包内 `status=outdated` 的节点默认跳过——搬一份已经被取代的旧版本没有意义。
6. **强敏感需显式放行**：命中强规则的节点必须加 `--include-secrets`，否则跳过并在 `skip_reasons` 里写明原因。

## 敏感扫描规则

解开和导入时都会扫，规则分两档：

**强规则（命中 = 拒绝，需 `--include-secrets` 放行）**

| 规则名 | 匹配什么 |
|---|---|
| `openai_key` | `sk-` 开头、20 字符以上的密钥 |
| `anthropic_key` | `sk-ant-` 开头的密钥 |
| `google_key` | `AIza` 开头的 Google API Key |
| `github_token` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` 开头的 token |
| `aws_key` | `AKIA` 开头的 Access Key ID |
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` |
| `ssh_key` | `ssh-rsa AAAA...` |
| `bearer` | `Bearer <长 token>` |

**弱规则（命中 = 放行但打标记）**

| 规则名 | 匹配什么 |
|---|---|
| `id_card` | 18 位身份证号 |
| `phone` | 11 位手机号 |

弱规则只是提醒，不阻断——它们出现在 `secret_hint` 里供审计，因为记忆里出现电话号码往往是有意的（比如「我的紧急联系人」）。

## 三个如实说明的边界

写文档最容易犯的错是把「没做」说成「做了」，所以这三条单独列：

1. **不是加密**：记忆包是明文 JSON。发给别人之前请自己确认——敏感扫描只覆盖上表这 10 类模式，不认识的凭据格式（自研 token、内网地址、身份证以外的证件号）它看不到。
2. **不做去重**：同一个包导入两次，你会得到两份节点。想去重请在导入后自己处理。
3. **不校验语义**：它只保证包的结构完整、字段合法。包里写的内容对不对、有没有过期，只能靠人看 `story` 和 `preview` 判断。

## 用 Python 直接读写（不经过 CLI）

如果你要把记忆包接进自己的流水线，可以直接导入模块：

```python
from core.memoryhub import build_pack, read_pack, summarize_pack
from core.trivium_store import TriviumStore

# 导出：从当前库产出一个包
store = TriviumStore()
pack = build_pack(store, name="我的记忆", story="这段时间做过的事")

# 读入：解开一个包（只读，不写库）
result = read_pack("some-pack.json")
if result["ok"]:
    print(result["stats"]["node_total"], "个节点")
    for issue in result["issues"]:
        print("问题:", issue)
```

`build_pack` 产出的 dict 直接 `json.dump` 即可落盘。

## 相关

- [`CONFIGURATION.md`](CONFIGURATION.md) —— 全部配置项
- [`API.md`](API.md) —— REST 端点完整清单（`/memoryhub/unpack`、`/memoryhub/import`）
- [`README.md`](../README.md) —— 项目总览与安装
