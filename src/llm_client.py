"""
llm_client.py — کلاینت برای ارتباط با LLM لوکال (OpenAI-compatible)
"""

import uuid
import httpx
from openai import OpenAI
from src.config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, CLIENT_ID
from src.observability.observability import Observability

obs = Observability.from_env()

def _make_http_client(user_id: str) -> httpx.Client:
    """یک httpx client با هدرهای مورد نیاز سرویس می‌سازد.
    - x-client-id  : ثابت، از .env
    - x-user-id    : از کد فراخوان پاس می‌شود
    - x-request-id : هر بار UUID تازه تولید می‌شود
    """
    return httpx.Client(
        headers={
            "x-client-id": CLIENT_ID,
            "x-user-id": user_id,
            "x-request-id": str(uuid.uuid4()),
        }
    )


class LLMClient:
    def __init__(self, user_id: str, agent_name: str) -> None:
        self._client = OpenAI(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            http_client=_make_http_client(user_id),
        )
        self.model = LLM_MODEL
        self.agent_name = agent_name

    def chat(self, user_message: str, system_prompt: str = "You are a helpful assistant.", temperature: float = 0.7, max_tokens: int = 1024) -> str:
        """یک پیام ساده به مدل می‌فرستد و پاسخ متنی برمی‌گرداند."""

        if obs.enabled:
            return self._chat_observed(user_message, system_prompt, temperature, max_tokens)
        return self._chat_raw(user_message, system_prompt, temperature, max_tokens)

    def _chat_raw(self, user_message, system_prompt, temperature, max_tokens) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        print(response)
        return response.choices[0].message.content or ""

    def _chat_observed(self, user_message, system_prompt, temperature, max_tokens) -> str:

        with obs.observation(
            name=f"{self.agent_name}-chat",
            model=self.model,
            input={"system": system_prompt, "user": user_message},
            model_parameters={"temperature": temperature, "max_tokens": max_tokens},
        ) as gen:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content or ""
            usage = response.usage
            gen.update(
                output=content,
                usage_details={
                    "input": usage.prompt_tokens if usage else None,
                    "output": usage.completion_tokens if usage else None,
                },
            )
            return content

        obs.flush()

    def stream_chat(
        self,
        user_message: str,
        system_prompt: str = "You are a helpful assistant.",
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ):
        """پاسخ را به صورت streaming (chunk به chunk) برمی‌گرداند."""
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

