# TestCase Box — اجرا (M7.1 تا M7.7)

این سند فقط چیزی را توصیف می‌کند که **پیاده شده است**: Gate، مالکیت، برنامه‌ریزی، runner (M7.1–M7.5)،
بازیابی، rerun و نظارتِ پروسه (M7.6) و رابطِ کاربری (M7.7).

## A. لایه‌ها

| لایه | ماژول | وضعیت |
|---|---|---|
| Execution Gate ِ مشترک (Batch و Box هرگز هم‌زمان) | `src/execution_gate/` | پیاده (M7.1)؛ Batch می‌گیرد (M7.2)؛ Box می‌گیرد (M7.5) |
| مالکیتِ run در پایگاه (schema v4، lease، heartbeat، پایانِ اتمی) | `src/testcase_box/storage.py`, `migrations.py` | پیاده (M7.3) |
| پروفایلِ اجرای run | `src/testcase_box/run_profile.py` | پیاده (M7.4) |
| برنامه‌ریزیِ run | `src/testcase_box/run_planning.py` | پیاده (M7.4) |
| ساختِ کالکشنِ Newman | `src/testcase_box/collection_preparation.py` | پیاده (M7.4) |
| runner (Gate، lease، Newman، artifact، لغو) | `src/testcase_box/runner.py` | پیاده (M7.5) — بخشِ G |
| بازیابی و بازپس‌گیریِ مالکِ کهنه | `src/testcase_box/recovery.py` | پیاده (M7.6) — بخشِ H |
| میزبانیِ اجرا برای UI، rerun | `src/testcase_box/run_service.py` | پیاده (M7.6) — بخشِ I |
| نظارتِ پروسه‌ی Newman (Job Object) | `src/test_execution/newman/supervision.py` | پیاده (M7.6، Windows) — بخشِ J |
| مدلِ نمایشیِ run و artifactِ امن | `src/testcase_box/run_views.py` | پیاده (M7.7) |
| UI ِ اجرا (انتخاب، پیشرفت، لغو، گزارش، rerun، بازیابی) | `ui/box_runs.py` (از `ui/box_app.py`) | پیاده (M7.7) — بخشِ K |

برنامه‌ریزی، پروفایل و ساختِ کالکشن (M7.4) هیچ چیزی اجرا یا در پایگاه نمی‌نویسند؛ فقط runner
(بخشِ G) Gate می‌گیرد، run می‌سازد و Newman را اجرا می‌کند.

## B. پروفایلِ اجرا (`RunProfile`)

نامِ کلاس عمداً `RunProfile` است، چون `profiles.ExecutionProfile` (M4) چیزِ دیگری است: نامِ
متغیرهایی که در environment/globals مقدارِ نهاییِ غیرِ خالی یا خالی دارند (بدونِ مقدار).

| فیلد | پیش‌فرض | مجاز |
|---|---|---|
| `schema` | 1 | فقط 1 (در `from_dict` اجباری؛ نسخه‌ی دیگر خودکار تبدیل نمی‌شود) |
| `host_intent` | — (اجباری) | `NORMAL` یا `VDI` — هرگز حدس زده نمی‌شود |
| `request_timeout_ms` | 30٬000 | 1٬000 تا 30٬000 |
| `process_timeout_s` | 900 (۱۵ دقیقه) | 60 تا 900 |
| `bail` | false | فقط false |
| `insecure` | false (TLS تأیید می‌شود) | فقط false |

`delay_request_ms` ِ موتور عمداً جزوِ پروفایل نیست (جزوِ گزینه‌های تأییدشده‌ی D10 نیست و با یک
درخواست در هر unit کاربردی ندارد)؛ اگر در ورودی بیاید مثلِ هر فیلدِ ناشناخته‌ی دیگر رد می‌شود و
اجرا همیشه پیش‌فرضِ ۰ ِ خودِ `NewmanOptions` را به کار می‌برد.

