# Swagger snapshots

Local OpenAPI/JSON snapshots that Step 2 reads during a normal run. Step 2 does
**not** fetch the remote Swagger UI while mapping: it loads one of these files.

## Files

| File | Source |
| --- | --- |
| `admin.json` | `https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Admin` |
| `customer.json` | `http://podium-back.devpod.ir/api/swagger-ui/index.html?urls.primaryName=Customer` |

## How they are maintained

These files are **manually maintained**. Open the source URL, take the OpenAPI
JSON document it serves and save it here under the matching filename.

**Never commit credentials.** No tokens, cookies, API keys or `Authorization`
values belong in these files. A snapshot is the public API description only.

## Refreshing

Refreshing them automatically is deliberately **not implemented**. The Step 2 UI
shows an `Update` button per source, but the button is a placeholder that reports
refresh as not implemented — it does not perform any network request.

The seam for a future refresh already exists: every source is registered in
`src/agents/test_case_generator/swagger_snapshots.py` with its `remote_url`, and
that registry is the only place a refresh would need to write to.

## Why local snapshots

* **Deterministic.** The same run maps against the same document, instead of
  whatever the remote server happened to serve — and Step 2 keeps running when
  the host is unreachable or requires authentication.
* **Small downstream payload.** The discovered catalog stays inside Step 2 for
  validation and debugging; only the compact mapping contract is passed to Step 3.
