# راهنمای تست‌نویسی

## ساختار تست‌ها

```
tests/
├── conftest.py              # fixture های مشترک (MemoryHandler، comment fixtures)
├── unit/                    # تست‌های واحد — بدون سرویس خارجی
│   ├── debug/
│   │   ├── test_levels.py       # DebugLevel enum
│   │   ├── test_logger.py       # AgentLogger
│   │   ├── test_file_handler.py # FileHandler
│   │   └── test_debug_config.py # DebugConfig
│   ├── scoring/
│   │   └── test_scorer.py       # calculate_score, save_score, format_score_comment
│   ├── gitlab/
│   │   └── test_dedup.py        # fingerprint و جلوگیری از کامنت تکراری
│   └── vector_store/
│       └── test_base.py         # Document، SearchResult
└── integration/             # تست‌های integration — با mock
    └── test_graph_flow.py       # جریان گراف multi-agent
```

---

## اجرای تست‌ها

```bash
# نصب وابستگی‌های dev
uv sync --extra dev

# اجرای همه تست‌ها با coverage
uv run pytest

# فقط unit tests
uv run pytest tests/unit/

# فقط یک فایل
uv run pytest tests/unit/scoring/test_scorer.py

# یک تست خاص
uv run pytest tests/unit/scoring/test_scorer.py::TestCalculateScore::test_single_critical

# بدون coverage (سریع‌تر)
uv run pytest --no-cov

# با verbose بیشتر
uv run pytest -vv
```

---

## انواع تست

### Unit Tests (`tests/unit/`)

- **تعریف:** تست یک واحد منطقی به صورت ایزوله
- **قانون:** هیچ اتصال شبکه، فایل I/O واقعی، یا سرویس خارجی نداریم
- **سرعت:** باید در کمتر از ۱ ثانیه اجرا شوند
- **mock:** هر چیزی که خارج از واحد مورد تست است باید mock شود

```python
# ✅ درست — ایزوله
def test_calculate_score_critical():
    result = calculate_score([_comment("critical")])
    assert result["total_score"] == -10

# ❌ اشتباه — به LLM وابسته است
def test_reviewer_agent():
    agent = CodeReviewerAgent()
    result = agent(state)   # این یه LLM call واقعی انجام می‌دهد
```

### Integration Tests (`tests/integration/`)

- **تعریف:** تست تعامل بین چند component
- **قانون:** سرویس‌های خارجی (LLM، GitLab، Redis) باید mock شوند
- **mock:** از `unittest.mock.patch` برای mock کردن استفاده کنید

```python
@patch("src.llm_client.LLMClient.chat")
def test_graph_routing(mock_chat, checkpointer):
    mock_chat.return_value = "research"
    graph = build_graph(checkpointer=checkpointer)
    result = graph.invoke(state, config)
    assert result["metadata"]["route"] == "research"
```

---

## قوانین تست‌نویسی

### ۱. نام‌گذاری

```python
# فرمت: test_{چه چیزی}_{شرط}_{نتیجه مورد انتظار}
def test_calculate_score_empty_list_returns_zero():
def test_logger_disabled_logs_nothing():
def test_fingerprint_different_files_different_result():
```

### ۲. ساختار — الگوی AAA

```python
def test_example():
    # Arrange — آماده‌سازی
    comments = [_comment("critical")]

    # Act — عمل
    result = calculate_score(comments)

    # Assert — بررسی
    assert result["total_score"] == -10
```

### ۳. یک assert منطقی per test

```python
# ✅ درست
def test_total_score_correct():
    result = calculate_score([_comment("critical")])
    assert result["total_score"] == -10

def test_issues_list_has_one_item():
    result = calculate_score([_comment("critical")])
    assert len(result["issues"]) == 1

# ❌ اشتباه — دو موضوع مختلف در یک تست
def test_everything():
    result = calculate_score([_comment("critical")])
    assert result["total_score"] == -10
    assert len(result["issues"]) == 1
    assert result["issues"][0]["category"] == "security"
```

### ۴. استفاده از fixture به جای تکرار

```python
# conftest.py یا بالای فایل تست
@pytest.fixture
def critical_comment() -> ReviewComment:
    return ReviewComment(file_path="f.py", line=1,
                         severity="critical", category="security", body="issue")

# در تست
def test_something(critical_comment):
    result = calculate_score([critical_comment])
    ...
```

