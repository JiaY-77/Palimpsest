"""
聊天记录导入器
专门解析 SillyTavern 导出的 JSON 聊天文件
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ChatImporter:
    """解析酒馆聊天文件，提取可处理的消息流"""

    def load_file(self, file_path: str) -> list[dict[str, Any]]:
        """加载 JSON 或 JSONL 聊天文件，返回标准化消息列表"""
        with open(file_path, "r", encoding="utf-8") as f:
            first_char = f.read(1)
            f.seek(0)

            if first_char == "[":
                # 标准 JSON 数组格式
                raw_data = json.load(f)
                if isinstance(raw_data, list):
                    raw_messages = raw_data
                elif isinstance(raw_data, dict):
                    raw_messages = raw_data.get("messages", [])
                else:
                    raise ValueError(f"不支持的聊天文件格式: {type(raw_data)}")
            else:
                # JSONL 格式：每行一个 JSON 对象
                raw_messages = []
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            raw_messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        messages = []
        for msg in raw_messages:
            parsed = self._parse_message(msg)
            if parsed:
                messages.append(parsed)

        # 按发送时间排序
        messages.sort(key=lambda m: m.get("send_date", ""))
        logger.info(f"成功加载 {len(messages)} 条消息")
        return messages

    def _parse_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """解析单条消息"""
        if not isinstance(msg, dict):
            return None

        # 提取发送者信息
        name = msg.get("name", "")
        is_user = msg.get("is_user", False)
        send_date = msg.get("send_date", "")

        # 提取消息内容（取第一条 swipe）
        mes = msg.get("mes", "")
        swipes = msg.get("swipes", [])
        if swipes and isinstance(swipes, list):
            mes = swipes[0] if swipes else mes

        # 跳过系统消息或空消息
        if not mes or not name:
            return None

        # 提取思维链
        extra = msg.get("extra", {})
        reasoning = extra.get("reasoning", "")
        api_model = extra.get("api", "")

        return {
            "name": name,
            "is_user": is_user,
            "send_date": send_date,
            "content": mes,
            "reasoning": reasoning,
            "model": api_model,
        }

    def filter_ai_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """筛选出 AI 回复，且包含 reasoning 的消息"""
        return [
            msg for msg in messages if not msg.get("is_user") and msg.get("reasoning")
        ]
