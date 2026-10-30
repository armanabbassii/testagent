"""
cli/i18n.py — رشته‌های قابل ترجمه CLI

هر رشته با یک کلید در MESSAGES تعریف می‌شود.
برای اضافه کردن زبان جدید، یک دیکشنری جدید بسازید و به LANGUAGES اضافه کنید.
"""

from typing import Literal

Lang = Literal["fa", "en"]
SUPPORTED_LANGS: list[Lang] = ["fa", "en"]
DEFAULT_LANG: Lang = "fa"

_FA: dict[str, str] = {
    # ── عمومی ──────────────────────────────────────────────────────────────
    "app_description": "ابزار خط فرمان برای Local LLM Agent Framework",
    "lang_help": "زبان خروجی (fa یا en)",
    "error_prefix": "خطا",
    "success_prefix": "موفق",

    # ── ingest ──────────────────────────────────────────────────────────────
    "ingest_description": "بارگذاری و ذخیره کاتالوگ سرویس‌های بیزینسی در vector store",
    "ingest_file_help": "مسیر فایل JSONL کاتالوگ سرویس‌ها",
    "ingest_collection_help": "نام collection مقصد (پیش‌فرض: business_catalog)",
    "ingest_embed_batch_size_help": "تعداد اسنادی که در هر مرحله به‌صورت هم‌زمان برای تولید بردار (Embedding) پردازش می‌شوند. مقدار پیش فرض 64 ",
    "ingest_header": "Ingest بیزینسی — Collection: {}",
    "ingest_running": "در حال پردازش و ذخیره‌سازی...",
    "ingest_done": "ingest با موفقیت انجام شد",
    "ingest_records": "رکوردهای خام",
    "ingest_chunks": "چانک‌های تولیدشده",
    "ingest_upserted": "ذخیره‌شده در vector store",
    "ingest_technical_description": "بارگذاری و ذخیره سورس جاوا/مستندات فنی در vector store",
    "ingest_technical_path_help": "مسیر پوشه پروژه جاوا/اسپرینگ‌بوت",
    "ingest_technical_collection_help": "نام collection مقصد (پیش‌فرض: technical_catalog)",
    "ingest_technical_no_html_help": "عدم اسکن فایل‌های .html (قالب‌ها)",
    "ingest_technical_header": "Ingest فنی — Collection: {}",
    "ingest_technical_running": "در حال اسکن، چانک‌بندی و ذخیره‌سازی...",

}

_EN: dict[str, str] = {
    # ── general ─────────────────────────────────────────────────────────────
    "app_description": "Command-line tool for Local LLM Agent Framework",
    "lang_help": "Output language (fa or en)",
    "error_prefix": "Error",
    "success_prefix": "Success",

    # ── ingest ──────────────────────────────────────────────────────────────
    "ingest_description": "Load and store business service catalog into vector store",
    "ingest_file_help": "Path to the JSONL service catalog file",
    "ingest_collection_help": "Destination collection name (default: business_catalog)",
    "ingest_embed_batch_size_help": "Specifies the number of documents processed simultaneously in each embedding generation batch. Default: 64",
    "ingest_header": "Business Ingest — Collection: {}",
    "ingest_running": "Processing and storing...",
    "ingest_done": "Ingest completed successfully",
    "ingest_records": "Raw records",
    "ingest_chunks": "Generated chunks",
    "ingest_upserted": "Upserted to vector store",
    "ingest_technical_description": "Load and store Java source / technical docs into vector store",
    "ingest_technical_path_help": "Path to the Java/Spring Boot project folder",
    "ingest_technical_collection_help": "Destination collection name (default: technical_catalog)",
    "ingest_technical_no_html_help": "Skip scanning .html template files",
    "ingest_technical_header": "Technical Ingest — Collection: {}",
    "ingest_technical_running": "Scanning, chunking, and storing...",
}

LANGUAGES: dict[str, dict[str, str]] = {
    "fa": _FA,
    "en": _EN,
}


class Translator:
    """ترجمه رشته‌های CLI بر اساس زبان انتخابی."""

    def __init__(self, lang: Lang = DEFAULT_LANG) -> None:
        if lang not in SUPPORTED_LANGS:
            lang = DEFAULT_LANG
        self._strings = LANGUAGES[lang]
        self.lang = lang

    def t(self, key: str, *args) -> str:
        """رشته ترجمه‌شده را برمی‌گرداند. args برای format استفاده می‌شود."""
        template = self._strings.get(key, f"[{key}]")
        return template.format(*args) if args else template