# معماری پروژه و جریانِ Test Case Generator

> این سند **پیاده‌سازیِ واقعی** را توصیف می‌کند، نه یک معماریِ ایده‌آل. هر
> ارجاع به فایل/کلاس/تابع مستقیماً به کدِ همین مخزن اشاره دارد.
> بخشِ اصلی سند جریانِ پنج‌قدمیِ `test_case_generator` است؛ بقیه‌ی پروژه
> (code review، ایجنت‌های عمومی، حافظه) فقط در حدِ نقشه‌ی کلی آمده‌اند و سندهای
> جداگانه‌ی خودشان را دارند ([index.md](../index.md)).
> مسیرِ اجرای دکمه‌ها و `Run All` (`ui/pipeline.py`)، مدیریتِ وضعیت و صادرات در
> [workflow-execution.md](workflow-execution.md) آمده است.

---

## A. نمای کلی

پروژه یک زیرساختِ multi-agent روی LLM و Embeddingِ لوکال (OpenAI-compatible)
است. سه کاربردِ اصلی دارد:

| کاربرد | ورودی | خروجی | نقطه‌ی ورود |
|--------|-------|-------|-------------|
| **تولیدِ تست‌کیس و Postman (ویزاردِ HITL)** | توصیفِ تسک + snapshotِ Swagger | Postman Collection v2.1 | `ui/app.py` (Streamlit) |
| تولیدِ تست‌کیس از Swagger/سناریوی YAML (جریانِ قدیمی) | URLِ Swagger یا فایلِ YAML در پیامِ کاربر | فایلِ `*.postman_collection.json` | `TestCaseGeneratorAgent` در `src/agents/graph.py` |
| Code review روی MRِ GitLab | شماره‌ی MR | کامنت، امتیاز، تصمیمِ approve | `python -m src.cli review` |

اصلِ معماریِ مشترک: **LLM فقط «معنا» را تفسیر می‌کند و پایتون همه‌چیز را
قطعی اعتبارسنجی می‌کند.** هیچ خروجیِ LLM بدونِ اعتبارسنجیِ سخت‌گیرانه به قدمِ
بعد نمی‌رود و هر تماسِ AI فقط از `LLMClient`/`EmbeddingClient` می‌گذرد
([core-clients.md](core-clients.md)).

---

## B. ساختارِ مخزن

```
main.py                         # فقط راهنمای اجرای examples/ را چاپ می‌کند
ui/                             # ویزاردِ Streamlit جریانِ test_case_generator
├── app.py                      # هماهنگ‌کننده‌ی پنج قدم (بدونِ منطقِ کسب‌وکار)
├── workflow.py                 # WorkflowState: قفل، تأیید/پذیرشِ خودکار، کهنه‌شدن، مصرفِ توکن
├── pipeline.py                 # execute_step + run_all (بدونِ Streamlit) — تنها محلِ صدا زدنِ Generatorها
├── step1_… step5_….py          # صفحه‌ی نازکِ هر قدم: render_step + _run (spinner دورِ pipeline)
├── formatting.py / components.py
src/
├── config.py                   # خواندنِ .env (LLM_*, EMBEDDING_*, LLM_MAX_OUTPUT_TOKENS, …)
├── llm_client.py               # LLMClient + LLMResponse (finish_reason، usage)
├── embedding_client.py         # EmbeddingClient
├── token_usage.py              # TokenUsage + collect_usage (جمعِ مصرفِ هر قدم)
├── debug/                      # DebugConfig و logger سطح‌بندی‌شده
├── observability/              # Langfuse (اختیاری)
├── task_sources/               # ورودیِ تسک از بیرون → TaskInputِ مستقل از منبع (قدم ۱)
│   ├── task_input.py / attachments.py / text.py
│   └── jira/                   # ورودِ ADFS SAML/PAT، REST v2، wiki markup (jira-integration.md)
├── agents/
│   ├── graph.py                # گرافِ LangGraphِ عمومی (router → …, test_case_generator)
│   ├── code_review/            # گرافِ code review (GitLab، Sonar، reviewer، decision)
│   └── test_case_generator/    # ← موضوعِ اصلیِ این سند
│       ├── task_analysis.py        # قدم ۱
│       ├── swagger_analyzer.py     # parse سندِ OpenAPI v2/v3 (مشترک)
│       ├── api_discovery.py        # قدم ۲ — بخشِ قطعی (کشفِ عملیات‌ها)
│       ├── swagger_snapshots.py    # قدم ۲ — رجیستریِ منابع و خواندنِ data/swagger/*.json
│       ├── source_selection.py     # قدم ۲ — انتخابِ خودکار/دستیِ منابعِ Swagger
│       ├── source_semantics.py     # قدم ۲ — تحلیلِ معناییِ تسک (فارسی/انگلیسی) با LLM
│       ├── api_relevance.py        # قدم ۲ — فیلترِ قطعیِ نامزدها پیش از LLM
│       ├── api_mapping.py          # قدم ۲ — نگاشتِ LLM + اعتبارسنجی + قراردادِ فشرده
│       ├── scenario_analysis.py    # قدم ۳
│       ├── postman_generation.py   # قدم ۴ (بدونِ LLM)
│       ├── postman_builder.py      # TestCaseSuite → Postman v2.1 (مشترک بینِ دو جریان)
│       ├── final_review.py         # قدم ۵ (بدونِ LLM)
│       ├── json_output.py          # استخراجِ یک شیءِ JSON از پاسخِ LLM
│       ├── agent.py / testcase_generator.py / scenario_parser.py  # جریانِ قدیمی
│       ├── prompts/                # پرامپت‌های هر قدم (*.md)
│       └── test_resolve.py, test_raw_llm.py, test_agent_run.py   # اسکریپت‌های دستی، نه pytest
├── checkpointer/ memory/ vector_store/ rules/ security/ cli/ api/
data/swagger/                   # snapshotهای محلیِ Swagger (admin.json، customer.json؛ crm/provider/site با Update)
tests/unit/test_case_generator/ # تست‌های واحدِ همه‌ی قدم‌ها
tests/unit/task_sources/        # Jira و ADFSِ ساختگی، پیوست‌ها، TaskInput
tests/unit/ui/                  # تست‌های workflow، pipeline/Run All، formatting و AppTestِ ویزارد
```

