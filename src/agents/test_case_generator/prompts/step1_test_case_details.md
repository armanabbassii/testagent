You are working on an existing Python-based Test Automation Agent.

The goal of this project is to reduce manual software testing effort by generating structured API test scenarios from a development task description and the APIs/services implemented for that task.

We are now implementing STEP 1 of an interactive, human-in-the-loop workflow.

==================================================
CURRENT STEP: TEST CASE DETAILS
==================================================

This is the second stage of a two-stage generation. A compact test case plan
was already produced. Your job is to expand a subset of that plan into full,
executable test case details.

You will receive:

1. The Task Description
2. A JSON array of planned test cases (id, title, type, priority, auth_intent, related_service)

For EVERY planned test case in the array, produce:

- id — copied exactly from the plan
- preconditions — only those needed to run the test case
- steps — concrete actions a manual tester can follow
- expected_result — the observable outcome

==================================================
STRICT CONTRACT
==================================================

- Return exactly ONE detail object for EVERY planned test case.
- Copy each id exactly. Do not rename, renumber, merge, split, add, or drop test cases.
- Do not change the title, type, priority, auth_intent, or related_service.
  They are already fixed by the plan.
- Do not introduce a test case that is not in the provided plan.
- Do not return an empty array.

==================================================
IMPORTANT RULE: DO NOT INVENT BUSINESS RULES
==================================================

The Task Description is the primary source of truth for business behavior.

Do not invent:

- Business rules
- Validation rules
- Status codes
- Error messages
- Database behavior
- Required fields
- Permission rules
- Authentication behavior
- Response structures

unless they are explicitly stated in the Task Description or can be directly inferred from the described behavior.

If the expected behavior cannot be determined safely, state that in
expected_result using:

"Requires clarification"

Do not guess.

==================================================
TEST CASE QUALITY
==================================================

Test cases must be:

- Independent where possible
- Clear
- Atomic
- Executable by a manual tester
- Specific to the task
- Free from unnecessary technical assumptions

Avoid vague steps such as:

"Test the API."

Instead describe the actual action.

Bad:

"Check voucher."

Good:

"Submit the voucher creation request with valid percentage and required information."

==================================================
DEPENDENCIES
==================================================

If one operation depends on another operation, mention the required data in
preconditions.

Example:

"An existing voucher ID is available."

Do not invent an actual ID.

==================================================
OUTPUT FORMAT
==================================================

Return ONLY valid JSON.

Do not return Markdown.

Do not wrap the JSON in ```json.

Use exactly this top-level structure:

{
  "test_cases": [
    {
      "id": "TC-001",
      "preconditions": [
        "..."
      ],
      "steps": [
        "...",
        "..."
      ],
      "expected_result": "..."
    }
  ]
}

Do not include any field other than id, preconditions, steps, and
expected_result.

==================================================
INPUT
==================================================

You will receive:

TASK DESCRIPTION:
{{task_description}}

TEST CASE PLAN:
{{test_case_plan}}

Expand every planned test case in the plan and return the JSON details.
