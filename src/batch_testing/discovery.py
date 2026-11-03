"""
src/batch_testing/discovery.py — کشفِ issueهای واجدِ شرایطِ یک بُرد (فقط‌خواندنی)

issue واجدِ شرایط است فقط اگر هر سه شرط برقرار باشد (AND):

    1. status — statusِ آن دقیقاً «Manual Test» است؛
    2. column — issue در ستونِ «QC TEST»ِ همین بُرد است: issue در بُرد است (فیلترِ
       بُرد + subQueryِ kanban) و statusش در پیکربندیِ بُرد
       (/board/{id}/configuration) به آن ستون نگاشت شده (نامِ ستون بدونِ حساسیت به
       حروف و فاصله: «QC Test» هم قبول است)؛
    3. label — برچسبِ دقیقِ «backend» را دارد (حساس به حروف).

ستونِ یک issue در Jira فقط از نگاشتِ status→ستونِ پیکربندیِ بُرد می‌آید و به
اسپرینت بستگی ندارد؛ اسپرینت فقط تعیین می‌کند نمای «اسپرینتِ فعال»ِ بُردِ scrum
issue را نشان بدهد یا نه. برای همین عضویت در اسپرینتِ فعال (in_active_sprint) فقط
اطلاعاتی است و هیچ‌گاه issue را رد نمی‌کند — issueِ backlog، اسپرینتِ آینده یا
بُردِ scrumِ بی‌اسپرینتِ فعال، با برقراریِ سه شرط واجدِ شرایط است.

هر دلیلِ رد با نامِ شرطش (failed_criteria، هم‌ترتیب با reasons) ثبت می‌شود.

JQL فقط مجموعه را کوچک می‌کند و برچسب را عمداً شرط نمی‌گذارد تا issueهای
کنارگذاشته با دلیل برگردند؛ تصمیمِ نهایی با evaluate است.
هیچ چیزی در Jira نوشته نمی‌شود.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.task_sources.jira.agile import (
    BOARD_KANBAN,
    BOARD_SCRUM,
    active_sprints,
    board_configuration,
    search_issues,
    status_ids,
)
from src.task_sources.jira.client import JiraClient

SEARCH_FIELDS = "summary,status,labels,issuetype,parent"


# نامِ سه شرطِ واجدِ شرایط بودن — برچسبِ هر دلیلِ رد
CRITERION_STATUS = "status"
CRITERION_COLUMN = "column"
CRITERION_LABEL = "label"
CRITERIA = (CRITERION_STATUS, CRITERION_COLUMN, CRITERION_LABEL)


@dataclass(frozen=True)
class EligibilityCriteria:
    column: str = "QC TEST"
    status: str = "Manual Test"
    label: str = "backend"


DEFAULT_CRITERIA = EligibilityCriteria()


class DiscoveryError(Exception):
    """بُرد برای این معیارها قابلِ سنجش نیست (ستون یا status وجود ندارد، نوعِ بُرد، …)."""


def normalize_name(text: str) -> str:
    """trim + فاصله‌های پشتِ‌سرِهم → یکی + casefold؛ برای مقایسه‌ی نامِ ستون."""
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class BoardContext:
    board_id: int
    board_name: str
    board_type: str
    filter_id: str
    sub_query: str
    column_name: str
    column_status_ids: frozenset[str]
    # statusهای «Manual Test» که در ستونِ هدف‌اند — همان چیزی که JQL می‌خواهد.
    target_status_ids: frozenset[str]
    # فقط scrum؛ برای kanban None.
    active_sprint_ids: tuple[int, ...] | None

    @property
    def is_scrum(self) -> bool:
        return self.board_type == BOARD_SCRUM


@dataclass(frozen=True)
class Candidate:
    key: str
    summary: str
    status_id: str
    status_name: str
    labels: tuple[str, ...]
    issue_type: str
    is_subtask: bool
    parent_key: str
    # فقط اطلاعاتی، نه شرط؛ None یعنی بُرد kanban است و اسپرینت معنا ندارد.
    in_active_sprint: bool | None
    reasons: tuple[str, ...]
    # نامِ شرطِ هر دلیل، هم‌ترتیب با reasons (status / column / label)
    failed_criteria: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class DiscoveryResult:
    context: BoardContext
    criteria: EligibilityCriteria
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> list[Candidate]:
        return [candidate for candidate in self.candidates if candidate.eligible]

    @property
    def excluded(self) -> list[Candidate]:
        return [candidate for candidate in self.candidates if not candidate.eligible]


# ── سنجش ─────────────────────────────────────────────────────────────────────
def evaluate(
    issue: dict[str, Any],
    context: BoardContext,
    criteria: EligibilityCriteria = DEFAULT_CRITERIA,
    *,
    in_active_sprint: bool | None,
) -> Candidate:
    """حکمِ نهایی برای یک issue و دلیلِ هر شرطِ برقرارنشده — بدونِ هیچ فراخوانیِ شبکه.

    in_active_sprint فقط در Candidate ثبت می‌شود و در حکم اثری ندارد.
    """
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    parent = fields.get("parent") or {}
    labels = tuple(str(label) for label in fields.get("labels") or [])
    status_id, status_name = str(status.get("id") or ""), str(status.get("name") or "")

    failures: list[tuple[str, str]] = []  # (شرط، دلیل) — حداکثر یکی برای هر شرط
    if status_name != criteria.status:
        failures.append((CRITERION_STATUS, f"status is '{status_name}', not '{criteria.status}'"))
    if status_id not in context.column_status_ids:
        failures.append(
            (
                CRITERION_COLUMN,
                f"status is not in the '{context.column_name}' column of this board",
            )
        )
    if criteria.label not in labels:
        variants = [label for label in labels if label.casefold() == criteria.label.casefold()]
        if variants:
            failures.append(
                (
                    CRITERION_LABEL,
                    f"label '{criteria.label}' is missing (has "
                    f"{', '.join(map(repr, variants))}; labels must match exactly)",
                )
            )
        else:
            failures.append((CRITERION_LABEL, f"label '{criteria.label}' is missing"))

    return Candidate(
        key=str(issue.get("key") or ""),
        summary=str(fields.get("summary") or ""),
        status_id=status_id,
        status_name=status_name,
        labels=labels,
        issue_type=str(issue_type.get("name") or ""),
        is_subtask=bool(issue_type.get("subtask")) or bool(parent),
        parent_key=str(parent.get("key") or ""),
        in_active_sprint=in_active_sprint if context.is_scrum else None,
        reasons=tuple(reason for _, reason in failures),
        failed_criteria=tuple(criterion for criterion, _ in failures),
    )


# ── بُرد ─────────────────────────────────────────────────────────────────────
def resolve_board(
    client: JiraClient, board_id: int, criteria: EligibilityCriteria = DEFAULT_CRITERIA
) -> BoardContext:
    config = board_configuration(client, board_id)
    if config.type not in (BOARD_SCRUM, BOARD_KANBAN):
        raise DiscoveryError(
            f"Board {board_id} is a '{config.type}' board; only scrum and kanban boards are supported."
        )

    wanted = normalize_name(criteria.column)
    matches = [column for column in config.columns if normalize_name(column.name) == wanted]
    if not matches:
        names = ", ".join(repr(column.name) for column in config.columns) or "none"
        raise DiscoveryError(
            f"Board '{config.name}' ({board_id}) has no '{criteria.column}' column "
            f"(columns: {names})."
        )
    if len(matches) > 1:
        raise DiscoveryError(
            f"Board '{config.name}' ({board_id}) has {len(matches)} columns matching "
            f"'{criteria.column}'; the target column is ambiguous."
        )
    column = matches[0]

    named = status_ids(client, criteria.status)
    if not named:
        raise DiscoveryError(f"Jira has no status named '{criteria.status}'.")
    target = column.status_ids & named
    if not target:
        raise DiscoveryError(
            f"The '{column.name}' column of board '{config.name}' ({board_id}) does not "
            f"contain the status '{criteria.status}', so no issue can be eligible."
        )

    sprints = (
        tuple(sprint.id for sprint in active_sprints(client, board_id))
        if config.type == BOARD_SCRUM
        else None
    )
    return BoardContext(
        board_id=config.board_id,
        board_name=config.name,
        board_type=config.type,
        filter_id=config.filter_id,
        sub_query=config.sub_query,
        column_name=column.name,
        column_status_ids=column.status_ids,
        target_status_ids=target,
        active_sprint_ids=sprints,
    )


def _ids(values) -> str:
    return ", ".join(sorted(map(str, values), key=lambda value: (len(value), value)))


def candidate_jql(context: BoardContext) -> str:
    """issueهای فیلترِ بُرد با statusِ هدف (+ subQueryِ kanban) — بدونِ شرطِ برچسب/اسپرینت."""
    jql = f"filter = {context.filter_id} AND status in ({_ids(context.target_status_ids)})"
    if context.board_type == BOARD_KANBAN and context.sub_query:
        jql += f" AND ({context.sub_query})"
    return jql


def sprint_jql(context: BoardContext) -> str:
    """عضویت در اسپرینتِ فعال — فقط برای نمایش، نه برای حکم."""
    return f"{candidate_jql(context)} AND sprint in ({_ids(context.active_sprint_ids or ())})"


def discover(
    client: JiraClient, board_id: int, criteria: EligibilityCriteria = DEFAULT_CRITERIA
) -> DiscoveryResult:
    """همه‌ی issueهای ستونِ هدف با حکم و دلیل؛ ترتیب بر اساسِ کلید، هر کلید یک بار."""
    context = resolve_board(client, board_id, criteria)
    issues = search_issues(client, f"{candidate_jql(context)} ORDER BY key ASC", SEARCH_FIELDS)

    in_sprint: set[str] = set()
    if context.is_scrum and context.active_sprint_ids:
        in_sprint = {
            str(issue.get("key"))
            for issue in search_issues(client, f"{sprint_jql(context)} ORDER BY key ASC", "status")
        }

    seen: set[str] = set()
    candidates: list[Candidate] = []
    for issue in issues:
        key = str(issue.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(
            evaluate(
                issue,
                context,
                criteria,
                in_active_sprint=(key in in_sprint) if context.is_scrum else None,
            )
        )
    return DiscoveryResult(context=context, criteria=criteria, candidates=tuple(candidates))


__all__ = [
    "CRITERIA",
    "CRITERION_COLUMN",
    "CRITERION_LABEL",
    "CRITERION_STATUS",
    "DEFAULT_CRITERIA",
    "SEARCH_FIELDS",
    "BoardContext",
    "Candidate",
    "DiscoveryError",
    "DiscoveryResult",
    "EligibilityCriteria",
    "candidate_jql",
    "discover",
    "evaluate",
    "normalize_name",
    "resolve_board",
    "sprint_jql",
]
