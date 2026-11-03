# Batch Testing — کشف، اجرای ترتیبی و Newman

این سند رفتارِ **پیاده‌شده‌ی** صفحه‌ی «Batch testing» را توصیف می‌کند: کدام issueها
واجدِ شرایط‌اند، هر issue چطور اجرا می‌شود، وضعیت‌ها چه معنایی دارند، کنترل‌ها و
بازیابی، اجرای Newman، artifactها و محدودیت‌ها. جزئیاتِ داخلیِ پنج قدم (از جمله
قاعده‌های انتخابِ میزبان) در [project-architecture.md](project-architecture.md)،
ورود به Jira در [jira-integration.md](jira-integration.md) و مسیرِ `run_all` در
[workflow-execution.md](workflow-execution.md) است و این‌جا تکرار نمی‌شود.

---

## A. نمای کلی

```
بُردِ Jira ──discover (فقط GET)──► issueهای واجدِ شرایط ──کاربر کنار می‌گذارد──► batch (SQLite)
                                                                                 │
                       BatchService (یکی در هر پروسه) ── thread ──► BatchRunner (lease + heartbeat)
                                                                                 │ هر issue، یکی‌یکی
                                                                                 ▼
             IssueExecutor: Jira (فقط خواندن) → run_all قدم ۱..۵ → Newman → artifactهای پاک‌شده
```

| فایل | مسئولیت |
|---|---|
| `src/task_sources/jira/agile.py` | بُردها، پیکربندیِ ستون‌ها، اسپرینت‌های فعال، `search` (همه GET) |
| `src/batch_testing/discovery.py` | سنجشِ سه شرط و دلیلِ هر رد |
| `src/batch_testing/models.py` | وضعیت‌ها، گذارهای مجاز، رکوردها |
| `src/batch_testing/storage.py` | SQLite: تراکنش‌ها، lease، تغییرناپذیریِ تاریخچه، migrationها |
| `src/batch_testing/service.py` | ساختِ batch، میزبانیِ runner برای UI، لغوِ batchِ ایستاده |
| `src/batch_testing/runner.py` | اجرای ترتیبی، Pause/Cancel/Resume/Retry، بررسیِ هشِ پیکربندی |
| `src/batch_testing/recovery.py` | بازیابیِ runnerِ ازکارافتاده |
| `src/batch_testing/executor.py` | اجرای یک issue، دروازه‌ی اجراپذیری، پاک‌سازیِ artifactها |
| `src/batch_testing/artifacts.py` | دسترسیِ امن به artifactهای منتشرشده |
| `src/batch_testing/summary.py` | شمارش و تاریخچه برای داشبورد |
| `src/test_execution/newman/` | موتورِ Newman (بخش H) |
| `ui/batch_app.py` | صفحه‌ی Batch: New batch، Batch (داشبورد)، History |

---

## B. راه‌اندازی و استفاده

**پیش‌نیازها** (علاوه بر [installation.md](../installation.md)):

- تنظیماتِ Jira در `.env` — همان تنظیماتِ ورودیِ Jiraِ ویزارد
  ([jira-integration.md](jira-integration.md) بخش B).
- توکنِ دسترسیِ Podium در `.env` (بخش H، «توکنِ دسترسی»):
  `PODIUM_TOKEN_REFRESH_URL` (پیش‌فرض فقط `https://`) و `PODIUM_REFRESH_TOKEN`؛
  اختیاری: `PODIUM_TOKEN_CA_BUNDLE`، `PODIUM_TOKEN_CONNECT_TIMEOUT_SECONDS` (۱۰)،
  `PODIUM_TOKEN_READ_TIMEOUT_SECONDS` (۳۰)، `PODIUM_TOKEN_ALLOW_INSECURE_HTTP` (فقط
  Dev — هشدارِ بخش H). `.env` در `.gitignore` است.
- Node.js و Newman در ریشه‌ی مخزن: `npm ci` (نسخه‌های ثابتِ `package.json`:
  `newman` 6.2.3، `newman-reporter-htmlextra` 1.23.1). موتور خودِ ماژولِ Newman را
  با `node` اجرا می‌کند.
- فایل‌های Postman (فقط خوانده می‌شوند):
  `src/agents/test_case_generator/postmanEnv/dev.postman_environment.json` و
  `workspace.postman_globals.json`.

**اجرا:** `streamlit run ui/app.py` → در ناوبری **Batch testing** (صفحه‌ی پیش‌فرض
همچنان Single issue است).

1. **New batch:** Load boards → انتخابِ بُرد → **Discover issues**. issueهای واجدِ
   شرایط با تیکِ «Include» و ناواجدها با «Failed criteria» و دلیل نمایش داده
   می‌شوند. کاربر می‌تواند issueهای واجدِ شرایط را کنار بگذارد.
2. پیش از ساخت، مسیر و SHA-256ِ Environment و Globals نشان داده می‌شود (محتوا
   هرگز). **Create batch** فقط issueهای انتخاب‌شده را منجمد می‌کند.
3. **Batch:** Start، پیشرفت (نوسازیِ خودکار هر ۲ ثانیه هنگامِ اجرا)، جدولِ issueها
   با فیلتر، جزئیاتِ هر تلاش، دانلودِ artifact، Retry.
4. **History:** همه‌ی batchها با شمارش‌ها.