فیلدِ ناشناخته (آرگومانِ Newman، مسیرِ اجرایی، اسکریپت، توکن، …) رد می‌شود و پیامِ خطا فقط
نامِ فیلد را دارد، هرگز مقدار را. `to_json()` قطعی است (کلیدهای مرتب) و `fingerprint` همان
SHA-256 است. `newman_option_values()` آرگومان‌های `NewmanOptions` ِ موتورِ موجود را (با همان
نام‌ها) می‌دهد؛ بسته‌ی Box هیچ import ای از موتور ندارد (مرزِ
`test_box_modules_import_only_stdlib_and_box`) و پیش‌فرض‌ها از `newman.models` کپی شده‌اند — تست
هم‌خوانی را بررسی می‌کند. runnerِ M7.5 به موتور نیاز دارد، پس آن مرز در M7.5 باید آگاهانه
بازبینی شود.

`profiles.variable_profile_from_documents(profile_id, environment, globals)` از محتوای
**از پیش parse‌شده‌ی** environment و globals یک `ExecutionProfile` می‌سازد (environment بر globals
مقدم؛ فقط ورودی‌های enabled؛ مقدارِ None یا سفید → خالی). فایلی نمی‌خواند، مقداری برنمی‌گرداند
و `token` را کنار می‌گذارد (runtime تزریقش می‌کند).

## C. برنامه‌ریزیِ run

```python
plan = plan_new_run(store, [case_id, ...], profile=run_profile, variables=variable_profile)
plan = plan_rerun(store, finished_run_id, profile=run_profile, variables=variable_profile)
```

- **فقط خواندن** از `BoxStore`. هر caseِ انتخاب‌شده دقیقاً یک `PlanEntry` است (D2: یک case = یک
  unit؛ `position` همان شماره‌ی unit است). هیچ انتخابی بی‌صدا حذف نمی‌شود — شناسه‌ی نامعتبر
  (`SELECTION_INVALID`)، تکراری (`SELECTION_DUPLICATE`) یا ناموجود (`CASE_NOT_FOUND`) هم
  ورودیِ مسدود است.
- **نسخه:** run ِ تازه نسخه‌ی جاری را سنجاق می‌کند و caseِ بایگانی را مسدود می‌کند
  (`CASE_ARCHIVED`). rerun (فقط برای runِ پایانی) همه‌ی caseهای همان run را با **همان نسخه‌های
  سنجاق‌شده و همان ترتیب** برنامه‌ریزی می‌کند، حتی اگر case اکنون بایگانی باشد (D6، R4)؛ هرگز
  نسخه‌ی جاری جایگزین نمی‌شود. نسخه‌ی گم‌شده (`PINNED_VERSION_MISSING`)، متعلق به caseِ دیگر
  (`PINNED_VERSION_INVALID`) یا با اثرانگشتِ ناهم‌خوان (`VERSION_INTEGRITY_FAILED`) مسدود است.
- **سیاستِ بی‌اسکریپت** (بخشِ E)، سپس `prepare_case` ِ M3/M4 بی‌تغییر. فقط `PREPARED` واجد است؛
  وضعیتِ دیگر → `EXECUTION_NOT_PREPARED` همراهِ یافته‌های M3/M4.
- **هشدار جدا از مسدودکننده:** یافته‌های WARNING/INFO (از جمله D7:
  `SCRIPT_TOKEN_ACCESS`، `SCRIPT_MUTATES_REQUEST`) در `warnings` می‌مانند و هرگز caseی را واجد
  یا مسدود نمی‌کنند.
- **منشأ:** همه‌ی رکوردهای منشأِ case (`source_type`، `source_reference`، نسخه) بی‌تغییر.
- **قطعیت:** `to_dict()` و `fingerprint` برای ورودی و پایگاهِ یکسان یکسان‌اند. snapshot شاملِ
  پروفایل، نامِ متغیرها (بدونِ مقدار) و ورودی‌هاست؛ payload ِ آماده‌شده در آن نمی‌آید (از نسخه‌ی
  سنجاق‌شده بازسازی‌پذیر است و اثرانگشتش هست).
- `plan.executable` یعنی دستِ کم یک ورودیِ واجد؛ برنامه‌ی کاملاً مسدود هم معتبر است و نمایش
  داده می‌شود (استثنا نمی‌دهد).

## D. ساختِ کالکشنِ Newman

