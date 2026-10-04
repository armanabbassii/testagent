"""
test_case_generator/json_output.py — بیرون کشیدن JSON از پاسخ خام LLM

هر دو مسیرِ تولیدِ تست‌کیس (تحلیلِ تسک و تحلیلِ Swagger) خروجیِ LLM را به یک
dict تبدیل می‌کنند. این منطق یک‌جا این‌جا نگه داشته می‌شود تا در چند ماژول
تکرار نشود.

این ماژول هیچ‌چیز درباره‌ی معنیِ داده نمی‌داند؛ فقط متن را به dict تبدیل
می‌کند. اعتبارسنجیِ ساختار وظیفه‌ی ماژولی است که این تابع را صدا می‌زند.
"""

from __future__ import annotations

import json
import re

# fence های ```json ... ``` که بعضی مدل‌ها دورِ خروجی می‌گذارند
_FENCE_START_RE = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_END_RE = re.compile(r"\s*```$")

# طولِ پیش‌نمایشِ پاسخ در پیامِ خطا
_PREVIEW_CHARS = 300


class JsonExtractionError(ValueError):
    """پاسخِ LLM به JSON قابلِ استفاده تبدیل نشد."""


def extract_json_object(raw: str) -> dict:
    """متنِ خامِ LLM را به dict تبدیل می‌کند — fence و متنِ اضافه را تحمل می‌کند.

    پرتاب می‌کند:
        JsonExtractionError : پاسخ خالی، غیرِ JSON، JSON نامعتبر، یا غیرِ شیء باشد
    """
    if not raw or not raw.strip():
        raise JsonExtractionError("LLM returned an empty response.")

    text = raw.strip()
    if text.startswith("```"):
        text = _FENCE_START_RE.sub("", text)
        text = _FENCE_END_RE.sub("", text).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # آخرین تلاش: بیرون کشیدن اولین بلوک { ... } از میان متنِ اضافه
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise JsonExtractionError(
                f"LLM did not return JSON. Response preview: {raw[:_PREVIEW_CHARS]}"
            ) from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise JsonExtractionError(
                f"LLM returned invalid JSON ({exc}). "
                f"Response preview: {raw[:_PREVIEW_CHARS]}"
            ) from exc

    if not isinstance(data, dict):
        raise JsonExtractionError(
            f"Expected a JSON object at the top level, got {type(data).__name__}."
        )
    return data
