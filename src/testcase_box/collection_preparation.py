"""
src/testcase_box/collection_preparation.py — ساختِ کالکشنِ Newman از برنامه‌ی run (M7.4)

    prepared = prepare_collections(plan)
    prepared.units      # یک PreparedUnit برای هر caseِ واجد — هر کدام کالکشنی با دقیقاً یک item
    prepared.blocked    # ورودی‌های مسدود (بی‌کالکشن) — runner آن‌ها را NOT_EXECUTABLE ثبت می‌کند

فقط ساختن و اعتبارسنجی؛ هیچ فایلی نوشته، هیچ Newman یا درخواستی اجرا و هیچ Gate ای گرفته
نمی‌شود (همه مالِ runnerِ M7.5). D2: هر case دقیقاً یک unit و هر unit دقیقاً یک کالکشن است، پس
هیچ متغیر یا وضعیتی از caseی به caseِ دیگر نمی‌رسد.

قالب: Postman Collection v2.1 — همان schema ِ postman_builder (کپی شده، نه import؛ تست
هم‌خوانی را بررسی می‌کند)، و همان شکلی که preflight ِ newman می‌پذیرد:

    {"info": {"name": "TestCase Box BX-000007 v3", "schema": <v2.1>},
     "item": [<payload ِ آماده‌شده‌ی M4 با name = "BX-000007 v3">]}

item همان payload ِ آماده‌شده‌ی M4 است — فقط name با نامِ خنثیِ case و نسخه جایگزین می‌شود
(عنوانِ case در کالکشن نمی‌آید). هیچ event، auth یا variable در سطحِ کالکشن، هیچ folder و هیچ
تبدیلِ دیگری نیست. پیش از تحویل، validate_unit_collection بررسی می‌کند:
  * شکلِ دقیقِ سطحِ بالا (فقط info و item؛ info فقط name و schema)؛
  * دقیقاً یک item، بی‌folder، و برابر با payload ِ آماده‌شده (به‌جز name) — پس هیچ چیزی تزریق نشده؛
  * اثرانگشتِ payload ِ آماده‌شده با برنامه می‌خواند؛
  * هیچ اسکریپتی (payload_scripts) و هیچ کلیدِ eventی جز null در هیچ عمقی ([] هم نه)؛
  * قراردادِ درخواستِ M3 (inspect_payload) هیچ خطای مسدودکننده‌ای ندارد.
هر نقض → CollectionPreparationError (fail-closed). توکن یا مقدارِ متغیری در کالکشن نیست:
Authorization فقط ارجاعِ {{token}} است و runtime ِ newman آن را تزریق می‌کند.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.testcase_box.models import from_json, payload_scripts, sha256_json, to_json
from src.testcase_box.payload import inspect_payload
from src.testcase_box.run_planning import PlanEntry, RunPlan, script_free_findings
from src.testcase_box.run_profile import RunProfile

# هم‌خوان با postman_builder.SCHEMA_V21 (تست)
COLLECTION_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


class CollectionPreparationError(ValueError):
    """کالکشن ساخته یا پذیرفته نشد. code پایدار است؛ پیام هیچ محتوای payload را ندارد."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedUnit:
    unit_number: int
    case_id: int
    case_key: str
    case_version_id: int
    version_number: int
    prepared_fingerprint: str
    collection_fingerprint: str
    collection: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class PreparedRun:
    plan_fingerprint: str
    profile: RunProfile
    units: tuple[PreparedUnit, ...]
    blocked: tuple[PlanEntry, ...]

    @property
    def executable(self) -> bool:
        return bool(self.units)


def _item_name(entry: PlanEntry) -> str:
    return f"{entry.case_key} v{entry.version_number}"