`prepare_collections(plan)` برای هر ورودیِ واجد یک `PreparedUnit` می‌سازد — کالکشنِ Postman v2.1
با **دقیقاً یک item** (همان payload ِ آماده‌شده‌ی M4، فقط با `name` ِ خنثیِ `BX-000007 v3`) — و
ورودی‌های مسدود را جدا در `blocked` نگه می‌دارد. هیچ event، auth یا variable در سطحِ کالکشن و
هیچ folder ای نیست. `validate_unit_collection` پیش از تحویل بررسی می‌کند که شکل دقیق است، item
برابرِ payload ِ آماده‌شده است (پس هیچ چیز تزریق نشده)، اثرانگشت با برنامه می‌خواند، هیچ
اسکریپت یا eventی در هیچ عمقی نیست و قراردادِ درخواستِ M3 خطای مسدودکننده ندارد. هر نقض →
`CollectionPreparationError` با `code` ِ پایدار. هیچ فایلی نوشته نمی‌شود (artifactها در M7.5
زیرِ `data/testcase_box/runs/` خواهند بود).

## E. سیاستِ اجرای بی‌اسکریپت در V1 (D1)

`run_planning.script_free_findings(payload, stored_script_fingerprint)` — ساختاری و fail-closed،
بی‌اجرا و بی‌تفسیرِ اسکریپت، و **مستقل از تأییدِ اسکریپت**:

سیاستِ تأییدشده (در هر عمقی از payload):

| ساختارِ Postman | نتیجه |
|---|---|
| `event` غایب | مجاز |
| `event: null` | مجاز |
| `event: []` | **مسدود** — `EXECUTION_EVENTS_PRESENT` |
| هر entry ِ event، حتی خالی | **مسدود** — `EXECUTION_EVENTS_PRESENT` (و اگر ممکن است اجرا شود `EXECUTION_SCRIPTS_PRESENT`) |
| شکلِ نامعتبر یا مبهم، یا ناهم‌خوانیِ اثرانگشتِ ذخیره‌شده و بازمحاسبه‌شده | **مسدود** — `EXECUTION_SCRIPT_STRUCTURE_INVALID` |

Box هیچ eventی را حذف نمی‌کند؛ caseِ مسدود همان‌طور که هست می‌ماند. همین یک تابع در برنامه‌ریزی، در
اعتبارسنجیِ کالکشن (`validate_unit_collection`) و در بررسیِ runner درست پیش از هر Newman به کار می‌رود. بررسیِ ایستا اسکریپت را «امن» نمی‌کند و
sandbox ِ Postman مرزِ امنیتی نیست؛ V1 به همین دلیل اصلاً اسکریپتی اجرا نمی‌کند.

## F. محدودیت‌ها و تصمیم‌های باز

- caseهای واردشده از Batch معمولاً اسکریپتِ تست دارند و در V1 واجد نیستند (R5 پذیرفته شده).
- caseِ بی‌اسکریپت assertion ندارد؛ نتیجه‌ی PASS ِ بی‌assertion همیشه UNVERIFIED ثبت و نمایش داده
  می‌شود (R1)، هرگز «PASS ِ تأییدشده».
- **D7 باز:** سیاستِ نهاییِ `SCRIPT_TOKEN_ACCESS` و `SCRIPT_MUTATES_REQUEST` تعیین نشده؛ فقط
  نمایش داده می‌شوند.
- **D8 نامعلوم:** رفتارِ refresh/انقضای توکن پیاده یا فرض نشده است.
- پروفایلِ متغیرها از محتوای environment/globals ساخته می‌شود؛ runner (بخشِ G) فایل‌ها را می‌خواند
  و هششان را پیش از هر unit دوباره می‌سنجد.

## G. runner (M7.5)

```python
result = BoxRunner(store_path).run(plan)     # plan از plan_new_run / plan_rerun
result.status       # COMPLETED | CANCELLED | INTERRUPTED | REFUSED | TERMINATION_UNCONFIRMED
result.units        # UnitOutcome برای هر unit: status، classification، verification
```

### مرزِ وابستگی

