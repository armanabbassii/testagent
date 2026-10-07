"""
base_agent.py — کلاس پایه‌ای که همه ایجنت‌ها از آن ارث می‌برند
"""

from abc import ABC, abstractmethod
from src.agents.state import AgentState
from src.llm_client import LLMClient
from src.security.prompt_guard import PromptGuard


class BaseAgent(ABC):
    def __init__(
        self,
        name: str,
        system_prompt: str,
        prompt_guard: PromptGuard | None = None,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        # اگر guard پاس نشد، از .env می‌خواند
        self._guard = prompt_guard if prompt_guard is not None else PromptGuard.from_env()

    def _get_llm(self, user_id: str) -> LLMClient:
        return LLMClient(user_id=user_id, agent_name=self.name)

    def _last_human_message(self, state: AgentState) -> str:
        from langchain_core.messages import HumanMessage
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                return msg.content
        return ""

    def _safe_last_human_message(self, state: AgentState) -> str:
        """آخرین پیام کاربر را بعد از بررسی injection برمی‌گرداند.

        اگر تلاش injection تشخیص داده شود PromptInjectionError پرتاب می‌شود.
        در ایجنت‌هایی که ورودی کاربر مستقیم به LLM می‌رود، این متد را استفاده کنید.
        """
        text = self._last_human_message(state)
        return self._guard.sanitize_input(text)

    def _build_system_prompt(self, state: AgentState) -> str:
        """system_prompt را با خاطرات بازیابی‌شده غنی می‌کند.

        اگر memory_context خالی باشد، همان system_prompt اصلی برگردانده می‌شود.
        """
        context: list[str] = state.get("memory_context", [])
        if not context:
            return self.system_prompt

        memory_block = "\n".join(f"  - {c}" for c in context)
        return (
            f"{self.system_prompt}\n\n"
            f"## Relevant memory about this user:\n{memory_block}\n"
            f"Use this context to personalize your response when relevant."
        )

    @abstractmethod
    def run(self, state: AgentState) -> dict:
        """منطق اصلی ایجنت — باید در زیرکلاس پیاده‌سازی شود."""
        ...

    def __call__(self, state: AgentState) -> dict:
        return self.run(state)