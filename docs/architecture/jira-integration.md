# ورودیِ تسک از Jira

این سند توضیح می‌دهد قدمِ اولِ ویزارد چطور یک issueِ Jira را به‌جای توصیفِ
دستی می‌گیرد: تنظیمات، روش‌های ورود، جریانِ ADFS، گواهیِ SSL، پیوست‌ها، کار با
UI و محدودیت‌ها.

---

## A. جایگاه در معماری

```
کلید یا نشانیِ issue (PROJ-123 یا https://<jira>/browse/PROJ-123)
        │
        ▼
src/task_sources/jira        ورود (ADFS SAML یا PAT) → REST v2 → پیوست‌ها
        │
        ▼
TaskInput (src/task_sources/task_input.py)   ← مستقل از Jira
        │  to_task_description()
        ▼
INPUT_TASK_DESCRIPTION + INPUT_JIRA_ISSUE  (ui/workflow.py)
        │
        ▼
قدم ۱ → قدم ۲ → … → قدم ۵   (بدونِ هیچ تغییری؛ Run All همان قبلی)
```

- **جریانِ پنج‌قدمی Jira را نمی‌شناسد.** Jira فقط کادرِ «Task Description» را پر
  می‌کند؛ اجرای قدمِ اول، Run All و انتخابِ منابعِ Swagger همان مسیرِ ورودیِ
  دستی‌اند.
- **TaskInput مستقل از منبع است** (عنوان، توضیح، کامنت‌ها، پیوست‌ها، منبع،
  شناسه، نشانی، metadata). سامانه‌ی دیگری (GitLab issue، …) فقط باید همین را
  بسازد.
- **کهنه‌شدن:** کلیدِ issue (`INPUT_JIRA_ISSUE`) ورودیِ قدمِ اول است. دریافتِ
  issueِ دیگر نتیجه‌ی قدمِ اول و پایین‌دستی را «out of date» می‌کند — حتی اگر
  متن تصادفاً یکی باشد. اگر کلید در UI عوض شود ولی دریافت نشود، اجرا ممکن نیست.

| فایل | مسئولیت |
|---|---|
| `src/task_sources/task_input.py` | `TaskInput` و متنِ ساختاریافته‌ی قدمِ اول |
| `src/task_sources/text.py` | پاک‌سازیِ متن (CRLF، NBSP، BOM) و fence |
| `src/task_sources/attachments.py` | نوعِ واقعی، سقفِ حجم، استخراجِ متن |
| `src/task_sources/jira/settings.py` | خواندن و اعتبارسنجیِ تنظیمات |
| `src/task_sources/jira/auth.py` | ورودِ ADFS SAML و PAT |
| `src/task_sources/jira/client.py` | REST v2، نشست، ورودِ دوباره، خطاها |
| `src/task_sources/jira/markup.py` | wiki markup (بلوک‌های `{code}` دست‌نخورده) |
| `src/task_sources/jira/importer.py` | issue → `TaskInput` با گزارشِ هر مرحله |
| `ui/pipeline.py` | `load_jira_task`، `jira_input_problem` |
| `ui/step1_task_analysis.py` | انتخابِ منبع، دکمه‌ی دریافت، وضعیت |

---

## B. تنظیمات (`.env`)

