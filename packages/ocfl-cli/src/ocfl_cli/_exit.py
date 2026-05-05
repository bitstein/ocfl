"""Side-effecting CLI error rendering and process exits."""

from typing import NoReturn

import typer

from ocfl_cli._io import err
from ocfl_cli.exit_codes import exit_code_for


def exit_for_error(exc: BaseException, *, message: str | None = None) -> NoReturn:
    """Render *exc* as a CLI error and exit with the canonical code."""
    err(str(exc) if message is None else message)
    raise typer.Exit(int(exit_code_for(exc))) from None
