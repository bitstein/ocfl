"""CLI help contract."""

import logging
import re

from typer.testing import CliRunner

from ocfl_cli.exit_codes import EXIT_CODE_DOCS, ExitCode, format_exit_code_help
from ocfl_cli.main import _configure_logging, app


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


def test_exit_code_help_is_generated_from_canonical_values() -> None:
    help_text = format_exit_code_help()

    assert tuple(doc.code for doc in EXIT_CODE_DOCS) == tuple(ExitCode)
    for doc in EXIT_CODE_DOCS:
        assert f"{int(doc.code)}  {doc.description}" in help_text


def test_default_logging_suppresses_httpx2_info() -> None:
    _configure_logging(json_mode=False, verbose=False, quiet=False)

    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.WARNING


def test_verbose_logging_allows_httpx2_info() -> None:
    _configure_logging(json_mode=False, verbose=True, quiet=False)

    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.INFO
