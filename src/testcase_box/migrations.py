"""
src/testcase_box/migrations.py — schemaِ پایگاهِ داده‌ی TestCase Box

migrationها شماره‌دار و فقط‌رو‌به‌جلو‌اند؛ هر کدام فهرستی از دستورهاست که db.migrate
در یک تراکنشِ IMMEDIATE اجرا می‌کند. migrationِ منتشرشده هرگز ویرایش نمی‌شود — هر
تغییر یک migrationِ تازه است.

قاعده‌هایی که در خودِ پایگاهِ داده (نه فقط Python) اعمال می‌شوند:
  * نسخه‌ها، منشأها و تأییدهای اسکریپت پس از ساخت تغییر یا حذف نمی‌شوند؛
  * هیچ case، import job، run، unit یا run caseی حذف نمی‌شود (cascade وجود ندارد)؛
  * نسخه‌ی جاریِ case، نسخه‌ی سنجاق‌شده‌ی run case و منشأِ نسخه‌دار باید مالِ همان
    case باشند (foreign keyِ ترکیبی روی (id, case_id))؛
  * گذارهای وضعیتِ run/unit/run case/import job فقط همان‌هایی‌اند که در models
    تعریف شده‌اند و رکوردِ پایانی تغییر نمی‌کند؛
  * تأییدِ اسکریپت فقط برای اثرانگشتِ اسکریپتِ همان نسخه ثبت می‌شود؛
  * (v2) ردیفِ تازه فقط با وضعیتِ آغازین ساخته می‌شود؛ run تا همه‌ی unitها و run caseهایش
    صریحاً پایانی نشده‌اند پایانی نمی‌شود و فرزندانِ runِ پایانی تغییر یا افزایش نمی‌یابند؛
    ستون‌های تاریخیِ run و case ثابت‌اند؛
  * (v3) همین قاعده در سطحِ unit: unit تا همه‌ی run caseهایش پایانی نشده‌اند پایانی
    نمی‌شود و run caseهای unitِ پایانی تغییر یا افزایش نمی‌یابند.
  * (v4) مالکیتِ اجرای run: حداکثر یک runِ RUNNING و حداکثر یک runِ دارای lease؛ فراداده‌ی
    مالک کامل یا کاملاً خالی است؛ runِ پایانی lease ندارد؛ lease_generation (fencing) هرگز
    کم نمی‌شود و هر مالکِ تازه نسلِ بالاتری دارد. این مالکیتِ پایگاهِ Box است — جدا از
    Execution Gate ِ مشترکِ سیستم‌عامل و جدا از leaseِ Batch.

نوعِ منشأ (source_type) عمداً CHECKِ فهرستی ندارد تا نوعِ تازه بی‌migration اضافه شود.
"""

from __future__ import annotations

from src.testcase_box.models import (
    APPROVAL_APPROVED,
    APPROVAL_STATUSES,
    CASE_STATUSES,
    IMPORT_FINAL,
    IMPORT_INITIAL,
    IMPORT_PENDING,
    IMPORT_STATUSES,
    IMPORT_TRANSITIONS,
    LINK_TYPES,
    RUN_CASE_FINAL,
    RUN_CASE_INITIAL,
    RUN_CASE_STATUSES,
    RUN_ACTION_NONE,
    RUN_ACTIONS,
    RUN_CASE_TRANSITIONS,
    RUN_FINAL,
    RUN_INITIAL,
    RUN_RUNNING,
    RUN_STATUSES,
    RUN_TRANSITIONS,
    UNIT_FINAL,
    UNIT_INITIAL,
    UNIT_STATUSES,
    UNIT_TRANSITIONS,
)

# «TCBX» — پایگاهِ داده‌ی دیگری (مثلاً Batch) هرگز به‌جای Box پذیرفته نمی‌شود
APPLICATION_ID = 0x54434258


def _in(values) -> str:
    return "(" + ", ".join(f"'{value}'" for value in sorted(values)) + ")"


def _pairs(transitions: dict[str, frozenset[str]]) -> str:
    return _in(f"{old}>{new}" for old, targets in transitions.items() for new in targets)


