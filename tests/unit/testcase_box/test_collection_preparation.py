"""
tests/unit/testcase_box/test_collection_preparation.py — ساختِ کالکشنِ Newman (M7.4)

فقط ساختن و اعتبارسنجی؛ هیچ Newman، Node، فایلِ اجرایی یا شبکه‌ای درگیر نیست.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from src.agents.test_case_generator.postman_builder import SCHEMA_V21
from src.testcase_box import models as m
from src.testcase_box.collection_preparation import (
    COLLECTION_SCHEMA,
    CollectionPreparationError,
    build_unit_collection,
    prepare_collections,
    validate_unit_collection,
)
from src.testcase_box.models import NewCase, NewRun, NewRunCase, NewRunUnit, NewVersion
from src.testcase_box.run_planning import plan_new_run, plan_rerun
from src.testcase_box.storage import BoxStore
from tests.unit.testcase_box.test_run_planning import PROFILE, VARIABLES, event, item


@pytest.fixture
def store(tmp_path):
    with BoxStore.open(tmp_path / "box.sqlite3") as opened:
        yield opened


def _plan(store, payloads):
    cases = [store.create_case(NewCase(title=f"C{n}", payload=p)) for n, p in enumerate(payloads)]
    return cases, plan_new_run(store, [c.id for c in cases], profile=PROFILE, variables=VARIABLES)


def _code(call) -> str:
    with pytest.raises(CollectionPreparationError) as caught:
        call()
    return caught.value.code


def test_the_schema_matches_the_postman_builder():
    assert COLLECTION_SCHEMA == SCHEMA_V21


def test_a_valid_script_free_collection(store):
    (case,), plan = _plan(store, [item()])
    prepared = prepare_collections(plan)
    (unit,) = prepared.units
    collection = unit.collection
    assert set(collection) == {"info", "item"}
    assert collection["info"] == {
        "name": f"TestCase Box {m.case_key(case.id)} v1",
        "schema": SCHEMA_V21,
    }
    (only,) = collection["item"]
    assert only["name"] == f"{m.case_key(case.id)} v1"
    assert {k: v for k, v in only.items() if k != "name"} == {
        k: v for k, v in plan.entries[0].prepared_payload.items() if k != "name"
    }
    assert only["request"]["header"] == [{"key": "Authorization", "value": "{{token}}"}]
    assert m.payload_scripts(collection) == [] and "event" not in json.dumps(collection)
    assert prepared.plan_fingerprint == plan.fingerprint and prepared.executable


def test_one_test_case_per_unit_with_pinned_versions(store):
    cases, plan = _plan(store, [item(path="a"), item(event()), item(path="c")])
    prepared = prepare_collections(plan)
    assert [u.unit_number for u in prepared.units] == [1, 3]
    assert [b.position for b in prepared.blocked] == [2]
    for unit, case in zip(prepared.units, [cases[0], cases[2]], strict=True):
        assert len(unit.collection["item"]) == 1
        assert unit.case_id == case.id and unit.case_version_id == case.current_version_id
        assert unit.collection_fingerprint == m.sha256_json(unit.collection)


def test_a_rerun_collection_keeps_the_pinned_version(store):
    case = store.create_case(NewCase(title="A", payload=item(path="old")))
    run = store.create_run(
        NewRun(units=(NewRunUnit(1, (NewRunCase(case.id, case.current_version_id, 1),)),))
    )
    store.set_run_case_status(store.run_cases(run.id)[0].id, m.CASE_RUN_CANCELLED)
    store.set_unit_status(store.run_units(run.id)[0].id, m.UNIT_CANCELLED)
    store.set_run_status(run.id, m.RUN_CANCELLED)
    store.add_version(case.id, NewVersion(payload=item(path="new")))
    (unit,) = prepare_collections(
        plan_rerun(store, run.id, profile=PROFILE, variables=VARIABLES)
    ).units
    assert unit.version_number == 1 and unit.collection["item"][0]["name"].endswith(" v1")
    assert "/api/old/" in unit.collection["item"][0]["request"]["url"]["raw"]


def test_a_fully_blocked_plan_yields_no_collections(store):
    _, plan = _plan(store, [item(event())])
    prepared = prepare_collections(plan)
    assert prepared.units == () and len(prepared.blocked) == 1 and not prepared.executable


def test_serialization_is_deterministic(store):
    _, plan = _plan(store, [item(), item(path="b")])
    first, second = prepare_collections(plan), prepare_collections(plan)
    assert [m.to_json(u.collection) for u in first.units] == [
        m.to_json(u.collection) for u in second.units
    ]
    assert [u.collection_fingerprint for u in first.units] == [
        u.collection_fingerprint for u in second.units
    ]


def test_blocked_entries_cannot_be_built(store):
    _, plan = _plan(store, [item(event())])
    assert _code(lambda: build_unit_collection(plan.entries[0])) == "ENTRY_NOT_ELIGIBLE"


class TestInjectionIsRejected:
    @pytest.fixture
    def entry(self, store):
        _, plan = _plan(store, [item()])
        return plan.entries[0]

    def test_a_tampered_prepared_payload_is_rejected(self, entry):
        tampered = dataclasses.replace(entry, prepared_payload=item(event()))
        assert _code(lambda: build_unit_collection(tampered)) == "PREPARED_PAYLOAD_CHANGED"

    def test_a_consistently_fingerprinted_script_payload_is_still_rejected(self, entry):
        scripted = item(event())
        forged = dataclasses.replace(
            entry, prepared_payload=scripted, prepared_fingerprint=m.sha256_json(scripted)
        )
        assert _code(lambda: build_unit_collection(forged)) == "COLLECTION_SCRIPTS"

    def test_an_empty_event_array_is_rejected_by_collection_validation(self, entry):
        with_empty = item(event=[])
        forged = dataclasses.replace(
            entry, prepared_payload=with_empty, prepared_fingerprint=m.sha256_json(with_empty)
        )
        assert _code(lambda: build_unit_collection(forged)) == "COLLECTION_SCRIPTS"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda c: c.update(event=[event()]),  # اسکریپتِ سطحِ کالکشن
            lambda c: c.update(variable=[{"key": "base_url", "value": "http://evil"}]),
            lambda c: c.update(auth={"type": "bearer"}),
            lambda c: c["info"].update(description="x"),
            lambda c: c["item"][0].update(event=[event(listen="prerequest")]),
            lambda c: c["item"][0].update(event=[]),  # آرایه‌ی خالی هم پذیرفته نمی‌شود
            lambda c: c["item"].append(dict(c["item"][0])),  # دو case در یک unit
            lambda c: c["item"][0].update(item=[]),  # folder
            lambda c: c["item"][0]["request"]["header"].append({"key": "X-Injected", "value": "1"}),
            lambda c: c["item"][0].update(name="Something else"),
            lambda c: c["info"].update(
                schema="https://schema.getpostman.com/json/collection/v2.0.0/"
            ),
        ],
    )
    def test_any_change_to_a_built_collection_is_rejected(self, entry, mutate):
        collection = build_unit_collection(entry)
        mutate(collection)
        with pytest.raises(CollectionPreparationError):
            validate_unit_collection(collection, entry)

    def test_an_invalid_request_structure_is_rejected(self, entry):
        broken = {"name": "x", "request": {"method": "GET"}}
        forged = dataclasses.replace(
            entry, prepared_payload=broken, prepared_fingerprint=m.sha256_json(broken)
        )
        assert _code(lambda: build_unit_collection(forged)) == "COLLECTION_REQUEST_INVALID"

    def test_the_source_test_case_is_not_modified(self, store, entry):
        before = store.get_version(entry.version_id)
        build_unit_collection(entry)
        assert store.get_version(entry.version_id) == before
        assert entry.prepared_payload["name"] == "TC"  # کپیِ مستقل؛ ورودیِ برنامه دست‌نخورده
