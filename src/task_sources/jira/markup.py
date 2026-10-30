"""
src/task_sources/jira/markup.py — آماده‌سازیِ متنِ wiki markupِ Jira Server

Jira Server توضیح و کامنت را با wiki markup برمی‌گرداند. این‌جا markup عمداً
به Markdown ترجمه نمی‌شود — LLM آن را می‌فهمد و ترجمه‌ی ناقص معنا را عوض
می‌کند. فقط دو کار انجام می‌شود:

  * بلوک‌های {code}/{code:json}/{noformat} به fence تبدیل می‌شوند و محتوایشان
    دست‌نخورده می‌ماند (جز پایانِ خط) — نمونه‌ی cURL یا JSON نباید عوض شود.
  * بیرون از آن بلوک‌ها همان پاک‌سازیِ عمومی (CRLF، NBSP، …) اجرا می‌شود.
"""

from __future__ import annotations

import re

from src.task_sources.text import fence_block, normalize_newlines, normalize_text

_BLOCK = re.compile(r"\{(code|noformat)(?::([^}]*))?\}(.*?)\{\1\}", re.DOTALL)


def _language(parameters: str | None) -> str:
    """«json» یا «language=json|title=…» → «json»."""
    for part in (parameters or "").split("|"):
        key, _, value = part.partition("=")
        if not value and key.strip():
            return key.strip().lower()
        if key.strip().lower() == "language" and value.strip():
            return value.strip().lower()
    return ""


def normalize_wiki(text: str) -> str:
    """متنِ wikiِ Jira → متنِ تمیز با بلوک‌های کدِ دست‌نخورده‌ی fenced."""
    text = normalize_newlines(text or "")
    parts: list[str] = []
    position = 0
    for match in _BLOCK.finditer(text):
        parts.append(normalize_text(text[position : match.start()]))
        kind, parameters, body = match.groups()
        language = _language(parameters) if kind == "code" else ""
        parts.append(fence_block(body, language))
        position = match.end()
    parts.append(normalize_text(text[position:]))
    return "\n\n".join(part for part in parts if part)


__all__ = ["normalize_wiki"]
