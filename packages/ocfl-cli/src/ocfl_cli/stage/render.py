"""Text renderers shared by the stage commands."""

from typing import TYPE_CHECKING

import typer
from rich.text import Text

from ocfl_cli import _style
from ocfl_cli import errors as errors

if TYPE_CHECKING:
    from rich.console import Console


def render_status_diff(out: Console, data: dict) -> None:
    action_prefix = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}
    sym_style = {
        "A": _style.added,
        "D": _style.deleted,
        "M": _style.modified,
        "R": _style.renamed,
    }
    for c in data["changes"]:
        prefix = action_prefix.get(c["action"], "?")
        styler = sym_style.get(prefix)
        styled_sym = styler(prefix) if styler else Text(prefix)
        parts: list = [styled_sym, " ", c["path"]]
        if c.get("renamed_from"):
            parts.append(f" (from {c['renamed_from']})")
        out.print(Text.assemble(*parts))


def render_section(out: Console, title: str, *, leading_blank: bool = False) -> None:
    if leading_blank:
        typer.echo()
    out.print(_style.key(f"{title}:"))


def render_metadata_status(
    out: Console, data: dict[str, str | None], *, leading_blank: bool = False
) -> None:
    render_section(out, "Metadata", leading_blank=leading_blank)
    for field in ("message", "user.name", "user.address"):
        value = data[field] if data[field] is not None else "missing"
        typer.echo(f"  {field}: {value}")


def pluralize(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


def short_job_id(job_id: str) -> str:
    return job_id[:8]
