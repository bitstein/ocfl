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
from ocfl_cli._appctx import AppCtx
from ocfl_cli.errors import CLIError, ErrorContext
from ocfl_cli.exit_codes import exit_code_for_class
from ocfl_lib import ErrorClass, OcflError

if TYPE_CHECKING:
    # Typer vendors Click and exports no public alias for its base Context.
    from rich.console import Console
    from rich.text import Text
    from typer._click import Context

HANDLED = (OcflError, CLIError, OSError, KeyboardInterrupt)


def error_class_of(exc: BaseException) -> ErrorClass:
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
    return _style.error(f"Error: {message}")


def handle_error(
    exc: BaseException, *, json_mode: bool, console: Console | None = None
) -> NoReturn:
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
    def invoke(self, ctx: Context) -> Any:
        try:
            return super().invoke(ctx)
        except HANDLED as exc:
            match ctx.obj:
                case AppCtx(err_console=console):
                    pass
                case _:
                    no_color = bool(ctx.params.get("no_color"))
                    console = _style.make_console(stderr=True, no_color=no_color)
            handle_error(
                exc, json_mode=bool(ctx.params.get("json_output")), console=console
            )