مکانِ داده: `data/batch_testing/batch.sqlite3` و `artifacts/batch_testing/` (هر دو
در `.gitignore`).

---

## C. کشف و شرایطِ واجدِ شرایط بودن

issue فقط وقتی واجدِ شرایط است که **هر سه** شرط برقرار باشد:

| # | شرط | سنجش |
|---|---|---|
| 1 | `status` | نامِ status دقیقاً `Manual Test` |
| 2 | `column` | issue در بُرد است (فیلترِ بُرد، و در Kanban + `subQuery`) و statusش در پیکربندیِ بُرد (`GET /rest/agile/1.0/board/{id}/configuration`) به ستونِ `QC TEST` نگاشت شده؛ نامِ ستون بدونِ حساسیت به حروف و با trimِ فاصله‌ها (`QC Test` هم قبول است) |
| 3 | `label` | برچسبِ دقیقِ `backend` (حساس به حروف؛ `Backend` رد می‌شود و دلیلش گفته می‌شود) |

- **عضویت در اسپرینتِ فعال فقط اطلاعاتی است** و هرگز نتیجه را تغییر نمی‌دهد.
  ستونِ issue در Jira فقط از نگاشتِ status→ستون می‌آید؛ اسپرینت فقط تعیین می‌کند
  نمای اسپرینتِ فعالِ بُردِ Scrum آن را نشان بدهد یا نه. issueِ backlog، اسپرینتِ
  آینده یا بسته، و issueهای بُردِ Scrumِ بدونِ اسپرینتِ فعال با همان سه شرط سنجیده
  می‌شوند؛ ستونِ «Active sprint» (Yes/No) فقط در بُردِ Scrum نمایش داده می‌شود.
- **JQL** فقط مجموعه را کوچک می‌کند: `filter = <filter> AND status in (<statusهای
  Manual Test در ستونِ هدف>)` (+ `subQuery`). برچسب و اسپرینت شرطِ JQL نیستند تا
  issueهای رد‌شده با دلیل برگردند؛ حکمِ نهایی با `evaluate` است.
- هر دلیلِ رد با نامِ شرطش (`failed_criteria`: `status` / `column` / `label`)
  ثبت می‌شود. بُردِ بدونِ ستونِ هدف، ستونِ مبهم، ستونِ بدونِ status `Manual Test`
  یا نوعِ بُردِ غیرِ Scrum/Kanban خطای روشن (`DiscoveryError`) است، نه نتیجه‌ی خالی.
- **کشف فقط می‌خواند:** همه‌ی درخواست‌های REST از نوعِ GET اند (تستِ
  `test_discovery_only_reads`).

### snapshotِ منجمد

`create_batch` فقط issueهای واجدِ شرایطِ همان کشف را به ترتیبِ کشف، منهای
کنارگذاشته‌ها، با زمینه‌ی بُرد، معیارها و دلیل‌ها (`criteria_json`، `discovery_json`)
در پایگاه ثبت می‌کند. فهرستِ issueها (`batch_issues`) با trigger تغییرناپذیر است و
batch یا اجرایی حذف نمی‌شود. batchِ خالی ساخته نمی‌شود.
**هنگامِ اجرا شرایط دوباره سنجیده نمی‌شوند،** ولی محتوای issue (توضیح، کامنت‌ها،
پیوست‌ها) در هر تلاش — از جمله Retry — تازه از Jira خوانده می‌شود.

---

## D. اجرای هر issue

runner issueها را **به ترتیبِ منجمد و یکی‌یکی** اجرا می‌کند؛ FAIL یا ERRORِ یک
issue بقیه را متوقف نمی‌کند. `IssueExecutor` هیچ منطقِ قدمی را دوباره پیاده نمی‌کند:

1. **Jira:** `load_jira_task` — همان دریافتِ فقط‌خواندنیِ ورودیِ Jiraِ ویزارد.
2. **پنج قدم:** `run_all(state, before_step=gate)` — همان خطِ لوله‌ی Run Allِ Single
   Issue (انتخابِ خودکارِ منابعِ Swagger، همان پرامپت‌ها و اعتبارسنج‌ها). دروازه
   پیش از هر قدم لغو را بررسی می‌کند و پیش از قدمِ ۳ اجراپذیری را.
3. **دروازه‌ی اجراپذیری (پیش از قدمِ ۳):** تست‌کیس اجراپذیر است اگر میزبانش
   `RESOLVED` باشد و قدمِ ۲ آن را به عملیاتِ کشف‌شده‌ای با base URL نگاشت کرده
   باشد — همان شرط‌هایی که قدمِ ۴ برای ساختِ درخواست دارد (`assess_runnability`).
   اگر هیچ تست‌کیسی اجراپذیر نباشد، خطِ لوله می‌ایستد: `NOT_EXECUTED` +
   `NEEDS_REVIEW`، **بدونِ Newman**.
4. **کالکشن:** اگر کالکشنِ قدمِ ۴ غایب باشد یا اعتبارسنج‌های قدمِ ۴ ردش کنند →
   `ERROR` (`COLLECTION_INVALID`). اگر قدمِ ۵ `blocked` بدهد → `ERROR`
   (`FINAL_REVIEW_BLOCKED`) با کدهای issueِ قدمِ ۵، **بدونِ Newman**.
   `needs_review`ِ قدمِ ۵ جلوی Newman را نمی‌گیرد.
