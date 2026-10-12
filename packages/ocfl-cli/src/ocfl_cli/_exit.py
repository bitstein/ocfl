"""The one CLI error handler: an error's `ErrorClass` picks its exit code.

Commands raise; `OcflGroup.invoke` catches every lib and CLI error (plus
`OSError` and `KeyboardInterrupt`) once, at the root. Text mode prints
`Error: <message>` on stderr; `--json` prints
`{"error": {"code", "message", "exit"}}` on stderr instead.
"""

import json
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from typer.core import TyperGroup

from ocfl_cli import _style
from ocfl_cli.errors import CLIError, ErrorContext
from ocfl_cli.exit_codes import exit_code_for_class
from ocfl_lib import ErrorClass, OcflError

if TYPE_CHECKING:
    import click
    from rich.console import Console
    from rich.text import Text

HANDLED = (OcflError, CLIError, OSError, KeyboardInterrupt)


def error_class_of(exc: BaseException) -> ErrorClass:
    """How the CLI reacts to `exc`; context wrappers report their cause."""
    match exc:
        case ErrorContext():
            return error_class_of(exc.cause)
        case OcflError() | CLIError():
            return exc.error_class
        case KeyboardInterrupt():
            return ErrorClass.INTERRUPTED
        case _:
            return ErrorClass.INVALID


def error_code_of(exc: BaseException) -> str:
    """The stable snake_case code reported under `--json`."""
    match exc:
        case ErrorContext():
            return error_code_of(exc.cause)
        case OcflError() | CLIError():
            return exc.code
        case KeyboardInterrupt():
            return "interrupted"
        case OSError():
            return "os_error"
        case _:
            return "error"


def error_line(message: str) -> Text:
    """An error as the CLI prints it: `Error: <message>`."""
    return _style.error(f"Error: {message}")


def handle_error(
    exc: BaseException, *, json_mode: bool, console: Console | None = None
) -> NoReturn:
    """Report `exc` on stderr (this run's `console`) and exit with its code."""
    code = exit_code_for_class(error_class_of(exc))
    message = str(exc) or ("interrupted" if isinstance(exc, KeyboardInterrupt) else "")
    if json_mode:
        payload = {
            "error": {
                "code": error_code_of(exc),
                "message": message,
                "exit": int(code),
            }
        }
        typer.echo(json.dumps(payload), err=True)
    else:
        if console is None:
            console = _style.make_console(stderr=True)
        console.print(error_line(message))
    raise typer.Exit(int(code))


class OcflGroup(TyperGroup):
    """Root command group that routes every error through `handle_error`."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except HANDLED as exc:
            obj = ctx.obj if isinstance(ctx.obj, dict) else {}
            console = obj.get("err_console")
            if console is None:
                no_color = bool(ctx.params.get("no_color"))
                console = _style.make_console(stderr=True, no_color=no_color)
            handle_error(
                exc, json_mode=bool(ctx.params.get("json_output")), console=console
            )
