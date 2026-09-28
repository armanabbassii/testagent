# ایجاد ایجنت جدید

## قوانین کلی

- هر ایجنت **یک پوشه مستقل** دارد زیر `src/agents/`
- هر ایجنت از `BaseAgent` ارث می‌برد
- متد `run()` باید `dict` برگرداند (نه کل state)
- همیشه از `self._build_system_prompt(state)` استفاده کنید تا memory context به صورت خودکار inject شود
- همیشه از `self._get_llm(state["user_id"])` استفاده کنید — هرگز مستقیم `LLMClient` نسازید
- لاگ از طریق `DebugConfig` مدیریت می‌شود — هرگز از `print` برای دیباگ استفاده نکنید

## ساختار پوشه

```
src/agents/my_agent/
├── __init__.py
└── agent.py
```

اگر ایجنت prompt فایل دارد:

```
src/agents/my_agent/
├── __init__.py
├── agent.py
└── prompts/
    ├── system.md
    └── rules/
        └── *.md
```

## مرحله ۱ — ساخت ایجنت

```python
# src/agents/my_agent/agent.py

from langchain_core.messages import AIMessage
from src.agents.base_agent import BaseAgent
from src.agents.state import AgentState
from src.debug import DebugConfig


class MyAgent(BaseAgent):
    name = "my_agent"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        super().__init__(
            name=self.name,
            system_prompt="You are a specialized assistant for ...",
        )
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def run(self, state: AgentState) -> dict:
        self._log.info("شروع پردازش", user_id=state["user_id"])

        llm = self._get_llm(state["user_id"])
        response = llm.chat(
            user_message=self._last_human_message(state),
            system_prompt=self._build_system_prompt(state),  # ← memory-aware
        )

        self._log.debug("پاسخ دریافت شد", response_chars=len(response))

        return {
            "messages": [AIMessage(content=response, name=self.name)],
            # فقط فیلدهایی که تغییر کرده‌اند را برگردانید
        }
```

## مرحله ۲ — ساخت `__init__.py`

```python
# src/agents/my_agent/__init__.py

from src.agents.my_agent.agent import MyAgent

__all__ = ["MyAgent"]
```

## مرحله ۳ — اضافه کردن به گراف

فایل `src/agents/graph.py` را ویرایش کنید:

```python
from src.agents.my_agent import MyAgent

def build_graph(...):
    ...
    my_agent = MyAgent(debug_config=cfg)
    builder.add_node(my_agent.name, my_agent)

    # اضافه کردن edge
    builder.add_edge("previous_node", my_agent.name)
    builder.add_edge(my_agent.name, "next_node")
```

## مرحله ۴ — اضافه کردن به State (در صورت نیاز)

اگر ایجنت به فیلد جدیدی در state نیاز دارد، فایل `src/agents/state.py` را ویرایش کنید:

```python
class AgentState(TypedDict):
    ...
    my_new_field: str   # فیلد جدید
```

## مثال کامل با prompt فایل

```python
from pathlib import Path

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"

class MyAgent(BaseAgent):
    name = "my_agent"

    def __init__(self, debug_config=None):
        super().__init__(
            name=self.name,
            system_prompt=_PROMPT_PATH.read_text(encoding="utf-8"),
        )
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
```

## چک‌لیست قبل از merge

- [ ] ایجنت در پوشه مستقل خودش قرار دارد
- [ ] از `BaseAgent` ارث برده
- [ ] متد `run()` فقط `dict` برمی‌گرداند
- [ ] از `_get_llm(state["user_id"])` استفاده شده
- [ ] از `_build_system_prompt(state)` استفاده شده (نه `self.system_prompt` مستقیم)
- [ ] لاگ از طریق `DebugConfig` پیاده‌سازی شده
- [ ] `__init__.py` ساخته شده
- [ ] به گراف اضافه شده
- [ ] مستند شده (این فایل آپدیت شده یا docstring کافی دارد)