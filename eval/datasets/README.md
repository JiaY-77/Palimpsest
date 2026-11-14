# 小帕评测题集（自建）

基于真实记忆库 + 借鉴公开基准（LoCoMo / 大肥鱼 chat-memory-benchmark）的题型设计。
**不是**直接跑外部题集——外部题集是合成人物，本目录的题全部锚定**本库真实节点**，gold 可核验。

## 目录

| 目录 | 题型 | 判分指标 |
|---|---|---|
| `retrieval/` | 检索层题（semantic / factual / temporal / domain_isolation） | recall@k / MRR / NDCG@k / 有效召回 |
| `end_to_end/` | 端到端题（negative / partial）— **草稿，暂未提交**：现有「相似度阈值」判假前提不严谨（库里同主题内容多时相似度天然高），口径要改成判「检索结果能否支撑答案」 | 规则判：是否召回、是否编造、是否表达不确定 |

## 题型定义

### temporal_twin（时间孪生）
同一问题在库里有多个版本的答案（配置变更、版本号、修复前后）。
- **gold** = 最新那条；**stale_ids** = 历史版本
- 判分：gold 在 top_k 内，且排名**优于所有 stale** —— 召回旧版本 = 答错
- 为什么重要：模型/版本号这类事实最容易因「召回旧节点」而答错，而 recall 只问「对的那条在不在」，看不见「错的那条排在前面」
- **两份题、两种 gold 口径**（2026-11-14 起）：
  - `retrieval/temporal.json` —— gold 指向**叙事记录**（`type=record`，如「发布完成」的流水）。这是**现状基线**：最新答案只写在多主题长记录里时，实测 recall 上不去（0/3）。
  - `retrieval/temporal_state.json` —— gold 指向**状态事实节点**（`type=memory`，答案第一句、词面对齐、变更时 `replaces` 上一条）。这是**目标形态**：按规范写入后同样的问题 7/7 全过。
  - 结论：这类题的瓶颈不在排序算法，而在「当前状态」有没有自己的节点——所以两份都留，别拿目标形态的成绩掩盖现状。

### false_premise（假前提 / negative control）
问库里根本不存在的事。
- 判分：top-1 相似度 < 0.55，且不得返回 >= 0.65 的结果
- 为什么重要：测「不该答时不编造」，这是记忆系统最容易被忽视的能力

### domain_isolation（域隔离，待补）
问 A 域的事，结果里不得出现 B 域节点。

## 复现

```bash
venv/Scripts/python.exe eval/check_datasets.py            # 全部题集
venv/Scripts/python.exe eval/check_datasets.py --only temporal
```

## 注

- 题集 JSON 在 git 里（不含个人隐私：query 是自然语言、gold 是节点 ID）
- 新增题目必须能回答「gold 为什么是这条」——写进 `why` 字段
- 节点变更后 gold 会失效，需定期 re-resolve（脚本待补）