همه از `src/config.py` خوانده می‌شوند (همان سازوکارِ بقیه‌ی پروژه) و همه
اختیاری‌اند: بدونِ آن‌ها ویزارد با توصیفِ دستی کار می‌کند و فقط دکمه‌ی
«Fetch issue» خطای روشنِ پیکربندی می‌دهد.

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `JIRA_BASE_URL` | — | نشانیِ Jira، فقط `https://` (مثلاً `https://jira.dotin.ir`) |
| `JIRA_AUTH_METHOD` | `adfs` | `adfs` (نام کاربری/رمز) یا `pat` |
| `JIRA_SSO_URL` | — | نشانیِ ADFS (`https://sso.dotin.ir/adfs/ls/`)؛ رمز فقط به همین میزبان فرستاده می‌شود |
| `JIRA_USERNAME` / `JIRA_PASSWORD` | — | برای `adfs` |
| `JIRA_TOKEN` | — | فقط برای `pat` |
| `JIRA_CA_BUNDLE` | خالی | فایلِ PEM برای تأییدِ گواهی (بخشِ D) |
| `JIRA_CONNECT_TIMEOUT_SECONDS` / `JIRA_READ_TIMEOUT_SECONDS` | `10` / `60` | timeoutِ هر درخواست |
| `JIRA_MAX_ATTACHMENT_BYTES` | `10485760` | سقفِ حجمِ هر پیوست |
| `JIRA_MAX_ATTACHMENT_CHARS` | `100000` | سقفِ متنِ استخراج‌شده از هر پیوست |

همه‌ی کمبودها با هم گزارش می‌شوند (مثلاً «JIRA_SSO_URL is not set; JIRA_PASSWORD
is not set»). `.env` در git نیست؛ رمز هرگز در کد، لاگ، UI یا پیامِ خطا نمی‌آید.

---

## C. روش‌های ورود

### ADFS — SAML 2.0 + Forms Authentication (پیش‌فرض)

این جریان روی `jira.dotin.ir` (Jira Server 9.12.8) واقعاً آزموده شده است:

1. `GET {JIRA_BASE_URL}/login.jsp` → افزونه‌ی SAMLِ Jira
   (`/plugins/servlet/samlsso`) یک فرمِ خودکار با `SAMLRequest` و `RelayState`
   برمی‌گرداند.
2. همان فرم به ADFS فرستاده می‌شود → صفحه‌ی Sign In با `loginForm`
   (`UserName`، `Password`، `AuthMethod=FormsAuthentication`).
3. نام کاربری و رمز با همان فیلدهای فرم فرستاده می‌شوند (`Kmsi` نه — نشستِ
   ماندگارِ ADFS خواسته نمی‌شود) → فرمِ خودکار با `SAMLResponse`.
4. `SAMLResponse` به ACSِ Jira فرستاده می‌شود → کوکی‌های نشستِ Jira.
5. `GET /rest/api/2/myself` هویت را تأیید می‌کند.

قواعدِ امنیتی:

- هیچ نشانی یا فیلدی حدس زده نمی‌شود؛ action و فیلدهای پنهان از خودِ فرم‌ها
  خوانده می‌شوند.
- رمز فقط به میزبانِ `JIRA_SSO_URL` و فقط روی https فرستاده می‌شود؛ اگر Jira یا
  فرمِ ADFS به میزبانِ دیگری اشاره کند، چیزی فرستاده نمی‌شود.
- `SAMLResponse` فقط به میزبانِ `JIRA_BASE_URL` فرستاده می‌شود.
- کوکی‌ها و assertion فقط در حافظه‌ی همان `requests.Session` اند؛ چیزی روی دیسک
  نوشته نمی‌شود.
- رمزِ ردشده → پیامِ خودِ ADFS (مثلاً «Incorrect user ID or password»).
- **MFA یا هر گامِ تعاملیِ دیگر تشخیص داده و دور زده نمی‌شود** → خطای
  «ADFS asked for an additional interactive verification step (MFA)».
- پاسخِ ناشناخته → خطای «Unexpected response …»، بدونِ ارسالِ دوباره‌ی رمز.

### نشست

- یک نشست برای همه‌ی دریافت‌ها (در حافظه‌ی پروسه‌ی Streamlit، یکی برای هر
  تنظیمات)؛ دریافتِ بعدی دوباره وارد نمی‌شود.