5. **Newman** روی همان کالکشنِ پاک‌شده‌ای که نوشته شده، با Environmentِ dev و
   Globalsِ workspace (بخش H).

**تست‌کیس‌های حل‌نشده هرگز اجرا نمی‌شوند:** قدمِ ۴ برای میزبانِ `CONFLICT` /
`NEEDS_REVIEW` یا نگاشتِ ناموجود درخواستی نمی‌سازد (در `unresolved` می‌مانند) و
اگر چنین درخواستی در کالکشن باشد قدمِ ۵ آن را `blocked` می‌کند
(`unresolved_host_request`). اگر بخشی از تست‌کیس‌ها اجراپذیر باشند، فقط همان‌ها
اجرا می‌شوند و بقیه در شمارشِ «not executed» و دلیل‌های بازبینی می‌آیند.

**هم‌ارزی با Single Issue:** برای ورودیِ یکسان، قدم‌ها همان کد و همان
اعتبارسنجی‌اند. تفاوت‌ها عمدی‌اند: Batch وقتی هیچ تست‌کیسی اجراپذیر نیست پیش از
قدمِ ۳ می‌ایستد؛ کالکشنِ ذخیره‌شده‌ی Batch نام و توضیحِ پاک‌شده دارد (بخش I)؛ و
Single Issue هیچ کالکشنی را اجرا نمی‌کند. خروجیِ LLM غیرقطعی است، پس دو اجرا
(یا دو تلاش) خروجیِ یکسان را تضمین نمی‌کنند.

---

## E. وضعیت‌ها

### وضعیتِ اجرای هر تلاش

| وضعیت | معنا |
|---|---|
| `QUEUED` / `RUNNING` | فعال؛ برای هر issue حداکثر یک اجرای فعال و در کلِ پایگاه حداکثر یک `RUNNING` |
| `PASS` | Newman اجرا شد و هیچ assertionی شکست نخورد. اگر هیچ assertionی اجرا نشده باشد، `no_assertions` ثبت می‌شود |
| `FAIL` | Newman اجرا شد و دست‌کم یک assertion شکست خورد |
| `ERROR` | خطای فنی؛ `error_kind` علت را می‌گوید: `JIRA_FETCH_FAILED`، `PIPELINE_STEP_FAILED`، `PIPELINE_EXCEPTION`، `COLLECTION_INVALID`، `FINAL_REVIEW_BLOCKED`، `NEWMAN_ERROR` (timeout، خطای شبکه، خطای اسکریپت، گزارشِ ناسازگار، …)، `ARTIFACT_WRITE_FAILED`، `ARTIFACT_SANITIZATION_FAILED`، `CONFIGURATION_CHANGED`، `AUTHENTICATION_FAILED` (توکن گرفته نشد؛ هیچ کاری اجرا نشد)، و خطاهای خودِ runner |
| `NOT_EXECUTED` | پس از قدمِ ۲ هیچ تست‌کیسی اجراپذیر نبود؛ Newman اجرا نشد؛ همیشه با `NEEDS_REVIEW` |
| `INTERRUPTED` | runner پیش از پایانِ این تلاش متوقف شد (پروسه کشته شد، heartbeat قطع شد، سرور بسته شد). حدس زده نمی‌شود Newman تمام شده یا نه. «تمام‌شده» حساب نمی‌شود؛ Resume برای آن تلاشِ تازه می‌سازد |
| `CANCELLED` | پیش از شروع لغو شد (`QUEUED`)، یا لغو پیش از قدمِ بعد، پیش از Newman یا با بستنِ پروسه‌ی Newman اثر کرد. نتیجه‌ی واقعیِ تلاشی که پیش از اثرِ لغو تمام شده بود هرگز با `CANCELLED` جایگزین نمی‌شود |

کدِ وضعیتِ HTTP به‌تنهایی نقشی ندارد؛ فقط assertionهای کالکشن PASS/FAIL را تعیین
می‌کنند (بخش H). رکوردِ پایانی تغییرناپذیر است (trigger در SQLite).

### وضعیتِ بازبینی — مستقل از اجرا

`review_status` (`NONE` / `NEEDS_REVIEW`) با دلیل‌های ساختاریافته
(`TEST_CASE_REVIEW`، `CONFLICT`، `REJECTED`، `UNMATCHED`، `NOT_RUNNABLE`،
`FINAL_REVIEW_ISSUE`) و `final_review_status`ِ قدمِ ۵ (`ready` / `needs_review` /
`blocked`) جدا ذخیره می‌شوند. `PASS` + `NEEDS_REVIEW` همچنان `PASS` است.

### وضعیتِ batch

| وضعیت | گذارهای مجاز |
|---|---|
| `CREATED` | → `RUNNING` (Start)، → `CANCELLED` |
| `RUNNING` | → `PAUSED`، `COMPLETED`، `CANCELLED`، `INTERRUPTED` |
| `PAUSED` / `INTERRUPTED` | → `RUNNING` (Resume)، → `CANCELLED` |
| `COMPLETED` / `CANCELLED` | پایانی (فقط Retryِ تک‌issue) |

داشبورد هر issue را با **آخرین** تلاشش می‌شمارد؛ «تمام‌شده» یعنی `PASS`، `FAIL`،
`ERROR`، `NOT_EXECUTED` یا `CANCELLED`.

---

## F. کنترل‌ها

