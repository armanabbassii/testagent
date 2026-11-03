"""
src/task_sources/jira/agile.py — خواندنِ بُردها، ستون‌ها، اسپرینت‌ها و جست‌وجوی issue

همه فقط GET اند و از همان JiraClient (ورود، re-auth، خطاهای تایپ‌دار) می‌گذرند:

    /rest/agile/1.0/board                    فهرستِ بُردها (صفحه‌بندی با isLast)
    /rest/agile/1.0/board/{id}/configuration ستون‌ها (مجموعه‌ی status id)، فیلتر، subQuery
    /rest/agile/1.0/board/{id}/sprint        اسپرینت‌های فعالِ بُردِ scrum
    /rest/api/2/status                       status id از روی نام
    /rest/api/2/search                       JQL (صفحه‌بندی با total)

نکته‌ی آزموده روی Jiraِ واقعی: /board/{id}/issue روی بُردِ kanban subQuery را
اعمال نمی‌کند؛ برای همین عضویت با search و «filter = … AND (subQuery)» سنجیده می‌شود.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.task_sources.jira.client import API, JiraClient
from src.task_sources.jira.errors import JiraResponseError

AGILE = "/rest/agile/1.0"
BOARD_PAGE_SIZE = 50
SEARCH_PAGE_SIZE = 100
BOARD_SCRUM = "scrum"
BOARD_KANBAN = "kanban"


@dataclass(frozen=True)
class Board:
    id: int
    name: str
    type: str
    project_key: str = ""


@dataclass(frozen=True)
class BoardColumn:
    name: str
    status_ids: frozenset[str]


@dataclass(frozen=True)
class BoardConfiguration:
    board_id: int
    name: str
    type: str
    filter_id: str
    sub_query: str
    columns: tuple[BoardColumn, ...]


@dataclass(frozen=True)
class Sprint:
    id: int
    name: str
    state: str


def _dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JiraResponseError(f"Jira returned a malformed {what}.")
    return value


def list_boards(client: JiraClient, *, name: str = "", project: str = "") -> list[Board]:
    """بُردهایی که این حساب می‌بیند؛ name و project فیلترِ سمتِ سرورند."""
    params: dict[str, Any] = {"maxResults": BOARD_PAGE_SIZE}
    if name:
        params["name"] = name
    if project:
        params["projectKeyOrId"] = project
    boards: list[Board] = []
    while True:
        body = _dict(client.get_json(f"{AGILE}/board", startAt=len(boards), **params), "board page")
        values = body.get("values")
        if not isinstance(values, list):
            raise JiraResponseError("Jira returned a malformed board page.")
        for item in values:
            location = item.get("location") or {}
            boards.append(
                Board(
                    id=int(item["id"]),
                    name=str(item.get("name") or ""),
                    type=str(item.get("type") or ""),
                    project_key=str(location.get("projectKey") or ""),
                )
            )
        if body.get("isLast", True) or not values:
            return boards


def board_configuration(client: JiraClient, board_id: int) -> BoardConfiguration:
    body = _dict(client.get_json(f"{AGILE}/board/{board_id}/configuration"), "board configuration")
    columns_raw = (body.get("columnConfig") or {}).get("columns")
    filter_id = (body.get("filter") or {}).get("id")
    if not isinstance(columns_raw, list) or not filter_id:
        raise JiraResponseError(
            f"Jira returned a board configuration without columns or filter for board {board_id}."
        )
    columns = tuple(
        BoardColumn(
            name=str(column.get("name") or ""),
            status_ids=frozenset(str(status["id"]) for status in column.get("statuses") or []),
        )
        for column in columns_raw
    )
    return BoardConfiguration(
        board_id=int(body.get("id") or board_id),
        name=str(body.get("name") or ""),
        type=str(body.get("type") or ""),
        filter_id=str(filter_id),
        sub_query=str((body.get("subQuery") or {}).get("query") or "").strip(),
        columns=columns,
    )


def active_sprints(client: JiraClient, board_id: int) -> list[Sprint]:
    sprints: list[Sprint] = []
    while True:
        body = _dict(
            client.get_json(
                f"{AGILE}/board/{board_id}/sprint",
                state="active",
                startAt=len(sprints),
                maxResults=BOARD_PAGE_SIZE,
            ),
            "sprint page",
        )
        values = body.get("values")
        if not isinstance(values, list):
            raise JiraResponseError(f"Jira returned a malformed sprint page for board {board_id}.")
        sprints += [
            Sprint(
                id=int(item["id"]),
                name=str(item.get("name") or ""),
                state=str(item.get("state") or ""),
            )
            for item in values
        ]
        if body.get("isLast", True) or not values:
            return sprints


def status_ids(client: JiraClient, name: str) -> frozenset[str]:
    """شناسه‌ی همه‌ی statusهایی که نامشان دقیقاً name است (حساس به حروف)."""
    body = client.get_json(f"{API}/status")
    if not isinstance(body, list):
        raise JiraResponseError("Jira returned a malformed status list.")
    return frozenset(str(item["id"]) for item in body if item.get("name") == name)


def search_issues(
    client: JiraClient, jql: str, fields: str, *, page_size: int = SEARCH_PAGE_SIZE
) -> list[dict[str, Any]]:
    """همه‌ی issueهای JQL، صفحه به صفحه تا total."""
    collected: list[dict[str, Any]] = []
    while True:
        body = _dict(
            client.get_json(
                f"{API}/search",
                jql=jql,
                fields=fields,
                startAt=len(collected),
                maxResults=page_size,
            ),
            "search page",
        )
        issues, total = body.get("issues"), body.get("total")
        if not isinstance(issues, list) or not isinstance(total, int):
            raise JiraResponseError("Jira returned a malformed search page.")
        collected += issues
        if len(collected) >= total:
            return collected
        if not issues:
            raise JiraResponseError(
                f"Jira reported {total} issues for the search but returned only "
                f"{len(collected)}; the result is incomplete."
            )


__all__ = [
    "BOARD_KANBAN",
    "BOARD_SCRUM",
    "Board",
    "BoardColumn",
    "BoardConfiguration",
    "Sprint",
    "active_sprints",
    "board_configuration",
    "list_boards",
    "search_issues",
    "status_ids",
]
