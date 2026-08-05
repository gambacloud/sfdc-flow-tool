"""Credential loading. The failure this prevents is a key that silently isn't there."""

import os

from flowtool.config import ENV_FILENAME, env_help, load_env, parse_env


class TestParse:
    def test_plain_pairs(self):
        assert parse_env("GEMINI_API_KEY=abc123") == {"GEMINI_API_KEY": "abc123"}

    def test_ignores_comments_and_blanks(self):
        text = "# a comment\n\nGEMINI_API_KEY=abc\n\n# trailing\n"
        assert parse_env(text) == {"GEMINI_API_KEY": "abc"}

    def test_tolerates_export_prefix(self):
        assert parse_env("export GEMINI_API_KEY=abc") == {"GEMINI_API_KEY": "abc"}

    def test_strips_surrounding_quotes(self):
        # Keys are usually pasted with the quotes still on.
        assert parse_env('GEMINI_API_KEY="abc"') == {"GEMINI_API_KEY": "abc"}
        assert parse_env("GEMINI_API_KEY='abc'") == {"GEMINI_API_KEY": "abc"}

    def test_keeps_equals_signs_inside_the_value(self):
        assert parse_env("K=a=b=c") == {"K": "a=b=c"}

    def test_tolerates_spaces_around_the_equals(self):
        assert parse_env("K = v") == {"K": "v"}

    def test_strips_a_byte_order_mark_from_the_name(self):
        # PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM. Left in,
        # the variable is named "﻿GEMINI_API_KEY", which prints identically
        # to the real name and matches nothing.
        assert parse_env("﻿GEMINI_API_KEY=abc") == {"GEMINI_API_KEY": "abc"}


class TestEncoding:
    def test_utf8_with_bom_loads(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        (tmp_path / ENV_FILENAME).write_bytes(
            b"\xef\xbb\xbfGEMINI_API_KEY=from-bom-file\n"
        )
        assert load_env(tmp_path) == ["GEMINI_API_KEY"]
        assert os.environ["GEMINI_API_KEY"] == "from-bom-file"

    def test_plain_utf8_still_loads(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        (tmp_path / ENV_FILENAME).write_bytes(b"GEMINI_API_KEY=plain\n")
        assert load_env(tmp_path) == ["GEMINI_API_KEY"]
        assert os.environ["GEMINI_API_KEY"] == "plain"

    def test_empty_value_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        (tmp_path / ENV_FILENAME).write_text("GEMINI_API_KEY=\n", encoding="utf-8")
        assert load_env(tmp_path) == []
        assert "GEMINI_API_KEY" not in os.environ


class TestLoad:
    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_env(tmp_path) == []

    def test_applies_values(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        (tmp_path / ENV_FILENAME).write_text("GEMINI_API_KEY=from-file", encoding="utf-8")
        assert load_env(tmp_path) == ["GEMINI_API_KEY"]
        assert os.environ["GEMINI_API_KEY"] == "from-file"

    def test_a_real_export_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "from-shell")
        (tmp_path / ENV_FILENAME).write_text("GEMINI_API_KEY=from-file", encoding="utf-8")
        assert load_env(tmp_path) == []
        assert os.environ["GEMINI_API_KEY"] == "from-shell"


class TestHelp:
    def test_names_the_file_it_wants(self, tmp_path):
        message = env_help(tmp_path)
        assert ENV_FILENAME in message
        assert "GEMINI_API_KEY" in message

    def test_the_instruction_is_the_file_not_a_shell_variable(self):
        # Indented lines read as "run this". A $env: line there is the advice
        # that led to a key being pasted into source; prose about it is fine.
        commands = [
            line.strip() for line in env_help(".").splitlines()
            if line.startswith("    ") and line.strip()
        ]
        assert any(c.startswith("GEMINI_API_KEY=") for c in commands)
        assert not any("$env:" in c for c in commands), commands


class TestGitignore:
    def test_env_file_is_ignored(self):
        with open(".gitignore", encoding="utf-8") as handle:
            patterns = {line.strip() for line in handle}
        assert ".env" in patterns, "a credentials file must never be committable"