def build_unit_collection(entry: PlanEntry) -> dict[str, Any]:
    """کالکشنِ تک‌itemِ یک ورودیِ واجد — اعتبارسنجی‌شده، پیش از هر تحویل."""
    if not entry.eligible or entry.prepared_payload is None:
        raise CollectionPreparationError(
            "ENTRY_NOT_ELIGIBLE", f"Unit {entry.position} is not eligible for execution."
        )
    payload = from_json(to_json(entry.prepared_payload))  # نسخه‌ی مستقل
    item = {**payload, "name": _item_name(entry)}
    collection = {
        "info": {
            "name": f"TestCase Box {entry.case_key} v{entry.version_number}",
            "schema": COLLECTION_SCHEMA,
        },
        "item": [item],
    }
    validate_unit_collection(collection, entry)
    return collection


def _without_name(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "name"}


def validate_unit_collection(collection: Any, entry: PlanEntry) -> None:
    """کالکشن دقیقاً همان چیزی است که برنامه اجازه می‌دهد — وگرنه CollectionPreparationError."""

    def reject(code: str, message: str) -> None:
        raise CollectionPreparationError(code, f"Unit {entry.position}: {message}")

    if entry.prepared_payload is None or entry.prepared_fingerprint is None:
        reject("ENTRY_NOT_ELIGIBLE", "the plan entry has no prepared payload.")
    if sha256_json(entry.prepared_payload) != entry.prepared_fingerprint:
        reject("PREPARED_PAYLOAD_CHANGED", "the prepared payload does not match the plan.")
    if not isinstance(collection, dict) or set(collection) != {"info", "item"}:
        reject("COLLECTION_SHAPE", "a collection has exactly 'info' and 'item'.")
    info = collection["info"]
    if not isinstance(info, dict) or set(info) != {"name", "schema"}:
        reject("COLLECTION_SHAPE", "'info' has exactly 'name' and 'schema'.")
    if info["schema"] != COLLECTION_SCHEMA:
        reject("COLLECTION_SCHEMA", "the collection is not Postman Collection v2.1.")
    items = collection["item"]
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        reject("COLLECTION_UNITS", "a unit collection holds exactly one test case.")
    item = items[0]
    if "item" in item:
        reject("COLLECTION_UNITS", "folders are not allowed.")
    if payload_scripts(collection):
        reject("COLLECTION_SCRIPTS", "the collection contains scripts.")
    if script_free_findings(item, None):
        reject("COLLECTION_SCRIPTS", "the item contains script events.")
    if _without_name(item) != _without_name(entry.prepared_payload):
        reject("COLLECTION_ITEM_CHANGED", "the item differs from the prepared payload.")
    if item.get("name") != _item_name(entry):
        reject("COLLECTION_ITEM_CHANGED", "the item name does not identify the pinned version.")
    if any(f.blocking for f in inspect_payload(item).findings):
        reject("COLLECTION_REQUEST_INVALID", "the request does not satisfy the item contract.")
    try:
        to_json(collection)
    except ValueError:
        reject("COLLECTION_NOT_JSON", "the collection is not serializable JSON.")


def prepare_collections(plan: RunPlan) -> PreparedRun:
    """یک کالکشن برای هر ورودیِ واجد، به ترتیبِ unit؛ ورودی‌های مسدود جدا و بی‌کالکشن."""
    if not isinstance(plan, RunPlan):
        raise TypeError("plan must be a RunPlan.")
    units = []
    for entry in plan.eligible_entries:
        collection = build_unit_collection(entry)
        units.append(
            PreparedUnit(
                unit_number=entry.unit_number,
                case_id=entry.case_id,
                case_key=entry.case_key,
                case_version_id=entry.version_id,
                version_number=entry.version_number,
                prepared_fingerprint=entry.prepared_fingerprint,
                collection_fingerprint=sha256_json(collection),
                collection=collection,
            )
        )
    return PreparedRun(plan.fingerprint, plan.profile, tuple(units), plan.blocked_entries)


__all__ = [
    "COLLECTION_SCHEMA",
    "CollectionPreparationError",
    "PreparedRun",
    "PreparedUnit",
    "build_unit_collection",
    "prepare_collections",
    "validate_unit_collection",
]