def _sha256_check(column: str) -> str:
    return f"(length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*')"


def _json_check(column: str, kind: str) -> str:
    return f"(json_valid({column}) AND json_type({column}) = '{kind}')"


def _no_delete(table: str, what: str) -> str:
    return f"""
        CREATE TRIGGER tr_{table}_no_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, 'immutable: {what} cannot be deleted'); END
    """


def _no_update(table: str, what: str) -> str:
    return f"""
        CREATE TRIGGER tr_{table}_no_update BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, 'immutable: {what} cannot be changed'); END
    """


def _status_guards(table: str, what: str, final, transitions) -> list[str]:
    return [
        f"""
        CREATE TRIGGER tr_{table}_final_immutable BEFORE UPDATE ON {table}
        WHEN OLD.status IN {_in(final)}
        BEGIN SELECT RAISE(ABORT, 'immutable: a final {what} cannot be changed'); END
        """,
        f"""
        CREATE TRIGGER tr_{table}_transition BEFORE UPDATE OF status ON {table}
        WHEN NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN
            {_pairs(transitions)}
        BEGIN SELECT RAISE(ABORT, 'integrity: invalid {what} status transition'); END
        """,
        _no_delete(table, f"{what} history"),
    ]


def _initial_status(table: str, what: str, initial) -> str:
    return f"""
        CREATE TRIGGER tr_{table}_initial_status BEFORE INSERT ON {table}
        WHEN NEW.status NOT IN {_in(initial)}
        BEGIN SELECT RAISE(ABORT, 'integrity: invalid initial {what} status'); END
    """


# v4: «owner_token، owner_pid، owner_host و heartbeat_at همه پر یا همه خالی؛ lease → generation ≥ 1»
_OWNER_CONSISTENT = (
    "(({row}.owner_token IS NULL AND {row}.owner_pid IS NULL AND {row}.owner_host IS NULL "
    "AND {row}.heartbeat_at IS NULL) OR ({row}.owner_token IS NOT NULL AND "
    "{row}.owner_pid IS NOT NULL AND {row}.owner_host IS NOT NULL AND "
    "{row}.heartbeat_at IS NOT NULL AND {row}.lease_generation >= 1))"
)


def _frozen_by_final_run(table: str, what: str) -> list[str]:
    final = _in(RUN_FINAL)
    return [
        f"""
        CREATE TRIGGER tr_{table}_run_final_update BEFORE UPDATE ON {table}
        WHEN (SELECT status FROM box_runs WHERE id = OLD.run_id) IN {final}
        BEGIN SELECT RAISE(ABORT, 'immutable: the run is final; its {what} cannot change'); END
        """,
        f"""
        CREATE TRIGGER tr_{table}_run_final_insert BEFORE INSERT ON {table}
        WHEN (SELECT status FROM box_runs WHERE id = NEW.run_id) IN {final}
        BEGIN SELECT RAISE(ABORT, 'immutable: the run is final; no {what} can be added'); END
        """,
    ]


MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "library and run tables",
        [
            f"PRAGMA application_id = {APPLICATION_ID}",
            # ── library ──────────────────────────────────────────────────────
            f"""
            CREATE TABLE box_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN {_in(CASE_STATUSES)}),
                current_version_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (current_version_id, id)
                    REFERENCES box_case_versions (id, case_id)
            )
            """,
            f"""
            CREATE TABLE box_case_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES box_cases (id),
                version_number INTEGER NOT NULL CHECK (version_number >= 1),
                payload_json TEXT NOT NULL CHECK {_json_check("payload_json", "object")},
                fingerprint TEXT NOT NULL CHECK {_sha256_check("fingerprint")},
                script_fingerprint TEXT
                    CHECK (script_fingerprint IS NULL OR {_sha256_check("script_fingerprint")}),
                created_at TEXT NOT NULL,
                created_by TEXT,
                UNIQUE (case_id, version_number),
                UNIQUE (id, case_id)
            )
            """,
            f"""
            CREATE TABLE box_case_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES box_cases (id),
                case_version_id INTEGER,
                source_type TEXT NOT NULL CHECK (length(trim(source_type)) > 0),
                source_reference TEXT NOT NULL CHECK (length(trim(source_reference)) > 0),
                source_metadata_json TEXT NOT NULL
                    CHECK {_json_check("source_metadata_json", "object")},
                created_at TEXT NOT NULL,
                UNIQUE (case_id, source_type, source_reference),
                FOREIGN KEY (case_version_id, case_id) REFERENCES box_case_versions (id, case_id)
            )
            """,
            f"""
            CREATE TABLE box_case_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_case_id INTEGER NOT NULL REFERENCES box_cases (id),
                target_case_id INTEGER NOT NULL REFERENCES box_cases (id),
                link_type TEXT NOT NULL CHECK (link_type IN {_in(LINK_TYPES)}),
                metadata_json TEXT NOT NULL CHECK {_json_check("metadata_json", "object")},
                created_at TEXT NOT NULL,
                CHECK (source_case_id <> target_case_id),
                UNIQUE (source_case_id, target_case_id, link_type)
            )
            """,
            f"""
            CREATE TABLE box_import_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL CHECK (length(trim(source_type)) > 0),
                filename TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN {_in(IMPORT_STATUSES)}),
                summary_json TEXT NOT NULL CHECK {_json_check("summary_json", "object")},
                created_at TEXT NOT NULL,
                completed_at TEXT,
                CHECK ((status = '{IMPORT_PENDING}') = (completed_at IS NULL))
            )
            """,
            f"""
            CREATE TABLE box_script_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_version_id INTEGER NOT NULL REFERENCES box_case_versions (id),
                script_fingerprint TEXT NOT NULL CHECK {_sha256_check("script_fingerprint")},
                approval_status TEXT NOT NULL CHECK (approval_status IN {_in(APPROVAL_STATUSES)}),
                review_findings_json TEXT NOT NULL
                    CHECK {_json_check("review_findings_json", "array")},
                approved_at TEXT,
                created_at TEXT NOT NULL,
                CHECK ((approval_status = '{APPROVAL_APPROVED}') = (approved_at IS NOT NULL))
            )
            """,
            # ── runs ─────────────────────────────────────────────────────────
            f"""
            CREATE TABLE box_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL CHECK (status IN {_in(RUN_STATUSES)}),
                retry_of_run_id INTEGER REFERENCES box_runs (id),
                metadata_json TEXT NOT NULL CHECK {_json_check("metadata_json", "object")},
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """,
            f"""
            CREATE TABLE box_run_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES box_runs (id),
                unit_number INTEGER NOT NULL CHECK (unit_number >= 1),
                status TEXT NOT NULL CHECK (status IN {_in(UNIT_STATUSES)}),
                started_at TEXT,
                finished_at TEXT,
                metadata_json TEXT NOT NULL CHECK {_json_check("metadata_json", "object")},
                UNIQUE (run_id, unit_number),
                UNIQUE (id, run_id)
            )
            """,
            f"""
            CREATE TABLE box_run_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES box_runs (id),
                run_unit_id INTEGER NOT NULL,
                case_id INTEGER NOT NULL REFERENCES box_cases (id),
                case_version_id INTEGER NOT NULL,
                execution_order INTEGER NOT NULL CHECK (execution_order >= 1),
                status TEXT NOT NULL CHECK (status IN {_in(RUN_CASE_STATUSES)}),
                result_metadata_json TEXT NOT NULL
                    CHECK {_json_check("result_metadata_json", "object")},
                UNIQUE (run_id, case_id),
                UNIQUE (run_unit_id, execution_order),
                FOREIGN KEY (run_unit_id, run_id) REFERENCES box_run_units (id, run_id),
                FOREIGN KEY (case_version_id, case_id) REFERENCES box_case_versions (id, case_id)
            )
            """,
            # ── indexes ──────────────────────────────────────────────────────
            "CREATE INDEX ix_box_cases_status ON box_cases (status, updated_at)",
            "CREATE INDEX ix_box_cases_current_version ON box_cases (current_version_id)",
            "CREATE INDEX ix_box_case_versions_fingerprint ON box_case_versions (fingerprint)",
            "CREATE INDEX ix_box_case_sources_reference "
            "ON box_case_sources (source_type, source_reference)",
            "CREATE INDEX ix_box_case_sources_version ON box_case_sources (case_version_id)",
            "CREATE INDEX ix_box_case_links_target ON box_case_links (target_case_id)",
            "CREATE INDEX ix_box_import_jobs_status ON box_import_jobs (status)",
            "CREATE INDEX ix_box_script_approvals_version "
            "ON box_script_approvals (case_version_id, script_fingerprint)",
            "CREATE INDEX ix_box_runs_status ON box_runs (status)",
            "CREATE INDEX ix_box_runs_retry_of ON box_runs (retry_of_run_id)",
            "CREATE INDEX ix_box_run_cases_case ON box_run_cases (case_id)",
            "CREATE INDEX ix_box_run_cases_version ON box_run_cases (case_version_id)",
            # ── triggers: library ────────────────────────────────────────────
            _no_delete("box_cases", "a test case (archive it instead)"),
            """
            CREATE TRIGGER tr_box_cases_current_version_required
            BEFORE UPDATE OF current_version_id ON box_cases
            WHEN NEW.current_version_id IS NULL AND OLD.current_version_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'integrity: a test case must keep a current version'); END
            """,
            _no_update("box_case_versions", "a test case version"),
            _no_delete("box_case_versions", "a test case version"),
            _no_update("box_case_sources", "a provenance record"),
            _no_delete("box_case_sources", "a provenance record"),
            *_status_guards("box_import_jobs", "import job", IMPORT_FINAL, IMPORT_TRANSITIONS),
            """
            CREATE TRIGGER tr_box_script_approvals_fingerprint
            BEFORE INSERT ON box_script_approvals
            WHEN NOT EXISTS (
                SELECT 1 FROM box_case_versions
                WHERE id = NEW.case_version_id AND script_fingerprint = NEW.script_fingerprint
            )
            BEGIN SELECT RAISE(ABORT,
                'integrity: the approval fingerprint does not match the version scripts'); END
            """,
            _no_update("box_script_approvals", "a script approval decision"),
            _no_delete("box_script_approvals", "a script approval decision"),
            # ── triggers: runs ───────────────────────────────────────────────
            *_status_guards("box_runs", "run", RUN_FINAL, RUN_TRANSITIONS),
            *_status_guards("box_run_units", "run unit", UNIT_FINAL, UNIT_TRANSITIONS),
            """
            CREATE TRIGGER tr_box_run_units_pinned BEFORE UPDATE OF run_id, unit_number
            ON box_run_units
            WHEN NEW.run_id IS NOT OLD.run_id OR NEW.unit_number IS NOT OLD.unit_number
            BEGIN SELECT RAISE(ABORT, 'immutable: a run unit cannot move'); END
            """,
            *_status_guards("box_run_cases", "run case", RUN_CASE_FINAL, RUN_CASE_TRANSITIONS),
            """
            CREATE TRIGGER tr_box_run_cases_pinned
            BEFORE UPDATE OF run_id, run_unit_id, case_id, case_version_id, execution_order
            ON box_run_cases
            WHEN NEW.run_id IS NOT OLD.run_id OR NEW.run_unit_id IS NOT OLD.run_unit_id
                OR NEW.case_id IS NOT OLD.case_id OR NEW.case_version_id IS NOT OLD.case_version_id
                OR NEW.execution_order IS NOT OLD.execution_order
            BEGIN SELECT RAISE(ABORT, 'immutable: a pinned run case cannot change'); END
            """,
        ],
    ),
    (
        2,
        "integrity hardening",
        # فقط trigger — هیچ جدول، ستون یا ردیفی تغییر نمی‌کند؛ ردیف‌های موجودِ v1 دست‌نخورده
        # می‌مانند و قاعده‌های تازه فقط بر نوشتن‌های بعدی اعمال می‌شوند.
        [
            # وضعیتِ آغازین: ردیفِ تازه فقط با وضعیتی ساخته می‌شود که models مجاز می‌داند
            _initial_status("box_runs", "run", RUN_INITIAL),
            _initial_status("box_run_units", "run unit", UNIT_INITIAL),
            _initial_status("box_run_cases", "run case", RUN_CASE_INITIAL),
            _initial_status("box_import_jobs", "import job", IMPORT_INITIAL),
            # run فقط وقتی پایانی می‌شود که همه‌ی unitها و run caseهایش صریحاً پایانی شده باشند
            f"""
            CREATE TRIGGER tr_box_runs_children_final BEFORE UPDATE OF status ON box_runs
            WHEN NEW.status <> OLD.status AND NEW.status IN {_in(RUN_FINAL)} AND (
                EXISTS (SELECT 1 FROM box_run_units
                        WHERE run_id = NEW.id AND status NOT IN {_in(UNIT_FINAL)})
                OR EXISTS (SELECT 1 FROM box_run_cases
                           WHERE run_id = NEW.id AND status NOT IN {_in(RUN_CASE_FINAL)})
            )
            BEGIN SELECT RAISE(ABORT,
                'integrity: a run cannot finish while its units or cases are unfinished'); END
            """,
            # فرزندانِ runِ پایانی نه تغییر می‌کنند و نه اضافه می‌شوند
            *_frozen_by_final_run("box_run_units", "units"),
            *_frozen_by_final_run("box_run_cases", "run cases"),
            # ستون‌های تاریخی
            """
            CREATE TRIGGER tr_box_runs_history BEFORE UPDATE OF id, created_at, retry_of_run_id
            ON box_runs
            WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
                OR NEW.retry_of_run_id IS NOT OLD.retry_of_run_id
            BEGIN SELECT RAISE(ABORT, 'immutable: run history columns cannot change'); END
            """,
            """
            CREATE TRIGGER tr_box_cases_history BEFORE UPDATE OF id, created_at ON box_cases
            WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
            BEGIN SELECT RAISE(ABORT, 'immutable: case history columns cannot change'); END
            """,
        ],
    ),
    (
        3,
        "unit finalization integrity",
        # فقط trigger — ردیف‌های موجود دست‌نخورده می‌مانند (unitِ پایانیِ قدیمی با caseِ باز
        # همان‌طور منجمد می‌شود و هیچ وضعیتی بازنویسی نمی‌شود).
        [
            # unit فقط وقتی پایانی می‌شود که همه‌ی run caseهایش صریحاً پایانی شده باشند
            f"""
            CREATE TRIGGER tr_box_run_units_children_final BEFORE UPDATE OF status ON box_run_units
            WHEN NEW.status <> OLD.status AND NEW.status IN {_in(UNIT_FINAL)} AND EXISTS (
                SELECT 1 FROM box_run_cases
                WHERE run_unit_id = NEW.id AND status NOT IN {_in(RUN_CASE_FINAL)}
            )
            BEGIN SELECT RAISE(ABORT,
                'integrity: a run unit cannot finish while its cases are unfinished'); END
            """,
            # run caseهای unitِ پایانی نه تغییر می‌کنند و نه اضافه می‌شوند
            f"""
            CREATE TRIGGER tr_box_run_cases_unit_final_update BEFORE UPDATE ON box_run_cases
            WHEN (SELECT status FROM box_run_units WHERE id = OLD.run_unit_id)
                IN {_in(UNIT_FINAL)}
            BEGIN SELECT RAISE(ABORT,
                'immutable: the unit is final; its run cases cannot change'); END
            """,
            f"""
            CREATE TRIGGER tr_box_run_cases_unit_final_insert BEFORE INSERT ON box_run_cases
            WHEN (SELECT status FROM box_run_units WHERE id = NEW.run_unit_id)
                IN {_in(UNIT_FINAL)}
            BEGIN SELECT RAISE(ABORT,
                'immutable: the unit is final; no run cases can be added'); END
            """,
        ],
    ),
    (
        4,
        "run execution ownership",
        # فقط ستون (با مقدارِ پیش‌فرضِ ثابت)، ایندکس و trigger — هیچ جدولی بازسازی نمی‌شود و
        # ردیف‌های موجود همان می‌مانند (ستون‌های تازه NULL / 0 / 'NONE'). پایگاهی که قیدهای
        # تازه را نقض کند (مثلاً دو runِ RUNNING) migrate نمی‌شود و کلِ تراکنش rollback می‌شود.
        [
            "ALTER TABLE box_runs ADD COLUMN owner_token TEXT CHECK (owner_token IS NULL OR "
            "(length(owner_token) = 32 AND owner_token NOT GLOB '*[^0-9a-f]*'))",
            "ALTER TABLE box_runs ADD COLUMN owner_pid INTEGER "
            "CHECK (owner_pid IS NULL OR owner_pid > 0)",
            "ALTER TABLE box_runs ADD COLUMN owner_host TEXT "
            "CHECK (owner_host IS NULL OR length(trim(owner_host)) > 0)",
            "ALTER TABLE box_runs ADD COLUMN heartbeat_at TEXT",
            # شمارنده‌ی fencing: هر مالکِ تازه (claim یا takeover) یکی بالاتر
            "ALTER TABLE box_runs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0 "
            "CHECK (lease_generation >= 0)",
            f"ALTER TABLE box_runs ADD COLUMN requested_action TEXT NOT NULL DEFAULT "
            f"'{RUN_ACTION_NONE}' CHECK (requested_action IN {_in(RUN_ACTIONS)})",
            # حداکثر یک runِ RUNNING و حداکثر یک runِ دارای lease در کلِ پایگاه
            f"CREATE UNIQUE INDEX ux_box_runs_single_running ON box_runs ((1)) "
            f"WHERE status = '{RUN_RUNNING}'",
            "CREATE UNIQUE INDEX ux_box_runs_single_lease ON box_runs ((1)) "
            "WHERE owner_token IS NOT NULL",
            # فرادادهی مالک کامل است یا کاملاً خالی؛ lease یعنی generation ≥ 1
            f"""
            CREATE TRIGGER tr_box_runs_owner_complete_insert BEFORE INSERT ON box_runs
            WHEN NOT {_OWNER_CONSISTENT.format(row="NEW")}
            BEGIN SELECT RAISE(ABORT, 'integrity: run owner metadata must be complete or empty');
            END
            """,
            f"""
            CREATE TRIGGER tr_box_runs_owner_complete_update
            BEFORE UPDATE OF owner_token, owner_pid, owner_host, heartbeat_at, lease_generation
            ON box_runs
            WHEN NOT {_OWNER_CONSISTENT.format(row="NEW")}
            BEGIN SELECT RAISE(ABORT, 'integrity: run owner metadata must be complete or empty');
            END
            """,
            # runِ پایانی lease نگه نمی‌دارد (trigger ِ final_immutable آن را منجمد می‌کرد)
            f"""
            CREATE TRIGGER tr_box_runs_final_without_lease
            BEFORE UPDATE OF status, owner_token ON box_runs
            WHEN NEW.status IN {_in(RUN_FINAL)} AND NEW.owner_token IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'integrity: a final run cannot keep its execution lease');
            END
            """,
            """
            CREATE TRIGGER tr_box_runs_lease_generation BEFORE UPDATE OF lease_generation
            ON box_runs WHEN NEW.lease_generation < OLD.lease_generation
            BEGIN SELECT RAISE(ABORT, 'integrity: the run lease generation never decreases'); END
            """,
            # مالکِ تازه همیشه با generation ِ بالاتر — توکنِ کهنه هرگز با نسلِ تازه نمی‌خواند
            """
            CREATE TRIGGER tr_box_runs_new_owner_generation BEFORE UPDATE OF owner_token
            ON box_runs
            WHEN NEW.owner_token IS NOT NULL AND NEW.owner_token IS NOT OLD.owner_token
                AND NEW.lease_generation <= OLD.lease_generation
            BEGIN SELECT RAISE(ABORT, 'integrity: a new run owner needs a new lease generation');
            END
            """,
        ],
    ),
]
LATEST_VERSION = MIGRATIONS[-1][0]