`runner.py` **تنها** ماژولِ بسته‌ی Box است که به `src.test_execution.newman` (موتورِ موجود، بی‌تغییر)،
`src.execution_gate` و `src.auth` وابسته است؛ هیچ ماژولِ دیگری از Box آن را import نمی‌کند. جهت:
`runner → {planning, profile, collection, storage, preparation} + {newman, gate, auth}`.
`test_box_modules_import_only_stdlib_and_box` (حالا بازگشتی) همین را بررسی می‌کند و
`test_only_the_runner_is_the_newman_boundary` جهتِ یک‌طرفه و نبودِ وابستگی به Batch/agents/UI را.

### چرخه‌ی نشست

1. **بررسیِ برنامه** (پیش از هر چیز): RunPlan ِ معتبر، پروفایلِ معتبر (از خودِ فیلدها دوباره
   ساخته می‌شود)، همه‌ی ورودی‌ها قابلِ ثبت (شناسه‌ی نامعتبر/تکراری/ناموجود → `PLAN_NOT_RECORDABLE`،
   هیچ انتخابی حذف نمی‌شود)، دستِ کم یک ورودیِ واجد (`NOTHING_TO_EXECUTE`) و ساختِ کالکشن‌ها. ریشه‌ی
   artifact (`data/testcase_box/runs/`) باید پوشه باشد (`ARTIFACT_ROOT_UNSAFE`).
2. **Gate:** `acquire("BOX", "box-plan-<fingerprint>")` — مشغول (`GATE_BUSY`) یا نامطمئن
   (`GATE_UNAVAILABLE`) → رد، هیچ run ای ساخته نمی‌شود. Gate تا پایانِ واقعیِ نشست نگه داشته می‌شود؛
   Batch در این مدت شروع نمی‌شود و برعکس.
3. **پیکربندی و برنامه:** Environment/Globals یک بار خوانده و هش می‌شوند؛ پروفایلِ متغیرهای ساخته‌شده از
   همان بایت‌ها باید با برنامه بخواند (`VARIABLES_CHANGED`، `CONFIGURATION_INVALID`) و برنامه‌ریزیِ دوباره
   باید همان fingerprint را بدهد (`PLAN_CHANGED`).
4. **run:** `create_owned_run` (یک unit برای هر ورودی؛ ورودیِ مسدود با caseِ `NOT_EXECUTABLE`)، پوشه‌ی یکتای
   `run_<id>_<UTC>_<uuid8>` و threadِ پایش؛ سپس `start_run`. unitهای مسدود → `NOT_RUN` (`BLOCKED`).
5. **توکن:** یک بار از `TokenProvider` (فقط در حافظه). شکست → `INTERRUPTED` / `AUTHENTICATION_FAILED`.
6. **هر unitِ واجد، به ترتیب:** شرط‌های توقف (لغو، شکستِ پایش، تغییرِ هشِ پیکربندی) ← بررسیِ کاملِ
   یکپارچگی ← نوشتنِ `collection.json` ← `gate.assert_held()` و `owns_run` ← `start_unit` ←
   `NewmanExecutionEngine.run(request, cancel_event)` ← `finish_unit`.
7. **پایان:** `finish_run` ِ اتمی و آزادسازیِ Gate در `finally`.

### بررسیِ یکپارچگیِ پیش از هر Newman (fail-closed)

پروفایل؛ نسخه‌ی سنجاق‌شده مالِ همان case و اثرانگشتش برابرِ payload و برابرِ برنامه؛ سیاستِ بی‌اسکریپت
(`script_free_findings`)؛ آماده‌سازیِ دوباره با همان `prepared_fingerprint`؛ ساخت و اعتبارسنجیِ دوباره‌ی
کالکشن با همان `collection_fingerprint` و دقیقاً یک item؛ نبودِ هیچ مقدارِ محرمانه‌ی Environment/Globals
یا توکن در کالکشن. هر شکست → آن unit `NOT_RUN` (`PRE_EXECUTION_BLOCKED`)، Newman شروع نمی‌شود و run
`INTERRUPTED` / `INTEGRITY_FAILED` می‌شود — هرگز هشدار یا بازسازیِ بی‌صدا.

### Newman

