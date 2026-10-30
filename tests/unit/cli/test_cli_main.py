"""تست‌های CLI entrypoint و argument parsing."""

import pytest
from unittest.mock import patch
from src.cli.__main__ import main


class TestCLIHelp:
    def test_no_command_returns_zero(self):
        result = main([])
        assert result == 0

    def test_help_flag_exits(self):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_ingest_business_help_exits(self):
        with pytest.raises(SystemExit) as exc:
            main(["ingest-business", "--help"])
        assert exc.value.code == 0

    def test_ingest_technical_help_exits(self):
        with pytest.raises(SystemExit) as exc:
            main(["ingest-technical", "--help"])
        assert exc.value.code == 0


class TestCLILangOption:
    def test_lang_before_command(self):
        result = main(["--lang", "en"])
        assert result == 0

    def test_lang_after_command(self):
        """--lang بعد از نامِ command هم باید قبول شود."""
        with patch("src.cli.commands.ingest.run", return_value=0) as run:
            result = main(["ingest-business", "--file", "catalog.jsonl", "--lang", "en"])
        assert result == 0
        assert run.call_args.args[0].lang == "en"

    def test_lang_before_command_reaches_the_command(self):
        with patch("src.cli.commands.ingest.run", return_value=0) as run:
            result = main(["--lang", "fa", "ingest-business", "--file", "catalog.jsonl"])
        assert result == 0
        assert run.call_args.args[0].lang == "fa"

    def test_invalid_lang_exits_with_error(self):
        with pytest.raises(SystemExit) as exc:
            main(["--lang", "de"])
        assert exc.value.code != 0


class TestCLIKeyboardInterrupt:
    def test_keyboard_interrupt_returns_130(self):
        with patch("src.cli.commands.ingest.run", side_effect=KeyboardInterrupt):
            result = main(["ingest-business", "--file", "catalog.jsonl"])
        assert result == 130
