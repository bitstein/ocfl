import io
import sys

from rich.console import Console
from rich.text import Text

from ocfl_cli import _style


def _render(text: Text, *, color: bool) -> str:
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=color,
        color_system="truecolor" if color else None,
        highlight=False,
    )
    console.print(text)
    return buf.getvalue()


def test_style_returns_rich_text():
    result = _style.style("added", "A")
    assert isinstance(result, Text)
    assert str(result) == "A"
    assert result.style == "bold green"


def test_unknown_category_no_style():
    result = _style.style("nonexistent", "x")
    assert isinstance(result, Text)
    assert str(result) == "x"
    assert result.style == ""


def test_convenience_helpers():
    cases = [
        (_style.added("A"), "A", "bold green"),
        (_style.deleted("D"), "D", "bold red"),
        (_style.modified("M"), "M", "bold yellow"),
        (_style.renamed("R"), "R", "bold cyan"),
        (_style.error("E"), "E", "bold red"),
        (_style.warning("W"), "W", "yellow"),
        (_style.valid("V"), "V", "bold green"),
        (_style.dim("d"), "d", "dim"),
        (_style.key("k"), "k", "bold"),
        (_style.version("v"), "v", "bold cyan"),
        (_style.digest("g"), "g", "dim"),
        (_style.date("t"), "t", "yellow"),
    ]
    for text, expected_str, expected_style in cases:
        assert isinstance(text, Text)
        assert str(text) == expected_str
        assert text.style == expected_style


def test_no_color_env_disables_color(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    _style.make_console().print(_style.added("hello"))
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "hello" in out


def test_force_color_env_enables_color(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _style.make_console().print(_style.added("hello"))
    out = capsys.readouterr().out
    assert "\x1b[" in out
    assert "hello" in out


def test_no_color_flag_disables(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")
    _style.make_console(no_color=True).print(_style.added("plain"))
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "plain" in out


def test_each_console_reads_the_env_when_made(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    plain = _style.make_console()
    monkeypatch.setenv("FORCE_COLOR", "1")
    coloured = _style.make_console()
    plain.print(_style.added("a"))
    coloured.print(_style.added("b"))
    out = capsys.readouterr().out
    assert out.startswith("a\n")
    assert "\x1b[" in out.split("a\n", 1)[1]


def test_non_tty_no_color(monkeypatch, capsys):
    assert not sys.stdout.isatty()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    _style.make_console().print(_style.added("notty"))
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "notty" in out


def test_should_color_no_color_beats_force_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert _style.should_color(io.StringIO()) is False


def test_should_color_handles_stream_without_isatty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    class NoIsatty:
        pass

    assert _style.should_color(NoIsatty()) is False


def test_should_color_handles_closed_stream(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    stream = io.StringIO()
    stream.close()
    assert _style.should_color(stream) is False


def test_stderr_console_routes_to_stderr(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    _style.make_console(stderr=True).print(_style.error("oops"))
    captured = capsys.readouterr()
    assert "oops" in captured.err
    assert "oops" not in captured.out


def test_print_plain_string(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _style.make_console().print("plain string")
    out = capsys.readouterr().out
    assert "plain string" in out
    assert "\x1b[" not in out


def test_render_helper_color_roundtrip():
    colored = _render(_style.added("X"), color=True)
    plain = _render(_style.added("X"), color=False)
    assert "\x1b[" in colored
    assert "\x1b[" not in plain
    assert "X" in colored and "X" in plain
