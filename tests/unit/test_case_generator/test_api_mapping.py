"""تست‌های نگاشتِ تست‌کیس به API و اعتبارسنجیِ نتیجه‌ی قدم دوم."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.agents.test_case_generator.api_discovery import (
    DiscoveredApi,
    DiscoveredService,
)
from src.agents.test_case_generator.api_mapping import (
    ApiMappingAgent,
    ApiMappingError,
    Step2ApiMappingGenerator,
    build_prompt,
    build_result,
    extract_test_cases,
    render_prompt,
    split_prompt,
    validate_mapping_payload,
)

PROMPT_PATH = (
    __import__("pathlib").Path(__file__).parents[3]
    / "src"
    / "agents"
    / "test_case_generator"
    / "prompts"
    / "step2_api_mapping.md"
)


def _test_cases() -> list[dict]:
    return [
        {
            "id": "TC-001",
            "title": "Verify successful retrieval of voucher details",
            "type": "positive",
            "priority": "high",
            "steps": ["Invoke the voucher details service"],
            "expected_result": "Voucher details are returned.",
            "related_service": None,
        },
        {
            "id": "TC-002",
            "title": "Update a voucher amount",
            "type": "positive",
            "priority": "medium",
        },
    ]


def _service(name: str = "admin") -> DiscoveredService:
    return DiscoveredService(
        name=name,
        source_url="https://host/api/swagger-ui/index.html",
        base_url="https://podium-admin.sandpod.ir",
        title="Admin API",
        version="1.0.0",
        apis=[
            DiscoveredApi(
                "GET",
                "/admin/voucher/{id}",
                operation_id="getVoucherDetails",
                summary="Get voucher details",
            ),
            DiscoveredApi(
                "PUT",
                "/admin/voucher/{id}",
                operation_id="updateVoucher",
                summary="Update voucher",
            ),
        ],
    )


# نگاشتِ پیش‌فرض در برابر «صریحاً null» — None خودش یک مقدارِ معنادار است
_UNSET = object()

_DEFAULT_API = {
    "method": "GET",
    "path": "/admin/voucher/{id}",
    "operation_id": "getVoucherDetails",
}


def _mapping(
    case_id: str = "TC-001",
    api: object = _UNSET,
    confidence: str = "high",
    **overrides,
) -> dict:
    mapping = {
        "test_case_id": case_id,
        "api": dict(_DEFAULT_API) if api is _UNSET else api,
        "confidence": confidence,
        "reason": "The operation matches the requirement.",
        "clarification": "",
    }
    mapping.update(overrides)
    return mapping


def _payload(mappings: list[dict] | None = None, **overrides) -> dict:
    payload = {
        "mappings": mappings
        if mappings is not None
        else [
            _mapping("TC-001"),
            _mapping(
                "TC-002",
                api={"method": "PUT", "path": "/admin/voucher/{id}", "operation_id": "updateVoucher"},
            ),
        ],
        "clarifications": [],
    }
    payload.update(overrides)
    return payload


# ── prompt ───────────────────────────────────────────────────────────────────

class TestPrompt:
    def test_shipped_prompt_splits(self):
        system_prompt, template = split_prompt(PROMPT_PATH.read_text(encoding="utf-8"))

        assert "SWAGGER DOCUMENT IS THE ONLY TECHNICAL SOURCE OF TRUTH" in system_prompt
        assert "{{test_cases}}" in template
        assert "{{discovered_apis}}" in template

    def test_missing_input_section_is_rejected(self):
        with pytest.raises(ApiMappingError, match="no INPUT section"):
            split_prompt("rules only, no input section")

    def test_missing_placeholder_is_rejected(self):
        with pytest.raises(ApiMappingError, match="missing placeholder"):
            split_prompt("rules\n====\nINPUT\n====\n{{test_cases}}\n")

    def test_render_substitutes_both_blocks(self):
        _, template = split_prompt(PROMPT_PATH.read_text(encoding="utf-8"))

        rendered = render_prompt(template, _test_cases(), [_service()])

        assert "TC-001" in rendered
        assert "getVoucherDetails" in rendered
        assert "{{test_cases}}" not in rendered

    def test_prompt_sent_to_the_llm_keeps_rules_in_the_system_message(self):
        system_prompt, user_message = build_prompt(PROMPT_PATH, _test_cases(), [_service()])

        assert "TC-001" not in system_prompt
        assert "TC-001" in user_message
        # پارامترها و بدنه‌ی کامل به پرامپت فرستاده نمی‌شوند؛ فقط نمای فشرده
        assert "parameters" not in user_message


# ── استخراجِ ورودیِ قدم اول ──────────────────────────────────────────────────

class TestExtractTestCases:
    def test_reads_test_cases_from_a_step1_result(self):
        assert len(extract_test_cases({"test_cases": _test_cases()})) == 2

    def test_accepts_a_bare_list(self):
        assert len(extract_test_cases(_test_cases())) == 2

    def test_result_without_test_cases_is_rejected(self):
        with pytest.raises(ApiMappingError, match="does not contain any test case"):
            extract_test_cases({"task_summary": "nothing here"})

    def test_empty_list_is_rejected(self):
        with pytest.raises(ApiMappingError, match="does not contain any test case"):
            extract_test_cases({"test_cases": []})

    def test_case_without_id_is_rejected(self):
        with pytest.raises(ApiMappingError, match="has no 'id'"):
            extract_test_cases({"test_cases": [{"title": "no id"}]})

    def test_duplicate_ids_are_rejected(self):
        with pytest.raises(ApiMappingError, match="duplicate test case id"):
            extract_test_cases({"test_cases": [{"id": "TC-1"}, {"id": "TC-1"}]})

    def test_non_object_result_is_rejected(self):
        with pytest.raises(ApiMappingError, match="must be a JSON object or an array"):
            extract_test_cases("nope")


# ── اعتبارسنجی ───────────────────────────────────────────────────────────────

class TestValidateMappingPayload:
    def test_valid_payload_is_accepted(self):
        mappings, clarifications = validate_mapping_payload(
            _payload(), test_cases=_test_cases(), services=[_service()]
        )

        assert [m["test_case_id"] for m in mappings] == ["TC-001", "TC-002"]
        assert mappings[0]["api"]["operation_id"] == "getVoucherDetails"
        assert clarifications == []

    def test_mappings_follow_the_test_case_order(self):
        payload = _payload(mappings=[_mapping("TC-002"), _mapping("TC-001")])

        mappings, _ = validate_mapping_payload(
            payload, test_cases=_test_cases(), services=[_service()]
        )

        assert [m["test_case_id"] for m in mappings] == ["TC-001", "TC-002"]

    def test_unmapped_case_is_allowed_and_kept(self):
        payload = _payload(
            mappings=[
                _mapping("TC-001"),
                _mapping("TC-002", api=None, confidence="low", clarification="Which one?"),
            ]
        )

        mappings, _ = validate_mapping_payload(
            payload, test_cases=_test_cases(), services=[_service()]
        )

        assert mappings[1]["api"] is None
        assert mappings[1]["confidence"] == "low"
        assert mappings[1]["clarification"] == "Which one?"

    def test_operation_that_was_not_discovered_is_rejected(self):
        payload = _payload(
            mappings=[
                _mapping("TC-001", api={"method": "GET", "path": "/invented", "operation_id": "nope"}),
                _mapping("TC-002"),
            ]
        )

        with pytest.raises(ApiMappingError, match="does not match any operation"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_operation_resolves_by_operation_id_alone(self):
        payload = _payload(
            mappings=[
                _mapping("TC-001", api={"operation_id": "getVoucherDetails"}),
                _mapping("TC-002"),
            ]
        )

        mappings, _ = validate_mapping_payload(
            payload, test_cases=_test_cases(), services=[_service()]
        )

        assert mappings[0]["api"]["path"] == "/admin/voucher/{id}"

    def test_invalid_confidence_is_rejected(self):
        payload = _payload(mappings=[_mapping("TC-001", confidence="certain"), _mapping("TC-002")])

        with pytest.raises(ApiMappingError, match="'confidence' must be one of"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_missing_reason_is_rejected(self):
        payload = _payload(mappings=[_mapping("TC-001", reason=""), _mapping("TC-002")])

        with pytest.raises(ApiMappingError, match="'reason' is missing"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_unknown_test_case_id_is_rejected(self):
        payload = _payload(mappings=[_mapping("TC-999"), _mapping("TC-002")])

        with pytest.raises(ApiMappingError, match="unknown test_case_id"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_duplicate_mapping_is_rejected(self):
        payload = _payload(mappings=[_mapping("TC-001"), _mapping("TC-001"), _mapping("TC-002")])

        with pytest.raises(ApiMappingError, match="duplicate mapping"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_missing_coverage_is_rejected(self):
        payload = _payload(mappings=[_mapping("TC-001")])

        with pytest.raises(ApiMappingError, match="no mapping was returned"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_empty_mappings_are_rejected(self):
        with pytest.raises(ApiMappingError, match="'mappings' must be a non-empty array"):
            validate_mapping_payload(
                {"mappings": []}, test_cases=_test_cases(), services=[_service()]
            )

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(ApiMappingError, match="not a JSON object"):
            validate_mapping_payload(
                ["nope"], test_cases=_test_cases(), services=[_service()]
            )

    def test_api_mapping_structure_must_be_an_object(self):
        payload = _payload(mappings=[_mapping("TC-001", api="GET /x"), _mapping("TC-002")])

        with pytest.raises(ApiMappingError, match="does not match any operation"):
            validate_mapping_payload(payload, test_cases=_test_cases(), services=[_service()])

    def test_clarifications_are_passed_through(self):
        payload = _payload(clarifications=["Which environment?"])

        _, clarifications = validate_mapping_payload(
            payload, test_cases=_test_cases(), services=[_service()]
        )

        assert clarifications == ["Which environment?"]

    def test_multiple_services_are_all_searchable(self):
        payload = _payload(
            mappings=[
                _mapping(
                    "TC-001",
                    api={"method": "GET", "path": "/orders/{orderId}", "operation_id": "getOrder"},
                ),
                _mapping("TC-002"),
            ]
        )
        other = DiscoveredService(
            name="orders",
            source_url="u",
            base_url="b",
            apis=[DiscoveredApi("GET", "/orders/{orderId}", operation_id="getOrder")],
        )

        mappings, _ = validate_mapping_payload(
            payload, test_cases=_test_cases(), services=[_service(), other]
        )

        assert mappings[0]["api"]["path"] == "/orders/{orderId}"


# ── عاملِ نگاشت ──────────────────────────────────────────────────────────────

class TestApiMappingAgent:
    def _run(self, payload: dict | str, **kwargs):
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            llm = MagicMock()
            llm.chat.return_value = payload if isinstance(payload, str) else json.dumps(payload)
            mock_cls.return_value = llm
            result = ApiMappingAgent(**kwargs).map_test_cases(
                _test_cases(), [_service()], "user-1"
            )
        return result, mock_cls, llm

    def test_returns_validated_mappings(self):
        mappings, _ = self._run(_payload())[0]

        assert [m["test_case_id"] for m in mappings] == ["TC-001", "TC-002"]

    def test_llm_client_is_constructed_with_user_and_agent(self):
        _, mock_cls, _ = self._run(_payload())

        mock_cls.assert_called_once_with(user_id="user-1", agent_name=ApiMappingAgent.name)

    def test_rules_stay_in_the_system_prompt(self):
        _, _, llm = self._run(_payload())
        kwargs = llm.chat.call_args.kwargs

        assert "THE SWAGGER DOCUMENT IS THE ONLY TECHNICAL SOURCE OF TRUTH" in (
            kwargs["system_prompt"]
        )
        assert "You must NOT:" in kwargs["system_prompt"]
        assert "TC-001" not in kwargs["system_prompt"]
        assert "TC-001" in kwargs["user_message"]

    def test_temperature_and_max_tokens_are_configurable(self):
        _, _, llm = self._run(_payload(), temperature=0.3, max_tokens=2048)
        kwargs = llm.chat.call_args.kwargs

        assert kwargs["temperature"] == 0.3
        assert kwargs["max_tokens"] == 2048

    def test_hallucinated_operation_is_rejected(self):
        payload = _payload(
            mappings=[
                _mapping("TC-001", api={"method": "DELETE", "path": "/nope", "operation_id": "nope"}),
                _mapping("TC-002"),
            ]
        )

        with pytest.raises(ApiMappingError, match="does not match any operation"):
            self._run(payload)

    def test_non_json_output_raises(self):
        with pytest.raises(ApiMappingError, match="did not return JSON"):
            self._run("not json at all")

    def test_custom_prompt_path_is_used(self, tmp_path):
        prompt_file = tmp_path / "custom.md"
        prompt_file.write_text(
            "Custom rules.\n====\nINPUT\n====\n"
            "CASES:\n{{test_cases}}\nAPIS:\n{{discovered_apis}}\n",
            encoding="utf-8",
        )

        _, _, llm = self._run(_payload(), prompt_path=prompt_file)

        assert llm.chat.call_args.kwargs["system_prompt"] == "Custom rules."


# ── نتیجه‌ی نهاییِ قدم دوم ───────────────────────────────────────────────────

class TestBuildResult:
    def test_result_has_the_step2_shape(self):
        mappings, clarifications = validate_mapping_payload(
            _payload(), test_cases=_test_cases(), services=[_service()]
        )

        result = build_result([_service()], mappings, clarifications)

        assert set(result) == {"services", "mappings", "clarifications"}
        assert result["services"][0]["name"] == "admin"
        assert result["services"][0]["base_url"] == "https://podium-admin.sandpod.ir"
        assert result["services"][0]["apis"][0]["operation_id"] == "getVoucherDetails"
        assert len(result["mappings"]) == 2


class TestStep2ApiMappingGenerator:
    def _generate(self, payload: dict, services=None, errors=None, step1=None, sources=None):
        with patch(
            "src.agents.test_case_generator.api_mapping.discover_services",
            return_value=(services if services is not None else [_service()], errors or []),
        ), patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(payload)
            result = Step2ApiMappingGenerator().generate(
                step1 if step1 is not None else {"test_cases": _test_cases()},
                sources if sources is not None else ["https://host/swagger"],
                "user-1",
            )
        return result

    def test_builds_services_and_mappings(self):
        result = self._generate(_payload())

        assert result["services"][0]["name"] == "admin"
        assert result["mappings"][0]["api"]["operation_id"] == "getVoucherDetails"
        assert result["clarifications"] == []

    def test_swagger_load_errors_surface_as_clarifications(self):
        result = self._generate(_payload(), services=[_service()], errors=["boom"])

        assert "boom" in result["clarifications"]

    def test_no_source_raises_value_error(self):
        with pytest.raises(ValueError, match="No Swagger source"):
            Step2ApiMappingGenerator().generate(
                {"test_cases": _test_cases()}, ["   "], "user-1"
            )

    def test_no_loadable_service_raises(self):
        with patch(
            "src.agents.test_case_generator.api_mapping.discover_services",
            return_value=([], ["could not load"]),
        ):
            with pytest.raises(ApiMappingError, match="No Swagger document could be loaded"):
                Step2ApiMappingGenerator().generate(
                    {"test_cases": _test_cases()}, ["https://host/swagger"], "user-1"
                )

    def test_invalid_step1_result_raises_before_any_network_call(self):
        with pytest.raises(ApiMappingError, match="does not contain any test case"):
            Step2ApiMappingGenerator().generate({}, ["https://host/swagger"], "user-1")
