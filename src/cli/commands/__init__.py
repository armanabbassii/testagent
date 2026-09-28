"""
commands/__init__.py

برای اضافه کردن دستور جدید:
  ۱. فایل src/cli/commands/my_command.py بساز
  ۲. تابع add_parser(subparsers, t: Translator) -> None پیاده‌سازی کن
  ۳. در src/cli/__main__.py آن را به _register_commands اضافه کن
  ۴. رشته‌های ترجمه را به src/cli/i18n.py اضافه کن
"""