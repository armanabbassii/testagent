# Daily Swagger synchronisation

Keeps the Swagger/OpenAPI snapshots in `data/swagger/` current, independently of Streamlit.
Every run keeps going until all enabled sources have been fetched, validated and
synchronised. This document describes what is implemented.

## Components

| Part | Module | Role |
|---|---|---|
| Env loading | `src/env_loader.py` | Loads `.env` (`ENV_FILE`, default `.env` relative to the current directory) without any required variable |
| Settings | `src/swagger_settings.py` | Swagger timeouts, base URL, shrink-guard ratios, `SWAGGER_SYNC_*` (strict parsing; errors name the variable, never the value) |
| Swagger core | `src/agents/test_case_generator/swagger_snapshots.py` | Registry (`SWAGGER_SOURCES`), `fetch_validated` (document + HTTP status), `decide_update`, `write_snapshot`, `remove_stale_temp_files`, `refresh_snapshot` (the Update button) |
| Shared locks | `src/agents/test_case_generator/swagger_locks.py` | Write lock (Update button and job) and run lock (job), on the Execution Gate's `OsLock` |
| Job | `src/swagger_sync/job.py` | Per-source scheduling, retry until success, report, `due_reason` |
| Reporting | `src/swagger_sync/report.py` | Rotating log and `last_run.json` / `last_check.json` |
| Scheduler | `src/swagger_sync/task.py` | Task XML, `install-task`, `update-task`, `uninstall-task`, `status` |
| Entry point | `python -m src.swagger_sync` / `scripts/swagger_sync.cmd` | |

**No LLM dependency.** Importing `src` loads `.env` but no longer imports `LLMClient`,
`EmbeddingClient` or `src/config.py`; `src` and `src.agents` export their names lazily (PEP 562)
with unchanged public names. The job never needs `LLM_*`, `EMBEDDING_*` or `CLIENT_ID`.

## Commands

```
python -m src.swagger_sync run                      # real run; retries until every source succeeds
python -m src.swagger_sync run --if-due             # same, only if no successful run since the last daily time
python -m src.swagger_sync run --check              # real GET + validation (same retries); no snapshot is changed
python -m src.swagger_sync run --check --probe-https  # also tries the https:// form of each URL once
python -m src.swagger_sync run --dry-run            # no network, no lock, no file written; JSON on stdout
python -m src.swagger_sync install-task | update-task | uninstall-task | status
```

Exit codes: `0` success (or disabled, or not due); `1` partial failure and `2` all sources
failed (only possible with a configured cap); `3` configuration error; `4` another run is in
progress; `5` interrupted.

## Per-source flow

1. `fetch_validated`: GET (connect/read timeouts from `SWAGGER_CONNECT_TIMEOUT_SECONDS` /
   `SWAGGER_READ_TIMEOUT_SECONDS`), HTTP 2xx, non-empty valid JSON, `openapi`/`swagger` field,
   at least one operation (`discover_from_spec`). Every failure carries a `kind` and, when a
   response arrived, its HTTP status (validation failures included).
2. `write_snapshot`, under the shared write lock:
   * identical document (same bytes or equal JSON) → `UNCHANGED`, nothing written;
   * shrink guard: new operations < ratio × current operations → refused (`kind=shrink`);
   * otherwise temp file in the same folder + `fsync` + `os.replace` → `UPDATED`.
3. Any failure keeps the previous file. Only the registered `<key>.json` files are ever
   written; the file name comes from the registry, never from the response.

The Update button in Step 2 uses the same `refresh_snapshot`: same validation, shrink guard,
unchanged detection and write lock. It does not retry; the user clicks again.

## Retry until success

* All sources are tried once, in registry order. A source that succeeded is never requested
  again in the same run; only failing sources are retried, each on its own schedule.
* **Every fetch/processing failure is retried:** connect/read timeout, connection and request
  errors, every HTTP 4xx/5xx/429, empty response, invalid JSON, not OpenAPI, unusable document,
  no operations, shrink guard, write lock busy, permission/write errors, and unexpected
  exceptions. The previous file is kept every time.
