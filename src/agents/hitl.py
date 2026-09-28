"""
hitl.py — Human-in-the-Loop با مکانیزم interrupt/resume لنگ‌گراف
"""

from langgraph.types import Command
from src.agents.state import AgentState


class HITLHandler:

    @staticmethod
    def prompt_user(state: AgentState) -> bool:
        """در CLI از کاربر تأیید می‌گیرد. در پروژه واقعی با UI/webhook جایگزین کن."""
        print("\n" + "─" * 50)
        print("⏸  Human-in-the-Loop — بررسی مورد نیاز است")
        print("─" * 50)

        messages = state.get("messages", [])
        for msg in messages[-3:]:
            role = getattr(msg, "name", None) or msg.__class__.__name__
            preview = msg.content[:200] + ("..." if len(msg.content) > 200 else "")
            print(f"[{role}]: {preview}")

        print("─" * 50)
        while True:
            answer = input("✅ ادامه دهم؟ (y/n): ").strip().lower()
            if answer in ("y", "yes", "بله", "آره"):
                return True
            if answer in ("n", "no", "خیر", "نه"):
                return False

    @staticmethod
    def resume(graph, approved: bool, config: dict) -> AgentState:
        """اجرای گراف را از نقطه interrupt ادامه می‌دهد."""
        return graph.invoke(Command(resume={"hitl_decision": approved}), config)