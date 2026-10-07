"""
llm_client.py — کلاینت برای ارتباط با LLM لوکال (OpenAI-compatible)
"""

import uuid
import httpx
from openai import OpenAI
from src.config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, CLIENT_ID
from src.observability.observability import Observability

obs = Observability.from_env()

# مقداری که ارائه‌دهنده وقتی تولید به سقفِ توکنِ خروجی می‌خورد برمی‌گرداند.
FINISH_REASON_LENGTH = "length"


class LLMResponse(str):
    """پاسخِ مدل به‌همراهِ فراداده‌ی ارائه‌دهنده.

    زیرکلاسِ ``str`` است تا قراردادِ ``LLMClient.chat()`` (برگرداندنِ رشته)
    دست‌نخورده بماند: هرجا قبلاً رشته انتظار می‌رفت، دقیقاً همان رشته می‌رسد و
    فراداده‌ها فقط همراهِ آن سوار می‌شوند. پس هیچ فراخوانِ قدیمی‌ای عوض نمی‌شود.

    فراداده‌ها:
        finish_reason     : دلیلِ پایانِ تولید؛ ``"length"`` یعنی بریده‌شدگی
        prompt_tokens     : توکن‌های ورودی (اگر ارائه‌دهنده گزارش کند)
        completion_tokens : توکن‌های خروجیِ تولیدشده
        max_tokens        : سقفی که خودمان در درخواست فرستادیم
    """

    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None
    max_tokens: int | None

    def __new__(
        cls,
        text: str = "",
        *,
        finish_reason: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        max_tokens: int | None = None,
    ) -> "LLMResponse":
        response = super().__new__(cls, text or "")
        response.finish_reason = finish_reason or ""
        response.prompt_tokens = prompt_tokens
        response.completion_tokens = completion_tokens
        response.max_tokens = max_tokens
        return response

    @property
    def truncated(self) -> bool:
        """آیا پاسخ به سقفِ توکنِ خروجی خورده و نیمه‌کاره مانده است؟"""
        return self.finish_reason == FINISH_REASON_LENGTH

    def truncation_message(self) -> str:
        """توضیحِ صریحِ بریده‌شدگی — برای قدم‌هایی که از پاسخ JSON می‌خواهند."""
        details = [f"finish_reason='{self.finish_reason}'"]
        if self.max_tokens is not None:
            details.append(f"max_tokens={self.max_tokens}")
        if self.completion_tokens is not None:
            details.append(f"completion_tokens={self.completion_tokens}")
        return (
            "The LLM response was truncated because it reached the output token "
            f"limit ({', '.join(details)}). The response is incomplete, so no "
            "result could be read from it — nothing was repaired or guessed. "
            "Raise LLM_MAX_OUTPUT_TOKENS or run the step with fewer test cases."
        )


def truncation_message(raw: object) -> str:
    """پیامِ بریده‌شدگی اگر ``raw`` واقعاً پاسخی بریده باشد، وگرنه رشته‌ی خالی.

    ورودی ``object`` است چون کلاینتِ تزریق‌شده در تست‌ها ممکن است رشته‌ی ساده
    برگرداند؛ آن‌جا فراداده‌ای وجود ندارد و چیزی هم نباید ادعا شود.
    """
    if isinstance(raw, LLMResponse) and raw.truncated:
        return raw.truncation_message()
    return ""


def _to_response(response: object, max_tokens: int) -> LLMResponse:
    """``ChatCompletion`` را به ``LLMResponse`` تبدیل می‌کند.

    فراداده‌ها با ``getattr`` خوانده می‌شوند تا ماکِ ناقصِ تست‌ها هم کار کند.
    """
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) or ""
    usage = getattr(response, "usage", None)
    finish_reason = getattr(choice, "finish_reason", None)
    return LLMResponse(
        content,
        # str() تا مقایسه‌ی ``truncated`` هرگز روی یک شیءِ ناغافل انجام نشود.
        finish_reason=str(finish_reason) if finish_reason else "",
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        max_tokens=max_tokens,
    )


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
        """یک پیام ساده به مدل می‌فرستد و پاسخ متنی برمی‌گرداند.

        خروجی از نوعِ ``LLMResponse`` است که خودش یک ``str`` است؛ پس قراردادِ
        قبلی («برگرداندنِ رشته») دست‌نخورده مانده و فراداده‌هایی مثل
        ``finish_reason`` هم در دسترس‌اند (``truncation_message(raw)``).
        """

        if obs.enabled:
            return self._chat_observed(user_message, system_prompt, temperature, max_tokens)
        return self._chat_raw(user_message, system_prompt, temperature, max_tokens)

    def _chat_raw(self, user_message, system_prompt, temperature, max_tokens) -> LLMResponse:
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
        return _to_response(response, max_tokens)

    def _chat_observed(self, user_message, system_prompt, temperature, max_tokens) -> LLMResponse:

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
            content = _to_response(response, max_tokens)
            usage = response.usage
            gen.update(
                output=content,
                usage_details={
                    "input": usage.prompt_tokens if usage else None,
                    "output": usage.completion_tokens if usage else None,
                },
            )
            if content.truncated:
                gen.update(
                    status_message=content.truncation_message(),
                    level="WARNING",
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

