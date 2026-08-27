# -*- coding: utf-8 -*-
"""
core.reporting —— 记忆报告生成
============================
将 main.py 的 /report 端点核心逻辑抽出，独立为可复用函数：
基于当前数据库中的所有记忆，调用 LLM 生成一份角色灵魂分析报告。
"""


async def generate_report(store):
    """
    基于当前数据库中的所有记忆，调用 LLM 生成一份角色灵魂分析报告。
    这不再是简单的摘要，而是对角色命运的洞察。

    参数 store：TriviumStore 实例（迭代其 payload 取记忆内容）。
    """
    # 1. 先从数据库获取所有记忆
    memories = []
    for nid, payload in store.iter_payloads():
        if payload.get("content"):
            memories.append(f"[{payload.get('type', '')}] {payload['content']}")

    if not memories:
        return {
            "status": "error",
            "message": "当前数据库中没有记忆，请先导入聊天记录。",
        }

    # 2. 把所有记忆文本组装成一个大的上下文
    memory_text = "\n".join(memories)

    # 3. 这是整个 Palimpsest 最核心的 Prompt 之一
    # 它定义了我们的系统如何从一个数据仓库，变成一个灵魂洞察师
    report_prompt = f"""你是一位极其敏锐的角色扮演心理分析师。你的任务不是复述剧情，而是洞察角色的灵魂。

下面是从一段漫长的角色扮演对话中，提取出的关键记忆碎片。这些碎片记录了角色的行为、状态、计划和内心独白。

请你根据这些碎片，生成一份深刻的角色分析报告。报告必须包含以下几个维度：

1.  **人格光谱**：分析该角色展现出的核心人格特质。不要只贴标签，要说明这些特质是如何通过具体行为体现的。特别要指出其人格中存在的“矛盾”或“复杂性”（例如，“优雅的残忍”、“伪装成粗心的占有欲”）。

2.  **命运时刻**：从记忆碎片中，找出 3 个最关键的事件转折点。这些时刻必须深刻影响了故事走向或角色关系。请说明为什么这些是转折点。

3.  **内心戏剧场**：深入分析角色的内心世界。他/她的伪装、压抑的渴望、内心的恐惧、对自己或他人的谎言是什么？他/她嘴上说的和心里想的，可能有什么不同？

4.  **未完成的交响曲**：基于已有的线索和角色的行为模式，大胆预测 2-3 条故事未来可能的发展方向，或者指出那些尚未被提及、但隐隐存在的“伏笔”。

请用优美、流畅、充满洞察力的中文撰写这份报告。你要像一位资深文学评论家在分析他最爱的角色一样，充满热情和深度。不要使用 markdown 格式的标题，用优雅的自然段落来分隔各个部分。

=== 记忆碎片 ===
{memory_text}
"""
    # 4. 调用 DeepSeek
    try:
        from openai import OpenAI
        from config import Config

        llm_cfg = Config.get_llm_config()
        client = OpenAI(api_key=llm_cfg["api_key"], base_url=llm_cfg["base_url"])

        completion = client.chat.completions.create(
            model=llm_cfg["model"],
            messages=[{"role": "user", "content": report_prompt}],
            temperature=0.8,
            max_tokens=4000,
        )
        report = completion.choices[0].message.content
        return {"status": "ok", "report": report}
    except Exception as e:
        return {"status": "error", "message": f"报告生成失败: {str(e)}"}
