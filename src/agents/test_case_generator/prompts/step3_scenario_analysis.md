You are working on an existing Python-based Test Automation Agent.

The project generates structured API test scenarios from a development task, and is being built as an interactive, human-in-the-loop workflow.

Step 1 is already implemented: it produced the structured business test cases.
Step 2 is already implemented: it discovered the real API operations from the Swagger/OpenAPI documents and mapped each test case to one of them.

==================================================
CURRENT STEP: SCENARIO & DEPENDENCY ANALYSIS
==================================================

You receive two structured results.

1. STEP 1 RESULT

- task_summary: what the development task is about
- identified_requirements: the business requirements that were identified
- test_cases: the business test cases, each with id, title, type, priority, preconditions, steps, expected_result and related_service
- clarifications: open questions left by Step 1

2. STEP 2 RESULT

- services: the API operations discovered from the real Swagger/OpenAPI documents, each with its parameters, request body and responses
- mappings: the operation chosen for each test case, or null when it could not be resolved
- clarifications: open questions left by Step 2

The STEP 2 RESULT is the only technical source of truth about the APIs. You do
not fetch Swagger, you do not parse Swagger, and you do not discover operations.
You only use what the STEP 2 RESULT already contains.

You never invent a response field, a parameter, a path or an operation.

==================================================
SCOPE OF THIS STEP
==================================================

You do NOT:

- Execute APIs
- Execute tests
- Build a Postman collection or Postman scripts
- Produce Postman syntax such as {{variable}}
- Extract runtime values
- Generate authorization headers or credentials

You describe the scenarios, the execution order and the dependencies. A later
step turns that description into Postman artifacts.

==================================================
WHAT YOU MUST PRODUCE
==================================================

1. SCENARIOS
   Groups of test cases that belong to the same business flow.

2. EXECUTION ORDER
   The order in which test cases are to be executed, and which test cases each
   one depends on.

3. DATA DEPENDENCIES
   Values produced by one test case that other test cases need as input.

4. CLARIFICATIONS
   Anything you could not determine reliably.

==================================================
SCENARIO RULES
==================================================

Group test cases by business meaning and by the APIs they are mapped to.

Base a scenario on shared business intent — the same flow, the same lifecycle,
the same resource being driven through several states.

Do NOT group unrelated test cases merely because they belong to the same service,
the same controller or the same tag.

Do NOT group unrelated test cases merely because they are all positive or all
negative.

A test case may belong to more than one scenario only when the analysis genuinely
requires it.

Every test case id you reference must exist in the STEP 1 RESULT. Never invent a
test case id.

Multiple scenarios are allowed. Scenario ids must be unique.

==================================================
EXECUTION ORDER RULES
==================================================

Every test case id you reference must exist in the STEP 1 RESULT.

depends_on may contain zero or more test case ids. An independent test case has
an empty depends_on.

Only declare a dependency when there is evidence for it: a data flow, a state
that a later test case relies on, or a precondition that another test case
creates. Do not invent dependencies.

Every test case id that appears in depends_on must itself have an entry in
execution_order, and its order must be lower than the order of the test case
that depends on it.

Circular dependencies are invalid:

  A depends on B, B depends on A

  A depends on B, B depends on C, C depends on A

Never produce them.

Order values must be unique positive integers and must be deterministic.

==================================================
DATA DEPENDENCY RULES
==================================================

This is the most important part of this step.

A data dependency describes one value produced by a source test case and
consumed by one or more target test cases.

For the source, give the location of the value inside the response of the source
test case, and a JSON path to it:

  "location": "response.body" or "response.header"
  "path": "$.id"

The path must name a field that actually exists in the response of the source
test case's mapped operation, as described in the STEP 2 RESULT.

Do NOT invent response fields. Do NOT assume a field is an identifier merely
because its name looks like one — for example, two different operations may both
expose an "id" that means different things. Use the STEP 1 business meaning to
decide whether the value really is the same one.

For each target, give where the value is consumed in the request of the target
test case:

  "location": "path" | "query" | "header" | "body"
  "parameter": the parameter or body field name

The parameter must exist on the target test case's mapped operation, as described
in the STEP 2 RESULT.

If a source test case has no resolved mapping in Step 2, it cannot be the source
of a data dependency.

If the source field cannot be determined reliably, do not guess. Omit the data
dependency and raise a clarification instead.

variable_name must be a deterministic identifier that a later step can turn into
a Postman variable. Use letters, digits and underscores, start with a letter or
an underscore, and use the same name every time for the same value.

==================================================
CONFIDENCE
==================================================

confidence must be exactly one of:

- high
   The relationship is clearly supported by the Step 1 test case intent and by
   the Step 2 API information.

- medium
   The relationship is the most plausible one, but it relies on interpretation.

- low
   The relationship is uncertain.

reason must always be a short, factual sentence explaining the inference.

If the relationship is ambiguous, use a lower confidence and raise a
clarification. Never silently guess.

==================================================
CLARIFICATIONS
==================================================

When you cannot reliably determine a scenario, an execution order, a dependency
or a data source, raise a clarification instead of inventing information. A
correct "I cannot determine this" is more useful than a confident guess.

Each clarification is an object:

  "type": a short machine-readable category, for example "data_dependency",
          "execution_order", "scenario" or "api_mapping"
  "test_case_id": the test case it concerns, or an empty string when it is not
          about one specific test case
  "message": one clear sentence a human reviewer can act on

Legitimate cases include: the response field cannot be identified; several
source fields are possible; a required dependency is ambiguous; test cases look
related but their order cannot be established; the Step 2 mapping is unresolved;
an input appears to need data from another test case but the source cannot be
identified.

==================================================
OUTPUT FORMAT
==================================================

Return ONLY valid JSON.

Do not return Markdown.

Do not wrap the JSON in ```json.

The ids, field names and operations in the examples below are placeholders.
Always use the exact test case ids from the STEP 1 RESULT, and only operations,
parameters and response fields that appear in the STEP 2 RESULT.

Use exactly this top-level structure:

{
  "scenarios": [
    {
      "id": "SC-001",
      "title": "<short business title>",
      "test_case_ids": ["<test case id from the input>"],
      "reason": "<why these test cases belong to the same flow>"
    }
  ],
  "execution_order": [
    {
      "test_case_id": "<test case id from the input>",
      "order": 1,
      "depends_on": []
    }
  ],
  "data_dependencies": [
    {
      "variable_name": "createdId",
      "source": {
        "test_case_id": "<test case id from the input>",
        "location": "response.body",
        "path": "$.id"
      },
      "targets": [
        {
          "test_case_id": "<test case id from the input>",
          "location": "path",
          "parameter": "id"
        }
      ],
      "confidence": "high",
      "reason": "<why this value flows from the source to the targets>"
    }
  ],
  "clarifications": []
}

Every test case from the STEP 1 RESULT that you place in a scenario must also
appear in execution_order. data_dependencies and clarifications may be empty
arrays, but the keys must always be present.

==================================================
INPUT
==================================================

STEP 1 RESULT:

{{step1_result}}

STEP 2 RESULT:

{{step2_result}}

Analyse the test cases above, group them into scenarios, establish the execution
order and the dependencies between them, and return the structured JSON result.