- نشستِ منقضی (HTTP 401 یا redirect به صفحه‌ی ورود) **فقط یک بار** دوباره ساخته
  می‌شود؛ اگر باز هم رد شود: «The Jira session expired and signing in again did
  not restore it». هیچ حلقه‌ی ورودی وجود ندارد.

### Personal Access Token (اختیاری)

`JIRA_AUTH_METHOD=pat` و `JIRA_TOKEN=<token>` → هدرِ `Authorization: Bearer`.
توکن را خودتان در Jira (Profile → Personal Access Tokens) بسازید؛ برنامه توکن
نمی‌سازد. توکنِ ردشده دوباره امتحان نمی‌شود.

> **وضعیتِ آزمون:** نقطه‌ی `/rest/pat/latest/tokens` روی این Jira فعال است، ولی
> ورود با PAT هنوز با یک توکنِ واقعی آزموده نشده است.

---

## D. گواهیِ SSL و `JIRA_CA_BUNDLE`

`jira.dotin.ir` فقط گواهیِ خودش (`*.dotin.ir`) را می‌فرستد و گواهیِ میانیِ
«Certum DV TLS G2 R39 CA» را نمی‌فرستد؛ پس Python (certifi) آن را تأیید نمی‌کند.
`sso.dotin.ir` زنجیره‌ی کامل را می‌فرستد. **راهِ درست، اصلاحِ زنجیره روی سرورِ
Jira است.** تا آن موقع:

```bash
# ۱. گواهیِ میانی (از زنجیره‌ی sso.dotin.ir — همان issuer)
openssl s_client -connect sso.dotin.ir:443 -servername sso.dotin.ir -showcerts </dev/null \
  | awk '/BEGIN CERT/{n++} n==2' | sed '/END CERT/q' > certum-dv-tls-g2-r39.pem

# ۲. تأییدِ آن با ریشه‌های certifi (باید «OK» بدهد)
openssl verify -CAfile "$(python -c 'import certifi; print(certifi.where())')" certum-dv-tls-g2-r39.pem

# ۳. bundle = ریشه‌های certifi + گواهیِ میانی
cat "$(python -c 'import certifi; print(certifi.where())')" certum-dv-tls-g2-r39.pem > jira-ca-bundle.pem
```

بعد در `.env`: `JIRA_CA_BUNDLE=/path/outside/the/repo/jira-ca-bundle.pem`

- تأییدِ گواهی هرگز خاموش نمی‌شود (`verify=False` در کد وجود ندارد).
- مسیرِ ناموجود خطای پیکربندی است، نه بازگشتِ بی‌صدا به پیش‌فرض.
- خطای SSL پیامِ روشن دارد و به `JIRA_CA_BUNDLE` اشاره می‌کند.
- فایلِ bundle را بیرون از مخزن نگه دارید؛ چیزی ماشین‌محور commit نکنید.
- بعد از تمدیدِ گواهیِ میانی (انقضای فعلی: ۲۰۳۹) دوباره بسازید.

---

## E. داده‌ای که از issue خوانده می‌شود

| بخش | نقطه‌ی REST | نکته |
|---|---|---|
| هویت | `GET /rest/api/2/myself` | فقط نامِ نمایشی در UI |
| عنوان، توضیح، نوع، وضعیت، پیوست‌ها | `GET /rest/api/2/issue/{key}?fields=summary,description,attachment,issuetype,status` | |
| همه‌ی کامنت‌ها | `GET /rest/api/2/issue/{key}/comment?startAt=&maxResults=50&orderBy=created` | صفحه به صفحه تا `total`؛ کمبود = خطا |
| فایلِ پیوست | نشانیِ `content`ِ پیوست | فقط روی میزبانِ Jira |

- **wiki markup:** ترجمه به Markdown نمی‌شود (LLM آن را می‌فهمد). بلوک‌های
  `{code}`، `{code:json}` و `{noformat}` به fence تبدیل می‌شوند و محتوایشان
  دست‌نخورده می‌ماند؛ بیرون از آن‌ها CRLF→LF، NBSP→فاصله و حذفِ BOM. نیم‌فاصله
  می‌ماند.
