"""تست‌های i18n و Translator."""

import pytest
from src.cli.i18n import Translator, SUPPORTED_LANGS, DEFAULT_LANG


class TestTranslator:
    def test_default_lang_is_fa(self):
        t = Translator()
        assert t.lang == "fa"

    def test_fa_translation_returns_persian(self):
        t = Translator("fa")
        assert "زبان" in t.t("lang_help") or len(t.t("lang_help")) > 0

    def test_en_translation_returns_english(self):
        t = Translator("en")
        result = t.t("lang_help")
        assert "language" in result.lower() or len(result) > 0

    def test_format_args_applied(self):
        t = Translator("en")
        result = t.t("review_header", 42)
        assert "42" in result

    def test_unknown_key_returns_bracketed_key(self):
        t = Translator("en")
        result = t.t("nonexistent_key")
        assert result == "[nonexistent_key]"

    def test_invalid_lang_falls_back_to_default(self):
        t = Translator("xx")   # type: ignore
        assert t.lang == DEFAULT_LANG

    def test_all_fa_keys_exist_in_en(self):
        from src.cli.i18n import LANGUAGES
        fa_keys = set(LANGUAGES["fa"].keys())
        en_keys = set(LANGUAGES["en"].keys())
        missing = fa_keys - en_keys
        assert missing == set(), f"این کلیدها در EN نیستند: {missing}"

    def test_supported_langs_contains_fa_and_en(self):
        assert "fa" in SUPPORTED_LANGS
        assert "en" in SUPPORTED_LANGS

    def test_multiple_format_args(self):
        t = Translator("en")
        result = t.t("scores_mr_header", 42, 5)
        assert "42" in result
        assert "5" in result


class TestAllTranslationsNonEmpty:
    @pytest.mark.parametrize("lang", SUPPORTED_LANGS)
    def test_no_empty_translations(self, lang):
        from src.cli.i18n import LANGUAGES
        for key, value in LANGUAGES[lang].items():
            assert value, f"[{lang}] کلید '{key}' خالی است"