فقط موتورِ موجود. `NewmanRunRequest` با `collection.json` ِ همان unit، فایل‌های Environment/Globals (بی‌تغییر)،
`output_dir` = پوشه‌ی unit، `NewmanOptions(**profile.newman_option_values())` — ۳۰ ثانیه، ۹۰۰ ثانیه،
`bail=False`، `insecure=False` و `delay_request_ms` همان پیش‌فرضِ ۰ ِ موتور — و `access_token` (تزریق از راهِ
مرزِ موجودِ موتور؛ هرگز در فایل). پاک‌سازی و بررسیِ نشتِ موتور دست نخورده است.

### وضعیت‌ها و UNVERIFIED (R1)

وضعیتِ unit و case همان وضعیت‌های موجود است؛ معنای دقیق در `metadata.classification` و `verification`:

| نتیجه‌ی موتور | وضعیت | classification | verification |
|---|---|---|---|
| PASS با assertion | PASS | `VERIFIED_PASS` | `VERIFIED` |
| PASS بی‌assertion | PASS | `UNVERIFIED` | `UNVERIFIED` — درخواست فرستاده شد ولی رفتارش سنجیده نشد |
| FAIL | FAIL | `ASSERTION_FAILED` | `FAILED` |
| ERROR (شبکه/timeoutِ درخواست، timeoutِ پروسه، گزارش، پاک‌سازی/نشت، …) | ERROR | `NETWORK_ERROR` / `PROCESS_TIMEOUT` / `REPORT_INVALID` / `ARTIFACT_UNSAFE` / `ENGINE_ERROR` | `NOT_VERIFIED` |
| artifactِ غایب یا بیرون از پوشه‌ی unit | ERROR | `ARTIFACT_INVALID` | `NOT_VERIFIED` |
| لغوِ کاربر / توقف به دستِ runner | CANCELLED | `CANCELLED` / `STOPPED_BY_RUNNER` | `NOT_VERIFIED` |

UNVERIFIED هرگز موفقیتِ تأییدشده شمرده نمی‌شود (`summary.verified_pass` و `summary.unverified` جدا).
در V1 (بی‌اسکریپت) caseها assertion ندارند، پس همه‌ی PASSها UNVERIFIED اند. شکستِ تست (FAIL) از
خطای زیرساخت (ERROR) جداست.

### توقف (R2/R3)

run ِ تمام‌شده → `COMPLETED` (حتی با unitِ FAIL/ERROR — خلاصه‌اش در `summary`). لغوِ کاربر از هر session
(`request_run_cancel`) → پایش `cancel_event` ِ موتور را ست می‌کند → `CANCELLED`. هر توقفِ دیگر → `INTERRUPTED`
با `metadata.stop_reason`: `GATE_LOST`، `GATE_UNAVAILABLE`، `HEARTBEAT_FAILED`، `CONFIGURATION_CHANGED`،
`AUTHENTICATION_FAILED`، `INTEGRITY_FAILED`، `ENGINE_ENVIRONMENT_ERROR` (Node/Newman/reporter نیست،
Environment/Globals نامعتبر، میزبانِ خالی، فایلِ ورودیِ تغییرکرده، توکنِ نامعتبر، …)، `ARTIFACT_PUBLICATION_FAILED`،
`STORAGE_FAILED`، `RUNNER_EXCEPTION`. unitهای باقی‌مانده `NOT_RUN` (یا `CANCELLED`) با همان دلیل.

- **lease از دست رفت:** هیچ نوشتنِ دیگری انجام نمی‌شود (مالکِ تازه دست نمی‌خورد)؛ نتیجه `INTERRUPTED` /
  `LEASE_LOST` و run به دستِ این runner نهایی نمی‌شود.