| کنترل | رفتار |
|---|---|
| **Start** | فقط `CREATED`. runner در threadِ پس‌زمینه‌ی سرویس اجرا می‌شود، نه در صفحه |
| **Pause** | فقط `RUNNING`؛ درخواست در پایگاه (`requested_action`). issueِ جاری کامل تمام می‌شود و پیش از issueِ بعدی batch → `PAUSED` (اگر کاری نمانده باشد `COMPLETED`) |
| **Cancel (RUNNING)** | درخواست در پایگاه؛ runner `cancel_event`ِ issueِ جاری را ست می‌کند (پروسه‌ی Newman بسته می‌شود)، اجراهای `QUEUED` → `CANCELLED` و batch → `CANCELLED`. UI تا تأییدِ runner «Cancellation requested» نشان می‌دهد |
| **Cancel (CREATED / PAUSED / INTERRUPTED)** | بدونِ runner و در یک تراکنش (`BatchStore.cancel_inactive_batch`، با تأییدِ دوم در UI): `QUEUED` → `CANCELLED`، نتیجه‌های پایانی و artifactها دست‌نخورده، هیچ تلاشی ساخته نمی‌شود. فقط وقتی هیچ leaseی نیست: leaseِ کهنه ابتدا با بازیابی آزاد می‌شود، leaseِ زنده یعنی رد |
| **Resume** | فقط `PAUSED` / `INTERRUPTED`. پیش از هر تغییر هشِ Environment/Globals بررسی می‌شود؛ با leaseِ خودش هر اجرای `RUNNING`ِ باقی‌مانده → `INTERRUPTED`. نتیجه‌های پایانی دوباره اجرا نمی‌شوند؛ issueهای `INTERRUPTED` (و `ERROR`ِ `CONFIGURATION_CHANGED` یا `AUTHENTICATION_FAILED`) تلاشِ تازه با `trigger=resume` می‌گیرند. هر نشست توکنِ دسترسیِ تازه می‌گیرد |
| **Retry** | فقط در batchِ `COMPLETED` / `CANCELLED` و برای issueی با تلاشِ پایانی. تلاشِ تازه (`trigger=retry`، `attempt` بعدی، `previous_execution_id`) — Jira، قدم‌های ۱ تا ۵ و Newman دوباره. تلاش‌های قبلی و artifactهایشان در پوشه‌ی خودشان می‌مانند |
| **Recover** | فقط برای batchِ `RUNNING` که runnerِ زنده‌ای ندارد (heartbeatِ کهنه یا بی‌مالک) و در این پروسه اجرا نمی‌شود — بخش G. leaseِ runnerِ زنده هرگز گرفته نمی‌شود |

هیچ چیزی خودکار ادامه نمی‌یابد یا دوباره اجرا نمی‌شود؛ Resume و Retry همیشه کارِ صریحِ
کاربرند. **هیچ تضمینِ «دقیقاً یک بار» برای فراخوانیِ APIها وجود ندارد:** تلاشِ
`INTERRUPTED` ممکن است پیش از قطع درخواست‌هایی فرستاده باشد و Resume یا Retry آن‌ها
را دوباره می‌فرستد.

### اجرای `RUNNING`ِ باقی‌مانده در batchِ ایستاده

اگر runner پیش از بستنِ یک اجرا (مثلاً با خطای ذخیره‌سازی) متوقف شده باشد، batchِ
`PAUSED`/`INTERRUPTED` می‌تواند هنوز یک اجرای `RUNNING` داشته باشد. لغوِ مستقیم رد
می‌شود و آن اجرا هرگز از UI پایانی نمی‌شود؛ UI هشدار می‌دهد، آن را «در حالِ اجرا»
نشان نمی‌دهد و دکمه‌ی Cancel را غیرفعال می‌کند. راهِ امن **Resume** است: runner
ابتدا lease را می‌گیرد (پس هیچ runnerِ دیگری آن را پیش نمی‌برد)، آن اجرا را
`INTERRUPTED` می‌کند و ادامه می‌دهد. **Resume همان issue را به‌عنوانِ تلاشِ تازه
دوباره اجرا می‌کند** و Pause یا Cancelِ بعدی پس از پایانِ issueِ در حالِ اجرا اثر
می‌کند.

---

## G. مالکیت، heartbeat و بازیابی

- **lease:** هر batch حداکثر یک مالک (`owner_token`) دارد و ایندکسِ یکتای جزئی فقط
  یک batchِ دارای lease را در کلِ پایگاه می‌گذارد؛ پس دو runner (حتی در دو پروسه)
  نمی‌توانند هم‌زمان مالک باشند. هر نوشتنِ runner `owner_token` می‌دهد و مالکیت در
  همان تراکنشِ `BEGIN IMMEDIATE` بررسی می‌شود؛ runnerی که lease را از دست داده
  هیچ چیزی نمی‌نویسد.
- **heartbeat:** threadِ پایش هر ۱۰ ثانیه heartbeat می‌زند (حتی وقتی issue دقیقه‌ها
  در LLM یا Newman است) و هر ثانیه Pause/Cancel را می‌خواند.
