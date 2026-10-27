# Swagger snapshots

Local OpenAPI/JSON snapshots that Step 2 reads during a normal run. Step 2 does
**not** fetch the remote Swagger UI while mapping: it loads one of these files.

## Files

| File | Source |
| --- | --- |
| `admin.json` | `http://podium-back.devpod.ir/api/v3/api-docs/Admin` |
| `customer.json` | `http://podium-back.devpod.ir/api/v3/api-docs/Customer` |

The URLs are registered in `SWAGGER_SOURCES`
(`src/agents/test_case_generator/swagger_snapshots.py`).

**Never commit credentials.** No tokens, cookies, API keys or `Authorization`
values belong in these files. A snapshot is the public API description only.

## Refreshing

In the Step 2 page of the wizard, `Update Admin` and `Update Customer` each fetch
their source URL (a fresh request on every click) and replace **only** their own
file. `refresh_snapshot` does the work:

1. Download the complete response (timeouts: `SWAGGER_CONNECT_TIMEOUT_SECONDS`,
   default 10, and `SWAGGER_READ_TIMEOUT_SECONDS`, default 120 — the server builds
   the document slowly on its first request after a restart).
2. Require an HTTP 2xx status, valid JSON, an `openapi`/`swagger` version field
   and at least one operation (validated with the same `discover_from_spec` Step 2
   uses).
3. Write the exact response bytes to a temporary file in this directory and
   atomically `os.replace` it over the target.

If any step fails, the previous file is kept untouched, the error is shown under
the button and the button can be clicked again. Nothing is retried automatically
and Step 2 mapping is not started by a refresh — the next Step 2 run reads the new
file (there is no in-memory cache). If Step 2 already has a result, the wizard
reports that the snapshot contents changed after it ran.

You can still replace a file by hand; the same rule applies — save the raw
OpenAPI JSON under the matching filename.

## Why local snapshots

* **Deterministic.** The same run maps against the same document, instead of
  whatever the remote server happened to serve — and Step 2 keeps running when
  the host is unreachable or requires authentication.
* **Small downstream payload.** The discovered catalog stays inside Step 2 for
  validation and debugging; only the compact mapping contract is passed to Step 3.
