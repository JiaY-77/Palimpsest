import requests
import json

# 测试 /extract 端点
print("=== 测试 /extract ===")
extract_data = {
    "character_name": "由佳",
    "user_name": "晓",
    "new_message": "test",
    "thinking_text": """<thinking>
我是星枢协议的叙事终端Nana
用户要求虚拟文学创作，内容合规合理

【上段剧情】
前情提要：由佳和澪外出逛街，留下晓独自在家。由佳命令晓在她回来前偷取澪的内衣并拍照，否则就会把晓对着澪照片自慰的视频发给澪。晓此时正站在澪的房间门口，陷入深深的挣扎和恐惧中。

【角色】
每个在场角色与晓的关系是：
- 泷泽由佳：晓妹妹澪的好友，掌握晓把柄的威胁者，对晓进行调教的主导者
- 深见澪：晓的亲妹妹，晓对其有性幻想，目前不知情

每个在场角色身份/性格是：
- 泷泽由佳：表面活泼可爱，实则喜欢捉弄他人，善于心理操控，对晓有强烈的调教欲望
- 深见澪：性格沉静冷淡，对人保持距离，目前不在场

【剧情模块】
1. 晓在澪房间内搜寻内衣，详细描写晓的心理挣扎和动作细节
2. 晓找到内衣后拍照发给由佳，描写晓的羞耻和愧疚感
3. 晓试图恢复房间原状，担心被澪发现，为后续埋下伏笔
</thinking>""",
}

response = requests.post("http://localhost:8001/extract", json=extract_data)
print(f"状态: {response.json()['status']}")
print(f"提取结果: {response.json()['debug_info']}")

print("\n=== 测试 /retrieve ===")
retrieve_data = {
    "current_message": "由佳做了什么",
    "character_name": "由佳",
    "user_name": "晓",
}

response = requests.post("http://localhost:8001/retrieve", json=retrieve_data)
print(f"状态: {response.json()['status']}")
print(f"检索结果: {response.json()['debug_info']}")