---

## C. اجرای جریانِ پنج‌قدمی

### ارکستراسیون

ارکستراسیون **LangGraph نیست**؛ `ui/app.py` با `WorkflowState`
(`ui/workflow.py`) قدم‌ها را هماهنگ می‌کند:

- قدم‌ها **ترتیبی** اجرا می‌شوند: قدمِ n فقط بعد از پذیرشِ قدمِ n-1 باز
  می‌شود (`is_unlocked`) — یا تأییدِ انسانی (`approve`) یا پذیرشِ خودکارِ
  Run All (`auto_advance`)، که جدا ثبت و جدا نمایش داده می‌شوند.
- اجرای هر قدم فقط در `ui/pipeline.py::execute_step` است (بدونِ Streamlit)؛
  دکمه‌ی «Run Step N» و `run_all` (دکمه‌ی Run All در قدم ۱) هر دو همین را صدا
  می‌زنند. بیرون از Run All هیچ قدمی خودکار اجرا نمی‌شود.
- هیچ اجرای موازی‌ای بینِ قدم‌ها نیست. موازی‌سازی/تکرار فقط **داخلِ** قدم‌هاست
  (دسته‌بندی و retry در قدم ۱ و ۲).
- `step_inputs(step)` نتیجه‌های قبلی را طبقِ `_PROPAGATED` پاس می‌دهد:
  قدم ۲ ← {1}، قدم ۳ ← {1,2}، قدم ۴ ← {1,2,3}، قدم ۵ ← {1,2,3,4}.
- `record_result` اگر نتیجه عوض شود، همه‌ی قدم‌های بعدی را **کهنه** (`stale`)
  می‌کند؛ نتیجه‌ی کهنه نه تأییدپذیر است و نه به قدمِ بعد پاس داده می‌شود.
- `record_error` نتیجه‌ی همان قدم را برمی‌دارد و کاربر را روی همان قدم نگه
  می‌دارد.
- هر صفحه‌ی `ui/stepN_*.py` مستقل هم اجراپذیر است؛ در حالتِ مستقل، نتیجه‌های
  قبلی به‌صورتِ JSON از کاربر گرفته می‌شوند.

```mermaid
flowchart TD
    U[کاربر: توصیفِ تسک + سرویس‌ها] --> S1
    subgraph S1[قدم ۱ — TaskAnalysisGenerator  LLM]
        P1[prompts/step1_*.md] --> L1[LLMClient.chat]
        L1 --> V1[validate_analysis / staged plan+details]
    end
    S1 -->|TaskAnalysisResult<br/>test_cases[]| H1{{تأییدِ انسانی}}
    H1 --> S2
    SW[(data/swagger/*.json)] --> SN[swagger_snapshots.load_snapshots]
    SN --> AD[api_discovery.discover_from_spec<br/>→ SwaggerAnalyzer.parse_spec]
    subgraph S2[قدم ۲ — Step2ApiMappingGenerator]
        AD --> DS[DiscoveredService / DiscoveredApi]
        DS --> REL[api_relevance.select_candidates]
        REL --> L2[ApiMappingAgent  LLM، دسته‌های ۱۰تایی + retry JSON]
        L2 --> V2[validate_mapping_payload]
    end
    S2 -->|Step2Result<br/>services[] + mappings[]| H2{{تأییدِ انسانی}}
    H2 --> C23[compact_step2_result<br/>Step3Contract]
    C23 --> S3
    subgraph S3[قدم ۳ — Step3ScenarioAnalysisGenerator  LLM]
        L3[ScenarioAnalysisAgent] --> V3[validate_scenario_payload<br/>ارجاع، ترتیب، حلقه، پارامتر]
    end
    S3 -->|Step3Result<br/>scenarios, execution_order,<br/>data_dependencies, clarifications| H3{{تأییدِ انسانی}}
    H2 -->|Step2Result کامل| S4
    H3 --> S4
    subgraph S4[قدم ۴ — Step4PostmanGenerator  بدونِ LLM]
        PL[_plan → _build_request_case] --> TS[TestCaseSuite]
        TS --> PB[PostmanBuilder.build]
        PB --> VC[validate_collection +<br/>check_variables_are_available]
    end
    S4 -->|Step4Result<br/>collection + metadata| H4{{تأییدِ انسانی}}
    H4 --> S5
    subgraph S5[قدم ۵ — FinalReviewGenerator  بدونِ LLM]
        RV[پوشش، سازگاریِ بین‌قدمی،<br/>validate_collection] --> EX[collection_json → دانلود]
    end
```

### قدم ۱ — Task Analysis

