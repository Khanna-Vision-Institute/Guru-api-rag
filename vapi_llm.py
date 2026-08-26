from typing import Optional, List
from langchain.schema import BaseMessage, HumanMessage, AIMessage
from langchain.schema.chat_models import BaseChatModel
import httpx
import os

class VapiChatModel(BaseChatModel):
    api_key: str
    model_url: str

    def _call(self, messages: List[BaseMessage], **kwargs) -> str:
        payload = {
            "messages": [
                {"role": "user", "content": m.content}
                if isinstance(m, HumanMessage)
                else {"role": "assistant", "content": m.content}
                for m in messages
            ]
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        response = httpx.post(self.model_url, json=payload, headers=headers)
        response.raise_for_status()

        return response.json()["message"]["content"]

    @property
    def _llm_type(self) -> str:
        return "vapi-chat-model"
