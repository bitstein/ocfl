"""Low-level CLI output helpers."""


def err(msg: str) -> None:
    """Print an error message to stderr."""
    from ocfl_cli import _style

    _style.echo(_style.error(f"Error: {msg}"), err=True)