| مورد | جزئیات |
|------|--------|
| هدف | تبدیلِ توصیفِ تسک به تست‌کیس‌های کسب‌وکاریِ ساختاریافته |
| پیاده‌سازی | `TaskAnalysisGenerator` در `task_analysis.py`؛ صفحه: `ui/step1_task_analysis.py` |
| ورودی | `task_description` (اجباری)، `developed_services` (اختیاری؛ زمینه‌ی پرامپت — URLهای Swagger و endpoint داخلِ آن، همراهِ متنِ تسک، منابعِ قدم ۲ را هم تعیین می‌کنند) |
| خروجی | `TaskAnalysisResult`: `task_summary`, `identified_requirements`, `test_cases[]` (`TaskTestCase`: id, title, type, priority, preconditions, steps, expected_result, related_service, auth_intent), `clarifications`, `variables[]` (متغیرهای درخواستیِ کاربر — بخشِ K) |
| LLM | `prompts/step1_task_analysis.md`؛ اگر پاسخ بریده شود یا خالی باشد، تولیدِ مرحله‌ای: `step1_task_plan.md` (برنامه) و `step1_test_case_details.md` (جزئیات در دسته‌ها) |
| خطا | `ValueError` (ورودیِ خالی)، `TaskAnalysisError` (خروجیِ نامعتبر) — پاسخِ بریده هرگز ترمیم نمی‌شود |
| نکته | این قدم Authorization header، اسکریپت یا ارجاعِ `{{...}}` نمی‌سازد؛ فقط متغیری را که تسک صراحتاً خواسته به‌صورتِ داده در `variables` ثبت می‌کند. `auth_intent` فقط فراداده است و هدر را تغییر نمی‌دهد (بخشِ J) |

### قدم ۲ — Swagger Analysis & API Mapping

| مورد | جزئیات |
|------|--------|
| هدف | کشفِ عملیات‌های واقعیِ API و نگاشتِ هر تست‌کیس به یک عملیات (یا `null`) |
| پیاده‌سازی | `Step2ApiMappingGenerator` و `ApiMappingAgent` در `api_mapping.py`؛ کشف در `api_discovery.py`/`swagger_snapshots.py`؛ parse در `swagger_analyzer.py` |
| ورودی | `step1_result` + یا `snapshot_keys` (مسیرِ عادیِ ویزارد: هر زیرمجموعه از `admin`/`customer`/`crm`/`provider`/`site`، بدونِ شبکه) یا `service_sources` (URL/فایل) |
| انتخابِ snapshot | `ui/pipeline.plan_swagger_sources` → `source_selection.select_sources` (قطعی، بدونِ LLM و شبکه): ۱) URLِ Swagger (`urls.primaryName` یا `…/api-docs/<Group>`)، ۲) مسیرِ endpoint که در متادیتای snapshotها پیدا شود یا پیشوندِ ثبت‌شده‌ی منبع را داشته باشد، ۳) تحلیلِ معناییِ چندزبانه‌ی متنِ تسک (`source_semantics.py`، یک فراخوانیِ LLM فقط در صورتِ نیاز؛ هر منبع/عملیاتِ پیشنهادی با snapshotِ واقعی تأیید می‌شود) و در نبودِ آن تطبیقِ واژه‌ای با مسیر/operationId/tag/summary؛ همه باید فعال و دارای snapshot باشند. شاهدِ ناکافی یا URLِ ناشناخته خطاست و هیچ fallbackی (`both`) نیست. انتخابِ دستیِ قدم ۲ جایگزین نمی‌شود؛ Run All همیشه خودکار است و انتخاب را یک بار ثابت می‌کند (جزئیات: [workflow-execution.md](workflow-execution.md) بخش D) |
| خروجی | `Step2Result`: `services[]` (`DiscoveredService.to_dict()`: name, source_url, base_url, apis[], authorization, unresolved), `mappings[]` (`ApiMapping`: test_case_id, api{method,path,operation_id}\|null, confidence, reason, clarification), `clarifications[]` |
| تبدیل‌ها | `SwaggerAnalyzer.parse_spec` → `EndpointSpec` → `_to_discovered_api` → `DiscoveredApi.to_dict()` (فقط method, path, operation_id, summary, parameters, request_body, responses) |
| LLM | `prompts/step2_api_mapping.md`؛ فقط method/path/operationId/summary به پرامپت می‌رود (`_service_for_prompt`) |
| اعتبارسنجی | `validate_mapping_payload`: هر تست‌کیس دقیقاً یک نگاشت؛ هر `api` باید با `operation_key` یا operationIdِ یکتا به یک عملیاتِ کشف‌شده resolve شود |
| retry | دسته‌های `DEFAULT_MAPPING_BATCH_SIZE=10`؛ JSONِ نامعتبر یک بار (`MALFORMED_JSON_RETRIES`) دوباره؛ بریدگی دوباره تلاش نمی‌شود؛ اگر فیلترِ ارتباط چیزی کم کرده باشد، تست‌کیس‌های حل‌نشده یک بار در برابرِ کاتالوگِ کامل دوباره بررسی می‌شوند |
| خطا | `ValueError`، `ApiMappingError`؛ خطای بارگذاریِ یک منبع جمع می‌شود و به `clarifications` می‌رود |

### قدم ۳ — Scenario & Dependency Analysis