- **بازیابی:** runnerی که heartbeatش بیش از ۶۰ ثانیه قدیمی است مرده حساب می‌شود.
  `recover_stale` lease را با یک UPDATEِ شرطی («هنوز همان مالک و هنوز کهنه») پس
  می‌گیرد، اجراهای `RUNNING` را `INTERRUPTED` می‌کند، batchِ `RUNNING` را
  `INTERRUPTED` (یا اگر Cancel درخواست شده بود `CANCELLED`) می‌کند و lease را آزاد
  می‌کند. بازیابی یک بار هنگامِ ساختِ سرویس در هر پروسه اجرا می‌شود (و با دکمه‌ی
  Recover)؛ اجرای دوباره‌اش بی‌ضرر است و هیچ اجرای پایانی‌ای را تغییر نمی‌دهد.
- **بسته‌شدنِ سرور وسطِ اجرا:** threadِ runner daemon است؛ lease کهنه می‌شود و
  بازیابیِ بعدی batch را `INTERRUPTED` می‌کند. ادامه با Resume است.
- **پیکربندی:** پیش از هر issue، Retry و Resume، Environment و Globals با هشِ
  ثبت‌شده در batch مقایسه می‌شوند. فایلِ گم‌شده یا تغییرکرده یعنی آن issue اجرا
  نمی‌شود (`ERROR`، `CONFIGURATION_CHANGED`) و runner می‌ایستد؛ پس از بازگرداندنِ
  فایل، Resume همان issue را دوباره اجرا می‌کند. هرگز با پیکربندیِ دیگری اجرا نمی‌شود.

### فرضِ میزبانی — یک پروسه‌ی سرورِ Streamlit برای هر پایگاه

| موضوع | رفتار |
|---|---|
| sessionهای مرورگر | همه‌ی تب‌ها و کاربرانِ یک پروسه‌ی `streamlit run` همان یک `BatchService` را می‌بینند (رجیستریِ سطحِ پروسه، کلید: مسیرِ پایگاه). rerun یا refresh هیچ runnerی را دوباره شروع نمی‌کند؛ وضعیتِ معتبر همیشه از SQLite خوانده می‌شود |
| چند پروسه روی یک پایگاه | درستی حفظ می‌شود (lease)، Start/Resume در پروسه‌ی دیگر رد می‌شود و Pause/Cancelِ batchِ `RUNNING` از هر پروسه کار می‌کند (از راهِ پایگاه). هر پروسه سرویس و بازیابیِ آغازینِ خودش را دارد |
| **لغوِ Retry** | فقط در پروسه‌ای ممکن است که آن Retry را اجرا می‌کند (`request_cancel()` درون‌پروسه‌ای است؛ batchِ پایانی درخواستِ پایگاهی نمی‌پذیرد). پروسه‌ی دیگر دکمه‌ی لغو نشان نمی‌دهد، می‌گوید Retry در پروسه‌ی دیگری است، و `BatchService.cancel` صریحاً رد می‌کند |
| هماهنگیِ توزیع‌شده | پیاده نشده است (صفِ مشترک، لغوِ Retry از راهِ پایگاه) |

---

## H. اجرای Newman (`src/test_execution/newman/`)

موتورِ مستقل از Streamlit، Batch و Jira؛ هیچ استثنایی بیرون نمی‌دهد:

1. **preflight (فقط خواندن):** کالکشن، Environment و Globals باید وجود داشته باشند،
   JSONِ معتبر و شکلِ Postman داشته باشند. میزبانِ هر درخواست فقط `{{base_url}}` یا
   `{{base_url_vdi}}` است، کالکشن هیچ‌کدام را تعریف نمی‌کند، و مقدارِ مؤثرِ هر
   متغیرِ میزبانِ استفاده‌شده (ترتیبِ Postman: globals < collection < environment،
   فقط ورودی‌های enabled) خالی نیست. SHA-256ِ ورودی‌ها ثبت می‌شود.
2. **اجرا:** `node runner.js` با گزارش‌های خام فقط در پوشه‌ی موقتِ خصوصی
   (`<TEMP>/agents-newman-engine/run-*`، با ACLِ محدود که بررسی می‌شود). پروسه فقط
   متغیرهای محیطیِ لازم را می‌گیرد — اعتبارنامه‌های پروسه‌ی Python به Newman
   نمی‌رسند. TLS تأیید می‌شود.
3. **سقفِ زمان و لغو:** timeoutِ هر درخواست ۳۰ ثانیه و کلِ پروسه ۱۵ دقیقه. در
   timeout یا لغو، کلِ درختِ پروسه بسته می‌شود (`taskkill /T /F` در Windows). اگر
   پروسه پیش از لغو تمام شده باشد همان نتیجه می‌ماند؛ لغو و timeout هم‌زمان → لغو.
4. **پاک‌سازی:** گزارشِ JSON، HTML (htmlextra)، stdout/stderr و نتیجه‌ی درخواست‌ها
   redact و دوباره بررسی می‌شوند؛ اگر مقدارِ محرمانه‌ی شناخته‌شده‌ای مانده باشد هیچ
   گزارشی منتشر نمی‌شود و نتیجه `ERROR` است.
5. **طبقه‌بندی** از رخدادها و گزارشِ JSON، نه فقط کدِ خروج (به این ترتیب):
   `ERROR` (پروسه شروع نشد، timeout، خطای runner/Newman، گزارشِ غایب/خراب/ناسازگار،
   خطای شبکه، خطای اسکریپت) → `FAIL` (assertionِ شکست‌خورده) → `PASS`.
