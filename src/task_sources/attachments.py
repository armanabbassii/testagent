"""
src/task_sources/attachments.py — اعتبارسنجی و استخراجِ متن از پیوستِ تسک

پیوست ورودیِ نامطمئن است: هرگز اجرا نمی‌شود، فقط متنش خوانده می‌شود. هر
پیوست یک وضعیتِ صریح می‌گیرد — processed، unsupported، too_large یا failed —
تا هیچ پیوستی بی‌صدا «پردازش‌شده» دیده نشود.

نوعِ واقعیِ فایل هم سنجیده می‌شود، نه فقط پسوند:

  * .pdf باید با امضای %PDF- شروع شود؛ متنش با pypdf خوانده می‌شود (فقط
    استخراجِ متن — نه اسکریپت، نه فرم، نه پیوستِ داخلی).
  * .txt/.md/.json/.yaml/.yml باید متنِ UTF-8 (یا UTF-16 با BOM) باشند؛ امضای
    فایل‌های دودویی (PDF، ZIP، تصویر، فایلِ اجرایی) یا بایتِ NUL یعنی «این متن
    نیست». JSON باید JSON معتبر باشد وگرنه با هشدار می‌آید (ممکن است نمونه‌ی
    ناقصِ عمدی باشد). YAML عمداً parse نمی‌شود — فقط متن است و parse کردنِ
    YAMLِ نامطمئن چیزی به تحلیل اضافه نمی‌کند.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import PurePosixPath

from src.task_sources.postman import PostmanCollection, parse_postman_collection
from src.task_sources.task_input import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    ATTACHMENT_TOO_LARGE,
    ATTACHMENT_UNSUPPORTED,
    TaskAttachment,
)
from src.task_sources.text import normalize_text

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".json", ".yaml", ".yml"})
PDF_EXTENSIONS = frozenset({".pdf"})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS

_PDF_MAGIC = b"%PDF-"
# امضای فایل‌های دودوییِ رایج — هیچ‌کدام نباید پشتِ پسوندِ متنی باشد.
_BINARY_MAGIC = (
    _PDF_MAGIC,
    b"PK\x03\x04",  # zip / docx / xlsx / jar
    b"\x89PNG",
    b"GIF8",
    b"\xff\xd8\xff",  # jpeg
    b"MZ",  # exe / dll
    b"\x7fELF",
    b"\xd0\xcf\x11\xe0",  # doc / xls قدیمی
    b"Rar!",
    b"7z\xbc\xaf",
    b"\x1f\x8b",  # gzip
)


class AttachmentError(Exception):
    """محتوای پیوست با نوعِ اعلام‌شده‌اش جور نیست یا خوانا نیست."""


class AttachmentTooLarge(Exception):
    """دانلود از سقفِ حجم بیشتر شد — همان‌جا متوقف می‌شود."""


def extension_of(filename: str) -> str:
    return PurePosixPath(filename or "").suffix.lower()


def is_supported(filename: str) -> bool:
    return extension_of(filename) in SUPPORTED_EXTENSIONS


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise AttachmentError("the file is not valid UTF-16 text") from exc
    if any(data.startswith(magic) for magic in _BINARY_MAGIC):
        raise AttachmentError("the content is a binary file, not text")
    if b"\x00" in data:
        raise AttachmentError("the content contains binary data (NUL bytes), not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AttachmentError("the file is not valid UTF-8 text") from exc


def _pdf_text(data: bytes) -> str:
    if not data.startswith(_PDF_MAGIC):
        raise AttachmentError("the content is not a PDF (missing %PDF- signature)")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - pypdf جزوِ وابستگی‌های پروژه است
        raise AttachmentError("PDF text extraction needs the pypdf package") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AttachmentError("the PDF is encrypted")
        pages = [page.extract_text() or "" for page in reader.pages]
    except AttachmentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise AttachmentError(f"the PDF could not be read ({type(exc).__name__})") from exc
    text = "\n\n".join(
        f"[page {number}]\n{page.strip()}" for number, page in enumerate(pages, 1) if page.strip()
    )
    if not text:
        raise AttachmentError(
            "the PDF has no extractable text (it may be scanned images; OCR is not supported)"
        )
    return text


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """متنِ پیوست و یک یادداشت (یا "")؛ AttachmentError یعنی پیوست خوانا نیست."""
    extension = extension_of(filename)
    if extension in PDF_EXTENSIONS:
        return _pdf_text(data), ""
    if extension not in TEXT_EXTENSIONS:
        raise AttachmentError(f"unsupported file type {extension or '(no extension)'}")
    text = _decode_text(data)
    note = ""
    if extension == ".json":
        try:
            json.loads(text)
        except ValueError:
            note = "the file is not valid JSON; its text is included as-is"
    return text, note


def process_attachment(
    *,
    filename: str,
    media_type: str,
    size: int,
    download: Callable[[int], bytes],
    max_bytes: int,
    max_chars: int,
    limit_reason: str = "",
) -> TaskAttachment:
    """یک پیوست را بررسی، دانلود و به متن تبدیل می‌کند — هرگز استثنا بیرون نمی‌دهد.

    ``download(max_bytes)`` بایت‌های فایل را برمی‌گرداند؛ اگر از سقف بیشتر شود
    AttachmentTooLarge و اگر شکست بخورد هر استثنای دیگری می‌دهد (پیامش در detail).

    ``limit_reason`` وقتی max_chars از سقفِ هر پیوست کمتر است (بودجه‌ی کلِ پیوست‌های
    issue) در یادداشتِ برش می‌آید تا مدل و کاربر بدانند چرا متن کوتاه شده است.
    max_chars=0 یعنی هیچ متنی از این پیوست جا نشد.
    """
    base = {"filename": filename, "media_type": media_type, "size": size}
    if not is_supported(filename):
        return TaskAttachment(
            **base,
            status=ATTACHMENT_UNSUPPORTED,
            detail=f"supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )
    if size > max_bytes:
        return TaskAttachment(
            **base,
            status=ATTACHMENT_TOO_LARGE,
            detail=f"{size} bytes exceeds the {max_bytes}-byte limit; not downloaded",
        )
    try:
        data = download(max_bytes)
    except AttachmentTooLarge:
        data = b""
        size_exceeded = True
    except Exception as exc:  # noqa: BLE001 - هر شکستِ دانلود باید گزارش شود، نه پنهان
        return TaskAttachment(**base, status=ATTACHMENT_FAILED, detail=f"download failed: {exc}")
    else:
        size_exceeded = len(data) > max_bytes
    if size_exceeded:
        return TaskAttachment(
            **base,
            status=ATTACHMENT_TOO_LARGE,
            detail=f"the download exceeded the {max_bytes}-byte limit",
        )
    try:
        text, note = extract_text(filename, data)
    except AttachmentError as exc:
        return TaskAttachment(**base, status=ATTACHMENT_FAILED, detail=str(exc))

    text = normalize_text(text)
    # پیش از برش: Collectionِ بزرگ‌تر از max_chars هم کامل تجزیه می‌شود، در حالی که
    # content همان برشِ قبلی را می‌گیرد. detail عمداً دست نمی‌خورد تا متنِ تسک
    # (to_task_description، که detail را نشان می‌دهد) دقیقاً همان بماند.
    postman = _postman_collection(filename, text)
    # JSONِ معتبر بدونِ تورفتگی: همان محتوا با کاراکترِ کمتر، پس پیش از برش بیشترش
    # در سقف جا می‌شود. Collectionِ بالا از متنِ اصلی ساخته شده و دست نمی‌خورد.
    if extension_of(filename) == ".json":
        text = compact_json(text) or text
    truncated = len(text) > max_chars
    if truncated:
        notes = [note] if note else []
        if max_chars == 0:
            cut = f"text not included ({len(text)} characters)"
        else:
            cut = f"text truncated to the first {max_chars} of {len(text)} characters"
        notes.append(f"{cut}: {limit_reason}" if limit_reason else cut)
        note = "; ".join(notes)
        text = text[:max_chars]
    return TaskAttachment(
        **base,
        status=ATTACHMENT_PROCESSED,
        content=text,
        detail=note,
        truncated=truncated,
        postman=postman,
    )


_JSON_WHITESPACE = frozenset(" \t\n\r")


def compact_json(text: str) -> str | None:
    """JSONِ معتبر بدونِ فاصله‌های بیرون از رشته‌ها — وگرنه None.

    بدونِ تغییرِ محتوا: هیچ کلید، مقدار، ترتیب، عدد یا رشته‌ای عوض نمی‌شود؛ فقط
    فاصله/تب/خطِ نوِ بینِ توکن‌ها (تورفتگیِ خروجیِ Postman و ویرایشگرها) حذف می‌شود.
    متنِ داخلِ رشته‌ها، از جمله escapeها، بایت‌به‌بایت می‌ماند. JSONِ نامعتبر None
    می‌گیرد تا همان متنِ اصلی بماند.
    """
    try:
        json.loads(text)
    except ValueError:
        return None
    out: list[str] = []
    in_string = escaped = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif char not in _JSON_WHITESPACE:
            out.append(char)
    return "".join(out)


def _postman_collection(filename: str, text: str) -> PostmanCollection | None:
    """Collectionِ شناخته‌شده‌ی یک پیوستِ JSON — وگرنه None.

    فقط ``.json``. JSONِ معمولی، نامعتبر و قالبِ پشتیبانی‌نشده‌ی Postman None
    می‌گیرند (همان رفتارِ قبلی). هر شکستِ پیش‌بینی‌نشده‌ی parser هم None است:
    دریافتِ issue هرگز به‌خاطرِ تحلیلِ Postman شکست نمی‌خورد.
    """
    if extension_of(filename) != ".json":
        return None
    try:
        result = parse_postman_collection(text)
    except Exception:  # noqa: BLE001 - تحلیلِ اختیاری نباید import را بشکند
        return None
    return result.collection if result.recognised else None


__all__ = [
    "PDF_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "AttachmentError",
    "AttachmentTooLarge",
    "compact_json",
    "extension_of",
    "extract_text",
    "is_supported",
    "process_attachment",
]