- **کامنت‌ها:** همه، قدیمی‌ترین اول، با نویسنده و تاریخ (و «edited» اگر ویرایش
  شده)، بدونِ کوتاه‌کردن.
- **خطاها:** 401 → ورود، 403 → دسترسی، 404 → پیدا نشد، 429 → محدودیتِ نرخ (با
  Retry-After)، 5xx → خطای سرور، شبکه/timeout/SSL → اتصال. هیچ شکستی نتیجه‌ی
  خالی نمی‌سازد.

### پیوست‌ها

| پسوند | بررسیِ نوعِ واقعی | استخراج |
|---|---|---|
| `.txt` `.md` `.yaml` `.yml` | UTF-8 (یا UTF-16 با BOM)؛ امضای فایلِ دودویی یا بایتِ NUL → failed | متن |
| `.json` | مثلِ بالا + `json.loads`؛ نامعتبر → با یادداشت می‌آید | متن |
| `.pdf` | امضای `%PDF-` | متنِ هر صفحه با `pypdf` (بدونِ OCR) |

هر پیوست یکی از این وضعیت‌ها را می‌گیرد و هیچ‌کدام بی‌صدا حذف نمی‌شود:
`processed`، `unsupported` (دانلود نمی‌شود)، `too_large` (بیش از سقف؛ دانلود
نمی‌شود یا همان‌جا قطع می‌شود)، `failed` (دانلود ناموفق، نوعِ واقعی متفاوت، PDFِ
رمزدار/خراب/بدونِ متن). متنِ بلندتر از سقف کوتاه و صریحاً علامت‌گذاری می‌شود.
پیوست‌ها هرگز اجرا نمی‌شوند و YAML عمداً parse نمی‌شود.

> نمونه‌ی واقعی: پیوستِ `ExcelType.txt` در `PODIUM-3386` با وجودِ پسوندِ `.txt`
> فایلِ دودویی است (امضای `EPFS` و بایت‌های NUL) و درست به‌عنوانِ `failed — not
> text` گزارش می‌شود.

---

## F. متنی که قدمِ اول می‌گیرد

```
# Task PROJ-123: <عنوان>
Source: jira PROJ-123 (https://<jira>/browse/PROJ-123) · type: Story · status: …

How to read this task: the Description is the original requirement. Comments …

## Description (original requirement)
…
## Comments (N, oldest first)
### Comment 1 — <نویسنده>, 2026-10-14 18:09 +03:30
…
## Attachments (1 processed, 1 not processed)
### Attachment: sample.json (application/json, 120 bytes)
~~~~json
…
~~~~
### Attachments not processed
- shot.png: unsupported file type — …
```

پرامپت‌های قدمِ اول (`step1_task_analysis.md`، `step1_task_plan.md`) بخشِ
«IMPORTED ISSUES» دارند: توضیح نیازمندیِ اصلی است؛ کامنت‌ها بحثِ بعدی‌اند و هر
کامنتی نیازمندی نیست (تناقض → clarification)؛ پیوست‌ها کمکی‌اند و به‌تنهایی
معتبر نیستند؛ محتوای کامنت و پیوست داده است، نه دستور.

---

## G. استفاده در UI

1. قدمِ ۱ → **Task source: Jira issue**.
2. کلید (`PODIUM-3386`) یا نشانی (`https://jira.dotin.ir/browse/PODIUM-3386`) را
   وارد کنید و **Fetch issue** را بزنید. هیچ قدمی خودکار اجرا نمی‌شود.
3. وضعیت‌ها: ورود (با نامِ نمایشی)، issue، تعدادِ کامنت‌ها، پیوست‌های
   پیداشده/پردازش‌شده/پشتیبانی‌نشده/ناموفق، جدولِ پیوست‌ها و هشدارها. در
   شکست، مرحله مشخص است («Jira sign-in failed» در برابرِ «Issue not accessible»).
