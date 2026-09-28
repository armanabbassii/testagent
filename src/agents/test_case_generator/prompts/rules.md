## Test Case Generation Rules

Follow these rules when turning an analyzed Swagger/OpenAPI specification into API
test cases. The specification is the only source of truth.

### 1. Positive test cases

- Generate at least one valid success scenario for every endpoint.
- Use the success status code documented in the specification (`success status`),
  never a guessed one.
- Build the request from the documented parameters, required headers, and request
  body sample. Send all required fields with valid values.
- Use the documented `Base URL` together with the endpoint path.

### 2. Negative test cases

Generate negative scenarios only where they apply to the endpoint. Pick from:

- Invalid request body (malformed JSON, wrong field types)
- Missing required fields (body fields, path/query parameters, required headers)
- Invalid parameter values (out of range, wrong type, wrong format)
- Invalid path or query parameters (non-existent resource id, unknown query value)
- Unauthorized request (missing or empty `Authorization` header → 401)
- Forbidden request (valid token without permission → 403), only when the
  specification documents 403
- Validation errors (constraint violations → 400 or 422 as documented)

Prefer the error status codes listed for the endpoint (`error statuses`). If an
error code is not documented, use the closest standard code and say it is assumed.
Skip a negative scenario when the endpoint has nothing to violate — for example a
`GET` with no parameters and no body needs no "missing required field" case.

### 3. HTTP method awareness

- `GET` — read only. Focus on query and path parameter validation, filtering,
  pagination when documented, and not-found cases. Never send a request body.
- `POST` — creation or action. Cover a valid payload, missing required fields,
  invalid types, and duplicate or conflicting data when the specification
  documents 409.
- `PUT` — full replacement. Send the complete body; cover a partial body as a
  negative case and a non-existent resource id for 404.
- `PATCH` — partial update. Send a subset of fields as the positive case; cover an
  empty body and invalid field values as negative cases.
- `DELETE` — removal. Cover deleting an existing resource, deleting a
  non-existent id (404), and a missing or invalid id parameter.

### 4. Authorization

- Every generated Postman request must include the header:

  ```
  Authorization: Bearer {{token}}
  ```

- Keep `{{token}}` as a Postman variable. Never generate, guess, or embed a real
  token, API key, or credential.
- The only exception is the unauthorized negative case, where the header is
  intentionally omitted or left empty to assert a 401.

### 5. Response validation

Every test case must state:

- The expected HTTP status code.
- The important response fields to assert, when the specification lists response
  fields. Assert that the fields exist and have the expected type; assert exact
  values only when the request determines them.
- The expected error behaviour for negative cases: the status code, and the error
  body shape when it is documented.

### 6. Swagger-driven behaviour

- Never invent an endpoint, path, or HTTP method that is not in the specification.
- Never invent request fields, except when a negative scenario explicitly needs an
  unknown or extra field, and label it as such.
- Use the documented parameters, request bodies, required fields, response fields,
  and status codes whenever they are available.
- When information is missing from the specification, state the assumption inside
  the test case instead of silently inventing it.

### 7. Test case quality

- Give every test case a clear, meaningful name that includes the method, the
  resource, and the scenario — for example
  `POST /api/v1/users — missing required email (Negative)`.
- Mark every test case explicitly as **Positive** or **Negative**.
- No duplicate test cases: one scenario per behaviour, not one per field
  permutation.
- Favour practical coverage over exhaustive combinations. A handful of meaningful
  cases per endpoint beats dozens of near-identical ones.
- Group test cases by controller, following the grouping in the specification.

### 8. Dependencies between endpoints

- When a response returns an id or other value that a later endpoint needs, note
  it as a reusable Postman variable (for example `{{userId}}`) and say which test
  case sets it.
- Order dependent test cases so the value is created before it is used — typically
  create (`POST`) → read (`GET`) → update (`PUT`/`PATCH`) → delete (`DELETE`).
- Only claim a dependency that the specification supports: a matching path
  parameter name, response field, or schema. Do not assume relationships between
  endpoints that the specification does not show.

### 9. Scenario-driven generation

These rules apply only when the request comes from a scenario file. A scenario
describes a business flow explicitly, so the flow is a contract rather than a
guess, and it narrows the rules above instead of replacing them.

- The scenario decides **what** is generated: which steps, in which order, with
  which negative cases. Swagger still decides **how** each request looks —
  method, path, parameters, schema, and documented status codes.
- Generate exactly the negative cases the scenario lists. Do not add others just
  because the endpoint would technically support them.
- Each negative case is its own request. It never mutates the positive request of
  its step.
- An expected status written in the scenario wins. Otherwise take it from
  Swagger. Only when neither provides one may a standard code be assumed, and
  the assumption must be stated.
- An `unauthorized` case omits the `Authorization` header entirely. Every other
  request keeps `Authorization: Bearer {{token}}`.
- A value the scenario declares — including a Postman variable such as
  `{{voucherId}}` — is used verbatim. Never resolve a variable to a real value,
  and never generate a real token, credential, or identifier.
- A variable is created by a step's `extract` block, written to the scope the
  scenario asks for (`global` or `collection`), and consumed by later steps in
  the path, the query string, or the request body.
