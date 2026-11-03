"""
تست‌های خواندنِ بُرد، ستون، اسپرینت، status و جست‌وجو — روی Jiraِ ساختگی، فقط GET.
"""

from __future__ import annotations

import pytest

from src.task_sources.jira import JiraClient, JiraResponseError
from src.task_sources.jira.agile import (
    BoardColumn,
    active_sprints,
    board_configuration,
    list_boards,
    search_issues,
    status_ids,
)
from tests.unit.task_sources.fake_jira import FakeJira, adfs_settings

CONFIG = "/rest/agile/1.0/board/7/configuration"


def _client(server: FakeJira) -> JiraClient:
    return JiraClient(adfs_settings(), session=server.session())


def _rest_methods(server: FakeJira) -> set[str]:
    return {call.method for call in server.calls_to("jira.test") if "/rest/" in call.url}


class TestBoards:
    def test_all_pages_are_read_until_is_last(self):
        server = FakeJira(
            boards=[
                {"id": n, "name": f"Board {n}", "type": "scrum", "location": {"projectKey": "DEMO"}}
                for n in range(1, 121)
            ]
        )

        boards = list_boards(_client(server))

        assert [board.id for board in boards] == list(range(1, 121))
        assert boards[0].project_key == "DEMO"
        assert _rest_methods(server) == {"GET"}

    def test_name_and_project_are_server_side_filters(self, monkeypatch):
        server = FakeJira(boards=[{"id": 1, "name": "Podium", "type": "kanban"}])
        client = _client(server)
        seen: list[dict] = []
        real = client.get_json

        def spy(path, **params):
            seen.append(params)
            return real(path, **params)

        monkeypatch.setattr(client, "get_json", spy)

        list_boards(client, name="Pod", project="PODIUM")

        assert seen[0]["name"] == "Pod"
        assert seen[0]["projectKeyOrId"] == "PODIUM"


class TestBoardConfiguration:
    def test_columns_filter_and_sub_query_are_read(self):
        server = FakeJira(
            routes={
                CONFIG: {
                    "id": 7,
                    "name": "Demo",
                    "type": "kanban",
                    "filter": {"id": "500"},
                    "subQuery": {"query": "fixVersion is EMPTY "},
                    "columnConfig": {
                        "columns": [
                            {"name": "Backlog", "statuses": [{"id": "1"}]},
                            {"name": "QC Test", "statuses": [{"id": "10518"}, {"id": "3"}]},
                            {"name": "Empty", "statuses": []},
                        ]
                    },
                }
            }
        )

        config = board_configuration(_client(server), 7)

        assert config.filter_id == "500"
        assert config.sub_query == "fixVersion is EMPTY"
        assert config.columns[1] == BoardColumn("QC Test", frozenset({"10518", "3"}))
        assert config.columns[2].status_ids == frozenset()

    def test_a_configuration_without_filter_is_malformed(self):
        server = FakeJira(routes={CONFIG: {"type": "scrum", "columnConfig": {"columns": []}}})

        with pytest.raises(JiraResponseError, match="without columns or filter"):
            board_configuration(_client(server), 7)


class TestSprintsAndStatuses:
    def test_active_sprints(self):
        server = FakeJira(
            routes={
                "/rest/agile/1.0/board/7/sprint": {
                    "values": [{"id": 31, "name": "Sprint 31", "state": "active"}],
                    "isLast": True,
                }
            }
        )

        assert [sprint.id for sprint in active_sprints(_client(server), 7)] == [31]

    def test_status_names_match_exactly(self):
        server = FakeJira(
            routes={
                "/rest/api/2/status": [
                    {"id": "10518", "name": "Manual Test"},
                    {"id": "77", "name": "manual test"},
                    {"id": "78", "name": "Manual Test "},
                ]
            }
        )

        assert status_ids(_client(server), "Manual Test") == frozenset({"10518"})


class TestSearch:
    def test_every_page_is_collected(self):
        issues = [{"key": f"DEMO-{n}"} for n in range(1, 251)]
        server = FakeJira(searches={"project = DEMO": issues})

        found = search_issues(_client(server), "project = DEMO", "status", page_size=100)

        assert [issue["key"] for issue in found] == [issue["key"] for issue in issues]
        assert _rest_methods(server) == {"GET"}

    def test_an_incomplete_result_is_an_error_not_a_short_list(self, monkeypatch):
        server = FakeJira(searches={"project = DEMO": [{"key": "DEMO-1"}]})
        client = _client(server)
        real = client.get_json

        def lying(path, **params):
            body = real(path, **params)
            body["total"] = 5
            return body

        monkeypatch.setattr(client, "get_json", lying)

        with pytest.raises(JiraResponseError, match="incomplete"):
            search_issues(client, "project = DEMO", "status")