* Delay after the *n*-th failed attempt of a source: `interval × multiplier^(n−1)`, capped at
  `SWAGGER_SYNC_MAX_RETRY_INTERVAL_SECONDS`. Both intervals are at least 1 second, so the server is
  never hammered. A numeric `Retry-After` is honoured if it is longer, but never beyond the
  same cap. Defaults: 30, 60, 120, 240, 300, 300, … seconds.
* Attempts and runtime are **unlimited by default** (`SWAGGER_SYNC_MAX_ATTEMPTS=0`,
  `SWAGGER_SYNC_MAX_RUNTIME_MINUTES=0`). If a cap is configured, a source still failing at the cap
  ends `FAILED`, the run ends `PARTIAL_FAILURE`/`FAILED` (never `SUCCESS`), and every report
  carries a warning about the cap.
* **Fail closed:** an invalid global setting (any `SWAGGER_SYNC_*`, the base URL, an unknown
  source key) stops the run before any request (exit 3, recorded in the report). A
  per-source `kind=configuration` failure is not retried either.
* **Crash/stop:** Ctrl+C ends the run as `INTERRUPTED` (exit 5). A hard kill, power loss or
  logoff simply ends the process: the OS releases the run lock, every snapshot is either the
  old or the complete new file, and the next run starts again from the beginning. A temporary
  file left by a write that was killed half-way (`.<key>.json.*.tmp`) is removed at the start
  of the next run, under the write lock. Only the registered sources' temp names are removed.

## Locks

| Lock | Holder | Held | When busy |
|---|---|---|---|
| `write.lock` | job and Update button | around each decide + atomic replace | wait up to 30 s, then `kind=locked`; the job retries, the button reports it |
| `run.lock` | job (`run`, `--check`) | the whole run, however long the retries take | exit 4, no request sent, report not overwritten |

Both are OS byte-range locks (`msvcrt.locking`), not PID or timestamp files, so a crashed
process can never leave a stale lock. For the real `data/swagger` the lock files are in
`logs/swagger_sync/` (git-ignored). For any other snapshot folder (tests) they are in a
per-folder directory under the system temp. Lock files are never deleted.

## Configuration (`.env`, all optional)

```ini
SWAGGER_SYNC_ENABLED=true                 # false → run logs "disabled", exits 0
SWAGGER_SYNC_DAILY_TIME=03:00             # local time; applied to the task by install-task/update-task
SWAGGER_SYNC_SOURCES=                     # empty = all enabled sources; e.g. admin,customer
SWAGGER_API_DOCS_BASE_URL=http://podium-back.devpod.ir/api/v3/api-docs
SWAGGER_CONNECT_TIMEOUT_SECONDS=10
SWAGGER_READ_TIMEOUT_SECONDS=120
SWAGGER_SYNC_RETRY_INTERVAL_SECONDS=30    # first delay, >= 1
SWAGGER_SYNC_BACKOFF_MULTIPLIER=2         # 1 = fixed interval
SWAGGER_SYNC_MAX_RETRY_INTERVAL_SECONDS=300   # cap for every delay and Retry-After, >= 1
SWAGGER_SYNC_MAX_ATTEMPTS=0               # 0 = unlimited (default); a number caps attempts per source
SWAGGER_SYNC_MAX_RUNTIME_MINUTES=0        # 0 = unlimited (default); also the task's time limit (+10 min)
SWAGGER_SYNC_MAX_SHRINK_RATIO=0.5         # 0..1; 0 disables
# SWAGGER_SYNC_MAX_SHRINK_RATIO_CRM=0.2   # per source (key in upper case)
SWAGGER_SYNC_LOG_DIR=logs/swagger_sync
SWAGGER_SYNC_LOG_BACKUPS=14               # rotated files of 1 MB each
```

An invalid value is a configuration error (exit 3, recorded in `last_run.json`), never a silent
fallback. An invalid `SWAGGER_API_DOCS_BASE_URL` (not http/https, credentials, query, fragment)
fails at import.

**Changing the daily time:** edit `SWAGGER_SYNC_DAILY_TIME`, then run
`.venv\Scripts\python.exe -m src.swagger_sync update-task`. `status` compares the registered
task with the current settings and says when `update-task` is needed.