| مورد | جزئیات |
|------|--------|
| هدف | گروه‌بندیِ سناریو، ترتیبِ اجرا و وابستگی‌های داده بینِ تست‌کیس‌ها |
| پیاده‌سازی | `Step3ScenarioAnalysisGenerator`/`ScenarioAnalysisAgent` در `scenario_analysis.py` |
| ورودی | `step1_result` + **قراردادِ فشرده‌ی** قدم ۲ (`compact_step2_result` در `ui/pipeline.py::run_scenario_analysis`، مشترک بینِ دکمه‌ی قدم ۳، Run All و صفحه‌ی مستقل) — کاتالوگِ کامل حذف می‌شود؛ فقط تعریفِ عملیات‌های نگاشت‌شده (`CompactMapping.operation`) می‌ماند |
| خروجی | `Step3Result`: `scenarios[]`, `execution_order[]`, `data_dependencies[]` (variable_name, source{test_case_id, location: response.body\|response.header, path}, targets[{test_case_id, location: path\|query\|header\|body, parameter}], scope: global\|collection, required, origin: task\|inferred), `clarifications[]` |
| اعتبارسنجی | `validate_scenario_payload`: ارجاع به تست‌کیس‌های واقعی، `find_cycle` برای حلقه، تطبیقِ پارامترِ مقصد با پارامترهای سند و فیلدِ مبدأ با فیلدهای پاسخ؛ متغیرهای `variables`ِ قدم ۱ پیش از آن به‌صورتِ قطعی به وابستگی تبدیل می‌شوند (`_resolve_requested_variables`) |

### قدم ۴ — Postman Collection Generation (قطعی)

| مورد | جزئیات |
|------|--------|
| هدف | تبدیلِ قطعیِ نتیجه‌ی قدم‌های ۱ تا ۳ به Postman Collection v2.1 |
| پیاده‌سازی | `Step4PostmanGenerator` در `postman_generation.py` → `PostmanBuilder` در `postman_builder.py` |
| ورودی | `step1_result`، **`step2_result` کامل** (نه فشرده — `services[]` لازم است)، `step3_result`، `collection_name` |
| خروجی | `Step4Result`: `collection`، `collection_name`، `scenarios`، `requests`، `unresolved`، `clarifications`، `warnings` |
| تبدیل‌ها | `extract_*` → `validate_step3_references` → `_plan` (برای هر تست‌کیس: `mapping_for` → `operation_index` → `_build_request_case` → `TestCase`) → پوشه‌ها بر اساسِ سناریو → `_producers_first` (تولیدکننده پیش از مصرف‌کننده) → `TestCaseSuite` → `PostmanBuilder.build` → `validate_collection` + `check_variables_are_available` + `check_dependency_wiring` |
| خطا | `PostmanGenerationError`؛ تست‌کیسِ بی‌نگاشت در `unresolved` می‌ماند و حدس زده نمی‌شود |

### قدم ۵ — Final Review & Export (قطعی)

| مورد | جزئیات |
|------|--------|
| هدف | بازبینیِ نهایی (`ready`/`needs_review`/`blocked`) و صادراتِ **همان** کالکشنِ قدم ۴ |
| پیاده‌سازی | `FinalReviewGenerator` در `final_review.py`؛ همان توابعِ اعتبارسنجیِ قدم ۴ را صدا می‌زند (`validate_collection`، `check_variables_are_available`، `validate_step3_references`) |
| نکته | کالکشن را **تغییر نمی‌دهد**؛ خروجیِ دانلود `collection_json(collection)` در `ui/step5_final_review.py::_render_export` است. دانلود برای بازبینیِ `blocked`، کالکشنِ غایب یا نتیجه‌ی کهنه غیرفعال است (`export_unavailable_reason`) |

---

## D. روابطِ اجزا

- **وابستگیِ ماژول‌ها (یک‌طرفه):**
  `swagger_analyzer` ← `api_discovery` ← `swagger_snapshots`/`api_relevance`/`api_mapping` ←
  `scenario_analysis` و `postman_generation` ← `final_review`.
  `postman_generation` و جریانِ قدیمی (`agent.py`) هر دو به `postman_builder` تکیه دارند و
  `postman_builder` مدل‌های `TestCase`/`TestCaseSuite` را از `testcase_generator` می‌گیرد.
- **کلاینت‌های مشترک:** قدم‌های ۱، ۲، ۳ هر کدام یک `LLMClient(user_id, agent_name)`
  تازه می‌سازند. قدم ۴ و ۵ هیچ LLM/شبکه‌ای ندارند.
- **انتقالِ داده:** فقط dict/TypedDictهای JSON-پذیر در `WorkflowState.results`
  (حافظه‌ی `st.session_state`). هیچ دیتابیس/فایلِ میانی‌ای برای این جریان نوشته
  نمی‌شود؛ `checkpoints.db` مربوط به گراف‌های LangGraph است.
- **کلیدِ تطبیقِ عملیات:** `api_discovery.operation_key(method, path)` در قدم‌های
  ۲، ۳، ۴ و ۵ یکسان استفاده می‌شود (`{id}` و `{voucherId}` یکی حساب می‌شوند).

---

## E. مدل‌ها و تبدیل‌ها — کجا فیلد اضافه/حذف می‌شود

| مرز | چه چیزی حذف یا اضافه می‌شود |
|-----|------------------------------|
| سندِ Swagger → `EndpointSpec` | `security` به یک bool تبدیل می‌شود؛ پارامترهای header (مثلاً `authorization`) به `ParamSpec` می‌روند؛ `securitySchemes` خوانده نمی‌شود |
| `EndpointSpec` → `DiscoveredApi.to_dict()` | `tags`، `description`، `required_headers`، `request_body_required_fields`، `security` **حذف** می‌شوند |
| `ApiSpec` → `DiscoveredService.to_dict()` | فیلدِ مشتقِ `authorization` **اضافه** می‌شود — فقط وقتی `global_security` یا `security` یک عملیات true باشد، وگرنه `None`؛ `title`/`version` حذف می‌شوند |
| `Step2Result` → `Step3Contract` | کاتالوگِ `services` (و در نتیجه `authorization`) **کامل حذف** می‌شود |
| `TestCase` → درخواستِ Postman | `headers` dict → لیستِ `{key, value}`؛ `Authorization: {{token}}` همیشه اول **اضافه/جایگزین** می‌شود؛ هدرِ تکراری (case-insensitive) حذف؛ `Content-Type` برای بدنه‌ی JSON اضافه؛ `{id}` → `:id` |
| `Step4Result` → فایلِ صادرشده | بدونِ تغییر (`json.dumps(..., indent=2)`) |

