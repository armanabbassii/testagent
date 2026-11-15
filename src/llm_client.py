"""
llm_client.py — کلاینت برای ارتباط با LLM لوکال (OpenAI-compatible)
"""

import contextvars
import functools
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx
from openai import APIStatusError, OpenAI
from src.config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, LLM_SSL_VERIFY, CLIENT_ID
from src.debug import AgentLogger, DebugConfig
from src.observability.observability import Observability
from src.token_usage import report_usage

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
        total_tokens      : جمعِ توکن‌های گزارش‌شده (اگر ارائه‌دهنده بدهد)
        cached_tokens     : توکن‌های ورودیِ کش‌شده (اگر گزارش شود)
        max_tokens        : سقفی که خودمان در درخواست فرستادیم
        response_id       : شناسه‌ی پاسخِ ارائه‌دهنده (``ChatCompletion.id``) برای
                            ردیابی — ``""`` اگر گزارش نشود
    """

    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    max_tokens: int | None
    response_id: str

    def __new__(
        cls,
        text: str = "",
        *,
        finish_reason: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        cached_tokens: int | None = None,
        max_tokens: int | None = None,
        response_id: str = "",
    ) -> "LLMResponse":
        response = super().__new__(cls, text or "")
        response.finish_reason = finish_reason or ""
        response.prompt_tokens = prompt_tokens
        response.completion_tokens = completion_tokens
        response.total_tokens = total_tokens
        response.cached_tokens = cached_tokens
        response.max_tokens = max_tokens
        response.response_id = response_id or ""
        return response

    @property
    def truncated(self) -> bool:
        """آیا پاسخ به سقفِ توکنِ خروجی خورده و نیمه‌کاره مانده است؟"""
        return self.finish_reason == FINISH_REASON_LENGTH

    def truncation_message(self) -> str:
        """توضیحِ صریحِ بریده‌شدگی — برای قدم‌هایی که از پاسخ JSON می‌خواهند.

        اگر ``max_tokens`` فرستاده نشده باشد (``None``)، سقفی که خورده سقفِ خودِ
        ارائه‌دهنده است، نه سقفِ برنامه — پیام هم همین را می‌گوید و عددی جعل نمی‌کند.
        """
        details = [f"finish_reason='{self.finish_reason}'"]
        if self.max_tokens is not None:
            details.append(f"max_tokens={self.max_tokens}")
        if self.completion_tokens is not None:
            details.append(f"completion_tokens={self.completion_tokens}")
        if self.max_tokens is None:
            return (
                "The LLM response was truncated because it reached an output limit "
                f"({', '.join(details)}). No max_tokens was sent with the request, "
                "so the limit came from the provider, model, context window or "
                "gateway, not from this application. The response is incomplete, "
                "so no result could be read from it — nothing was repaired or "
                "guessed. Run the step with a smaller input or raise the "
                "provider-side limit."
            )
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


def _to_response(response: object, max_tokens: int | None) -> LLMResponse:
    """``ChatCompletion`` را به ``LLMResponse`` تبدیل می‌کند.

    فراداده‌ها با ``getattr`` خوانده می‌شوند تا ماکِ ناقصِ تست‌ها هم کار کند.
    """
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) or ""
    usage = getattr(response, "usage", None)
    finish_reason = getattr(choice, "finish_reason", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    response_id = getattr(response, "id", None)
    return LLMResponse(
        content,
        # str() تا مقایسه‌ی ``truncated`` هرگز روی یک شیءِ ناغافل انجام نشود.
        finish_reason=str(finish_reason) if finish_reason else "",
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        cached_tokens=getattr(prompt_details, "cached_tokens", None),
        max_tokens=max_tokens,
        response_id=response_id if isinstance(response_id, str) else "",
    )


@functools.cache
def _response_logger() -> AgentLogger:
    """loggerِ فراداده‌ی پاسخ‌ها — یک بار در هر پروسه ساخته می‌شود.

    سطحش مثلِ هر ایجنتِ دیگر با ``LOGGING_AGENTS_LLM_CLIENT_LEVEL`` تنظیم می‌شود.
    """
    return DebugConfig.from_env().get_logger("llm_client")


def _log_response(response: LLMResponse, *, model: str, agent_name: str) -> None:
    """فقط فراداده‌ی امن: نه متنِ پاسخ، نه استدلالِ (reasoning) مدل، نه پرامپت.

    جای ``print(response)`` قبلی است که کلِ ``ChatCompletion`` — از جمله
    استدلالِ کاملِ مدل — را روی کنسول می‌ریخت.
    """
    _response_logger().debug(
        "پاسخِ LLM دریافت شد",
        model=model,
        agent=agent_name,
        response_id=response.response_id,
        finish_reason=response.finish_reason,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        total_tokens=response.total_tokens,
        max_tokens=response.max_tokens,
        content_chars=len(response),
    )


# ── عیب‌یابیِ تلاش‌های HTTP (فقط فراداده) ─────────────────────────────────────
# برچسبِ مرحله‌ای که فراخوان (مثلاً Task Analysis) برای فراخوانیِ جاری گذاشته است.
# hookها فقط وقتی برچسب هست لاگ می‌کنند؛ پس فراخوانی‌های بقیه‌ی قدم‌ها لاگِ جدیدی
# نمی‌گیرند. کلاینت sync است و hookها در همان thread اجرا می‌شوند، پس contextvar
# دیده می‌شود.
_diagnostic_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_diagnostic_stage", default=None
)

# هدری که OpenAI SDK خودش روی هر تلاش می‌گذارد (۰ برای تلاشِ اول، ۱ برای اولین retry …).
_RETRY_COUNT_HEADER = "x-stainless-retry-count"


@contextmanager
def diagnostic_stage(stage: str) -> Iterator[None]:
    """در این بازه هر تلاشِ HTTPِ LLM با همین برچسب لاگ می‌شود."""
    token = _diagnostic_stage.set(stage)
    try:
        yield
    finally:
        _diagnostic_stage.reset(token)


class _AttemptTimer:
    """hookهای httpx برای زمان‌سنجیِ هر تلاشِ HTTP — هرگز رفتارِ درخواست را عوض نمی‌کنند.

    * زمانِ شروع روی خودِ شیءِ Request (WeakKeyDictionary) نگه داشته می‌شود؛ SDK برای
      هر retry یک Request تازه می‌سازد، پس تلاش‌ها از هم جدا می‌مانند.
    * شماره‌ی تلاش از هدرِ x-stainless-retry-count خوانده می‌شود؛ هیچ هدرِ دیگری،
      بدنه یا URL خوانده یا لاگ نمی‌شود.
    * hookِ پاسخ پیش از خواندنِ بدنه اجرا می‌شود؛ پس زمان تا رسیدنِ هدرهای پاسخ است.
    * هر خطای داخلیِ hook بلعیده می‌شود تا retry یا پاسخ هرگز تحتِ‌تأثیر قرار نگیرد.
    """

    def __init__(self, agent_name: str, max_attempts: int | None) -> None:
        self._agent_name = agent_name
        self._max_attempts = max_attempts
        self._started: weakref.WeakKeyDictionary[httpx.Request, float] = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    def _attempt(self, request: httpx.Request) -> int | None:
        try:
            return int(request.headers.get(_RETRY_COUNT_HEADER, "")) + 1
        except ValueError:
            return None

    def on_request(self, request: httpx.Request) -> None:
        try:
            stage = _diagnostic_stage.get()
            if stage is None:
                return
            with self._lock:
                self._started[request] = time.perf_counter()
            _response_logger().info(
                "تلاشِ HTTPِ LLM شروع شد",
                agent=self._agent_name,
                stage=stage,
                attempt=self._attempt(request),
                max_attempts=self._max_attempts,
            )
        except Exception:  # noqa: BLE001 — عیب‌یابی نباید درخواست را بشکند
            pass

    def on_response(self, response: httpx.Response) -> None:
        try:
            stage = _diagnostic_stage.get()
            if stage is None:
                return
            request = response.request
            with self._lock:
                started = self._started.pop(request, None)
            elapsed = None if started is None else round(time.perf_counter() - started, 3)
            _response_logger().info(
                "تلاشِ HTTPِ LLM پاسخ گرفت",
                agent=self._agent_name,
                stage=stage,
                attempt=self._attempt(request),
                max_attempts=self._max_attempts,
                http_status=response.status_code,
                elapsed_to_headers_s=elapsed,
            )
        except Exception:  # noqa: BLE001 — عیب‌یابی نباید پاسخ را بشکند
            pass


def _attach_attempt_hooks(http_client: object, agent_name: str, max_attempts: int | None) -> None:
    """hookهای زمان‌سنجی را به کلاینتِ httpxِ موجود اضافه می‌کند (hookهای قبلی حفظ می‌شوند).

    سازنده‌ی httpx.Client دست نمی‌خورد؛ کلاینتِ غیرِ httpx (مثلاً ماکِ تست) نادیده
    گرفته می‌شود.
    """
    if not isinstance(http_client, httpx.Client):
        return
    timer = _AttemptTimer(agent_name, max_attempts)
    hooks = http_client.event_hooks
    http_client.event_hooks = {
        "request": [*hooks.get("request", []), timer.on_request],
        "response": [*hooks.get("response", []), timer.on_response],
    }


def _token_limit(max_tokens: int | None) -> dict[str, int]:
    """پارامترِ سقفِ توکن برای درخواست.

    ``None`` یعنی کلیدِ ``max_tokens`` اصلاً فرستاده نشود تا پیش‌فرضِ ارائه‌دهنده
    اعمال شود. ``max_tokens=None`` را نمی‌شود مستقیم به SDK داد، چون SDK آن را به
    ``"max_tokens": null`` تبدیل می‌کند؛ فقط نبودنِ کلید آن را از بدنه حذف می‌کند.
    """
    return {} if max_tokens is None else {"max_tokens": max_tokens}


def _make_http_client(user_id: str, *, verify: bool = True) -> httpx.Client:
    """یک httpx client با هدرهای مورد نیاز سرویس می‌سازد.
    - x-client-id  : ثابت، از .env
    - x-user-id    : از کد فراخوان پاس می‌شود
    - x-request-id : هر بار UUID تازه تولید می‌شود

    ``verify`` پیش‌فرض ``True`` است تا فراخوان‌های موجود بدونِ تغییر همان
    رفتارِ قبلی را داشته باشند. فقط ``LLMClient`` آن را از ``LLM_SSL_VERIFY``
    مقدار می‌دهد؛ Embedding از کارخانه‌ی جداگانه‌ی خودش استفاده می‌کند.
    """
    return httpx.Client(
        headers={
            "x-client-id": CLIENT_ID,
            "x-user-id": user_id,
            "x-request-id": str(uuid.uuid4()),
        },
        verify=verify,
    )


def _redacted_url(url: str) -> str:
    """URLِ امن برای پیامِ خطا: بدونِ userinfo، query و fragment."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _actionable_html_error(exc: APIStatusError) -> APIStatusError | None:
    """اگر پاسخِ خطا HTML بود، همان نوعِ خطا را با پیامی کوتاه و قابلِ‌اقدام برمی‌گرداند.

    پاسخِ مشاهده‌شده به مسیرِ ``/chat/completions`` (که خودِ OpenAI SDK به
    base URL می‌چسباند) HTML بود و بدنه‌ی HTML خام در پیامِ خطا ظاهر می‌شد.
    اینجا نوعِ خطا و بدنه‌ی اصلی در ``body``/``__cause__`` حفظ می‌شود، ولی پیام
    کوتاه و بدونِ HTML است تا وضعیت، URLِ نهایی و request id قابلِ‌تشخیص باشند.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None

    content_type = (response.headers.get("content-type") or "").lower()
    if not content_type.startswith("text/html"):
        return None

    request = getattr(exc, "request", None)
    method = getattr(request, "method", "POST")
    request_id = request.headers.get("x-request-id", "") if request is not None else ""
    suffix = f" (x-request-id: {request_id})" if request_id else ""
    body_length = len(exc.body) if isinstance(exc.body, str) else 0
    message = (
        f"LLM endpoint returned HTTP {exc.status_code} with content-type 'text/html' "
        f"for {method} {_redacted_url(str(response.url))}{suffix}. "
        "The endpoint answered with an HTML page instead of an OpenAI-compatible "
        "JSON response for this route. The OpenAI SDK appends '/chat/completions' "
        "to LLM_BASE_URL, and this client passes the configured base URL through "
        "unchanged; the remote service's routing/API contract for this path is "
        "the remaining unknown. No URL was modified automatically. "
        f"The original HTML body ({body_length} characters) was omitted."
    )

    try:
        return type(exc)(message, response=response, body=exc.body)
    except TypeError:  # pragma: no cover - امضای متفاوت در نسخه‌های دیگرِ SDK
        return None


class LLMClient:
    def __init__(self, user_id: str, agent_name: str, *, max_retries: int | None = None) -> None:
        """``max_retries=None`` (پیش‌فرض) یعنی همان پیش‌فرضِ SDK (۲ retry) — هیچ
        کلیدی به OpenAI داده نمی‌شود، پس رفتارِ همه‌ی فراخوان‌های موجود عوض نمی‌شود.
        عدد (مثلاً ۰ برای Task Analysis) فقط retryِ همین کلاینت را تعیین می‌کند؛
        timeout دست نمی‌خورد.
        """
        if max_retries is not None and (
            isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer or None.")
        http_client = _make_http_client(user_id, verify=LLM_SSL_VERIFY)
        self._client = OpenAI(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            http_client=http_client,
            **({} if max_retries is None else {"max_retries": max_retries}),
        )
        # فقط مشاهده: timeout و هدرها دست نمی‌خورند.
        max_retries = getattr(self._client, "max_retries", None)
        _attach_attempt_hooks(
            http_client,
            agent_name,
            max_retries + 1 if isinstance(max_retries, int) else None,
        )
        self.model = LLM_MODEL
        self.agent_name = agent_name

    def _create_completion(self, **kwargs):
        """تنها مسیرِ فراخوانیِ chat completions در این کلاس.

        خطاهای HTTPِ دارای بدنه‌ی HTML با پیامِ کوتاه و قابلِ‌اقدام دوباره پرتاب
        می‌شوند (نوعِ خطا و بدنه‌ی اصلی حفظ می‌شود). سایرِ خطاها دست‌نخورده
        بالا می‌روند.
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            improved = _actionable_html_error(exc)
            if improved is None:
                raise
            raise improved from exc

    def chat(self, user_message: str, system_prompt: str = "You are a helpful assistant.", temperature: float = 0.7, max_tokens: int | None = 1024) -> LLMResponse:
        """یک پیام ساده به مدل می‌فرستد و پاسخ متنی برمی‌گرداند.

        خروجی از نوعِ ``LLMResponse`` است که خودش یک ``str`` است؛ پس قراردادِ
        قبلی («برگرداندنِ رشته») دست‌نخورده مانده و فراداده‌هایی مثل
        ``finish_reason`` هم در دسترس‌اند (``truncation_message(raw)``).

        مصرفِ پاسخ دقیقاً یک بار — بلافاصله پس از ساختِ ``LLMResponse`` در
        مسیرِ خام یا مشاهده‌شده — به گیرنده‌ی فعال گزارش می‌شود؛ پس هیچ لایه‌ی
        بالاتری نباید دوباره حساب کند. ثبت پیش از کارهای بعدی (مثلِ
        ``gen.update``) انجام می‌شود تا خطای احتمالیِ آن مصرفِ انجام‌شده را از
        بین نبرد.

        ``max_tokens=None`` (فقط وقتی صریحاً داده شود) یعنی کلیدِ ``max_tokens``
        از درخواست حذف می‌شود و سقفِ پیش‌فرضِ ارائه‌دهنده اعمال می‌شود؛ فراخوانی
        بدونِ این آرگومان همچنان ۱۰۲۴ می‌فرستد.
        """

        if obs.enabled:
            return self._chat_observed(user_message, system_prompt, temperature, max_tokens)
        return self._chat_raw(user_message, system_prompt, temperature, max_tokens)

    def _chat_raw(self, user_message, system_prompt, temperature, max_tokens) -> LLMResponse:
        response = self._create_completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            **_token_limit(max_tokens),
        )
        parsed = _to_response(response, max_tokens)
        report_usage(parsed)
        _log_response(parsed, model=self.model, agent_name=self.agent_name)
        return parsed

    def _chat_observed(self, user_message, system_prompt, temperature, max_tokens) -> LLMResponse:

        with obs.observation(
            name=f"{self.agent_name}-chat",
            model=self.model,
            input={"system": system_prompt, "user": user_message},
            model_parameters={"temperature": temperature, **_token_limit(max_tokens)},
        ) as gen:
            response = self._create_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=temperature,
                **_token_limit(max_tokens),
            )
            content = _to_response(response, max_tokens)
            report_usage(content)
            _log_response(content, model=self.model, agent_name=self.agent_name)
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
        max_tokens: int | None = 1024,
    ):
        """پاسخ را به صورت streaming (chunk به chunk) برمی‌گرداند."""
        stream = self._create_completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            **_token_limit(max_tokens),
            stream=True,
        )
        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

