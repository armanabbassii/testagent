"""
تست‌های کشفِ issueهای واجدِ شرایط — ستونِ QC TEST، statusِ Manual Test، برچسبِ
backend، عضویتِ واقعی در بُرد. روی Jiraِ ساختگی؛ هیچ نوشتنی در Jira نیست.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.batch_testing import (
    BoardContext,
    DiscoveryError,
    EligibilityCriteria,
    discover,
    evaluate,
    resolve_board,
)
from src.batch_testing.discovery import candidate_jql, normalize_name, sprint_jql
from src.task_sources.jira import JiraClient, JiraNotFoundError, JiraResponseError
from tests.unit.task_sources.fake_jira import FakeJira, adfs_settings

MANUAL = ("10518", "Manual Test")
STATUSES = [
    {"id": "10518", "name": "Manual Test"},
    {"id": "1", "name": "To Do"},
    {"id": "4", "name": "QC Passed"},
]
SCRUM_JQL = "filter = 500 AND status in (10518) ORDER BY key ASC"
SPRINT_JQL = "filter = 500 AND status in (10518) AND sprint in (31) ORDER BY key ASC"


def issue(
    key: str,
    *,
    labels: tuple[str, ...] = ("backend",),
    status: tuple[str, str] = MANUAL,
    subtask: bool = False,
    parent: str = "",
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": f"Summary of {key}",
        "status": {"id": status[0], "name": status[1]},
        "labels": list(labels),
        "issuetype": {"name": "Sub-task" if subtask else "Story", "subtask": subtask},
    }
    if parent:
        fields["parent"] = {"key": parent}
    return {"key": key, "fields": fields}


def configuration(board_type: str = "scrum", columns=None, sub_query: str = "") -> dict[str, Any]:
    return {
        "id": 7,
        "name": "Demo board",
        "type": board_type,
        "filter": {"id": "500"},
        "subQuery": {"query": sub_query} if sub_query else {},
        "columnConfig": {
            "columns": columns
            or [
                {"name": "To Do", "statuses": [{"id": "1"}]},
                {"name": "QC  Test ", "statuses": [{"id": "10518"}, {"id": "4"}]},
            ]
        },
    }


def server_for(
    *, board_type: str = "scrum", sprints=(31,), columns=None, sub_query: str = "", searches=None
) -> FakeJira:
    return FakeJira(
        routes={
            "/rest/agile/1.0/board/7/configuration": configuration(board_type, columns, sub_query),
            "/rest/api/2/status": STATUSES,
            "/rest/agile/1.0/board/7/sprint": {
                "values": [{"id": s, "name": f"Sprint {s}", "state": "active"} for s in sprints],
                "isLast": True,
            },
        },
        searches=searches or {},
    )


def client_for(server: FakeJira) -> JiraClient:
    return JiraClient(adfs_settings(), session=server.session())


def context(board_type: str = "scrum") -> BoardContext:
    return BoardContext(
        board_id=7,
        board_name="Demo board",
        board_type=board_type,
        filter_id="500",
        sub_query="",
        column_name="QC Test",
        column_status_ids=frozenset({"10518", "4"}),
        target_status_ids=frozenset({"10518"}),
        active_sprint_ids=(31,) if board_type == "scrum" else None,
    )


class TestNormalizeName:
    @pytest.mark.parametrize("name", ["QC TEST", "QC Test", "  qc   test ", "Qc\tTest"])
    def test_case_and_spacing_do_not_matter(self, name):
        assert normalize_name(name) == normalize_name("QC TEST")

    def test_different_words_do_not_match(self):
        assert normalize_name("QC Tests") != normalize_name("QC TEST")


class TestEvaluate:
    def test_an_issue_meeting_every_condition_is_eligible(self):
        candidate = evaluate(issue("DEMO-1"), context(), in_active_sprint=True)

        assert candidate.eligible
        assert candidate.reasons == ()
        assert candidate.in_active_sprint is True

    def test_a_different_status_in_the_same_column_is_excluded(self):
        candidate = evaluate(
            issue("DEMO-1", status=("4", "QC Passed")), context(), in_active_sprint=True
        )

        assert candidate.reasons == ("status is 'QC Passed', not 'Manual Test'",)

    def test_a_status_outside_the_column_is_excluded(self):
        candidate = evaluate(
            issue("DEMO-1", status=("999", "Manual Test")), context(), in_active_sprint=True
        )

        assert candidate.reasons == ("status is not in the 'QC Test' column of this board",)

    def test_on_scrum_an_issue_outside_the_active_sprint_meeting_all_three_is_eligible(self):
        """ستون از نگاشتِ status→ستونِ بُرد می‌آید، نه از اسپرینت؛ اسپرینت شرط نیست."""
        candidate = evaluate(issue("DEMO-1"), context(), in_active_sprint=False)

        assert candidate.eligible
        assert candidate.reasons == () and candidate.failed_criteria == ()
        assert candidate.in_active_sprint is False  # فقط اطلاعاتی

    def test_sprint_membership_never_rescues_a_failed_criterion(self):
        candidate = evaluate(
            issue("DEMO-1", status=("999", "Manual Test")), context(), in_active_sprint=True
        )

        assert candidate.reasons == ("status is not in the 'QC Test' column of this board",)
        assert candidate.failed_criteria == ("column",)

    @pytest.mark.parametrize(
        "case",
        [
            issue("DEMO-1"),
            issue("DEMO-1", labels=()),
            issue("DEMO-1", status=("4", "QC Passed")),
            issue("DEMO-1", status=("999", "Manual Test")),
        ],
    )
    def test_sprint_membership_does_not_change_the_verdict(self, case):
        inside = evaluate(case, context(), in_active_sprint=True)
        outside = evaluate(case, context(), in_active_sprint=False)

        assert (outside.reasons, outside.failed_criteria) == (
            inside.reasons,
            inside.failed_criteria,
        )

    def test_each_reason_is_tagged_with_its_criterion(self):
        candidate = evaluate(issue("DEMO-1", labels=("Backend",)), context(), in_active_sprint=True)

        assert candidate.failed_criteria == ("label",)
        assert evaluate(issue("DEMO-1"), context(), in_active_sprint=True).failed_criteria == ()

    def test_on_kanban_sprints_are_not_considered(self):
        candidate = evaluate(issue("DEMO-1"), context("kanban"), in_active_sprint=None)

        assert candidate.eligible
        assert candidate.in_active_sprint is None

    @pytest.mark.parametrize("labels", [(), ("back",), ("frontend", "api")])
    def test_without_the_label_the_issue_is_excluded(self, labels):
        candidate = evaluate(issue("DEMO-1", labels=labels), context(), in_active_sprint=True)

        assert candidate.reasons == ("label 'backend' is missing",)

    def test_labels_must_match_exactly(self):
        candidate = evaluate(issue("DEMO-1", labels=("Backend",)), context(), in_active_sprint=True)

        assert not candidate.eligible
        assert candidate.reasons == (
            "label 'backend' is missing (has 'Backend'; labels must match exactly)",
        )

    def test_the_label_may_sit_among_other_labels(self):
        candidate = evaluate(
            issue("DEMO-1", labels=("api", "backend", "urgent")), context(), in_active_sprint=True
        )

        assert candidate.eligible

    def test_every_failed_condition_is_reported_once_per_criterion(self):
        candidate = evaluate(
            issue("DEMO-1", labels=(), status=("1", "To Do")), context(), in_active_sprint=False
        )

        assert len(candidate.reasons) == 3
        assert candidate.failed_criteria == ("status", "column", "label")
        assert not any("sprint" in reason for reason in candidate.reasons)

    def test_a_sub_task_is_judged_on_its_own_status_and_labels(self):
        child = issue("DEMO-2", subtask=True, parent="DEMO-1", labels=())

        candidate = evaluate(child, context(), in_active_sprint=True)

        assert candidate.is_subtask
        assert candidate.parent_key == "DEMO-1"
        assert candidate.reasons == ("label 'backend' is missing",)

    def test_custom_criteria(self):
        criteria = EligibilityCriteria(column="Ready", status="To Do", label="api")
        ready = BoardContext(**{**context().__dict__, "column_status_ids": frozenset({"1"})})

        candidate = evaluate(
            issue("DEMO-1", labels=("api",), status=("1", "To Do")),
            ready,
            criteria,
            in_active_sprint=True,
        )

        assert candidate.eligible


class TestResolveBoard:
    def test_the_column_is_found_by_normalised_name(self):
        ctx = resolve_board(client_for(server_for()), 7)

        assert ctx.column_name == "QC  Test "
        assert ctx.column_status_ids == frozenset({"10518", "4"})
        assert ctx.target_status_ids == frozenset({"10518"})
        assert ctx.active_sprint_ids == (31,)

    def test_kanban_boards_have_no_sprints(self):
        server = server_for(board_type="kanban", sub_query="fixVersion is EMPTY")

        ctx = resolve_board(client_for(server), 7)

        assert ctx.active_sprint_ids is None
        assert not any("/sprint" in call.url for call in server.calls)

    def test_a_board_without_the_column_is_an_error(self):
        server = server_for(columns=[{"name": "QC", "statuses": [{"id": "10518"}]}])

        with pytest.raises(DiscoveryError, match="has no 'QC TEST' column"):
            resolve_board(client_for(server), 7)

    def test_two_matching_columns_are_ambiguous(self):
        columns = [
            {"name": "QC Test", "statuses": [{"id": "10518"}]},
            {"name": "qc test", "statuses": [{"id": "4"}]},
        ]

        with pytest.raises(DiscoveryError, match="ambiguous"):
            resolve_board(client_for(server_for(columns=columns)), 7)

    def test_a_column_without_the_status_is_an_error(self):
        columns = [{"name": "QC Test", "statuses": [{"id": "4"}]}]

        with pytest.raises(DiscoveryError, match="does not contain the status 'Manual Test'"):
            resolve_board(client_for(server_for(columns=columns)), 7)

    def test_a_missing_status_is_an_error(self):
        server = server_for()
        server.routes["/rest/api/2/status"] = [{"id": "1", "name": "To Do"}]

        with pytest.raises(DiscoveryError, match="no status named 'Manual Test'"):
            resolve_board(client_for(server), 7)

    def test_unsupported_board_types_are_refused(self):
        with pytest.raises(DiscoveryError, match="only scrum and kanban"):
            resolve_board(client_for(server_for(board_type="simple")), 7)


class TestJql:
    def test_scrum_jql_narrows_by_filter_and_status_only(self):
        assert candidate_jql(context()) == "filter = 500 AND status in (10518)"
        assert sprint_jql(context()) == "filter = 500 AND status in (10518) AND sprint in (31)"

    def test_kanban_jql_adds_the_sub_query(self):
        kanban = BoardContext(
            **{**context("kanban").__dict__, "sub_query": "fixVersion is EMPTY OR fixVersion = 1"}
        )

        assert candidate_jql(kanban) == (
            "filter = 500 AND status in (10518) AND (fixVersion is EMPTY OR fixVersion = 1)"
        )


class TestDiscover:
    def test_scrum_board_end_to_end(self):
        server = server_for(
            searches={
                SCRUM_JQL: [
                    issue("DEMO-1"),
                    issue("DEMO-2", labels=("back",)),
                    issue("DEMO-3"),
                    issue("DEMO-4", subtask=True, parent="DEMO-9"),
                    issue("DEMO-1"),
                ],
                SPRINT_JQL: [issue("DEMO-1"), issue("DEMO-2"), issue("DEMO-4")],
            }
        )

        result = discover(client_for(server), 7)

        assert [c.key for c in result.candidates] == ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4"]
        # DEMO-3 بیرون از اسپرینتِ فعال است ولی هر سه شرط را دارد
        assert [c.key for c in result.eligible] == ["DEMO-1", "DEMO-3", "DEMO-4"]
        assert {c.key: c.reasons for c in result.excluded} == {
            "DEMO-2": ("label 'backend' is missing",),
        }
        sprint = {c.key: c.in_active_sprint for c in result.candidates}
        assert sprint == {"DEMO-1": True, "DEMO-2": True, "DEMO-3": False, "DEMO-4": True}

    def test_a_scrum_board_without_an_active_sprint_still_has_eligible_issues(self):
        server = server_for(
            sprints=(), searches={SCRUM_JQL: [issue("DEMO-1"), issue("DEMO-2", labels=())]}
        )

        result = discover(client_for(server), 7)

        assert [c.key for c in result.eligible] == ["DEMO-1"]
        assert result.eligible[0].in_active_sprint is False
        assert result.excluded[0].failed_criteria == ("label",)
        assert not any("sprint in" in str(call.url) for call in server.calls)

    def test_issues_only_in_a_future_or_closed_sprint_are_eligible(self):
        """sprint_jql فقط اسپرینت‌های فعال را می‌پرسد؛ issueِ اسپرینتِ آینده یا بسته
        (یا backlog) در آن نیست و با این حال بر اساسِ سه شرط واجدِ شرایط است."""
        server = server_for(
            searches={SCRUM_JQL: [issue("DEMO-5"), issue("DEMO-6")], SPRINT_JQL: []}
        )

        result = discover(client_for(server), 7)

        assert [c.key for c in result.eligible] == ["DEMO-5", "DEMO-6"]
        assert all(c.in_active_sprint is False for c in result.eligible)

    def test_kanban_board_end_to_end(self):
        jql = "filter = 500 AND status in (10518) AND (fixVersion is EMPTY) ORDER BY key ASC"
        server = server_for(
            board_type="kanban",
            sub_query="fixVersion is EMPTY",
            searches={jql: [issue("OPS-1"), issue("OPS-2", labels=("Backend",))]},
        )

        result = discover(client_for(server), 7)

        assert [c.key for c in result.eligible] == ["OPS-1"]
        assert result.excluded[0].reasons[0].startswith("label 'backend' is missing (has 'Backend'")
        assert all(c.in_active_sprint is None for c in result.candidates)
        assert not any("sprint" in str(call.url) for call in server.calls)

    def test_discovery_only_reads(self):
        server = server_for(searches={SCRUM_JQL: [issue("DEMO-1")], SPRINT_JQL: [issue("DEMO-1")]})

        discover(client_for(server), 7)

        rest_calls = [call for call in server.calls_to("jira.test") if "/rest/" in call.url]
        assert rest_calls
        assert {call.method for call in rest_calls} == {"GET"}

    def test_an_invisible_board_is_an_error_not_an_empty_result(self):
        server = server_for()
        server.routes.pop("/rest/agile/1.0/board/7/configuration")

        with pytest.raises(JiraNotFoundError):
            discover(client_for(server), 7)

    def test_a_rejected_search_is_an_error_not_an_empty_result(self):
        server = server_for(searches={})

        with pytest.raises(JiraResponseError, match="HTTP 400"):
            discover(client_for(server), 7)