- **پایانِ تأییدنشده (`termination_confirmed`):** فقط دو حالت اثباتِ «هیچ پروسه‌ای زنده نیست» است:
  پروسه شروع و reap شد (`process_id` و `exit_code` هر دو هست)، یا موتور یکی از خطاهای صریحِ پیش از شروع را
  بی‌`process_id` داد (`PRE_START_ERROR_KINDS`: گزینه/توکنِ نامعتبر، Node/Newman/reporter نیست، خطای preflight،
  پوشه‌ی خروجی/خصوصی، `PROCESS_START_FAILED`) یا لغو پیش از شروع بود. موتور `process_id` را فقط وقتی
  `run_process` برگردد پر می‌کند، پس هر نتیجه‌ی دیگری — نبودِ `process_id` به‌تنهایی، `INTERNAL_ERROR` ِ catch-all ِ
  موتور (مثلاً وقتی `taskkill` در `_kill_tree` به timeout می‌خورد)، PASS/FAIL بی‌`process_id` — یا استثنای
  `engine.run` → `TERMINATION_UNCONFIRMED`: هیچ unitِ دیگری شروع **نمی‌شود**، run نهایی **نمی‌شود** و Gate **آزاد
  نمی‌شود** (lease ِ آن تا پایانِ همان پروسه‌ی برنامه نگه داشته می‌شود). آزاد شدن فقط با بسته شدنِ پروسه‌ی برنامه است.
- **درخواستِ دیرهنگام:** پس از آخرین unit، وضعیت از نتیجه‌های واقعیِ unitها می‌آید. لغو یا تغییرِ پیکربندی‌ای که
  فقط پس از پایانِ واقعیِ همه‌ی unitها دیده شود runِ کامل را `CANCELLED`/`INTERRUPTED` نمی‌کند (`requested_action`
  ِ ثبت‌شده در ردیفِ run می‌ماند). ولی اگر unitی وسطِ اجرا متوقف شده باشد، توقف واقعی است: شکستِ پایش → همان دلیل،
  درخواستِ کاربر → `CANCELLED`، و unitِ لغوشده بی‌درخواست → `INTERRUPTED`.
- خطای پاک‌سازی (آزادسازیِ Gate، نهایی نشدنِ run) در `result.cleanup_errors` می‌آید و پنهان نمی‌شود.

### artifactها و رازها

`data/testcase_box/runs/run_<id>_<UTC>_<uuid8>/unit_<NNN>/` شاملِ `collection.json` و پوشه‌ی منتشرشده‌ی
موتور (`<newman_run_id>/` با خروجی‌های پاک‌شده). پوشه‌ها با `exist_ok=False` ساخته و فایل‌ها با `open("x")` نوشته
می‌شوند — هرگز بازنویسی و هرگز خودکار حذف نمی‌شوند. گزارش‌های خام فقط در پوشه‌ی خصوصیِ موقتِ موتورند.
پایگاه فقط مسیرهای نسبیِ داخلِ پوشه‌ی run، شمارش‌ها، کدهای وضعیتِ HTTP، `error_kind` و اثرانگشت‌ها را
نگه می‌دارد؛ فراداده‌ی هر unit پیش از ثبت با رازهای Environment/Globals و توکن سنجیده می‌شود (نشت →
`ARTIFACT_UNSAFE`). توکنِ دسترسی و توکنِ مالکیت در پایگاه، فراداده، artifactها، نام‌ها، repr و پیام‌ها نیستند.

### مرزها

runner فقط از راهِ `BoxRunService` (بخشِ I) میزبانی می‌شود؛ بازیابی در بخشِ H و UI در بخشِ K است.

## H. بازیابیِ runِ رهاشده (M7.6)

```python
candidates = inspect_runs(store_path, gate_factory=..., stale_after=60)   # فقط‌خواندنی
result = recover_run(store_path, run_id, gate_factory=..., expected_generation=g)
result.status   # RECOVERED | ALREADY_FINAL | BLOCKED | FAILED
```

- `recovery.py` فقط به `src.execution_gate` وابسته است (نه runner، نه موتور، نه auth) — `test_contract`.
- **heartbeat ِ کهنه به‌تنهایی هرگز مجوزِ بازپس‌گیری نیست (G2).** ترتیب: بررسی (نهایی → `ALREADY_FINAL`؛
  بی‌lease → `NOT_LEASED`؛ نسلِ lease عوض شده → `OWNER_CHANGED`؛ هنوز تازه → `NOT_STALE`)، سپس گرفتنِ
  **Gate ِ مشترک** با `BOX box-recovery-<id>`. اگر Batch یا Box ِ دیگری — حتی پروسه‌ی معلق — Gate را دارد،
  `GATE_BUSY`؛ اگر Gate تأییدپذیر نیست، `GATE_UNAVAILABLE`. داشتنِ Gate یعنی هیچ اجرای زنده‌ای نیست.
