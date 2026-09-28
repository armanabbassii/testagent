"""
code_review/sonar/diff_utils.py — استخراج خطوط افزوده‌شده از diff یکپارچه GitLab

چرا این ماژول لازم است:
    برای scan_mode="diff": فقط issue‌های سونار که روی خطوط واقعاً تغییر کرده
    (نه کل تاریخچه فایل) باشند نگه داشته می‌شوند — تا پروژه‌های قدیمی که هرگز
    با Sonar بررسی نشده‌اند، امتیاز کاربر را با بدهی فنی preexisting جریمه
    نکنند. اسکن Sonar همچنان روی کل پروژه انجام می‌شود (برای دقت تحلیل
    semantic)؛ این ماژول فقط issue‌های خروجی را بعد از دریافت فیلتر می‌کند.

فرمت diff ورودی: خروجی GitLabClient.get_mr_diff که هر فایل را با
"### {file_path}\n{unified diff hunks}" از هم جدا کرده است. هر hunk با
یک هدر '@@ -a,b +c,d @@' شروع می‌شود؛ شماره خط در نسخه جدید از c شروع
می‌شود و برای هر خط '+' یا context (بدون پیشوند) افزایش می‌یابد، برای
خط '-' افزایش نمی‌یابد (چون در نسخه جدید وجود ندارد).
"""

import re

_FILE_HEADER_RE = re.compile(r"^### (.+)$", re.MULTILINE)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_added_lines(diff_text: str) -> dict[str, set[int]]:
    """خطوط افزوده/تغییرکرده هر فایل را از یک diff یکپارچه استخراج می‌کند.

    برمی‌گرداند:
        {file_path: {شماره خطوطی که در نسخه جدید افزوده یا ویرایش شده‌اند}}

    فقط خطوط '+' (نه '-' و نه context) به‌عنوان «تغییر کرده» شمرده می‌شوند —
    این‌ها همان خطوطی هستند که در نسخه جدید فایل واقعاً نوشته/ویرایش شده‌اند.
    """
    if not diff_text:
        return {}

    result: dict[str, set[int]] = {}
    for file_path, section in _split_by_file(diff_text).items():
        lines = _parse_file_hunks(section)
        if lines:
            result[file_path] = lines

    return result


def _split_by_file(diff_text: str) -> dict[str, str]:
    """diff یکپارچه (خروجی چند فایل کنار هم) را بر اساس هدر '### file' جدا می‌کند."""
    matches = list(_FILE_HEADER_RE.finditer(diff_text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        file_path = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        sections[file_path] = diff_text[start:end]
    return sections


def _parse_file_hunks(section: str) -> set[int]:
    """خطوط افزوده‌شده یک فایل را از تمام hunk های آن استخراج می‌کند."""
    added: set[int] = set()
    current_line = 0
    in_hunk = False

    for raw_line in section.splitlines():
        hunk_match = _HUNK_HEADER_RE.match(raw_line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if raw_line.startswith("+"):
            added.add(current_line)
            current_line += 1
        elif raw_line.startswith("-"):
            pass  # خط فقط در نسخه قدیم بوده — شماره خط جدید افزایش نمی‌یابد
        else:
            current_line += 1  # خط context — در هر دو نسخه هست

    return added