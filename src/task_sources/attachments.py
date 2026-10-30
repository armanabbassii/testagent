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
) -> TaskAttachment:
    """یک پیوست را بررسی، دانلود و به متن تبدیل می‌کند — هرگز استثنا بیرون نمی‌دهد.

    ``download(max_bytes)`` بایت‌های فایل را برمی‌گرداند؛ اگر از سقف بیشتر شود
    AttachmentTooLarge و اگر شکست بخورد هر استثنای دیگری می‌دهد (پیامش در detail).
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
    truncated = len(text) > max_chars
    if truncated:
        notes = [note] if note else []
        notes.append(f"text truncated to the first {max_chars} of {len(text)} characters")
        note = "; ".join(notes)
        text = text[:max_chars]
    return TaskAttachment(
        **base, status=ATTACHMENT_PROCESSED, content=text, detail=note, truncated=truncated
    )


__all__ = [
    "PDF_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "AttachmentError",
    "AttachmentTooLarge",
    "extension_of",
    "extract_text",
    "is_supported",
    "process_attachment",
]
