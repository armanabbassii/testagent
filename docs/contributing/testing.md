# راهنمای تست‌نویسی

## ساختار تست‌ها

```
tests/
├── conftest.py              # fixture های مشترک (MemoryHandler)
├── unit/                    # تست‌های واحد — بدون سرویس خارجی
│   ├── debug/
│   │   ├── test_levels.py       # DebugLevel enum
│   │   ├── test_logger.py       # AgentLogger
│   │   ├── test_file_handler.py # FileHandler
│   │   └── test_debug_config.py # DebugConfig
│   ├── task_sources/
│   │   ├── fake_jira.py         # Jira + ADFSِ ساختگی برای تست‌ها
│   │   └── test_jira_settings.py # کلید و نشانیِ issue، تنظیماتِ Jira
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
uv run pytest tests/unit/task_sources/test_jira_settings.py

# یک تست خاص
uv run pytest tests/unit/task_sources/test_jira_settings.py::TestIssueReference::test_project_keys_with_digits_and_underscores

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
def test_parse_issue_reference_lowercase_key():
    assert parse_issue_reference("demo-7") == "DEMO-7"

# ❌ اشتباه — به سرویسِ واقعی وابسته است
def test_import_issue():
    result = import_issue("PODIUM-3386")   # به Jiraِ واقعی وصل می‌شود
```

### Integration Tests (`tests/integration/`)

- **تعریف:** تست تعامل بین چند component
- **قانون:** سرویس‌های خارجی (LLM، Jira، Redis) باید mock شوند
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
def test_parse_issue_reference_empty_raises():
def test_logger_disabled_logs_nothing():
def test_normalize_text_crlf_becomes_lf():
```

### ۲. ساختار — الگوی AAA

```python
def test_example():
    # Arrange — آماده‌سازی
    reference = "https://jira.test/browse/demo-7"

    # Act — عمل
    key = parse_issue_reference(reference, "https://jira.test")

    # Assert — بررسی
    assert key == "DEMO-7"
```

### ۳. یک assert منطقی per test

```python
# ✅ درست
def test_text_attachment_is_processed():
    result = _process("notes.txt", b"hello")
    assert result.status == ATTACHMENT_PROCESSED

def test_text_attachment_content_is_extracted():
    result = _process("notes.txt", b"hello")
    assert result.content == "hello"

# ❌ اشتباه — دو موضوع مختلف در یک تست
def test_everything():
    result = _process("notes.txt", b"hello")
    assert result.status == ATTACHMENT_PROCESSED
    assert result.content == "hello"
    assert result.truncated is False
```

### ۴. استفاده از fixture به جای تکرار

```python
# conftest.py یا بالای فایل تست
@pytest.fixture
def server() -> FakeJira:
    return FakeJira(issues={"DEMO-7": issue_body()})

# در تست
def test_something(server):
    client = JiraClient(adfs_settings(), session=server.session())
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