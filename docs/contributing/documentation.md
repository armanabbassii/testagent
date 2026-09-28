# قوانین مستندسازی

## اصل کلی

> کد بدون مستند، کد ناقص است.
> هر چیزی که برای فهمیدن آن بیش از ۳۰ ثانیه فکر کردید، مستند کنید.

---

## ۱. Docstring

### الزامی برای

- تمام کلاس‌های public
- تمام متدهای public
- تمام توابع با منطق غیر بدیهی

### فرمت

```python
def calculate_score(comments: list[ReviewComment]) -> dict:
    """امتیاز کلی را از لیست کامنت‌های ریویو محاسبه می‌کند.

    پارامترها:
        comments : لیست ReviewComment های تولیدشده توسط CodeReviewerAgent

    برمی‌گرداند:
        دیکشنری شامل:
          - total_score : امتیاز کل (عدد منفی یا صفر)
          - issues      : تفکیک بر اساس severity
          - categories  : تفکیک بر اساس category

    مثال:
        score = calculate_score(comments)
        print(score["total_score"])   # -17
    """
```

### ممنوع

```python
# ❌ docstring بدیهی که چیزی اضافه نمی‌کند
def get_user_id(self) -> str:
    """user_id را برمی‌گرداند."""
    return self._user_id

# ✅ فقط کد کافی است
def get_user_id(self) -> str:
    return self._user_id
```

---

## ۲. Comment های داخل کد

### چه موقع بنویسیم

- توضیح **چرا** نه **چه** — کد خودش «چه» را نشان می‌دهد
- هشدار درباره side effect یا رفتار غیرمنتظره
- توضیح workaround یا محدودیت کتابخانه

```python
# ✅ توضیح «چرا»
# qdrant-client >= 1.10 متد search را حذف کرد؛ از query_points استفاده می‌کنیم
response = self._client.query_points(...)

# ✅ هشدار درباره رفتار
# unapprove ممکن است خطا بدهد اگر MR قبلاً approve نشده باشد — نادیده می‌گیریم
try:
    self._gl.unapprove_mr(mr_iid)
except Exception:
    pass

# ❌ توضیح «چه» (بدیهی)
# شمارش کامنت‌ها
count = len(comments)
```

### ممنوع

- کد comment-out شده در commit نهایی
- TODO بدون issue tracker reference: `# TODO: fix this` ← ❌
- TODO با reference: `# TODO(#42): handle pagination` ← ✅

---

## ۳. مستندات `docs/`

### ساختار

```
docs/
├── index.md                      # فهرست و نقشه راه مستندات
├── requirements.md               # نیازمندی‌ها
├── installation.md               # نصب
├── how-to/                       # راهنماهای گام‌به‌گام
│   ├── new-agent.md
│   └── new-log-driver.md
├── architecture/                 # تصمیمات معماری
│   └── core-clients.md
├── contributing/                 # قوانین مشارکت
│   └── documentation.md
└── roadmap.md                    # توسعه‌های آینده
```

### قوانین فایل‌های `docs/`

| قانون | توضیح |
|-------|-------|
| زبان | فارسی برای توضیحات، انگلیسی برای کد و اصطلاحات فنی |
| عنوان | هر فایل با `# عنوان` شروع می‌شود |
| به‌روزرسانی | هر تغییر در کد که رفتار public را عوض می‌کند باید مستند را هم آپدیت کند |
| مثال کد | همه مثال‌ها باید قابل اجرا باشند |

---

## ۴. فایل‌های Prompt (`.md` در پوشه `prompts/`)

### قوانین

- هر فایل prompt یک مسئولیت واحد دارد (system، rules/security، rules/style، ...)
- تغییر prompt = تغییر رفتار ایجنت — حتماً در commit message ذکر شود
- هر rule file باید با `## [نام] Rules` شروع شود
- مثال‌های خوب و بد در prompt مفیدتر از توضیحات طولانی هستند

---

## ۵. Commit Message

```
نوع(حوزه): توضیح کوتاه

توضیح بلندتر در صورت نیاز (اختیاری)
```

**انواع:**

| نوع | کاربرد |
|-----|---------|
| `feat` | قابلیت جدید |
| `fix` | رفع باگ |
| `refactor` | بازنویسی بدون تغییر رفتار |
| `docs` | فقط مستندات |
| `prompt` | تغییر در فایل‌های prompt |
| `config` | تغییر در تنظیمات |

**مثال:**

```
feat(code_review): اضافه کردن سیستم امتیازدهی به ریویو

- امتیاز بر اساس severity: critical=-10, major=-7, minor=-3, suggestion=-1
- ذخیره در JSONL با فیلدهای issues و categories
- کامنت خلاصه امتیاز روی MR ثبت می‌شود
```