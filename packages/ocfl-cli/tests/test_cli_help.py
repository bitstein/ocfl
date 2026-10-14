"""CLI help contract."""

import importlib.metadata
import inspect
import logging
import re

import pytest
import typer
from rich.text import Text
from typer.testing import CliRunner

from ocfl_cli._common import installed_cli_version
from ocfl_cli.exit_codes import EXIT_CODE_DOCS, ExitCode, format_exit_code_help
from ocfl_cli.main import app, configure_logging


def test_root_help_groups_commands_and_documents_exit_codes(
    runner: CliRunner,
) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Archive" in result.output
    assert "Storage admin" in result.output
    assert "Staging" in result.output
    normalized_output = re.sub(r"\s+", " ", result.output)
    for line in format_exit_code_help().splitlines():
        assert re.sub(r"\s+", " ", line).strip() in normalized_output


def test_root_help_lists_one_exit_code_per_line(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"], terminal_width=120)

    lines = [line.strip() for line in result.output.splitlines()]
    for doc in EXIT_CODE_DOCS:
        assert f"{int(doc.code)}  {doc.description}" in lines, result.output


def test_timestamp_help_uses_ots_panel(runner: CliRunner) -> None:
    result = runner.invoke(app, ["stage", "--help"])

    assert result.exit_code == 0, result.output
    assert "OTS" in result.output
    assert "timestamp" in result.output


def test_validate_help_names_the_bitcoin_section(runner: CliRunner) -> None:
    result = runner.invoke(app, ["validate", "--help"])

    assert result.exit_code == 0, result.output
    unboxed = re.sub(r"[\s│]+", " ", result.output)
    assert "auto (chain when [bitcoin] is configured" in unboxed


def _help_strings(cmd, path: str):
    yield path, cmd.help
    yield path, cmd.epilog
    for param in cmd.params:
        yield f"{path} {param.name}", getattr(param, "help", None)
    for name, sub in getattr(cmd, "commands", {}).items():
        yield from _help_strings(sub, f"{path} {name}")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(inspect.cleandoc(text), id=where)
        for where, text in _help_strings(typer.main.get_command(app), "ocfl")
        if text
    ],
)
def test_help_text_survives_rich_markup(text: str) -> None:
    rendered = Text.from_markup(text).plain
    assert rendered == text.replace("\\[", "["), (
        "Rich markup ate part of the help; wrap the string in rich.markup.escape()"
    )


def test_exit_code_help_is_generated_from_canonical_values() -> None:
    help_text = format_exit_code_help()

    assert tuple(doc.code for doc in EXIT_CODE_DOCS) == tuple(ExitCode)
    for doc in EXIT_CODE_DOCS:
        assert f"{int(doc.code)}  {doc.description}" in help_text


def test_default_logging_suppresses_httpx2_info() -> None:
    configure_logging(json_mode=False, verbose=False, quiet=False)

    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.WARNING


def test_verbose_logging_allows_httpx2_info() -> None:
    configure_logging(json_mode=False, verbose=True, quiet=False)

    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.INFO


def test_version_prints_the_installed_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"ocfl {installed_cli_version()}\n"


def test_object_version_options_are_not_called_version(runner: CliRunner) -> None:
    for command in ("ls", "export"):
        result = runner.invoke(app, [command, "--help"])
        assert "--object-version" in result.output, command


def test_cli_version_is_cached(monkeypatch) -> None:
    calls = 0

    def fake_version(distribution: str) -> str:
        nonlocal calls
        calls += 1
        assert distribution == "ocfl-cli"
        return "1.2.3"

    installed_cli_version.cache_clear()
    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    try:
        assert installed_cli_version() == "1.2.3"
        assert installed_cli_version() == "1.2.3"
    finally:
        installed_cli_version.cache_clear()
    assert calls == 1


def test_cli_version_unknown_without_metadata(monkeypatch) -> None:
    def missing(distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError(distribution)

    installed_cli_version.cache_clear()
    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        assert installed_cli_version() == "unknown"
    finally:
        installed_cli_version.cache_clear()
