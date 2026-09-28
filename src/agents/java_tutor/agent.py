from langchain_core.messages import AIMessage
from src.agents.base_agent import BaseAgent
from src.agents.state import AgentState
from src.debug import DebugConfig


class JavaTutorAgent(BaseAgent):
    name = "java_tutor"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        super().__init__(
            name=self.name,
            system_prompt=(
                "You are a Java learning assistant. "
                "When the user asks a Java-related question, analyze it and "
                "provide a simple, clear explanation. "
                "Use short code examples when helpful. "
                "Keep answers beginner-friendly and concise."
            ),
        )
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def run(self, state: AgentState) -> dict:
        self._log.info("processing java question", user_id=state["user_id"])

        llm = self._get_llm(state["user_id"])
        response = llm.chat(
            user_message=self._last_human_message(state),
            system_prompt=self._build_system_prompt(state),
        )

        self._log.debug("response generated", response_chars=len(response))

        return {
            "messages": [AIMessage(content=response, name=self.name)],
        }
