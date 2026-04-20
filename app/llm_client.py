"""
llm_client.py — 通用 LLM 聊天客户端

封装了对 OpenAI 兼容接口（如 vLLM、HuggingFace Router）的 HTTP 调用。
发送 system+user 消息，返回 assistant 回复文本。
注意：实际项目中主要使用 VLLMExtractor（llm_extractor.py）来调用 LLM，
本文件是更通用的封装层。
"""

from __future__ import annotations

import os

import requests


class LLMClient:
    """通用 LLM Chat 客户端，支持 API Key、超时、JSON 响应格式。"""

    def __init__(
        self,
        url: str,                           # LLM API 端点 URL
        model: str,                         # 模型名称
        timeout: int = 90,                  # HTTP 超时秒数
        api_key: str = "",                  # 显式传入的 API Key
        api_key_env: str = "HF_TOKEN",     # 从环境变量读取 Key 的变量名
        use_response_format: bool = False,  # 是否要求 API 返回 JSON 格式
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self.api_key = api_key.strip() or os.getenv(api_key_env, "").strip()
        self.use_response_format = use_response_format
        self.session = requests.Session()  # 复用连接池

    def chat(
        self,
        prompt: str,
        system_prompt: str = "你是一个只输出 JSON 的助手。",
        temperature: float = 0.1,
    ) -> str:
        """
        发送一轮对话并返回 assistant 的文本回复。
        构造 messages = [system, user] 的 payload，通过 POST 发送到 LLM API。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        if self.use_response_format:
            payload["response_format"] = {"type": "json_object"}
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.session.post(self.url, json=payload, timeout=self.timeout, headers=headers)
        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]
