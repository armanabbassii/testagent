"""
code_review/gitlab/agent.py — ایجنت GitLab

GitLabClient  : کلاینت REST API
GitLabFetcherAgent   : دریافت diff، اطلاعات MR، و fingerprint کامنت‌های موجود
GitLabCommenterAgent : ثبت کامنت‌های جدید (با dedup) + کامنت امتیاز
"""

import hashlib
import requests
from datetime import datetime, timezone
from langchain_core.messages import AIMessage
from src.agents.code_review.state import CodeReviewState, ReviewComment
from src.config import GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECT_ID
from src.debug.config import DebugConfig


# ── helpers ───────────────────────────────────────────────────────────────────

def _comment_fingerprint(file_path: str, line: int | None, body_prefix: str) -> str:
    """یک fingerprint یکتا برای یک کامنت می‌سازد.

    از file_path + line + ۸۰ کاراکتر اول body استفاده می‌کند.
    اگر همین ترکیب قبلاً ثبت شده باشد، کامنت تکراری است.
    """
    key = f"{file_path}::{line}::{body_prefix[:80]}"
    return hashlib.sha1(key.encode()).hexdigest()


# ── GitLab Client ─────────────────────────────────────────────────────────────

class GitLabClient:
    """کلاینت ساده برای GitLab REST API."""
    name = "gitlab_client"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        self._base = f"{GITLAB_URL.rstrip('/')}/api/v4"
        self._headers = {
            "PRIVATE-TOKEN": GITLAB_TOKEN,
            "Content-Type": "application/json",
        }
        self._project_id = GITLAB_PROJECT_ID
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self._base}/projects/{self._project_id}/{path}"
        resp = requests.get(url, headers=self._headers, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self._base}/projects/{self._project_id}/{path}"
        resp = requests.post(url, headers=self._headers, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_mr(self, mr_iid: int) -> dict:
        self._log.info("start get merge request info", mr_iid=mr_iid)
        mr = self._get(f"merge_requests/{mr_iid}")
        self._log.debug("finish get merge request info", mr_iid=mr_iid)
        return mr

    def get_mr_diff(self, mr_iid: int) -> str:
        """diff تمام فایل‌های تغییریافته را به صورت یک رشته برمی‌گرداند."""
        changes = self._get(f"merge_requests/{mr_iid}/changes")
        parts = []
        self._log.info("start get diff merge request", mr_iid=mr_iid)    
        for change in changes.get("changes", []):
            parts.append(f"### {change['new_path']}\n{change.get('diff', '')}")
        
        self._log.debug("finish get diff merge request", mr_iid=mr_iid)
        return "\n\n".join(parts)

    def get_mr_notes(self, mr_iid: int) -> list[dict]:
        """همه کامنت‌های (notes) موجود روی MR را برمی‌گرداند."""
        notes = []
        page = 1
        while True:
            self._log.info("start get notes", mr_iid=mr_iid, page = page)    
            batch = self._get(f"merge_requests/{mr_iid}/notes", params={"per_page": 100, "page": page})
            self._log.debug("finish get notes", mr_iid=mr_iid, page = page)    
            if not batch:
                break
            notes.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return notes

    def get_mr_discussions(self, mr_iid: int) -> list[dict]:
        """همه discussion های inline موجود را برمی‌گرداند."""
        discussions = []
        page = 1
        while True:
            self._log.info("start get discussions", mr_iid=mr_iid, page = page)    
            batch = self._get(f"merge_requests/{mr_iid}/discussions", params={"per_page": 100, "page": page})
            self._log.debug("finish get discussions", mr_iid=mr_iid, page = page)    
            if not batch:
                break
            discussions.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return discussions

    def post_inline_comment(self, mr_iid: int, comment: ReviewComment, base_sha: str, head_sha: str, start_sha: str) -> None:
        """یک کامنت inline روی یک خط خاص ثبت می‌کند."""
        body: dict = {
            "body": f"**[{comment['severity'].upper()}]** {comment['body']}",
            "position": {
                "position_type": "text",
                "base_sha": base_sha,
                "head_sha": head_sha,
                "start_sha": start_sha,
                "new_path": comment["file_path"],
                "old_path": comment["file_path"],
            },
        }
        if comment.get("line"):
            body["position"]["new_line"] = comment["line"]

        self._log.info("start post inline comment", mr_iid=mr_iid)
        self._post(f"merge_requests/{mr_iid}/discussions", body)
        self._log.debug("finish post inline comment", mr_iid=mr_iid)

    def post_comment(self, mr_iid: int, body: str) -> None:
        """یک کامنت کلی روی MR ثبت می‌کند."""
        self._log.info("start post comment", mr_iid=mr_iid)
        self._post(f"merge_requests/{mr_iid}/notes", {"body": body})
        self._log.debug("finish post comment", mr_iid=mr_iid)

    def approve_mr(self, mr_iid: int) -> None:
        self._log.info("start approve merge request`", mr_iid=mr_iid)
        self._post(f"merge_requests/{mr_iid}/approve", {})
        self._log.debug("start approve merge request`", mr_iid=mr_iid)

    def unapprove_mr(self, mr_iid: int) -> None:
        self._log.info("start unapprove merge request", mr_iid=mr_iid)
        self._post(f"merge_requests/{mr_iid}/unapprove", {})
        self._log.debug("start unapprove merge request", mr_iid=mr_iid)

    def get_project(self) -> dict:
        """اطلاعات پروژه (شامل http_url_to_repo) را برمی‌گرداند — برای clone در SonarAnalyzerAgent."""
        self._log.info("start get project info")
        url = f"{self._base}/projects/{self._project_id}"
        resp = requests.get(url, headers=self._headers, timeout=30)
        resp.raise_for_status()
        self._log.debug("finish get project info")
        return resp.json()


# ── Fetcher ───────────────────────────────────────────────────────────────────

class GitLabFetcherAgent:
    """اطلاعات و diff MR را دریافت می‌کند و fingerprint کامنت‌های موجود را می‌سازد."""

    name = "gitlab_fetcher"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        self._gl = GitLabClient(debug_config=debug_config)
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def _build_existing_fingerprints(self, mr_iid: int) -> list[str]:
        """fingerprint همه کامنت‌های موجود روی MR را می‌سازد."""
        fingerprints: list[str] = []

        # کامنت‌های general (notes)
        for note in self._gl.get_mr_notes(mr_iid):
            body = note.get("body", "")
            fingerprints.append(_comment_fingerprint("__note__", None, body))

        # کامنت‌های inline (discussions)
        for discussion in self._gl.get_mr_discussions(mr_iid):
            for note in discussion.get("notes", []):
                pos = note.get("position") or {}
                file_path = pos.get("new_path", "__note__")
                line = pos.get("new_line")
                body = note.get("body", "")
                fingerprints.append(_comment_fingerprint(file_path, line, body))

        return fingerprints

    def __call__(self, state: CodeReviewState) -> dict:
        mr_iid = state["mr_iid"]

        mr = self._gl.get_mr(mr_iid)
        diff = self._gl.get_mr_diff(mr_iid)
        existing = self._build_existing_fingerprints(mr_iid)

        return {
            "mr_title": mr.get("title", ""),
            "mr_description": mr.get("description", "") or "",
            "mr_source_branch": mr.get("source_branch", ""),
            "diff": diff,
            "existing_comments": existing,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "messages": [AIMessage(
                content=(
                    f"MR #{mr_iid} دریافت شد: {mr.get('title')} | "
                    f"{len(diff)} کاراکتر diff | "
                    f"{len(existing)} کامنت موجود"
                ),
                name=self.name,
            )],
        }


# ── Commenter ─────────────────────────────────────────────────────────────────

class GitLabCommenterAgent:
    """کامنت‌های جدید را ثبت می‌کند (dedup) و کامنت امتیاز را پست می‌کند."""

    name = "gitlab_commenter"

    def __init__(self, score_output_path: str = "review_scores.jsonl", debug_config: DebugConfig | None = None) -> None:
        self._gl = GitLabClient(debug_config=debug_config)
        self._score_path = score_output_path
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def __call__(self, state: CodeReviewState) -> dict:
        from src.agents.code_review.scoring import save_score, format_score_comment

        mr_iid = state["mr_iid"]
        comments = state.get("review_comments", [])
        existing_fps = set(state.get("existing_comments", []))

        # دریافت SHA های لازم برای inline comment
        mr_info = self._gl.get_mr(mr_iid)
        diff_refs = mr_info.get("diff_refs", {})
        base_sha  = diff_refs.get("base_sha", "")
        head_sha  = diff_refs.get("head_sha", "")
        start_sha = diff_refs.get("start_sha", "")

        posted = skipped = errors = 0

        for comment in comments:
            # ساخت fingerprint برای این کامنت
            body_text = f"**[{comment['severity'].upper()}]** {comment['body']}"
            if comment.get("line") and base_sha:
                fp = _comment_fingerprint(comment["file_path"], comment["line"], body_text)
            else:
                fp = _comment_fingerprint("__note__", None, body_text)

            # بررسی تکراری بودن
            if fp in existing_fps:
                skipped += 1
                continue

            try:
                if comment.get("line") and base_sha:
                    self._gl.post_inline_comment(mr_iid, comment, base_sha, head_sha, start_sha)
                else:
                    # کامنت کلی اگر خط نداشت یا SHA در دسترس نبود
                    self._gl.post_comment(
                        mr_iid,
                        f"**[{comment['severity'].upper()}]** `{comment['file_path']}`\n\n{comment['body']}",
                    )
                existing_fps.add(fp)
                posted += 1
            except Exception:
                errors += 1

        # ── محاسبه و ذخیره امتیاز ────────────────────────────────────────────
        score_record = save_score(
            project_id=self._gl._project_id,
            mr_iid=mr_iid,
            user_id=state["user_id"],
            comments=comments,
            created_at=state.get("created_at", ""),
            output_path=self._score_path,
            sonar_enabled=state.get("sonar_enabled", False),
            sonar_scan_mode=state.get("sonar_scan_mode"),
        )

        # ثبت کامنت امتیاز روی MR
        self._gl.post_comment(mr_iid, format_score_comment(score_record))

        return {
            "score_record": score_record,
            "messages": [AIMessage(
                content=(
                    f"{posted} کامنت ثبت شد | "
                    f"{skipped} تکراری رد شد | "
                    f"{errors} خطا | "
                    f"امتیاز: {score_record['total_score']}"
                ),
                name=self.name,
            )],
        }