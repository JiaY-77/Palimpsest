"""MemoryHub - 记忆提取器"""

import json
import logging

from openai import OpenAI
from config import Config

logger = logging.getLogger(__name__)


class Extractor:
    def __init__(self):
        llm_cfg = Config.get_llm_config()
        print(f"DEBUG: 后端 = {Config.LLM_BACKEND}")
        print(f"DEBUG: base_url = {llm_cfg['base_url']}")
        print(f"DEBUG: model = {llm_cfg['model']}")
        print(f"DEBUG: api_key 存在 = {bool(llm_cfg['api_key'])}")

        self.client = OpenAI(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
        )
        self.model = llm_cfg["model"]