4. متنِ ساخته‌شده در کادرِ «Task Description» است (قابلِ ویرایش) و در
   «Extracted Jira content» به‌صورتِ متنِ خام دیده می‌شود.
5. **Run Step 1** یا **Run All** — همان مسیرِ ورودیِ دستی.

اگر کلید را عوض کنید و دوباره دریافت نکنید، هشدار و جلوگیری از اجرا می‌بینید.
حالتِ **Manual description** دقیقاً رفتارِ قبلی است.

---

## H. تست‌ها

| فایل | پوشش |
|---|---|
| `tests/unit/task_sources/fake_jira.py` | Jira + ADFSِ ساختگی (همان جریانِ واقعی، داده‌ی ساختگی) |
| `tests/unit/task_sources/test_jira_client.py` | جریانِ ADFS، MFA، میزبانِ نادرست، PAT، نشست و ورودِ دوباره، کدهای خطا، SSL، timeout، صفحه‌بندی، دانلود |
| `tests/unit/task_sources/test_jira_settings.py` | کلید و نشانیِ issue، تنظیمات و متغیرهای محیطی |
| `tests/unit/task_sources/test_attachments.py` | نوعِ واقعی، سقفِ حجم، PDF، JSON، کدگذاری |
| `tests/unit/task_sources/test_task_input.py` | پاک‌سازی، wiki markup، متنِ ساختاریافته |
| `tests/unit/task_sources/test_jira_importer.py` | issue → TaskInput و گزارشِ هر مرحله |
| `tests/unit/ui/test_jira_input.py` | وضعیتِ ویزارد، کهنه‌شدن، Run All، حالتِ دستی |
| `tests/unit/ui/test_app_wizard.py` (`TestJiraInput`) | UI روی runtime واقعیِ Streamlit |

هیچ تستی به Jiraِ واقعی، اعتبارنامه یا شبکه نیاز ندارد. آزمونِ واقعی جداست و
دستی اجرا می‌شود (با `.env` واقعی):

```python
from src.task_sources.jira import import_issue
result = import_issue("PODIUM-3386")
print(result.ok, result.error_stage, result.error)
```

---

## I. محدودیت‌ها

- **ADFS به HTMLِ صفحه‌ی ورود وابسته است.** تغییرِ صفحه‌ی ADFS یا فعال‌شدنِ MFA
  برای حساب، ورود را متوقف می‌کند (با خطای روشن)؛ PAT پایدارتر است.
- **PAT با توکنِ واقعی آزموده نشده است.**
- **رمزِ سازمانی در `.env` نگه داشته می‌شود** و نشستِ Jira بینِ همه‌ی کاربرانِ
  همان پروسه‌ی Streamlit مشترک است (اعتبارنامه هم سراسری است).
- **PDF:** فقط متنِ قابلِ استخراج؛ PDFِ اسکن‌شده (تصویر) OCR نمی‌شود.
- **فقط شش نوع فایل؛** تصویر، Word، Excel و فایل‌های فشرده پشتیبانی نمی‌شوند.
- **wiki markup ترجمه نمی‌شود؛** فقط بلوک‌های کد fence می‌شوند.
- **زنجیره‌ی گواهیِ jira.dotin.ir ناقص است** و تا اصلاحِ سرور `JIRA_CA_BUNDLE`
  لازم است.
- **متنِ بلند:** کامنت‌ها کوتاه نمی‌شوند؛ issueِ بسیار بزرگ می‌تواند پرامپتِ
  قدمِ اول (و تحلیلِ معناییِ قدمِ دوم) را بزرگ کند.
- **Developed Services** از Jira پر نمی‌شود؛ endpointهای داخلِ کامنت‌ها فقط در
  متنِ توصیف‌اند.