- زیرِ Gate: بررسیِ دوباره، `take_over_run(expected_generation, stale_before)` (fencing با `lease_generation`؛
  دو بازیابیِ هم‌زمان فقط یکی موفق می‌شود)، سپس **یک** `finish_run` ِ اتمی.
- طبقه‌بندی: unitِ تمام‌شده دست نمی‌خورد (PASS/FAIL حفظ می‌شود)؛ unitِ در صف → `NOT_RUN` (یا `CANCELLED`
  اگر لغو درخواست شده بود)؛ unitِ RUNNING → `ERROR` با `classification=OUTCOME_UNKNOWN` و
  `verification=NOT_VERIFIED` — **هرگز PASS**. run: همه‌ی unitها تمام‌شده → `COMPLETED`
  (`RECOVERED_FINALIZATION`)؛ لغوِ درخواست‌شده → `CANCELLED`؛ وگرنه `INTERRUPTED`
  (`RECOVERED_AFTER_APPLICATION_TERMINATION`). فراداده‌ی run: `summary`، `recovery` (دلیل، زمان، PID/host و
  نسلِ مالکِ قبلی، unitهای نامعلوم و اجرانشده) و `stop_reason`.
- شکستِ نهایی‌سازی → `FAILED`؛ run leased و بازیابی‌پذیر می‌ماند. Gate همیشه در `finally` آزاد می‌شود.
- `TERMINATION_UNCONFIRMED`: runner run را نهایی نمی‌کند و Gate را تا پایانِ پروسه نگه می‌دارد
  (`_UNRELEASED_GATES`)؛ پس بازیابی تا خروجِ آن برنامه `GATE_BUSY` است. هیچ «force» وجود ندارد.

## I. rerun و میزبانیِ اجرا (M7.6)

- `BoxRunService` (یکی برای هر پروسه و هر پایگاه، `get_run_service`) runner را در threadِ پس‌زمینه‌ی
  `box-run-host` اجرا می‌کند؛ حداکثر یک اجرا در هر پروسه (`ALREADY_RUNNING`)، هم‌زمانی با Batch را Gate رد
  می‌کند (`GATE_BUSY` با نامِ دارنده). rerun ِ صفحه‌ی Streamlit هیچ اجرایی را دوباره شروع نمی‌کند.
- لغو فقط از راهِ `request_run_cancel` در پایگاه است؛ runner و موتور آن را انجام می‌دهند.
- **rerun (R4):** `plan_rerun(run_id)` کلِ run ِ مبدأ را با **همان نسخه‌های سنجاق‌شده و همان ترتیب**
  برنامه‌ریزی می‌کند — حتی اگر case بایگانی (D6) یا ویرایش شده باشد. پروفایلِ پیش‌فرض همان پروفایلِ ثبت‌شده‌ی
  مبدأ است. سیاستِ بی‌اسکریپت، Environment/Globals ِ **جاری** و بررسیِ یکپارچگی دوباره اعمال می‌شوند؛
  متغیرِ لازمِ ناموجود → مسدود (`VARIABLE_UNAVAILABLE`)، با پیامِ صریح. rerun runِ تازه‌ای با
  `retry_of_run_id` و پوشه‌ی artifactِ تازه است؛ runِ مبدأ هرگز تغییر نمی‌کند. runِ نیمه‌کاره هرگز خودکار
  تکرار نمی‌شود.

## J. نظارتِ پروسه‌ی Newman (M7.6، مهار پیش از اجرا در پذیرشِ نهایی)

- Windows، به ترتیب: (۱) Job Object ساخته و با `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` پیکربندی می‌شود؛
  (۲) پروسه با `CREATE_SUSPENDED` ساخته می‌شود — threadِ اصلی هیچ کدِ user-mode ای (loader، Node،
  runner.js) اجرا نکرده؛ (۳) `assign` پروسه را به job می‌سپارد و با `IsProcessInJob` تأیید می‌کند؛
  (۴) فقط پس از تأیید، `start` تنها threadِ پروسه را resume می‌کند (پیدا کردن با Toolhelp32، چون `Popen`
  handle ِ thread را می‌بندد؛ انتظارِ دقیقاً یک thread و شمارِ تعلیقِ ۱). پس پروسه پیش از مهار هیچ کدی
  اجرا نمی‌کند و هیچ درخواستی نمی‌فرستد، و هر فرزندش خودکار عضوِ job است.
