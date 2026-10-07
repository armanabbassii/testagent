# test case generator from swagger

import sys
sys.path.insert(0, "/home/dotin/Downloads/agents2/agents")

from langchain_core.messages import HumanMessage  # noqa: E402
from src.agents.test_case_generator.agent import TestCaseGeneratorAgent  # noqa: E402

agent = TestCaseGeneratorAgent()

url = "https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Admin#/voucher-admin-controller"
# url = "https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Admin#/quote-management-admin-controller"

state = {
    "user_id": "test-user",
    "messages": [HumanMessage(content=f"لطفا برای این سرویس تست کیس بساز: {url}")],
}

result = agent.run(state)
print(result["messages"][-1].content)

# ------------------------------------
# test scenarios generator
#
import sys  # noqa: E402

sys.path.insert(0, "/home/dotin/Downloads/agents2/agents")

from langchain_core.messages import HumanMessage  # noqa: E402
from src.agents.test_case_generator.agent import TestCaseGeneratorAgent  # noqa: E402


SCENARIO_PATH = (
    "/home/dotin/Downloads/agents2/agents/"
    "src/agents/test_case_generator/scenarios/"
    "voucher_creation_flow.yaml"
)


def main():
    agent = TestCaseGeneratorAgent()

    state = {
        "user_id": "test-user",
        "messages": [
            HumanMessage(
                content=(
                    "لطفا بر اساس این Scenario یک Postman Collection بساز: "
                    f"{SCENARIO_PATH}"
                )
            )
        ],
    }

    result = agent.run(state)

    print("\n========== RESULT ==========\n")
    print(result["messages"][-1].content)


if __name__ == "__main__":
    main()