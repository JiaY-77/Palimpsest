"""测试聊天记录导入器"""

from core.importer import ChatImporter

importer = ChatImporter()

# 用你之前发的那份聊天记录测试
chat_file = "泷泽由佳 - 2026-04-25@17h40m44s.json"
messages = importer.load_file(chat_file)

print(f"总共加载 {len(messages)} 条消息")

ai_messages = importer.filter_ai_messages(messages)
print(f"其中包含思维链的 AI 回复: {len(ai_messages)} 条")

# 打印前 3 条包含思维链的消息摘要
for i, msg in enumerate(ai_messages[:3]):
    reasoning_preview = msg["reasoning"][:80].replace("\n", " ")
    print(f"\n--- 消息 {i+1} ---")
    print(f"角色: {msg['name']}")
    print(f"时间: {msg['send_date']}")
    print(f"思维链预览: {reasoning_preview}...")
