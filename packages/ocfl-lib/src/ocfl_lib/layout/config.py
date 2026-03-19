"""Layout config I/O: read/write extension configs and ocfl_layout.json."""

from typing import Any

import msgspec

from ocfl_lib.fs import FS, WriteFS, read_all, write_bytes

_EXTENSIONS_DIR = "extensions"
_OCFL_LAYOUT_FILE = "ocfl_layout.json"


class _OcflLayout(msgspec.Struct):
    extension: str
    description: str


def read_layout_config(fsys: FS, layout_name: str) -> dict[str, Any]:
    """Read extensions/{layout_name}/config.json and return parsed dict."""
    path = f"{_EXTENSIONS_DIR}/{layout_name}/config.json"
    data = read_all(fsys, path)
    return msgspec.json.decode(data)


def write_layout_config(fsys: WriteFS, layout_name: str, config: dict) -> None:
    """Write config dict to extensions/{layout_name}/config.json."""
    dir_path = f"{_EXTENSIONS_DIR}/{layout_name}"
    fsys.mkdir(dir_path)
    data = msgspec.json.encode(config)
    write_bytes(fsys, f"{dir_path}/config.json", data)


def read_ocfl_layout(fsys: FS) -> tuple[str, str] | None:
    """Read ocfl_layout.json → (extension, description) or None."""
    try:
        data = read_all(fsys, _OCFL_LAYOUT_FILE)
    except FileNotFoundError:
        return None
    layout = msgspec.json.decode(data, type=_OcflLayout)
    return layout.extension, layout.description


def write_ocfl_layout(fsys: WriteFS, *, extension: str, description: str) -> None:
    """Write ocfl_layout.json."""
    layout = _OcflLayout(extension=extension, description=description)
    data = msgspec.json.encode(layout)
    write_bytes(fsys, _OCFL_LAYOUT_FILE, data)