6. پوشه‌ی موقت در هر حالت حذف می‌شود و بررسی می‌شود که Environment/Globals دست
   نخورده‌اند (`INPUT_FILE_MODIFIED`). پوشه‌ی رهاشده پس از کشته‌شدنِ پروسه در اجرای
   بعدی پاک می‌شود.

**Authorization:** هر درخواست `Authorization: {{token}}` دارد
([project-architecture.md](project-architecture.md) بخش J) و کالکشن، Environment و
Globals متغیرِ `token`ِ خالی دارند. مقدارِ واقعی در زمانِ اجرا تزریق می‌شود
(پایین‌تر).

### توکنِ دسترسی (`src/auth/token_provider.py`)

- **گرفتن:** `TokenProvider.fetch()` یک `POST` به `PODIUM_TOKEN_REFRESH_URL` با بدنه‌ی
  JSONِ `{"refreshToken": <PODIUM_REFRESH_TOKEN>}` می‌فرستد: session تازه و بدونِ
  cookie، بدونِ دنبال‌کردنِ redirect، timeoutِ اتصال/خواندن، تأییدِ TLS (یا
  `PODIUM_TOKEN_CA_BUNDLE`) و بدونِ تلاشِ دوباره. پاسخ باید HTTP 200 با
  `result.token`ِ رشته‌ایِ ناتهی و بدونِ فاصله باشد. `result.refreshToken` خوانده
  نمی‌شود — refresh tokenِ `.env` ثابت است.
- **HTTPS به‌طورِ پیش‌فرض:** آدرسِ `https://` با تأییدِ گواهیِ TLS (هرگز خاموش
  نمی‌شود). آدرسِ `http://` رد می‌شود و هیچ درخواستی فرستاده نمی‌شود، مگر
  `PODIUM_TOKEN_ALLOW_INSECURE_HTTP=true`. فقط `true` (بی‌حساسیت به حروف و فاصله‌ی
  دوطرف) فعالش می‌کند؛ نبود، خالی، `false`، `0` و `no` یعنی خاموش و هر مقدارِ دیگر
  (`yes`، `1`، …) خطای پیکربندی است — مقدار در پیام تکرار نمی‌شود. redirect دنبال
  نمی‌شود، پس پاسخِ `https://` هرگز به `http://` نمی‌برد. این تنظیم فقط
  `TokenProvider` را تغییر می‌دهد؛ هیچ درخواستِ دیگری از برنامه اثر نمی‌گیرد.

> **⚠ هشدارِ امنیتی — `PODIUM_TOKEN_ALLOW_INSECURE_HTTP=true`:** HTTP هیچ
> رمزگذاری‌ای ندارد. refresh token (در بدنه‌ی درخواست) و توکنِ دسترسی (در پاسخ و
> سپس در هدرِ `Authorization` درخواست‌های Newman به همان میزبان) برای هر کسی که
> مسیرِ شبکه را ببیند آشکارند و ممکن است در proxyها یا لاگ‌های میانی ثبت شوند. این
> استثنا فقط برای یک محیطِ Devِ ایزوله و تأییدشده است — هرگز برای production یا
> شبکه‌ی مشترکِ نامطمئن. refresh tokenِ Dev را رازی کم‌ارزش و قابلِ چرخش در نظر
> بگیرید و اگر بیرون از آن شبکه دیده شد، باطلش کنید.

**بررسیِ دستی روی endpointِ واقعی** (`examples/podium_token_check.py`، فقط با اجرای
صریح؛ pytest جمعش نمی‌کند و هیچ توکن، بدنه یا مقدارِ `.env` چاپ نمی‌شود):

1. `python examples/podium_token_check.py` — خشک: پیکربندی و HTTP guard، هیچ درخواستی.
2. `... --send` — یک `POST` به `PODIUM_TOKEN_REFRESH_URL`؛ فقط «HTTP 200 و
   `result.token`ِ معتبر» یا نوعِ خطا گزارش می‌شود.
3. `... --send --newman` — Newmanِ واقعی با کپیِ موقتِ Environment/Globals که
   `base_url`شان به سرورِ ضبط‌کننده روی `127.0.0.1` اشاره می‌کند (هیچ APIِ کسب‌وکاری):
   سرور تأیید می‌کند `Authorization` دقیقاً `Bearer <همان توکن>` است، فایل‌های خروجی
   برای توکن جست‌وجو می‌شوند و SHA-256ِ فایل‌های واقعیِ Environment/Globals پیش و پس
   مقایسه می‌شود.
- **`.env`:** با `src/config.py` بار می‌شود (`load_dotenv`، که متغیرِ محیطیِ از قبل
  تنظیم‌شده را بازنویسی نمی‌کند) و در لحظه‌ی fetch خوانده می‌شود.
- **خطا:** `AuthenticationError` با `kind` (پیکربندی، HTTP ناامن، timeout، اتصال،
  TLS، 401/403، 5xx، وضعیتِ دیگر، پاسخِ خراب) و پیامِ ثابت — هرگز توکن، بدنه‌ی
  درخواست/پاسخ یا متنِ استثنای `requests`.
- **نشستِ Batch:** هر Start، Resume و Retry پیش از اولین issue (پیش از Jira و LLM)
  یک توکنِ تازه می‌گیرد و برای همه‌ی issueهای همان نشست به کار می‌برد؛ توکن فقط در
  حافظه است (`ExecutionRequest.access_token`) و با پایانِ نشست پاک می‌شود. نشستی که
  کاری برای اجرا ندارد توکن نمی‌گیرد.
