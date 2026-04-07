"""Centralized output styling. Honors NO_COLOR, --no-color, and TTY detection."""

from __future__ import annotations

import os
import sys
from typing import IO, Any

from rich.console import Console
from rich.text import Text

_STYLES: dict[str, str] = {
    "added":    "bold green",
    "deleted":  "bold red",
    "modified": "bold yellow",
    "renamed":  "bold cyan",
    "error":    "bold red",
    "warning":  "yellow",
    "valid":    "bold green",
    "invalid":  "bold red",
    "dim":      "dim",
    "key":      "bold",
    "version":  "bold cyan",
    "digest":   "dim",
    "date":     "yellow",
}


def _should_color(stream: Any) -> bool:
    """Honor NO_COLOR (disable), FORCE_COLOR (enable), else TTY detection."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _make_console(stream: IO, *, stderr: bool = False) -> Console:
    if _should_color(stream):
        return Console(highlight=False, stderr=stderr, force_terminal=True)
    return Console(color_system=None, highlight=False, stderr=stderr)


_stdout_console = _make_console(sys.stdout)
_stderr_console = _make_console(sys.stderr, stderr=True)


def set_no_color() -> None:
    """Explicit --no-color override: force both consoles to plain output."""
    global _stdout_console, _stderr_console
    _stdout_console = Console(color_system=None, highlight=False)
    _stderr_console = Console(color_system=None, highlight=False, stderr=True)


def reinit_from_env() -> None:
    """Re-detect TTY + env vars. Mainly for tests that mutate os.environ."""
    global _stdout_console, _stderr_console
    _stdout_console = _make_console(sys.stdout)
    _stderr_console = _make_console(sys.stderr, stderr=True)


def style(category: str, text: str) -> Text:
    """Return a rich Text with the named semantic style applied."""
    return Text(text, style=_STYLES.get(category, ""))


def echo(text: str | Text, *, err: bool = False) -> None:
    """Print to stdout or stderr via rich console (handles styling + TTY)."""
    console = _stderr_console if err else _stdout_console
    console.print(text, soft_wrap=True)


# Convenience helpers for high-frequency call sites.
def added(text: str) -> Text:    return style("added", text)
def deleted(text: str) -> Text:  return style("deleted", text)
def modified(text: str) -> Text: return style("modified", text)
def renamed(text: str) -> Text:  return style("renamed", text)
def error(text: str) -> Text:    return style("error", text)
def warning(text: str) -> Text:  return style("warning", text)
def valid(text: str) -> Text:    return style("valid", text)
def dim(text: str) -> Text:      return style("dim", text)
def key(text: str) -> Text:      return style("key", text)
def version(text: str) -> Text:  return style("version", text)
def digest(text: str) -> Text:   return style("digest", text)
def date(text: str) -> Text:     return style("date", text)