- اگر برنامه (حتی با kill) بمیرد، سیستم‌عامل handle ِ job را می‌بندد و Node و هر فرزندش کشته می‌شوند — پس
  مالکِ تازه‌ی Gate هرگز با فرزندِ قدیمی هم‌زمان نیست (تست با پروسه‌های واقعی).
- fail-closed: ساختِ job ناموفق → پروسه ساخته نمی‌شود؛ assign یا resume ِ ناموفق → پروسه‌ی معلق کشته
  می‌شود؛ `start` بدونِ assign ِ تأییدشده رد می‌شود؛ هر استثنای پیش‌بینی‌نشده پیش از resume پروسه‌ی معلق
  را می‌کشد. handleها (job، snapshot، thread) در همه‌ی مسیرها بسته می‌شوند.
- محدودیت‌های باقی‌مانده: اگر پروسه‌ی Python درست بینِ ساختِ پروسه‌ی معلق و assign بمیرد، آن پروسه معلق
  می‌ماند (هیچ کدی اجرا نکرده؛ کسی resume اش نمی‌کند) تا دستی بسته شود؛ POSIX پوشش داده نشده (no-op).

## K. رابطِ کاربری (M7.7)

`ui/box_app.py` دو بخشِ تازه دارد (پیش‌فرض همچنان «Library»)؛ پیاده‌سازی در `ui/box_runs.py` است و فقط
از `BoxRunService`، `run_views` و پیامِ نتیجه‌ها استفاده می‌کند — هرگز Newman، Gate یا SQLِ نوشتنی.

- **Run tests:** چند-انتخابیِ caseهای فعال (به ترتیبِ انتخاب)؛ caseهای بایگانی‌شده انتخاب‌پذیر نیستند و
  جداگانه فهرست می‌شوند. پروفایل: host (NORMAL/VDI، بی‌پیش‌فرض)، request timeout ۱۰۰۰–۳۰۰۰۰ ms، process
  timeout ۶۰–۹۰۰ s؛ bail همیشه خاموش و TLS همیشه تأییدشده. «Preview run plan»: جدولِ واجد/مسدود با دلیل،
  هشدارها، تعدادِ unit، «1 test case = 1 unit» و فقط **نامِ** متغیرها. «Start run» با token ِ یک‌بارمصرف.
- **Runs:** جدولِ تاریخچه؛ جزئیات در `st.fragment` که فقط در حالِ اجرا خودکار نوسازی می‌شود: پیشرفت از
  وضعیت‌های ثبت‌شده، شمارش‌ها (PASS ِ تأییدشده و UNVERIFIED جدا، با توضیحِ UNVERIFIED)، دلیلِ توقف/بازیابی،
  دکمه‌ی لغو، artifactها (موجود: دانلود؛ ناموجود یا ناامن: «در دسترس نیست» با دلیل — فقط نام‌های مجاز
  داخلِ پوشه‌ی همان run)، rerun (آماده‌سازی، پیش‌نمایش، شروع) و پنلِ بازیابی (بدونِ دکمه‌ی force).
- پیام‌ها فقط پس از نتیجه‌ی واقعی نشان داده می‌شوند و پیش از هر بازخوانیِ کامل مصرف نمی‌شوند.

## L. محدودیت‌ها (M7.6/M7.7)

- D7 و D8 همچنان باز؛ caseهای دارای اسکریپت یا هر `event` (حتی `[]`) اجرا نمی‌شوند.
- یک اجرای Box در هر پروسه؛ فقط تک‌میزبان (G3).
- نظارتِ پروسه فقط روی Windows ثابت شده (بخشِ J).
- artifactها هرگز خودکار حذف نمی‌شوند (D9)؛ پاک‌سازی دستی است.
- پایگاهِ واقعی خودکار به v4 مهاجرت داده نشده است (D3)؛ مهاجرت فقط روی پایگاه‌های موقت تست شده است.