- **شکست:** issueِ بعدی اجرا نمی‌شود و با `ERROR` / `AUTHENTICATION_FAILED`
  (`error_stage=authentication`، بدونِ هیچ فیلدِ Newman) ثبت می‌شود؛ runner با
  `authentication_failed` می‌ایستد و batch `INTERRUPTED` می‌شود. Resume توکنِ تازه
  می‌گیرد و همان issue را دوباره اجرا می‌کند. در Retry هیچ تلاشی ساخته نمی‌شود.
  `IssueExecutor` هم بدونِ توکن هیچ کاری نمی‌کند.
- **تزریق به Newman:** موتور (`NewmanRunRequest.access_token`) تنها جایی است که
  `Bearer ` اضافه می‌شود. مقدار از راهِ متغیرِ محیطیِ پروسه‌ی `runner.js`
  (`TESTAGENT_NEWMAN_ENV_VARS`) می‌رسد — نه `config.json` و نه هیچ فایلی —
  `runner.js` آن را فوراً از `process.env` حذف می‌کند و به‌صورتِ `envVar` به Newman
  می‌دهد. Newman 6.2.3 `envVar` را پس از بارکردنِ فایلِ Environment اعمال می‌کند، پس
  بر `token`ِ Environment، کالکشن و Globals مقدم است؛ این با Newmanِ واقعی و سرورِ
  محلی آزموده شده (`test_newman_token_injection.py`). فایل‌های Environment و Globals
  دست نمی‌خورند (SHA-256ِ پیش و پس). توکنِ خالی یا نامعتبر Newman را اجرا نمی‌کند
  (`ACCESS_TOKEN_INVALID`).
- **پاک‌سازی:** شکلِ خام و `Bearer`ِ توکن جزوِ مقادیرِ محرمانه‌ی موتور و
  artifactهای Batch‌اند و از گزارش‌ها، stdout/stderr، `result.json` و artifactها پاک
  می‌شوند؛ اگر نشد، هیچ گزارشی منتشر نمی‌شود.

---

## I. artifactها و پاک‌سازی

هر تلاش پوشه‌ی خودش را دارد و چیزی را بازنویسی نمی‌کند:

```
artifacts/batch_testing/<batch_id>/<issue_key>/execution_<attempt>/
    execution_summary.json   collection.json   step1.json … step5.json
    newman/<run_id>/newman-report.html   newman-report.json   result.json
```

- **فهرستِ مجاز، نه فهرستِ ممنوع:** متنِ آزاد (خلاصه، عنوان، گام‌ها، نتیجه‌ی مورد
  انتظار، دلیل‌ها، ابهام‌ها، نام‌ها، توضیح‌ها) ممکن است متنِ Jira یا بازنویسیِ مدل
  باشد و به‌جایش `{redacted, sha256, chars}` نوشته می‌شود. توضیح، کامنت و متنِ
  پیوستِ Jira هرگز ذخیره نمی‌شوند؛ فقط شناسه‌ها، enumها، شمارش‌ها، متد/مسیرِ
  عملیات‌های تطبیق‌شده با Swagger و هش‌ها.
- **کالکشنِ ذخیره‌شده همان چیزی است که Newman اجرا می‌کند:** نام‌ها و توضیح‌ها خنثی
  می‌شوند؛ URL، متد، هدر، query، بدنه و اسکریپت‌ها دست نمی‌خورند و یکسان‌بودنشان پیش
  از نوشتن بررسی می‌شود.
- **مقدارهای محرمانه‌ی شناخته‌شده** (مقدارِ متغیرهای Environment/Globals و
  «نامزدهای محرمانه‌ی Jira»: مقدارِ پس از کلیدهای حساس، Authorization، userinfo و
  query parameterهای حساسِ URLها، رشته‌های توکن‌مانند) فقط در حافظه‌اند. هر artifact
  با آن‌ها بررسی می‌شود؛ اگر یکی مانده باشد آن artifact نوشته نمی‌شود، Newman اجرا
  نمی‌شود و نتیجه `ERROR` (`ARTIFACT_SANITIZATION_FAILED`) است.
- **دانلود در UI:** فقط نام‌های فهرستِ مجاز، فقط داخلِ پوشه‌ی دقیقِ همان تلاش (پس از
  resolveِ پیوندها). مسیر هیچ‌وقت از ورودیِ کاربر ساخته نمی‌شود؛ پوشه‌ی بیرون از
  ریشه، «..»، پیوندِ بیرونی، پوشه‌ی تلاشِ دیگر و پوشه‌ی موقتِ Newman رد می‌شوند.
  گزارش‌های خام و `stdout.log` هرگز قابلِ دانلود نیستند.
- **نگهداری:** هیچ حذف یا نگهداریِ خودکاری پیاده نشده است. پایگاه و artifactها
  تا وقتی دستی پاک نشوند می‌مانند (تاریخچه در SQLite تغییرناپذیر است).

---

## J. تست‌ها

