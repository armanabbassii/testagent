"""
example_agents.py — چند نمونه ایجنت آماده برای شروع

هر ایجنت با _build_system_prompt کار می‌کند که memory_context را
به صورت خودکار به system prompt اضافه می‌کند.
"""

from langchain_core.messages import AIMessage
from src.agents.base_agent import BaseAgent
from src.agents.state import AgentState


class RouterAgent(BaseAgent):
    """مسیریاب درخواست‌ها.

    پارامتر:
        enable_rag : اگر True باشد، "rag" هم یکی از دسته‌بندی‌های ممکن می‌شود
                    (برای سوالاتی که نیاز به جستجو در اسناد دارند)
    """

    def __init__(self, enable_rag: bool = False) -> None:
        self._enable_rag = enable_rag
        categories = ["research", "summarize", "general", "test_case_generator"]
        if enable_rag:
            categories.insert(0, "rag")

        rag_hint = (
            "Use 'rag' if the question likely requires looking up specific facts "
            "from a knowledge base or documents (e.g., 'what does our policy say about...', "
            "'according to the docs...'). "
        ) if enable_rag else ""

        testcase_hint = (
            "Use 'test_case_generator' ONLY when the user wants to generate structured API "
            "test cases or a Postman collection from Swagger/OpenAPI documentation "
            "(e.g., 'generate API test cases from this Swagger URL', 'create a Postman "
            "collection from OpenAPI', 'analyze these Swagger endpoints and generate "
            "positive/negative test cases'), or from a scenario YAML file describing an "
            "API flow (e.g., 'build a Postman collection from scenarios/x.yaml', "
            "'generate the test collection for this scenario file'). "
            "Do NOT use it for normal programming questions, "
            "general API questions, or unrelated Postman questions. "
        )

        super().__init__(
            name="router",
            system_prompt=(
                f"You are a routing assistant. Analyze the user's request and classify it "
                f"as one of: {categories}. "
                f"{rag_hint}"
                f"{testcase_hint}"
                f"Reply with ONLY the category word, nothing else."
            ),
        )

    def run(self, state: AgentState) -> dict:
        llm = self._get_llm(state["user_id"])
        # router از memory_context استفاده نمی‌کند — فقط مسیریابی می‌کند
        category = llm.chat(
            user_message=self._last_human_message(state),
            system_prompt=self.system_prompt,
            temperature=0.0,
            max_tokens=10,
        ).strip().lower()

        return {
            "metadata": {**state.get("metadata", {}), "route": category},
            "messages": [AIMessage(content=f"[router] → {category}", name=self.name)],
        }


class ResearchAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="research",
            system_prompt=(
                "You are a research assistant. Provide detailed, factual answers "
                "with clear structure. Be thorough but concise."
            ),
        )

    def run(self, state: AgentState) -> dict:
        llm = self._get_llm(state["user_id"])
        answer = llm.chat(
            user_message=self._last_human_message(state),
            system_prompt=self._build_system_prompt(state),  # ← memory inject
        )
        return {"messages": [AIMessage(content=answer, name=self.name)]}


class SummaryAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="summarizer",
            system_prompt=(
                "You are a summarization assistant. Create clear, concise summaries "
                "that capture the key points. Use bullet points when helpful."
            ),
        )

    def run(self, state: AgentState) -> dict:
        llm = self._get_llm(state["user_id"])
        summary = llm.chat(
            user_message=self._last_human_message(state),
            system_prompt=self._build_system_prompt(state),  # ← memory inject
        )
        return {"messages": [AIMessage(content=summary, name=self.name)]}


class GeneralAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="general",
            system_prompt="You are a helpful general-purpose assistant.",
        )

    def run(self, state: AgentState) -> dict:
        llm = self._get_llm(state["user_id"])
        response = llm.chat(
            user_message=self._last_human_message(state),
            system_prompt=self._build_system_prompt(state),  # ← memory inject
        )
        return {"messages": [AIMessage(content=response, name=self.name)]}