## Task Scheduler

`install-task` registers `AgentSwaggerSync` (`schtasks /Create /XML … /F`, replacing an existing
definition). `update-task` does the same but requires the task to exist already.

| Setting | Value | Why |
|---|---|---|
| Trigger 1 | daily at `SWAGGER_SYNC_DAILY_TIME`, first run = next future occurrence | the daily sync |
| Trigger 2 | at logon of the current user, 3-minute delay | catch up a run missed or interrupted by shutdown, restart or logoff |
| Action | `.venv\Scripts\pythonw.exe -m src.swagger_sync run --if-due`, working directory = repository root | no console window; `.env` is found. `--if-due` makes the logon trigger a no-op when today's run already succeeded |
| Principal | current user, `InteractiveToken` (only when logged on, no stored password), `LeastPrivilege` | |
| `StartWhenAvailable` | true | a run missed while the PC was off starts as soon as possible |
| `MultipleInstancesPolicy` | `IgnoreNew` | never two runs at once (plus the job's own run lock) |
| `ExecutionTimeLimit` | `PT0S` (no limit) by default; max runtime + 10 min if a runtime cap is set | the task never kills a job that is still retrying |
| Batteries | allowed to start and continue on battery | |

`--if-due` runs when there is no report yet, when the last run did not end `SUCCESS`
(including `IN_PROGRESS` left by a killed run), or when the last success finished before the
most recent daily time. Otherwise it logs "not due" and exits 0 without touching the report.

Paths containing spaces are supported. The `Command` is quoted and the working directory is
not, as Task Scheduler requires.

## Reporting

* `logs/swagger_sync/swagger_sync.log` (rotating, 1 MB × `SWAGGER_SYNC_LOG_BACKUPS`). It records
  start and finish, and one line per attempt: source, attempt number, duration, HTTP status (also
  for failed attempts), error kind with the core's fixed message, and the delay until the next
  retry. On success it adds the operation counts and sizes (previous and new), validation,
  shrink guard, comparison and update result.
* `last_run.json` (run) / `last_check.json` (`--check`) are written atomically after **every
  attempt** while the run is in progress (`result: IN_PROGRESS`), then once at the end. They
  hold:
  * `succeeded` / `pending` / `failed` key lists;
  * per source: outcome, start and finish times, attempts, `attempt_history` (number, start and
    finish, duration, HTTP status, error kind, `retry_in_s`; the last 50, plus
    `attempts_omitted`), `next_retry_at`;
  * per source, the previous and new size and operation count, `validation`, `shrink_guard`,
    `comparison` and `update`.
* A run refused because another run is active does not overwrite the reports. A run with
  `--if-due` that is not due does not overwrite them either. `--dry-run` writes nothing.
* Never response bodies, headers, tokens, credentials or `.env` values.

## Runbook

1. `scripts\swagger_sync.cmd --dry-run`: check sources, counts, the retry schedule, and that
   no cap warning appears.
2. `scripts\swagger_sync.cmd --check`: real GETs, no file changes.
3. `scripts\swagger_sync.cmd`: real run; review `git diff --stat data/swagger` and `last_run.json`.
4. `.venv\Scripts\python.exe -m src.swagger_sync install-task`, then `status`.

Rollback: `uninstall-task`; snapshots are tracked in Git (`git diff` / `git checkout -- <file>`
by the user).

## Limitations

* Snapshots stay tracked in Git, so updates appear in `git status` until committed.
* The default base URL is plain HTTP; integrity is structural validation only. `--probe-https`
  reports whether HTTPS works; switching is a configuration change.
* "Only when logged on": nothing runs while logged off; the logon trigger catches up.
* With unlimited retry, a source that never recovers keeps the run alive (one request per
  source every `SWAGGER_SYNC_MAX_RETRY_INTERVAL_SECONDS`). Because of `IgnoreNew`, the next day's
  trigger is then skipped while the same run keeps retrying. `status` and `last_run.json` show
  which source is pending and why.
* The shrink guard compares operation counts only, not content. A server that permanently
  serves far fewer operations needs a lower `SWAGGER_SYNC_MAX_SHRINK_RATIO_<KEY>`.
