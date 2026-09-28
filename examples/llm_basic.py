"""
examples/llm_basic.py — نمونه استفاده از LLMClient

اجرا:
    uv run python examples/llm_basic.py
"""

from src.llm_client import LLMClient

USER_ID = "user-123"


def example_simple_chat() -> None:
    """ارسال یک پیام ساده و دریافت پاسخ."""
    print("── Simple Chat ──────────────────────────")
    llm = LLMClient(user_id=USER_ID, agent_name="test-llm-basic-simple")

    response = llm.chat(
        user_message="سلام! یه جمله کوتاه درباره هوش مصنوعی بگو.",
        system_prompt="You are a helpful assistant. Answer in the same language as the user.",
    )
    print(f"پاسخ: {response}\n")


def example_streaming() -> None:
    """دریافت پاسخ به صورت streaming (chunk به chunk)."""
    print("── Streaming Chat ───────────────────────")
    llm = LLMClient(user_id=USER_ID, agent_name="test-llm-basic-streaming")

    print("پاسخ: ", end="", flush=True)
    for chunk in llm.stream_chat(
        user_message="عدد پی رو تا ۱۰ رقم اعشار بگو.",
        temperature=0.0,
    ):
        print(chunk, end="", flush=True)
    print("\n")


def example_custom_system_prompt() -> None:
    """استفاده از system prompt اختصاصی."""
    print("── Custom System Prompt ─────────────────")
    llm = LLMClient(user_id=USER_ID, agent_name="test-llm-basic-custom-prompt")

    response = llm.chat(
        user_message="Explain recursion.",
        system_prompt="You are a computer science teacher. Explain concepts using simple analogies. Keep answers under 3 sentences.",
        temperature=0.3,
        max_tokens=200,
    )
    print(f"پاسخ: {response}\n")


if __name__ == "__main__":
    example_simple_chat()
    example_streaming()
    example_custom_system_prompt()