### ۵. تست edge cases الزامی

برای هر تابع باید این edge cases تست شوند:
- ورودی خالی (`[]`، `""`، `None`)
- ورودی نامعتبر (مقدار خارج از محدوده، نوع اشتباه)
- ورودی حداکثر/حداقل
- رفتار با مقادیر boundary

### ۶. mock کردن صحیح

```python
# ✅ mock در سطح import (پیشنهادشده)
@patch("src.llm_client.LLMClient.chat")
def test_agent(mock_chat):
    mock_chat.return_value = "mocked response"
    ...

# ✅ mock با context manager
def test_with_env():
    with patch.dict("os.environ", {"LOGGING_LEVEL": "debug"}):
        config = DebugConfig.from_env()
        ...

# ❌ اشتباه — mock در مسیر اشتباه
@patch("langchain_core.messages.AIMessage")   # جایی که استفاده می‌شود، نه جایی که تعریف شده
```

### ۷. تست Generator Functions (مهم)

اگر تابعی که تست می‌کنید از `yield` استفاده می‌کند (مثل `stream_chat`)،
باید patch را در طول کل اجرای generator فعال نگه دارید.

**مشکل:** patch داخل `with` block غیرفعال می‌شود قبل از اینکه generator اجرا شود:

```python
# ❌ اشتباه — patch قبل از اجرای generator غیرفعال می‌شود
def test_stream_wrong():
    with patch("src.llm_client.OpenAI"):
        client = LLMClient(user_id="u1")
        client._client = mock_inner
    # اینجا patch دیگر فعال نیست
    result = list(client.stream_chat("hi"))   # ← generator اینجا اجرا می‌شود
    assert result == ["hello"]   # ← شکست می‌خورد!
```

**راه‌حل:** از **pytest fixture با `yield`** استفاده کنید:

```python
# ✅ درست — patch تا پایان تست فعال است
@pytest.fixture
def llm_client():
    with patch("src.llm_client.OpenAI"), \
         patch("src.llm_client._make_http_client"):
        client = LLMClient(user_id="test-user")
        mock_inner = MagicMock()
        client._client = mock_inner
        yield client, mock_inner   # ← patch اینجا فعال می‌ماند

def test_stream_correct(llm_client):
    client, mock_inner = llm_client
    mock_inner.chat.completions.create.return_value = [
        _chunk("hello"), _chunk(" world"),
    ]
    assert list(client.stream_chat("hi")) == ["hello", " world"]   # ✅
```

---

## Coverage

هدف: **حداقل ۸۰٪** برای هر ماژول.

```bash
# گزارش coverage در ترمینال
uv run pytest --cov=src --cov-report=term-missing

# گزارش HTML (قابل مرور در مرورگر)
uv run pytest --cov=src --cov-report=html
open htmlcov/index.html
```

### موارد exempt از coverage

```python
# pragma: no cover — برای خطوطی که نمی‌توان تست کرد
if __name__ == "__main__":   # pragma: no cover
    main()

@abstractmethod
def emit(self) -> None: ...  # abstract methods خودکار exempt هستند
```

---

## وضعیت فعلی

- **306 تست** — 0 failed، 4 skipped (نیاز به سرور خارجی)
- **93% coverage** — بالاتر از threshold 80%
- فایل‌های exempt (نیاز به Redis/Chroma): در `pyproject.toml` تحت `[tool.coverage.run].omit`

---

## چک‌لیست قبل از merge

- [ ] تمام تست‌های موجود pass می‌شوند (`uv run pytest`)
- [ ] coverage حداقل ۸۰٪ است (`--cov-fail-under=80`)
- [ ] برای هر تابع/متد جدید حداقل یک تست نوشته شده
- [ ] edge cases (خالی، نامعتبر، boundary) تست شده‌اند
- [ ] تست‌های unit به سرویس خارجی وابسته نیستند
- [ ] اگر تابع از `yield` استفاده می‌کند، از fixture با yield استفاده شده
- [ ] نام تست‌ها گویا هستند
- [ ] از `conftest.py` برای fixture های مشترک استفاده شده