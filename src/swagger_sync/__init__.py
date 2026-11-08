"""
src/swagger_sync — همگام‌سازیِ روزانه‌ی snapshotهای Swagger، مستقل از Streamlit

    python -m src.swagger_sync run              # دریافت، اعتبارسنجی و جایگزینیِ امن
    python -m src.swagger_sync run --check      # GET و اعتبارسنجیِ واقعی؛ هیچ snapshotی عوض نمی‌شود
    python -m src.swagger_sync run --dry-run    # بدونِ شبکه و بدونِ نوشتن؛ فقط شبیه‌سازی

همه‌ی قاعده‌های دریافت، اعتبارسنجی، محافظِ کاهش و نوشتنِ اتمی همان هسته‌ی
src/agents/test_case_generator/swagger_snapshots.py اند (همان مسیرِ دکمه‌ی Update)؛ این پکیج فقط
retry، deadline، قفلِ اجرا، گزارش و ثبتِ Task Scheduler را اضافه می‌کند. به تنظیماتِ LLM وابسته
نیست. این فایل عمداً چیزی import نمی‌کند.
"""
