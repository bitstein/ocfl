"""Side-effecting CLI error rendering and process exits."""

from typing import NoReturn

import typer

from ocfl_cli._common import err
from ocfl_cli.exit_codes import exit_code_for


def exit_for_error(exc: BaseException) -> NoReturn:
    """Render *exc* as a CLI error and exit with the canonical code."""
    err(str(exc))
    raise typer.Exit(int(exit_code_for(exc))) from None
