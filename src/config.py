"""
config.py — بارگذاری تنظیمات از فایل .env
"""
from pathlib import Path

from dotenv import load_dotenv
import os

env_file = Path(os.getenv("ENV_FILE", ".env"))

if env_file.exists():
    load_dotenv(env_file)


def _require(key: str, default: str | None = None) -> str:
    value = os.getenv(key, default)
    if value is None or value == "":
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set."
        )

    return value

# ── LLM ──────────────────────────────────────────────────────────────────────
LLM_BASE_URL: str = _require("LLM_BASE_URL")
LLM_MODEL: str = _require("LLM_MODEL")
LLM_API_KEY: str = _require("LLM_API_KEY")

# سقفِ توکنِ خروجی — یک مقدارِ مشترک برای همه‌ی قدم‌های جریانِ test_case_generator
# تا هر generator عددِ سرخودِ خودش را نداشته باشد.
#
# چرا ۸۱۹۲:
#   * مشاهده‌ی واقعی روی همین سرویس: پاسخِ قدم دوم با سقفِ ۴۰۹۶ دقیقاً روی
#     ۴۰۹۶ توکن تمام شد (finish_reason="length") و JSON نیمه‌کاره ماند. ۸۱۹۲
#     دو برابرِ همان سقف است — حاشیه‌ی کافی برای همان پاسخ.
#   * اندازه‌ی واقعیِ خروجی: هر نگاشتِ تست‌کیس↔API (شناسه + متد/مسیر/
#     operation_id + اطمینان + دلیل + ابهام) حدودِ ۱۵۰–۲۵۰ توکن است. برای
#     جریان‌های معمولی (چند ده تست‌کیس) به‌علاوه‌ی مقدمه و بلوکِ clarifications،
#     ۸۱۹۲ با حاشیه‌ی محسوس کافی است.
#   * همین عدد از قبل در قدم سوم استفاده می‌شد؛ یکسان‌کردنِ مقدارها رفتارِ قدم
#     سوم را عوض نمی‌کند و فقط سقفِ قدم‌های اول و دوم را بالا می‌برد.
#   * بزرگ‌تر گذاشتنش (مثلاً ۳۲K) هم بی‌فایده است (مدل به‌خودی‌خود بیشتر تولید
#     نمی‌کند) و هم روی سرویس‌های OpenAI-compatible معمولاً پذیرفته نمی‌شود.
#
# این سقف با کاهشِ حجمِ پرامپت (فیلترِ قطعیِ ارتباط در قدم دوم) هم‌راستاست:
# پرامپتِ کوچک‌تر یعنی مقدمه‌چینیِ کمتر و خروجیِ کوتاه‌تر.
LLM_MAX_OUTPUT_TOKENS: int = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "8192"))

# ── Embedding ─────────────────────────────────────────────────────────────────
EMBEDDING_BASE_URL: str = _require("EMBEDDING_BASE_URL")
EMBEDDING_MODEL: str = _require("EMBEDDING_MODEL")
EMBEDDING_API_KEY: str = _require("EMBEDDING_API_KEY")

# ── Custom Headers ────────────────────────────────────────────────────────────
CLIENT_ID: str = _require("CLIENT_ID")

# ── GitLab ────────────────────────────────────────────────────────────────────
GITLAB_URL: str = _require("GITLAB_URL")
GITLAB_TOKEN: str = _require("GITLAB_TOKEN")
GITLAB_PROJECT_ID: str = _require("GITLAB_PROJECT_ID")

# ── Checkpointer ──────────────────────────────────────────────────────────────
# backend: sqlite (توسعه/تست/single-node) | redis (production/multi-node)
CHECKPOINTER_BACKEND: str = os.getenv("CHECKPOINTER_BACKEND", "sqlite")
CHECKPOINTER_SQLITE_PATH: str = os.getenv("CHECKPOINTER_SQLITE_PATH", "checkpoints.db")

# ── Redis Checkpointer ────────────────────────────────────────────────────────
# credentials (username/password) باید داخل URL باشند:
#   redis://localhost:6379                        بدون احراز هویت
#   redis://:password@localhost:6379              فقط password
#   redis://username:password@localhost:6379      username + password
#   redis://username:@localhost:6379              فقط username
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

# ── Observability (LangFuse) ────────────────────────────────────────────────────────
LANGFUSE_ENABLED: bool = os.getenv("LANGFUSE_ENABLED", "false").lower() in ("true", "1", "yes")
LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# ── SonarQube (اختیاری — فقط اگر enable_sonar=True در گراف کد ریویو) ──────────
SONAR_URL: str = os.getenv("SONAR_URL", "http://localhost:9000")
SONAR_TOKEN: str = os.getenv("SONAR_TOKEN", "")
SONAR_SCANNER_BIN: str = os.getenv("SONAR_SCANNER_BIN", "sonar-scanner")
SONAR_PROJECT_PREFIX: str = os.getenv("SONAR_PROJECT_PREFIX", "cr-mr")
SONAR_POLL_INTERVAL_SECONDS: int = int(os.getenv("SONAR_POLL_INTERVAL_SECONDS", "5"))
SONAR_POLL_TIMEOUT_SECONDS: int = int(os.getenv("SONAR_POLL_TIMEOUT_SECONDS", "600"))
SONAR_DELETE_PROJECT_AFTER_SCAN: bool = (
    os.getenv("SONAR_DELETE_PROJECT_AFTER_SCAN", "true").lower() in ("true", "1", "yes")
)

# ── SonarQube (اختیاری — فقط با --sonar در CLI فعال می‌شود) ──────────────────
SONAR_URL: str = os.getenv("SONAR_URL", "http://localhost:9000")
SONAR_TOKEN: str = os.getenv("SONAR_TOKEN", "")
SONAR_SCANNER_BIN: str = os.getenv("SONAR_SCANNER_BIN", "sonar-scanner")
SONAR_PROJECT_PREFIX: str = os.getenv("SONAR_PROJECT_PREFIX", "cr-mr")
SONAR_POLL_INTERVAL_SECONDS: int = int(os.getenv("SONAR_POLL_INTERVAL_SECONDS", "5"))
SONAR_POLL_TIMEOUT_SECONDS: int = int(os.getenv("SONAR_POLL_TIMEOUT_SECONDS", "600"))
SONAR_DELETE_PROJECT_AFTER_SCAN: bool = (
    os.getenv("SONAR_DELETE_PROJECT_AFTER_SCAN", "true").lower() in ("true", "1", "yes")
)
# full = همه issue‌های باز پروژه گزارش شوند
# diff = فقط issue‌هایی که روی خطوط واقعاً تغییرکرده در این MR هستند
#        (پیش‌فرض — برای جلوگیری از جریمه‌شدن پروژه‌های قدیمی با بدهی فنی preexisting)
SONAR_SCAN_MODE: str = os.getenv("SONAR_SCAN_MODE", "diff").lower().strip()