| فایل | پوشش |
|---|---|
| `tests/unit/batch_testing/test_discovery.py` | سه شرط، اسپرینتِ اطلاعاتی، Kanban/Scrum، فقط GET |
| `tests/unit/batch_testing/test_storage.py` | تراکنش‌ها، گذارها، lease و take-over، تغییرناپذیری، لغوِ batchِ ایستاده |
| `tests/unit/batch_testing/test_runner.py` | ترتیب، Pause/Cancel/Resume/Retry، از دست دادنِ lease، هشِ پیکربندی، توکنِ هر نشست و شکستِ احراز هویت |
| `tests/unit/batch_testing/test_executor.py` | دروازه‌ها، نگاشتِ وضعیت‌ها، پاک‌سازیِ artifactها، اجرای واقعیِ Newman روی سرورِ محلی |
| `tests/unit/batch_testing/test_service.py`، `test_summary.py`، `test_artifacts.py` | میزبانی، شمارش، دسترسیِ امن به artifact |
| `tests/unit/test_execution/` | موتورِ Newman: preflight، لغو، timeout، redaction، طبقه‌بندی (ساختگی + واقعی روی سرورِ محلی) |
| `tests/unit/auth/test_token_provider.py` | گرفتنِ توکن: پیکربندی، ردِ HTTP، 401/403/5xx، timeout، پاسخِ خراب، بدونِ cookie، refresh tokenِ ثابت، نبودِ راز در خطا |
| `tests/unit/test_execution/test_newman_token_injection.py` | Newmanِ واقعی روی سرورِ محلی: توکنِ زمانِ اجرا بر `token`ِ خالیِ Environment مقدم است |
| `tests/unit/ui/test_batch_app.py` | صفحه روی runtimeِ واقعیِ Streamlit |
| `tests/unit/test_case_generator/test_host_propagation.py` | انتخابِ میزبان از قدم ۱ تا ۵ (+ جریانِ قدیمی) |

هیچ تستی Jira، LLM، endpointِ توکن یا APIِ بیرونی را صدا نمی‌زند (تنظیماتِ توکنِ
`.env` در تست‌ها خنثی می‌شود — `tests/conftest.py`)؛ تست‌های Newmanِ واقعی فقط به
سرورِ HTTPِ محلیِ خودِ تست درخواست می‌دهند و بدونِ Node/Newman skip می‌شوند.

---

## K. محدودیت‌های شناخته‌شده

- **دامنه‌ی بُرد:** «در بُرد بودن» یعنی تطبیق با فیلترِ بُرد (+ `subQuery`ِ Kanban).
  تنظیماتِ سمتِ Jira که API نشان نمی‌دهد (مثلاً پنهان‌کردنِ issueهای تمام‌شده در
  Kanban، quick filterها یا swimlaneها) مدل نشده‌اند.
- **شرایط هنگامِ ساختِ batch سنجیده می‌شوند،** نه هنگامِ اجرا؛ issueی که بعداً در
  Jira جابه‌جا شود همچنان اجرا می‌شود.
- **redaction کامل نیست:** فقط مقدارهای شناخته‌شده (متغیرهای Environment/Globals،
  هدرها و کوکی‌ها و query parameterهای حساس، نامزدهای اعتبارنامه‌مانندِ Jira) و
  الگوهای «کلید=مقدار» پیدا می‌شوند؛ رازی با شکلِ دلخواه که با هیچ‌کدام نخواند
  تشخیص داده نمی‌شود. مقدارِ کوتاه فقط به‌صورتِ توکنِ کامل جست‌وجو می‌شود.
- **انقضای توکن:** هر نشستِ Batch یک توکن می‌گیرد و پس از 401 آن را خودکار تازه
  نمی‌کند (طولِ عمرِ توکنِ backend تأیید نشده). batchِ خیلی طولانی ممکن است در
  میانه به انقضا بخورد؛ نتیجه‌ی آن issueها به assertionهای کالکشن بستگی دارد.
  Pause و Resume توکنِ تازه می‌گیرد. `AccessToken.obtained_at` برای تازه‌سازیِ پیش
  از انقضا در آینده نگه داشته می‌شود.
- **HTTP فقط با opt-inِ صریح:** endpointِ `http://` بدونِ
  `PODIUM_TOKEN_ALLOW_INSECURE_HTTP=true` به `AUTHENTICATION_FAILED` می‌رسد؛ با آن،
  اعتبارنامه‌ها رمزنشده جابه‌جا می‌شوند (هشدارِ بخش H).
- **Single issue** Newman اجرا نمی‌کند؛ `TokenProvider` مستقل از Batch است تا مسیرِ
  اجرای آینده همان را به کار ببرد.
- **بدونِ تضمینِ exactly-once:** Resume و Retry ممکن است درخواست‌های API را دوباره
  بفرستند؛ ازسرگیری خودکار نیست.
- **یک پروسه‌ی سرور برای هر پایگاه** فرض شده؛ لغوِ Retry بینِ پروسه‌ها ممکن نیست.
- **اجرای `RUNNING`ِ باقی‌مانده** فقط با Resume (و اجرای دوباره‌ی همان issue) پاک
  می‌شود.
- **نگهداریِ artifactها** دستی است.
- **خروجیِ LLM غیرقطعی است**؛ دو تلاش روی یک issue ممکن است کالکشنِ متفاوتی بسازند.
- `newman-reporter-htmlextra` و Node باید نصب باشند؛ نبودشان `ERROR`
  (`NODE_NOT_FOUND` / `NEWMAN_NOT_FOUND` / `REPORTER_NOT_FOUND`) است.
