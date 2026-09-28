"""
code_review/sonar/agent.py — ایجنت اتصال به SonarQube (Community Edition)

محدودیت مهم CE:
    SonarQube Community Edition از branch/PR analysis پشتیبانی نمی‌کند
    (این قابلیت مخصوص Developer Edition به بالاست). برای دور زدن این
    محدودیت، هر MR با یک project key موقت (ephemeral) اسکن می‌شود:
        {SONAR_PROJECT_PREFIX}-{mr_iid}
    بعد از دریافت issue ها، اگر SONAR_DELETE_PROJECT_AFTER_SCAN فعال باشد،
    پروژه موقت از سرور Sonar حذف می‌شود.

حالت اسکن (scan_mode):
    اسکن همیشه روی کل کد پروژه (برنچ MR) انجام می‌شود — چون محدود کردن
    ورودی scanner می‌تواند دقت تحلیل semantic را خراب کند. تفاوت دو حالت
    فقط در issue‌هایی است که در نهایت گزارش می‌شوند:
      - "full" : همه issue‌های باز پروژه گزارش می‌شوند
      - "diff" : فقط issue‌هایی که روی خطوط واقعاً افزوده/تغییرکرده در این
                 MR هستند گزارش می‌شوند (با پارس diff — نگاه کنید به
                 diff_utils.parse_added_lines)
    این تفکیک برای جلوگیری از جریمه شدن پروژه‌های قدیمی (که هرگز با Sonar
    بررسی نشده‌اند) با بدهی فنی preexisting لازم است.

جریان کار:
    ۱. clone سطحی (shallow) برنچ مبدا MR در یک پوشه موقت
    ۲. اجرای sonar-scanner (subprocess) با project key موقت
    ۳. خواندن ceTaskId از .scannerwork/report-task.txt و poll کردن
       api/ce/task تا status=SUCCESS (یا FAILED/CANCELED/timeout)
    ۴. دریافت issue ها از api/issues/search و تبدیل به ReviewComment
    ۵. اگر scan_mode="diff": فیلتر issue‌ها بر اساس خطوط تغییرکرده در diff
    ۶. (اختیاری) حذف پروژه موقت از سرور Sonar

نیازمند:
    - باینری sonar-scanner در PATH (یا SONAR_SCANNER_BIN)
    - باینری git در PATH
    - دسترسی HTTPS clone به GITLAB_URL با GITLAB_TOKEN
"""

import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from langchain_core.messages import AIMessage
from src.agents.code_review.state import CodeReviewState, ReviewComment
from src.agents.code_review.gitlab.agent import GitLabClient
from src.agents.code_review.sonar.diff_utils import parse_added_lines
from src.config import (
    GITLAB_TOKEN,
    SONAR_URL, SONAR_TOKEN, SONAR_SCANNER_BIN, SONAR_PROJECT_PREFIX,
    SONAR_POLL_INTERVAL_SECONDS, SONAR_POLL_TIMEOUT_SECONDS,
    SONAR_DELETE_PROJECT_AFTER_SCAN, SONAR_SCAN_MODE,
)
from src.debug.config import DebugConfig


# نگاشت severity سونار → severity پروژه (هم‌راستا با SEVERITY_SCORES در scoring/scorer.py)
_SEVERITY_MAP: dict[str, str] = {
    "BLOCKER":  "critical",
    "CRITICAL": "major",
    "MAJOR":    "major",
    "MINOR":    "minor",
    "INFO":     "suggestion",
}

# نگاشت type سونار → category پروژه
_CATEGORY_MAP: dict[str, str] = {
    "VULNERABILITY":    "security",
    "SECURITY_HOTSPOT": "security",
    "BUG":               "correctness",
    "CODE_SMELL":         "style",
}

_VALID_SCAN_MODES = ("full", "diff")


class SonarScanError(RuntimeError):
    """خطای اجرای اسکن سونار (clone، scanner، یا timeout در poll)."""


