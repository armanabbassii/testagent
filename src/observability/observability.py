from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any

from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler

from .models import TraceContext
from .settings import LangfuseSettings


class Observability:
    """
    Facade ساده برای Langfuse.

    این کلاس تنها نقطه ورود پروژه به قابلیت Observability است.

    مسئولیت‌ها:
        - مدیریت Lazy Langfuse Client
        - مدیریت Lazy CallbackHandler
        - ساخت config مناسب برای LangGraph
        - ایجاد Trace Context
        - Flush کردن داده‌ها
    """

    def __init__(self, settings: LangfuseSettings) -> None:
        self._settings = settings

        self._client: Langfuse | None = None
        self._callback: CallbackHandler | None = None

    @classmethod
    def from_env(cls) -> "Observability":
        return cls(LangfuseSettings.from_env())

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def get_client(self) -> Langfuse | None:
        """
        Lazy Langfuse client.
        """

        if not self.enabled:
            return None

        if self._client is None:
            self._client = Langfuse(
                public_key=self._settings.public_key,
                secret_key=self._settings.secret_key,
                host=self._settings.host,
            )

        return self._client

    def get_callback_handler(self) -> CallbackHandler | None:
        """
        CallbackHandler مورد استفاده LangGraph/LangChain.
        """

        if not self.enabled:
            return None

        if self._callback is None:
            self._callback = CallbackHandler()

        return self._callback

    def graph_config(
        self,
        context: TraceContext,
    ) -> RunnableConfig:
        """
        تولید RunnableConfig برای LangGraph.
        """

        config: dict[str, Any] = {
            "configurable": {
                "thread_id": context.thread_id,
            }
        }

        callback = self.get_callback_handler()

        if callback is not None:
            config["callbacks"] = [callback]

        return config

    def trace(
        self,
        context: TraceContext,
    ) -> AbstractContextManager[Any]:
        """
        ایجاد Trace Context برای Langfuse.
        """

        if not self.enabled:
            return nullcontext()

        return propagate_attributes(
            user_id=context.user_id,
            session_id=context.session_id,
            trace_name=context.trace_name,
            metadata=context.metadata,
        )

    def flush(self) -> None:
        """
        ارسال Traceهای باقی‌مانده.
        """

        client = self.get_client()

        if client is not None:
            client.flush()

    def observation(
        self,
        *,
        name: str,
        model: str | None = None,
        input: Any | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> AbstractContextManager:
        """
        Creates a Langfuse Observation.

        When observability is disabled a nullcontext is returned.
        """

        if not self.enabled:
            return nullcontext()

        client = self.get_client()

        return client.start_as_current_observation(
            name=name,
            model=model,
            input=input,
            model_parameters=model_parameters,
        )