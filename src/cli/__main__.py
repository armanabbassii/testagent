"""
cli/__main__.py — entrypoint برای `python -m src.cli`

ساختار — --lang هر جایی قبول می‌شود:
    python -m src.cli --lang fa review --mr 42
    python -m src.cli review --mr 42 --lang en
    python -m src.cli scores --last 5 --lang fa

دستورهای موجود:
    review   اجرای code review روی یک MR
    scores   نمایش تاریخچه امتیازها

افزودن دستور جدید:
    ۱. یک فایل در src/cli/commands/ بسازید
    ۲. تابع add_parser(subparsers, t, lang_parent) را پیاده‌سازی کنید
    ۳. آن را در _register_commands اضافه کنید
    ۴. رشته‌های ترجمه را به i18n.py اضافه کنید
"""

import sys
import argparse

from src.cli.i18n import Translator, SUPPORTED_LANGS, DEFAULT_LANG


def _make_lang_parent() -> argparse.ArgumentParser:
    """یک parent parser فقط با --lang می‌سازد.

    هر subcommand این را به عنوان parents=[] می‌گیرد،
    بنابراین --lang هم قبل و هم بعد از نام command قبول می‌شود.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--lang",
        default=DEFAULT_LANG,
        choices=SUPPORTED_LANGS,
        dest="lang",
    )
    return parent


def _register_commands(subparsers, t: Translator, lang_parent: argparse.ArgumentParser) -> None:
    """همه دستورها اینجا ثبت می‌شوند.
    برای اضافه کردن دستور جدید، فقط این تابع را ویرایش کنید.
    """
    from src.cli.commands.review import add_parser as add_review
    from src.cli.commands.scores import add_parser as add_scores
    from src.cli.commands.ingest import add_parser as add_ingest
    from src.cli.commands.ingest_technical import add_parser as add_ingest_technical

    add_review(subparsers, t, lang_parent)
    add_scores(subparsers, t, lang_parent)
    add_ingest(subparsers, t, lang_parent)
    add_ingest_technical(subparsers, t, lang_parent)


def main(argv: list[str] | None = None) -> int:
    # ── مرحله ۱: --lang را از هر جایی بخوان (قبل یا بعد از command) ──────────
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--lang", default=DEFAULT_LANG, choices=SUPPORTED_LANGS)
    pre_args, _ = pre_parser.parse_known_args(argv)
    t = Translator(lang=pre_args.lang)

    # ── مرحله ۲: پارسر اصلی ──────────────────────────────────────────────────
    lang_parent = _make_lang_parent()

    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description=t.t("app_description"),
        parents=[lang_parent],
    )

    subparsers = parser.add_subparsers(dest="command")
    _register_commands(subparsers, t, lang_parent)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # lang ممکن است در subcommand override شده باشد — t را refresh کن
    final_lang = getattr(args, "lang", pre_args.lang)
    if final_lang != t.lang:
        t = Translator(lang=final_lang)

    try:
        return args.func(args, t)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())