---

## F. معماریِ LLM و Embedding

- `LLMClient` (`src/llm_client.py`): کلاینتِ `openai.OpenAI` با `LLM_BASE_URL`،
  `LLM_API_KEY`، `LLM_MODEL` و یک `httpx.Client` با هدرهای `x-client-id`/`x-user-id`/
  `x-request-id`. تأییدِ SSL با `LLM_SSL_VERIFY`.
- `chat()` یک `LLMResponse` (زیرکلاسِ `str`) برمی‌گرداند که `finish_reason` و
  usage را حمل می‌کند؛ `truncation_message(raw)` بریدگی را تشخیص می‌دهد.
  مصرف با `report_usage` به گیرنده‌ی فعال (`collect_usage` در
  `WorkflowState.track_usage`) گزارش می‌شود.
- با `LANGFUSE_ENABLED`، فراخوانی از مسیرِ `_chat_observed` می‌گذرد.
- سقفِ خروجی برای همه‌ی قدم‌ها `LLM_MAX_OUTPUT_TOKENS` (پیش‌فرض ۸۱۹۲) است.
- پرامپت‌ها فایل‌های `prompts/*.md` با یک بخشِ `INPUT` و جای‌نگهدارهای
  `{{...}}` هستند (`split_prompt`/`render_prompt` در هر ماژول). خروجی با
  `json_output.extract_json_object` استخراج و سپس اعتبارسنجی می‌شود.
- `EmbeddingClient` در این جریان استفاده نمی‌شود (فیلترِ ارتباطِ قدم ۲ توکن‌محور
  و قطعی است — `api_relevance.py`).

---

## G. ساختِ Postman

`Step4PostmanGenerator._plan` برای هر تست‌کیسِ حل‌شده `_build_request_case`
را صدا می‌زند و یک `TestCase` می‌سازد؛ `PostmanBuilder._build_request` آن را
به درخواستِ Postman تبدیل می‌کند:

| بخش | منبع |
|-----|------|
| `method` / `path` | عملیاتِ کشف‌شده در قدم ۲ |
| URL | `{{baseUrl}}` (یا `{{baseUrl_<service>}}` برای سرویس‌های بعدی) + مسیر با `:param`؛ پارامترِ مسیری که مقدارش یک متغیرِ وابستگی است مستقیم در مسیر می‌نشیند (`/voucher/{{voucherId}}`)؛ query از ارجاع‌های وابستگیِ قدم ۳ |
| `header` | `PostmanBuilder._build_headers(case["headers"], has_body)` |
| `body` | `request_body`ِ سند + فیلدهای مقصدِ وابستگی؛ mode `raw` / JSON |
| `event` | `test`: assertionِ کدِ وضعیت (فقط وقتی یک کدِ موفقِ یکتا مستند باشد) + اسکریپتِ استخراجِ محافظت‌شده‌ی متغیر؛ `prerequest`: guardِ مصرف‌کننده (بخشِ K) |
| متغیرهای کالکشن | `baseUrl`، `token` (خالی)، و `baseUrl_<service>` |

