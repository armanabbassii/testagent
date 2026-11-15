"""
مرحله‌ی 3d — درخواست‌های ساختاریافته‌ی Postman به‌عنوانِ شاهدِ کمکیِ قدم ۲

زنجیره‌ی واقعی: process_attachment → TaskInput → sources_from_task_input →
select_sources (SourceSelection.postman) → Step2ApiMappingGenerator → پرامپتِ نگاشت.

شاهد از raw_urlِ امن ساخته می‌شود: میزبانِ لفظی دقیقاً همان‌طور که نوشته شده،
میزبانِ متغیر بدونِ حل، مسیرِ نسبی بدونِ URLِ مطلق. انتخابِ منبعِ Swagger، شواهد،
کلیدها و قواعدِ URLِ صریح هیچ تغییری نمی‌کنند.

همه‌ی fixtureها ساختگی‌اند؛ LLMClient و کشفِ Swagger ماک‌اند و شبکه‌ای در کار نیست.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.agents.test_case_generator.api_mapping import (
    ApiMappingAgent,
    Step2ApiMappingGenerator,
    build_prompt,
    render_prompt,
    split_prompt,
)
from src.agents.test_case_generator.postman_evidence import (
    HEADER,
    HOST_LITERAL,
    HOST_NONE,
    HOST_VARIABLE,
    MAX_PROMPT_CHARS,
    MAX_PROMPT_REQUESTS,
    PostmanEvidence,
    collect_postman_evidence,
    render_postman_evidence,
)
from src.agents.test_case_generator.source_selection import select_sources
from src.agents.test_case_generator.swagger_snapshots import SELECTION_ADMIN
from src.task_sources import TaskAttachment, TaskComment, TaskInput
from src.task_sources.attachments import process_attachment
from src.task_sources.sources import (
    SOURCE_ATTACHMENT,
    TaskSource,
    sources_for_task,
    sources_from_task_input,
    sources_from_text,
)

STEP2_PROMPT = (
    Path(__file__).parents[3]
    / "src"
    / "agents"
    / "test_case_generator"
    / "prompts"
    / "step2_api_mapping.md"
)
V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
V20 = "https://schema.getpostman.com/json/collection/v2.0.0/collection.json"
DEFAULT_HOST = "default-sentinel-host.test"
SECRET = "S3CR3T-SENTINEL-VALUE"
SENTINELS = (
    SECRET,
    "BODY-SENTINEL-LEAK",
    "HEADER-SENTINEL-LEAK",
    "SCRIPT-SENTINEL-LEAK",
    "EXAMPLE-SENTINEL-LEAK",
    "QUERY-SENTINEL-LEAK",
    "PATHVAR-SENTINEL-42",
)
_MAPPING_LLM = "src.agents.test_case_generator.api_mapping.LLMClient"
_DISCOVER = "src.agents.test_case_generator.api_mapping.discover_services"


# ── fixtureها ────────────────────────────────────────────────────────────────


def _request(name: str, method: str | None, url, **request) -> dict:
    body = {"url": url, **request}
    if method:
        body["method"] = method
    return {
        "name": name,
        "request": body,
        "event": [{"listen": "test", "script": {"exec": ["pm.test('SCRIPT-SENTINEL-LEAK')"]}}],
        "response": [{"name": "ok", "body": "EXAMPLE-SENTINEL-LEAK"}],
    }


def _rich_items() -> list[dict]:
    return [
        _request(
            "Literal host",
            "POST",
            {
                "raw": "https://API.Test:8443/admin/voucher",
                "host": ["API", "Test"],
                "port": "8443",
                "path": ["admin", "voucher"],
            },
            header=[{"key": "Authorization", "value": "Bearer HEADER-SENTINEL-LEAK"}],
            body={"mode": "raw", "raw": '{"password": "BODY-SENTINEL-LEAK"}'},
            auth={"type": "bearer", "bearer": [{"key": "token", "value": SECRET}]},
        ),
        _request(
            "Base url", "GET", "{{base_url}}/admin/voucher/:id?lang=fa&token=QUERY-SENTINEL-LEAK"
        ),
        _request("Base url vdi", "PUT", "{{base_url_vdi}}/admin/voucher/{{voucherId}}"),
        _request("Relative", None, "/admin/voucher/list"),
        _request("Versioned", "GET", "https://api.test/{{ver}}/items"),
        _request("Secret segment", "GET", "https://api.test/{{segment}}/x"),
    ]


def _collection(items=None, schema: str = V21, name: str = "Voucher") -> str:
    return json.dumps(
        {
            "info": {"name": name, "schema": schema},
            "item": [{"name": "Voucher", "item": items if items is not None else _rich_items()}],
            "variable": [
                {"key": "base_url", "value": f"http://{DEFAULT_HOST}/api"},
                {"key": "base_url_vdi", "value": f"http://vdi.{DEFAULT_HOST}/api"},
                {"key": "ver", "value": "v2"},
                {"key": "apiKey", "value": SECRET},
                {"key": "segment", "value": "pre-{{apiKey}}"},
            ],
        }
    )


def _attachment(filename: str, content: str) -> TaskAttachment:
    data = content.encode("utf-8")
    return process_attachment(
        filename=filename,
        media_type="application/json",
        size=len(data),
        download=lambda _limit: data,
        max_bytes=10 * 1024 * 1024,
        max_chars=100_000,
    )


def _bare(attachment: TaskAttachment) -> TaskAttachment:
    """همان پیوست بدونِ دادهِ ساختاریافته — رفتارِ پیش از مراحلِ 3b تا 3d."""
    return replace(attachment, postman=None)


def _task(*attachments: TaskAttachment, description: str = "Admin creates a voucher.") -> TaskInput:
    return TaskInput(
        title="Create voucher",
        description=description,
        comments=(TaskComment("dev", "2026-11-01", "See the collection.", id="10001"),),
        attachments=attachments,
        source="Jira",
        source_reference="PRJ-1",
    )


_OPERATIONS = {
    "admin": [
        ("get", "/admin/voucher/{id}", "getVoucher"),
        ("post", "/admin/voucher", "createVoucher"),
    ],
    "customer": [("get", "/customer/contact/{id}", "getContact")],
    "crm": [("get", "/crm/requests", "getRequests")],
    "provider": [("get", "/provider/serviceSet/{id}", "getServiceSet")],
    "site": [("get", "/serviceSet", "getServiceSets")],
}


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    for key, operations in _OPERATIONS.items():
        paths: dict[str, dict] = {}
        for method, path, operation_id in operations:
            paths.setdefault(path, {})[method] = {
                "operationId": operation_id,
                "tags": [f"{key}-controller"],
                "summary": operation_id,
                "responses": {"200": {"description": "OK"}},
            }
        spec = {
            "openapi": "3.0.1",
            "info": {"title": key, "version": "1"},
            "servers": [{"url": "http://podium-back.devpod.ir/api"}],
            "paths": paths,
        }
        (tmp_path / f"{key}.json").write_text(json.dumps(spec), encoding="utf-8")
    return tmp_path


def _select(snapshots: Path, task: TaskInput, services: str = "", manual: str = ""):
    text = task.to_task_description()
    return select_sources(
        text, services, manual=manual, directory=snapshots, sources=sources_for_task(text, task)
    )


def _evidence(*attachments: TaskAttachment):
    return collect_postman_evidence(sources_from_task_input(_task(*attachments)))


def _by_name(evidence: PostmanEvidence) -> dict:
    return {item.name: item for item in evidence.requests}


def _no_sentinels(text: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in text
    assert DEFAULT_HOST not in text


def _everything(evidence: PostmanEvidence) -> str:
    return repr(evidence) + json.dumps(evidence.to_dict()) + render_postman_evidence(evidence)


# ── شاهد ─────────────────────────────────────────────────────────────────────


class TestEvidence:
    @pytest.mark.parametrize(("schema", "version"), [(V20, "2.0.0"), (V21, "2.1.0")])
    def test_a_supported_collection_contributes_step2_evidence(self, snapshots, schema, version):
        selection = _select(
            snapshots, _task(_attachment("c.postman_collection.json", _collection(schema=schema)))
        )

        assert len(selection.postman.requests) == 6
        assert {item.collection_version for item in selection.postman.requests} == {version}
        assert selection.postman.collections[0]["version"] == version

    def test_multiple_collections_keep_attachment_provenance(self):
        evidence = _evidence(
            _attachment("admin.json", _collection(name="Admin")),
            _attachment("plain.json", json.dumps({"a": 1})),
            _attachment(
                "crm.json",
                _collection(
                    items=[_request("CRM list", "GET", "{{base_url}}/crm/requests")], name="CRM"
                ),
            ),
        )
        labels = [
            (item.source_label, item.source_reference, item.collection_name)
            for item in evidence.requests
        ]

        assert labels[:6] == [("Attachment: admin.json", "admin.json", "Admin")] * 6
        assert labels[6:] == [("Attachment: crm.json", "crm.json", "CRM")]
        assert all(item.source_type == SOURCE_ATTACHMENT for item in evidence.requests)
        assert [c["label"] for c in evidence.collections] == [
            "Attachment: admin.json",
            "Attachment: crm.json",
        ]

    def test_requests_keep_their_identity_within_a_collection(self):
        evidence = _evidence(_attachment("c.json", _collection()))

        assert [(item.request_index, item.name) for item in evidence.requests] == [
            (0, "Literal host"),
            (1, "Base url"),
            (2, "Base url vdi"),
            (3, "Relative"),
            (4, "Versioned"),
            (5, "Secret segment"),
        ]
        assert evidence.requests[0].folder == ("Voucher",)

    def test_a_literal_host_is_preserved_unmodified(self):
        item = _by_name(_evidence(_attachment("c.json", _collection())))["Literal host"]

        assert item.host_kind == HOST_LITERAL
        assert item.host == "API.Test:8443"
        assert item.path == "/admin/voucher"
        assert item.host_variable is None

    @pytest.mark.parametrize(
        ("name", "variable"), [("Base url", "base_url"), ("Base url vdi", "base_url_vdi")]
    )
    def test_base_url_variables_keep_their_identity_unresolved(self, name, variable):
        evidence = _evidence(_attachment("c.json", _collection()))
        item = _by_name(evidence)[name]

        assert item.host_kind == HOST_VARIABLE
        assert item.host == f"{{{{{variable}}}}}"
        assert item.host_variable == variable
        assert item.path.startswith("/admin/voucher/")
        # پیش‌فرضِ Collection هرگز به‌عنوانِ میزبان یا پایه‌ی مسیر نمی‌آید.
        assert DEFAULT_HOST not in _everything(evidence)
        assert "/api/admin" not in item.path
        line = next(
            line for line in render_postman_evidence(evidence).splitlines() if f'"{name}"' in line
        )
        assert f"host: {{{{{variable}}}}} (variable; value not known)" in line

    def test_relative_paths_never_become_absolute_urls(self):
        evidence = _evidence(_attachment("c.json", _collection()))
        item = _by_name(evidence)["Relative"]

        assert item.host_kind == HOST_NONE
        assert item.host == ""
        assert item.path == "/admin/voucher/list"
        line = next(
            line for line in render_postman_evidence(evidence).splitlines() if '"Relative"' in line
        )
        assert "host: none (relative path; no base URL is known)" in line
        assert "://" not in line
        assert "method not set in the collection (Postman default GET)" in line

    def test_no_evidence_field_is_an_absolute_url(self):
        evidence = _evidence(_attachment("c.json", _collection()))

        for item in evidence.to_dict()["requests"]:
            for value in (item["host"], item["path"]):
                assert "://" not in value

    def test_path_variables_stay_unresolved(self):
        items = _by_name(_evidence(_attachment("c.json", _collection())))

        assert items["Base url"].path == "/admin/voucher/:id"
        assert items["Base url"].path_variables == ("id",)
        assert items["Base url vdi"].path == "/admin/voucher/{{voucherId}}"
        assert "voucherId" in items["Base url vdi"].unresolved
        # پیش‌فرضِ Collection («v2») در مسیر جایگذاری نمی‌شود.
        assert items["Versioned"].path == "/{{ver}}/items"

    def test_query_names_are_kept_and_values_are_not(self):
        evidence = _evidence(_attachment("c.json", _collection()))

        assert _by_name(evidence)["Base url"].query_keys == ("lang", "token")
        assert "QUERY-SENTINEL-LEAK" not in _everything(evidence)
        assert "=fa" not in _everything(evidence)
        assert '"fa"' not in json.dumps(_by_name(evidence)["Base url"].to_dict())

    def test_secrets_and_indirect_secrets_never_appear(self):
        evidence = _evidence(_attachment("c.json", _collection()))
        item = _by_name(evidence)["Secret segment"]

        assert item.path == "/{{segment}}/x"
        assert "pre-" not in _everything(evidence)
        _no_sentinels(_everything(evidence))

    def test_bodies_headers_auth_scripts_and_examples_are_absent(self):
        text = _everything(_evidence(_attachment("c.json", _collection())))

        _no_sentinels(text)
        for word in ("Authorization", "Bearer", "password", "pm.test"):
            assert word not in text

    def test_ordinary_json_contributes_nothing(self):
        evidence = _evidence(
            _attachment("sample.json", json.dumps({"url": "https://api.test/admin/voucher"}))
        )

        assert evidence.empty
        assert render_postman_evidence(evidence) == ""

    def test_description_comments_and_developed_services_gain_no_evidence(self):
        collection = _attachment("c.json", _collection()).postman
        sources = (
            TaskSource(
                "description",
                "",
                "Description",
                "x",
                requests=collection.requests,
                collection=collection,
            ),
            TaskSource(
                "comment",
                "1",
                "Comment 1",
                "x",
                requests=collection.requests,
                collection=collection,
            ),
            TaskSource("developed_services", "", "Developed Services", "x", collection=collection),
        )

        assert collect_postman_evidence(sources).empty
        assert collect_postman_evidence(sources_from_text("manual")).empty
        assert collect_postman_evidence(None).empty

    def test_task_source_requests_are_not_mutated(self):
        sources = sources_from_task_input(_task(_attachment("c.json", _collection())))
        before = sources[-1].requests
        snapshot = repr(before)

        collect_postman_evidence(sources)

        assert sources[-1].requests is before
        assert repr(sources[-1].requests) == snapshot


# ── رابطه با انتخابِ منبع و URLهای صریح ───────────────────────────────────────


def _selection_without_postman(selection):
    return replace(selection, postman=PostmanEvidence())


class TestSourceSelectionIsUnchanged:
    def test_postman_evidence_never_selects_or_registers_swagger_sources(self, snapshots):
        # مسیرهای /crm فقط از راهِ {{base_url}} (شاهدِ ساختاریافته) دیده می‌شوند.
        items = [
            _request("CRM", "GET", "{{base_url}}/crm/requests"),
            _request("Admin", "POST", "https://podium.test/api/admin/voucher"),
        ]
        attachment = _attachment("c.json", _collection(items=items))
        with_postman = _select(snapshots, _task(attachment))
        without = _select(snapshots, _task(_bare(attachment)))

        assert len(with_postman.postman.requests) == 2
        assert with_postman.keys == without.keys == (SELECTION_ADMIN,)
        assert _selection_without_postman(with_postman) == without

    def test_explicit_url_provenance_and_precedence_are_unchanged(self, snapshots):
        services = "http://podium-back.devpod.ir/api/v3/api-docs/Admin\nPOST /admin/voucher"
        attachment = _attachment("c.json", _collection())
        with_postman = _select(snapshots, _task(attachment), services=services)
        without = _select(snapshots, _task(_bare(attachment)), services=services)

        assert with_postman.urls == without.urls
        assert with_postman.evidence == without.evidence
        assert with_postman.endpoints == without.endpoints
        assert with_postman.warnings == without.warnings
        assert with_postman.keys == without.keys

    def test_postman_evidence_is_not_explicit_description_or_developed_services_evidence(
        self, snapshots
    ):
        selection = _select(snapshots, _task(_attachment("c.json", _collection())))

        assert {item.source_label for item in selection.postman.requests} == {"Attachment: c.json"}
        assert {item.source_type for item in selection.postman.requests} == {SOURCE_ATTACHMENT}
        # URLِ لفظیِ داخلِ پیوست همچنان فقط منشأِ «Attachment» دارد (رفتارِ مرحله‌ی ۲).
        assert selection.urls.origins_of("https://API.Test:8443/admin/voucher") == (
            "Attachment: c.json",
        )

    def test_unknown_explicit_swagger_still_stops_step2(self, snapshots):
        unknown = "https://host.test/api/v3/api-docs/Billing"
        selection = _select(
            snapshots, _task(_attachment("c.json", _collection())), services=unknown
        )

        assert "do not match a registered Swagger source" in selection.error
        assert len(selection.postman.requests) == 6

    def test_manual_selection_is_unchanged(self, snapshots):
        attachment = _attachment("c.json", _collection())
        with_postman = _select(snapshots, _task(attachment), manual="crm")
        without = _select(snapshots, _task(_bare(attachment)), manual="crm")

        assert with_postman.keys == without.keys
        assert _selection_without_postman(with_postman) == without

    def test_empty_requests_preserve_step2_selection_exactly(self, snapshots):
        attachment = _attachment(
            "plain.json", json.dumps({"url": "https://api.test/admin/voucher"})
        )
        selection = _select(snapshots, _task(attachment))

        assert selection.postman.empty
        assert selection == _select(snapshots, _task(_bare(attachment)))


class TestConflicts:
    def test_conflicting_hosts_keep_both_origins_and_override_nothing(self, snapshots):
        items = [_request("Create", "POST", "https://api.old.test/admin/voucher")]
        attachment = _attachment("c.json", _collection(items=items))
        description = "Use https://api.prod.test/admin/voucher?token=abc to create vouchers."
        with_postman = _select(snapshots, _task(attachment, description=description))
        without = _select(snapshots, _task(_bare(attachment), description=description))

        [conflict] = with_postman.postman.conflicts
        assert conflict.path == "/admin/voucher"
        assert conflict.postman_host == "api.old.test"
        assert conflict.postman_source == "Attachment: c.json"
        assert conflict.jira_host == "api.prod.test"
        assert conflict.jira_origins == ("Description",)
        [warning] = [w for w in with_postman.warnings if "disagree on the host" in w]
        assert "Attachment: c.json" in warning and "Description" in warning
        assert "token=abc" not in warning
        # هیچ‌کدام دیگری را کنار نگذاشت: URLِ Jira و درخواستِ Postman هر دو باقی‌اند.
        assert "https://api.prod.test/admin/voucher?token=abc" in with_postman.urls.endpoints
        assert with_postman.postman.requests[0].host == "api.old.test"
        assert with_postman.keys == without.keys
        assert with_postman.evidence == without.evidence

    def test_same_host_or_variable_host_is_not_a_conflict(self, snapshots):
        items = [
            _request("Same", "POST", "https://api.prod.test/admin/voucher"),
            _request("Variable", "POST", "{{base_url}}/admin/voucher"),
        ]
        description = "Use https://api.prod.test/admin/voucher to create vouchers."
        selection = _select(
            snapshots,
            _task(_attachment("c.json", _collection(items=items)), description=description),
        )

        assert selection.postman.conflicts == ()

    def test_urls_only_in_attachments_are_never_compared_as_explicit(self, snapshots):
        items = [_request("Create", "POST", "https://api.old.test/admin/voucher")]
        other = _attachment(
            "notes.json", json.dumps({"see": "https://api.prod.test/admin/voucher"})
        )
        selection = _select(
            snapshots, _task(_attachment("c.json", _collection(items=items)), other)
        )

        assert selection.postman.conflicts == ()


# ── نگاشتِ API ───────────────────────────────────────────────────────────────


def _catalog() -> list[DiscoveredService]:
    return [
        DiscoveredService(
            name="admin",
            source_url="https://host/swagger",
            base_url="https://host",
            apis=[
                DiscoveredApi(
                    "POST", "/admin/voucher", operation_id="createVoucher", summary="Create voucher"
                ),
                DiscoveredApi(
                    "GET", "/admin/voucher/{id}", operation_id="getVoucher", summary="Get voucher"
                ),
            ],
        )
    ]


def _cases() -> list[dict]:
    return [
        {"id": "TC-001", "title": "Create a voucher", "steps": ["Submit"]},
        {"id": "TC-002", "title": "Read a voucher", "steps": ["Open"]},
    ]


def _payload(case_ids=("TC-001", "TC-002")) -> str:
    apis = {
        "TC-001": {"method": "POST", "path": "/admin/voucher", "operation_id": "createVoucher"},
        "TC-002": {"method": "GET", "path": "/admin/voucher/{id}", "operation_id": "getVoucher"},
    }
    return json.dumps(
        {
            "mappings": [
                {
                    "test_case_id": cid,
                    "api": apis[cid],
                    "confidence": "high",
                    "reason": "Matches.",
                    "clarification": "",
                }
                for cid in case_ids
            ],
            "clarifications": [],
        }
    )


def _template_without_slot() -> str:
    _, template = split_prompt(STEP2_PROMPT.read_text(encoding="utf-8"))
    assert "{{postman_evidence}}\n\n" in template
    return template.replace("{{postman_evidence}}\n\n", "")


class TestApiMapping:
    def test_without_evidence_the_user_message_is_unchanged(self):
        _, user_message = build_prompt(STEP2_PROMPT, _cases(), _catalog())

        assert user_message == render_prompt(_template_without_slot(), _cases(), _catalog())
        assert "postman_evidence" not in user_message
        assert HEADER not in user_message

    def test_the_evidence_is_placed_after_the_candidate_apis(self):
        evidence = render_postman_evidence(_evidence(_attachment("c.json", _collection())))
        _, user_message = build_prompt(STEP2_PROMPT, _cases(), _catalog(), evidence)

        assert (
            user_message.index("CANDIDATE APIS:")
            < user_message.index(HEADER)
            < user_message.index("Map every test case")
        )

    def test_placeholders_inside_the_evidence_are_not_substituted(self):
        template = "T:\n{{test_cases}}\n\nA:\n{{discovered_apis}}\n\n{{postman_evidence}}\n\nEnd."
        message = render_prompt(template, [], [], "path /{{test_cases}}/{{base_url}}")

        assert "path /{{test_cases}}/{{base_url}}" in message

    def test_every_batch_and_the_retry_receive_the_same_evidence(self):
        evidence = render_postman_evidence(_evidence(_attachment("c.json", _collection())))
        responses = [_payload(("TC-001",)), _payload(("TC-002",))]
        with patch(_MAPPING_LLM) as mock_cls:
            mock_cls.return_value.chat.side_effect = responses
            ApiMappingAgent(batch_size=1).map_test_cases(
                _cases(), _catalog(), "user-1", postman_evidence=evidence
            )

        messages = [
            call.kwargs["user_message"] for call in mock_cls.return_value.chat.call_args_list
        ]
        assert len(messages) == 2
        assert all(evidence in message for message in messages)

    def test_generate_adds_evidence_to_the_prompt_and_an_additive_result_key(self):
        evidence = _evidence(_attachment("c.json", _collection()))
        with patch(_DISCOVER, return_value=(_catalog(), [])), patch(_MAPPING_LLM) as mock_cls:
            mock_cls.return_value.chat.return_value = _payload()
            result = Step2ApiMappingGenerator().generate(
                {"test_cases": _cases()},
                ["https://host/swagger"],
                "user-1",
                postman_evidence=evidence,
            )

        user_message = mock_cls.return_value.chat.call_args.kwargs["user_message"]
        assert HEADER in user_message
        # فیلدهای مرحله‌ی 3d دست‌نخورده‌اند؛ مرحله‌ی 3e فقط «matches» را افزوده است.
        produced = dict(result["postman_evidence"])
        matches = produced.pop("matches")
        assert produced == evidence.to_dict()
        assert len(result["postman_evidence"]["requests"]) == 6
        assert [(m["source"]["label"], m["request_index"]) for m in matches] == [
            ("Attachment: c.json", index) for index in range(6)
        ]
        assert all(m["postman_path"] == r["path"] for m, r in zip(matches, produced["requests"]))
        assert matches[0]["category"] == "EXACT_MATCH"
        assert matches[0]["operations"] == [
            {"service": "admin", "method": "POST", "path": "/admin/voucher", "operation_id": "createVoucher"}
        ]
        assert "Swagger match: EXACT_MATCH" in user_message
        # نگاشت‌ها همان عملیاتِ Swaggerاند؛ شاهدِ Postman آن‌ها را عوض نکرد.
        assert [m["api"]["operation_id"] for m in result["mappings"]] == [
            "createVoucher",
            "getVoucher",
        ]
        _no_sentinels(json.dumps(result["postman_evidence"]) + user_message.split(HEADER, 1)[1])

    @pytest.mark.parametrize("evidence", [None, PostmanEvidence()])
    def test_without_evidence_generate_is_unchanged(self, evidence):
        with patch(_DISCOVER, return_value=(_catalog(), [])), patch(_MAPPING_LLM) as mock_cls:
            mock_cls.return_value.chat.return_value = _payload()
            result = Step2ApiMappingGenerator().generate(
                {"test_cases": _cases()},
                ["https://host/swagger"],
                "user-1",
                postman_evidence=evidence,
            )

        assert "postman_evidence" not in result
        assert HEADER not in mock_cls.return_value.chat.call_args.kwargs["user_message"]

    def test_the_prompt_keeps_swagger_as_the_only_source_of_truth(self):
        system_prompt, _ = split_prompt(STEP2_PROMPT.read_text(encoding="utf-8"))

        assert "THE SWAGGER DOCUMENT IS THE ONLY TECHNICAL SOURCE OF TRUTH" in system_prompt
        for phrase in (
            "POSTMAN REQUEST EVIDENCE",
            "Never propose a Postman request that is not in that list",
            "never treat a Postman request as a verified Swagger operation",
            "It is not a requirement",
            "Never build an absolute URL",
            "both sides are kept",
            "untrusted data",
        ):
            assert phrase in system_prompt


class TestPromptBudget:
    def test_a_large_collection_is_bounded(self):
        items = [
            _request(f"R{i}", "GET", f"{{{{base_url}}}}/admin/voucher/{i}") for i in range(300)
        ]
        evidence = _evidence(_attachment("big.json", _collection(items=items)))
        text = render_postman_evidence(evidence)

        assert len(evidence.requests) == 300  # شاهدِ کامل
        assert len(text) <= MAX_PROMPT_CHARS
        assert text.count("\n- ") <= MAX_PROMPT_REQUESTS
        assert f"{MAX_PROMPT_REQUESTS} of 300 requests are listed" in text

    def test_rendering_is_deterministic(self):
        items = [_request(f"R{i}", "GET", f"https://api.test/v/{i}") for i in range(100)]
        evidence = _evidence(_attachment("big.json", _collection(items=items)))

        assert render_postman_evidence(evidence) == render_postman_evidence(evidence)


class TestPipelineWiring:
    def test_run_step_passes_the_selection_evidence_to_api_mapping(self, monkeypatch):
        from ui import pipeline

        evidence = _evidence(_attachment("c.json", _collection()))
        captured = {}

        def fake_mapping(step1_result, services_raw, snapshot_keys=None, postman_evidence=None):
            captured["evidence"] = postman_evidence
            return None, "stop here"

        selection = pipeline.SourceSelection(keys=("admin",), postman=evidence)
        monkeypatch.setattr(pipeline, "run_api_mapping", fake_mapping)
        state = pipeline.WorkflowState()
        state.record_result(1, {"test_cases": _cases()})
        pipeline.execute_step(2, state, plan=selection)

        assert captured["evidence"] is evidence