class SonarClient:
    """کلاینت ساده برای SonarQube Web API (Community Edition).

    پارامترها:
        base_url : آدرس سرور Sonar (پیش‌فرض: SONAR_URL از .env)
        token    : توکن احراز هویت — به‌عنوان HTTP Basic username پاس می‌شود
    """

    def __init__(self, base_url: str = SONAR_URL, token: str = SONAR_TOKEN,
                 debug_config: DebugConfig | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._auth = (token, "")
        self._log = (debug_config or DebugConfig.off()).get_logger("sonar_client")

    def poll_task(self, task_id: str, interval: int = SONAR_POLL_INTERVAL_SECONDS,
                  timeout: int = SONAR_POLL_TIMEOUT_SECONDS) -> dict:
        """تا رسیدن task به یک وضعیت نهایی (SUCCESS/FAILED/CANCELED) صبر می‌کند."""
        self._log.info("start poll sonar ce task", task_id=task_id)
        elapsed = 0
        while elapsed < timeout:
            resp = requests.get(f"{self._base}/api/ce/task", params={"id": task_id},
                                auth=self._auth, timeout=30)
            resp.raise_for_status()
            task = resp.json()["task"]
            status = task.get("status")
            if status in ("SUCCESS", "FAILED", "CANCELED"):
                self._log.debug("finish poll sonar ce task", status=status)
                return task
            time.sleep(interval)
            elapsed += interval

        raise SonarScanError(f"Sonar CE task {task_id} در {timeout} ثانیه به پایان نرسید (timeout)")

    def fetch_issues(self, project_key: str) -> list[dict]:
        """همه issue های باز یک پروژه را (با pagination) برمی‌گرداند."""
        issues: list[dict] = []
        page = 1
        while True:
            self._log.info("start fetch sonar issues", project=project_key, page=page)
            resp = requests.get(
                f"{self._base}/api/issues/search",
                params={"componentKeys": project_key, "resolved": "false", "ps": 100, "p": page},
                auth=self._auth, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            issues.extend(data.get("issues", []))
            if page * 100 >= data.get("total", 0):
                break
            page += 1

        self._log.debug("finish fetch sonar issues", count=len(issues))
        return issues

    def delete_project(self, project_key: str) -> None:
        """پروژه موقت را از سرور Sonar حذف می‌کند (best-effort — خطا را نادیده می‌گیرد)."""
        try:
            self._log.info("start delete sonar project", project=project_key)
            requests.post(f"{self._base}/api/projects/delete", params={"project": project_key},
                          auth=self._auth, timeout=30).raise_for_status()
            self._log.debug("finish delete sonar project")
        except Exception as e:
            self._log.warning("حذف پروژه موقت سونار ناموفق بود", project=project_key, error=str(e))


def _clone_branch(repo_url: str, branch: str, dest: Path, debug_config: DebugConfig | None = None) -> None:
    """یک shallow clone از یک برنچ مشخص می‌گیرد."""
    log = (debug_config or DebugConfig.off()).get_logger("sonar_git_clone")
    log.info("start clone", branch=branch)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(dest)],
            check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        raise SonarScanError(f"git clone برنچ '{branch}' ناموفق بود: {e.stderr}") from e
    log.debug("finish clone")


def _parse_task_id(scannerwork_dir: Path) -> str:
    """ceTaskId را از فایل report-task.txt (تولیدشده توسط sonar-scanner) استخراج می‌کند."""
    report_file = scannerwork_dir / "report-task.txt"
    if not report_file.exists():
        raise SonarScanError(f"report-task.txt پیدا نشد: {report_file}")

    match = re.search(r"^ceTaskId=(.+)$", report_file.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SonarScanError("ceTaskId در report-task.txt پیدا نشد")
    return match.group(1).strip()


def _run_scanner(project_dir: Path, project_key: str, scanner_bin: str = SONAR_SCANNER_BIN) -> Path:
    """sonar-scanner را روی project_dir اجرا می‌کند و مسیر .scannerwork را برمی‌گرداند.

    توجه: sonar.sources همیشه '.' (کل پروژه) است، صرف‌نظر از scan_mode —
    فیلتر بر اساس diff بعد از دریافت issue‌ها انجام می‌شود، نه در ورودی
    scanner (نگاه کنید به docstring بالای فایل).
    """
    try:
        subprocess.run(
            [
                scanner_bin,
                f"-Dsonar.host.url={SONAR_URL}",
                f"-Dsonar.token={SONAR_TOKEN}",
                f"-Dsonar.projectKey={project_key}",
                f"-Dsonar.projectBaseDir={project_dir}",
                "-Dsonar.sources=.",
            ],
            check=True, capture_output=True, text=True, timeout=1800, cwd=str(project_dir),
        )
    except subprocess.CalledProcessError as e:
        raise SonarScanError(f"اجرای sonar-scanner ناموفق بود: {e.stderr[-2000:]}") from e
    except FileNotFoundError as e:
        raise SonarScanError(
            f"باینری '{scanner_bin}' پیدا نشد — sonar-scanner را نصب یا SONAR_SCANNER_BIN را تنظیم کنید"
        ) from e

    return project_dir / ".scannerwork"


def _map_issue(issue: dict) -> ReviewComment:
    """یک issue خام Sonar را به فرمت ReviewComment پروژه تبدیل می‌کند."""
    text_range = issue.get("textRange") or {}
    return ReviewComment(
        file_path=issue.get("component", "").split(":", 1)[-1],
        line=text_range.get("startLine"),
        severity=_SEVERITY_MAP.get(issue.get("severity", "MINOR"), "minor"),
        category=_CATEGORY_MAP.get(issue.get("type", "CODE_SMELL"), "general"),
        body=f"[Sonar] {issue.get('message', '')} (rule: {issue.get('rule', '')})",
        source="sonar",
    )


def _filter_to_diff(comments: list[ReviewComment], added_lines: dict[str, set[int]]) -> list[ReviewComment]:
    """فقط issue‌هایی را نگه می‌دارد که فایلشان در diff تغییر کرده و خطشان
    (در صورت وجود) واقعاً افزوده/ویرایش شده باشد.

    issue‌های فایل‌سطح (line=None) فقط وقتی نگه داشته می‌شوند که خود فایل
    در diff حضور داشته باشد؛ در غیر این صورت (فایل کاملاً دست‌نخورده) حذف
    می‌شوند تا بدهی فنی preexisting امتیاز را جریمه نکند.
    """
    filtered: list[ReviewComment] = []
    for c in comments:
        file_lines = added_lines.get(c["file_path"])
        if file_lines is None:
            continue
        if c["line"] is None or c["line"] in file_lines:
            filtered.append(c)
    return filtered


class SonarAnalyzerAgent:
    """node گراف کد ریویو — کد MR را با SonarQube اسکن و issue ها را برمی‌گرداند.

    خروجی در state["sonar_issues"] نوشته می‌شود (نه review_comments مستقیم)
    تا با خروجی موازی CodeReviewerAgent تداخل نداشته باشد — ترکیب نهایی
    توسط MergeFindingsNode انجام می‌شود.

    پارامترها:
        debug_config : تنظیمات لاگ
        sonar_client : برای تست/override — نمونه SonarClient آماده
        scan_mode    : "full" (همه issue‌ها) یا "diff" (فقط خطوط تغییرکرده
                       در این MR) — پیش‌فرض از SONAR_SCAN_MODE در .env
    """

    name = "sonar_analyzer"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        sonar_client: SonarClient | None = None,
        scan_mode: str = SONAR_SCAN_MODE,
    ) -> None:
        if scan_mode not in _VALID_SCAN_MODES:
            raise ValueError(f"scan_mode نامعتبر: '{scan_mode}'. مقادیر مجاز: {_VALID_SCAN_MODES}")

        self._gl = GitLabClient(debug_config=debug_config)
        self._sonar = sonar_client or SonarClient(debug_config=debug_config)
        self._scan_mode = scan_mode
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._debug_config = debug_config

    def _build_repo_url(self) -> str:
        """آدرس HTTPS clone را با توکن احراز هویت GitLab می‌سازد."""
        project = self._gl.get_project()
        http_url = project.get("http_url_to_repo", "")
        if not http_url.startswith("https://"):
            raise SonarScanError(f"http_url_to_repo نامعتبر یا غیر-https است: {http_url}")
        return http_url.replace("https://", f"https://oauth2:{GITLAB_TOKEN}@", 1)

    def __call__(self, state: CodeReviewState) -> dict:
        mr_iid = state["mr_iid"]
        branch = state.get("mr_source_branch", "")

        if not branch:
            self._log.warning("mr_source_branch خالی است — اسکن سونار رد شد", mr_iid=mr_iid)
            return {
                "sonar_issues": [],
                "sonar_enabled": True,
                "sonar_scan_mode": self._scan_mode,
                "messages": [AIMessage(
                    content="اسکن Sonar رد شد: برنچ مبدا MR در دسترس نبود", name=self.name,
                )],
            }

        project_key = f"{SONAR_PROJECT_PREFIX}-{mr_iid}"
        self._log.info("شروع اسکن Sonar", mr_iid=mr_iid, project_key=project_key,
                       branch=branch, scan_mode=self._scan_mode)
        sonar_issues: list[ReviewComment] = []

        with tempfile.TemporaryDirectory(prefix=f"sonar-mr-{mr_iid}-") as tmp:
            project_dir = Path(tmp)
            try:
                repo_url = self._build_repo_url()
                _clone_branch(repo_url, branch, project_dir, self._debug_config)

                scannerwork_dir = _run_scanner(project_dir, project_key)
                task_id = _parse_task_id(scannerwork_dir)
                task = self._sonar.poll_task(task_id)

                if task.get("status") != "SUCCESS":
                    raise SonarScanError(f"وضعیت نهایی Sonar task: {task.get('status')}")

                raw_issues = self._sonar.fetch_issues(project_key)
                sonar_issues = [_map_issue(i) for i in raw_issues]
                total_found = len(sonar_issues)

                if self._scan_mode == "diff":
                    added_lines = parse_added_lines(state.get("diff", ""))
                    sonar_issues = _filter_to_diff(sonar_issues, added_lines)
                    self._log.info(
                        "فیلتر diff اعمال شد", mr_iid=mr_iid,
                        total_found=total_found, after_filter=len(sonar_issues),
                    )

                self._log.info("اسکن Sonar کامل شد", mr_iid=mr_iid, issue_count=len(sonar_issues))

            except SonarScanError as e:
                self._log.error("اسکن Sonar با خطا مواجه شد", mr_iid=mr_iid, error=str(e))
                return {
                    "sonar_issues": [],
                    "sonar_enabled": True,
                    "sonar_scan_mode": self._scan_mode,
                    "messages": [AIMessage(
                        content=f"اسکن Sonar ناموفق بود: {e}", name=self.name,
                    )],
                }

            finally:
                if SONAR_DELETE_PROJECT_AFTER_SCAN:
                    self._sonar.delete_project(project_key)

        return {
            "sonar_issues": sonar_issues,
            "sonar_enabled": True,
            "sonar_scan_mode": self._scan_mode,
            "messages": [AIMessage(
                content=f"اسکن Sonar کامل شد ({self._scan_mode}) — {len(sonar_issues)} issue پیدا شد",
                name=self.name,
            )],
        }