**احراز هویت:** `PostmanBuilder._build_headers` روی **هر** درخواست دقیقاً یک
`{"key": "Authorization", "value": "{{token}}", "type": "text"}` می‌گذارد —
بی‌قید و شرط و مستقل از Swagger و نتیجه‌ی قدم‌های قبلی. جزئیات، علتِ مشکلِ
قبلی و نحوه‌ی اعمال در بخشِ [J](#j-هدرِ-authorization--علت-و-قاعده‌ی-سراسری).

سریال‌سازی: قدم ۴ dict برمی‌گرداند؛ `ui/formatting.collection_json` آن را برای
نمایش/دانلود به JSON تبدیل می‌کند. جریانِ قدیمی در
`TestCaseGeneratorAgent._save_collection` فایل را در
`src/agents/test_case_generator/output/` ذخیره می‌کند.

---

## H. تست و نقاطِ توسعه

- تست‌ها با `pytest` (`tests/unit/...`)؛ هیچ تستی LLM یا شبکه‌ی واقعی را صدا
  نمی‌زند (LLM با mock، Swagger با dictِ آماده یا `tmp_path`).
- تست‌های کلیدی: `test_postman_builder.py`، `test_postman_generation.py`،
  `test_final_review.py`، `test_api_mapping.py`، `test_step3_payload.py`،
  `tests/unit/ui/test_workflow.py`.
- `addopts` در `pyproject.toml` شاملِ `--cov-fail-under=80` است؛ اجرای یک
  زیرمجموعه از تست‌ها همیشه شکستِ پوشش گزارش می‌کند — از `--no-cov` استفاده کنید.
- نقاطِ توسعه:
  - قاعده‌ای که باید روی **هر** درخواستِ Postman اعمال شود → `PostmanBuilder`
    (هر دو جریان از آن می‌گذرند).
  - قاعده‌ای که به نتیجه‌ی قدم‌های ۱–۳ وابسته است → `_build_request_case`.
  - بررسیِ ساختاریِ کالکشن که قدم ۵ هم باید ببیند → `validate_collection`.
  - منبعِ Swagger تازه → رجیستریِ `SWAGGER_SOURCES` در `swagger_snapshots.py`
    (`remote_url` همان نشانیِ خامِ `v3/api-docs` است که دکمه‌ی Update می‌گیرد).

### تازه‌سازیِ snapshotها (دکمه‌های Update قدم ۲)

- `swagger_snapshots.refresh_snapshot(key)`: درخواستِ تازه به `remote_url` با
  timeoutهای `SWAGGER_CONNECT_TIMEOUT_SECONDS`/`SWAGGER_READ_TIMEOUT_SECONDS` →
  بررسیِ 2xx → JSON → فیلدِ `openapi`/`swagger` → `discover_from_spec` با دست‌کم
  یک عملیات → نوشتنِ بایت‌های خام در فایلِ موقتِ همان پوشه و `os.replace`. هر
  شکست یک `SwaggerRefreshError` («Failed to refresh Admin Swagger: …») است و
  فایلِ قبلی دست‌نخورده می‌ماند.
- `ui/swagger_refresh.py`: وضعیتِ idle/loading/success/error هر منبع در
  `st.session_state`؛ فقط دکمه‌ی همان منبع هنگامِ بارگذاری غیرفعال است و پرچمِ
  loading در `finally` پاک می‌شود.
- هیچ cacheی وجود ندارد (`load_snapshot` در هر اجرا فایل را می‌خواند). ورودیِ
  `INPUT_SWAGGER_SNAPSHOTS` (اثرانگشتِ sha256 فایل‌ها) باعث می‌شود نتیجه‌ی
  موجودِ قدم ۲ پس از تغییرِ فایل به‌عنوانِ «متعلق به ورودیِ قدیمی» گزارش شود؛
  نگاشت خودکار اجرا نمی‌شود.

---

## I. محدودیت‌های شناخته‌شده

- `DiscoveredApi.to_dict()` هدرهای الزامی و `security` را منتشر نمی‌کند؛ قدم‌های
  بعد هیچ اطلاعاتِ امنیتیِ per-operation ندارند.
- snapshotهای فعلی (`admin.json`، `customer.json`) هیچ `security` یا
  `securitySchemes`ی ندارند؛ بنابراین `authorization` در نتیجه‌ی قدم ۲ همیشه
  `null` است (این فیلد حالا فقط نمایشی است — بخشِ J).
- همه‌ی عملیات‌های این snapshotها یک پارامترِ header اختیاری با نامِ
  `authorization` (حروفِ کوچک) مستند می‌کنند؛ مقایسه‌ی نامِ هدر همه‌جا
  case-insensitive است.
- `api_discovery.AUTHORIZATION_VALUE` و `testcase_generator._AUTH_VALUE` هنوز
  `Bearer {{token}}` هستند (اولی برای نمایش در قدم ۲، دومی قراردادِ خروجیِ LLM
  در جریانِ قدیمی). خروجیِ Postman از آن‌ها پیروی نمی‌کند؛ منبعِ حقیقت
  `postman_builder.AUTHORIZATION_VALUE` است.
- قدم ۵ کالکشن را عمداً تغییر نمی‌دهد؛ نتیجه‌ی قدیمیِ قدم ۴ (در
  `session_state` یا JSONِ چسبانده‌شده) اصلاح نمی‌شود، بلکه با issueِ
  `invalid_collection` مسدود (`blocked`) می‌شود تا دوباره ساخته شود.
- `WorkflowState` فقط در حافظه‌ی Streamlit است؛ با بستنِ نشست از دست می‌رود.
- اجرای کاملِ `pytest` در این مخزن به دلیلِ ماژول‌های ناموجود
  (`src.ingest`، `src.agents.business_rag`، …) در مرحله‌ی collection خطا می‌دهد.
- `test_resolve.py`، `test_raw_llm.py`، `test_agent_run.py` داخلِ `src/` اسکریپتِ
  دستی‌اند و با الگوی `test_*.py` اشتباه گرفته می‌شوند؛ `test_resolve.py` مسیرِ
  مطلقِ یک ماشینِ دیگر را hard-code کرده است.

---

## J. هدرِ Authorization — علت و قاعده‌ی سراسری

### قاعده

هر تست‌کیسی که به درخواستِ Postman تبدیل می‌شود — در ویزارد و در جریانِ
قدیمیِ `agent.py` — دقیقاً یک هدرِ زیر را دارد، بدونِ هیچ شرطی (نه وابسته به
`security`ِ Swagger، نه به نوعِ تست‌کیس، نه به متنِ «without authentication»):

```json
{"key": "Authorization", "value": "{{token}}", "type": "text"}
```

### علتِ مشکلِ قبلی (مسیرِ داده)

1. `SwaggerAnalyzer.parse_spec` → `EndpointSpec.security = bool(operation.security ?? spec.security)`.
   snapshotهای `data/swagger/` هیچ `security`ی ندارند → همه `False`.
2. `api_discovery._service_from_api_spec` → `DiscoveredService.requires_auth = False`
   → `DiscoveredService.authorization` = `None` → در `Step2Result.services[].authorization`
   مقدارِ `null` نوشته می‌شود. (پارامترِ هدرِ `authorization` در `DiscoveredApi.parameters`
   می‌ماند ولی هیچ‌جا به هدر تبدیل نمی‌شد.)
3. قدم ۳ فقط قراردادِ فشرده (`compact_step2_result`) را می‌بیند؛ فیلد آن‌جا اصلاً
   وجود ندارد — اما قدم ۴ از `step2_result`ِ کامل می‌خواند، پس قدم ۳ علت نبود.
4. **نقطه‌ی حذف:** `postman_generation._build_request_case` هدر را فقط وقتی
   می‌ساخت که `service["authorization"]` یک dict با `value` باشد؛ با `null`،
   `TestCase.headers` خالی می‌ماند. همچنین `case_expects_no_authentication` برای
   تست‌کیس‌های «without token/authentication» هدر را عمداً حذف می‌کرد.
5. `PostmanBuilder._build_headers` فقط همان dict را به لیست تبدیل می‌کرد و
   چیزی اضافه نمی‌کرد → JSONِ نهایی بدونِ `Authorization`.
6. قدم ۵ کالکشن را دست‌نخورده صادر می‌کرد و `validate_collection` هدرها را
   بررسی نمی‌کرد → مشکل تا فایلِ دانلودشده پنهان می‌ماند.

### پیاده‌سازی

| جا | تغییر |
|----|-------|
| `postman_builder.py` | ثابت‌های عمومیِ `AUTHORIZATION_HEADER` / `AUTHORIZATION_VALUE` (`"{{" + _TOKEN_VAR + "}}"`)؛ `_build_headers` هدرِ الزامی را همیشه اول می‌گذارد و هر `Authorization`ِ موجود (با هر حروف/فاصله/مقداری) را حذف می‌کند. **نقطه‌ی اعمالِ واحد** — هر دو جریان از آن می‌گذرند. |
| `postman_generation.py` | شرطِ `service["authorization"]` و `case_expects_no_authentication`/`case_text` حذف شدند؛ `TestCase.headers` با همان هدر شروع می‌شود تا مدلِ میانی با خروجی یکی باشد. `has_required_authorization` + بررسی در `validate_collection`. |
| `final_review.py` | بدونِ تغییر — چون `validate_collection` را صدا می‌زند، کالکشنِ بدونِ هدر (artifactِ قدیمی) را `blocked` اعلام می‌کند. |
| `api_discovery.py` | فقط توضیح: `authorization` نمایشی است و هدرِ Postman از آن ساخته نمی‌شود. اسکیمای قدم ۲ عوض نشده است. |
| `testcase_generator.py` | متنِ assumptionِ کیسِ `unauthorized` اصلاح شد (هدر دیگر حذف نمی‌شود). |

چرا `PostmanBuilder`؟ تنها نقطه‌ای است که **هر** درخواست — از هر دو جریان — از آن
عبور می‌کند و بعد از آن هیچ لایه‌ای هدرها را تغییر نمی‌دهد. گذاشتنِ فیلدِ تازه در
قدم ۲ مشکل را حل نمی‌کرد: یا باید دوباره در قدم ۴ خوانده می‌شد (همان شرطِ
شکننده) یا در artifactهای قدیمی وجود نداشت.

### سازگاری با artifactهای قدیمی

- نتیجه‌ی قدیمیِ قدم ۲ (با `authorization: null` یا بدونِ این کلید) بدونِ
  تغییر کار می‌کند — قدم ۴ دیگر این فیلد را نمی‌خواند.
- نتیجه‌ی قدیمیِ قدم ۴ که در `WorkflowState` مانده تغییر نمی‌کند (قدم ۵ عمداً
  کالکشن را ویرایش نمی‌کند)؛ قدم ۵ آن را با پیامِ «regenerate the collection in
  Step 4» مسدود می‌کند.

### تست‌ها

`tests/unit/test_case_generator/test_authorization_header.py` خودِ JSONِ صادرشده
(`ui.formatting.collection_json(postman_collection(step5))`) را از مسیرِ واقعیِ
Swagger → `discover_from_spec` → `build_result` → قدم ۴ → قدم ۵ می‌سنجد:
نبودِ auth در Swagger/قدم ۲، artifactِ قدیمی، همه‌ی تست‌کیس‌های یک API (منبعِ
موجود، ناموجود، شناسه‌ی نامعتبر، …)، جایگزینیِ مقدارِ قبلی، حروفِ مختلف، حفظِ
بقیه‌ی هدرها، وابستگیِ قدم ۳ که هدرِ `authorization` را هدف گرفته، و مسدودشدنِ
کالکشنِ کهنه در قدم ۵.

---

## K. متغیرهای درخواستیِ کاربر (استخراج، ذخیره و مصرف)

کاربر می‌تواند در تسک بخواهد مقداری از پاسخِ یک API استخراج و بررسی شود، در یک
متغیرِ Postman (معمولاً global) ذخیره شود و درخواست‌های بعدی از آن استفاده کنند
— مثلاً `result.body.id` از «ایجادِ بن» → `voucherId` → مسیرِ «دریافتِ بن».
این قابلیت عمومی است: هیچ نام، مسیر یا endpointی در کد هاردکد نشده است
(`test_no_endpoint_or_variable_name_is_hardcoded` همین را بررسی می‌کند).

### علتِ مشکلِ قبلی

| لایه | چه چیزی گم می‌شد |
|------|-------------------|
| قدم ۱ | قراردادِ خروجی فیلدی برای متغیر نداشت و پرامپت صراحتاً «Generate Postman variables» را منع می‌کرد؛ نام، اسکوپ و مسیرِ کاربر حداکثر در متنِ requirements می‌ماند |
| قدم ۳ | LLM نام و مسیر را خودش حدس می‌زد (مثلاً `createdVoucherId` و `$.id`) و ممکن بود کیسِ منفیِ «بنِ ناموجود» را هم مقصد کند؛ `DataDependency` فیلدی برای اسکوپ/الزام نداشت |
| قدم ۴ | `source_variables` اسکوپ را همیشه `collection` می‌گذاشت؛ اسکریپت یک خطِ `set(..., jsonData.id)` بدونِ هیچ بررسی بود (مقدارِ undefined هم ذخیره می‌شد)؛ مسیر `:id` با جدولِ متغیر بود؛ ترتیبِ مخالف فقط هشدار می‌گرفت و هیچ guardی در مصرف‌کننده نبود |
| قدم ۵ | فقط وجودِ نامِ متغیر را می‌سنجید، نه اینکه واقعاً ذخیره و در محلِ درست مصرف شود |

### جریانِ داده

```text
قدم ۱  variables[]  ── RequestedVariable (variable_dependencies.py)
         name, scope, source_test_case_id, response_path, required, consumers[], instruction
           │  validate_requested_variables: نامِ معتبر، اسکوپ، مبدأِ موجود و غیرِمنفی،
           │  مسیرِ ساده («result.body.id» → «$.result.body.id»)، مصرف‌کننده‌ی موجود
           ▼
قدم ۳  _resolve_requested_variables (قطعی، بعد از پاسخِ LLM)
           │  نام/اسکوپ/مبدأ/مسیر/مصرف‌کننده‌های کاربر برنده‌اند؛ پیشنهادِ مطابقِ LLM
           │  فقط جای خالی را پر می‌کند و کنار گذاشته می‌شود (متغیرِ دومی ساخته نمی‌شود)
           │  مسیرِ تأییدناپذیر/ناسازگار با سند → حفظ + clarification (نه جایگزینیِ بی‌صدا)
           ▼
       data_dependencies[] با scope، required، origin="task"
           ▼
قدم ۴  source_variables → SaveVariable(scope, required)      روی تولیدکننده
       required_variables → RequiredVariable                   روی مصرف‌کننده‌ها
       target_references → path/query/header/body (فیلدِ تودرتوی «a.b» در بدنه)
       _producers_first → تولیدکننده پیش از مصرف‌کننده (داخل و بینِ پوشه‌ها)
       PostmanBuilder → اسکریپت‌ها؛ check_dependency_wiring → سنجشِ JSONِ نهایی
           ▼
قدم ۵  check_dependency_wiring + missing_requested_variables روی همان کالکشن
```

### اسکریپت‌ها

تولیدکننده (`PostmanBuilder._required_save_lines`، eventِ `test`):

```javascript
pm.globals.unset("voucherId");
pm.test("Response provides result.body.id and saves it as global variable 'voucherId'", function () {
    pm.expect(pm.response.code, "HTTP status of the response that provides voucherId").to.be.within(200, 299);
    const response = pm.response.json();
    pm.expect(response.result, "response.result").to.exist;
    pm.expect(response.result.body, "response.result.body").to.exist;
    const value = response.result.body.id;
    pm.expect(value, "response.result.body.id").to.not.be.oneOf([undefined, null, ""]);
    pm.globals.set("voucherId", value);
});
```

- `unset` اول: اجرای ناموفق مقدارِ کهنه‌ی اجرای قبلی را باقی نمی‌گذارد.
- هر `expect`ِ ناموفق تابع را متوقف می‌کند، پس `set` فقط پس از همه‌ی بررسی‌ها
  اجرا می‌شود.

مصرف‌کننده (`PostmanBuilder._guard_lines`، eventِ `prerequest`): اگر متغیر ست
نباشد، یک تستِ ناموفقِ گویا ثبت می‌شود و درخواست با `pm.execution.skipRequest()`
فرستاده نمی‌شود (در Postman/Newmanِ قدیمی که آن را ندارند، خطای اسکریپت). این
همان حالتی را پوشش می‌دهد که Runner فقط یک پوشه را اجرا کند یا ترتیب را عوض
کند — جایگاهِ درخواست به‌تنهایی کافی نیست.

### کیس‌های منفی

- فقط تست‌کیسِ مبدأ اسکریپتِ ذخیره دارد؛ کیسِ منفیِ همان endpoint (مثلاً ایجادِ
  نامعتبر) به متغیر دست نمی‌زند.
- مبدأِ یک متغیرِ درخواستی نمی‌تواند تست‌کیسِ منفی باشد (قدم ۱ رد می‌کند).
- وقتی کاربر مصرف‌کننده‌ها را گفته، فقط همان‌ها مقصدند؛ پیشنهادِ LLM برای کیسِ
  «بنِ ناموجود» کنار گذاشته می‌شود و آن درخواست ورودیِ مستقلِ خودش (`:id`) را
  نگه می‌دارد. مقدارِ آن شناسه‌ی ناموجود ساخته نمی‌شود (هیچ مقداری حدس زده
  نمی‌شود) و به‌عنوانِ assumption روی همان درخواست گزارش می‌شود.

### سازگاری با artifactهای قدیمی

- قدم ۱ بدونِ `variables` → `[]`؛ قدم ۳ بدونِ `scope`/`required`/`origin` →
  `collection` / `true` / `inferred` (`dependency_scope`، `dependency_required`).
- وابستگیِ قدیمی همان اسکوپِ collection را نگه می‌دارد ولی اسکریپتش محافظت‌شده
  می‌شود؛ پارامترِ مسیرِ بدونِ وابستگی همچنان `:id` است.
- کالکشنِ قدیمیِ قدم ۴ (یک‌خطی، بدونِ guard) در قدم ۵ با issueِ
  `broken_dependency` مسدود می‌شود تا دوباره ساخته شود.

### تست‌ها

`tests/unit/test_case_generator/test_variable_dependencies.py` زنجیره‌ی واقعی
(اعتبارسنجِ قدم ۱ → کشفِ Swagger → قراردادِ فشرده → عاملِ قدم ۳ با پاسخِ ثابتِ
LLM → قدم ۴ → قدم ۵ → JSONِ صادرشده) را می‌سنجد و اسکریپت‌های تولیدشده را با
Node و یک شبیه‌سازِ کوچکِ `pm` واقعاً اجرا می‌کند (بدونِ Node همان تست‌ها skip
می‌شوند).
