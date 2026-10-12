"""Layout config I/O: read/write extension configs and ocfl_layout.json.

`root` is the storage root's path within the FS ("" for the FS root).
"""

from typing import Any

import msgspec

from ocfl_lib._path import join_path
from ocfl_lib.fs import FS, WriteFS, read_all, write_bytes

_EXTENSIONS_DIR = "extensions"
_OCFL_LAYOUT_FILE = "ocfl_layout.json"


class _OcflLayout(msgspec.Struct):
    extension: str
    description: str = ""


def _config_dir(root: str, layout_name: str) -> str:
    return join_path(root, _EXTENSIONS_DIR, layout_name)


def read_layout_config(
    fsys: FS, layout_name: str, *, root: str = ""
) -> dict[str, Any] | None:
    """Read extensions/{layout_name}/config.json, or None if absent."""
    path = join_path(_config_dir(root, layout_name), "config.json")
    try:
        data = read_all(fsys, path)
    except FileNotFoundError:
        return None
    return msgspec.json.decode(data)


def write_layout_config(
    fsys: WriteFS, layout_name: str, config: dict[str, Any], *, root: str = ""
) -> None:
    """Write config dict to extensions/{layout_name}/config.json."""
    dir_path = _config_dir(root, layout_name)
    fsys.mkdir(dir_path)
    data = msgspec.json.encode(config)
    write_bytes(fsys, join_path(dir_path, "config.json"), data)


def read_ocfl_layout(fsys: FS, *, root: str = "") -> tuple[str, str] | None:
    """Read ocfl_layout.json → (extension, description) or None."""
    try:
        data = read_all(fsys, join_path(root, _OCFL_LAYOUT_FILE))
    except FileNotFoundError:
        return None
    layout = msgspec.json.decode(data, type=_OcflLayout)
    return layout.extension, layout.description


def write_ocfl_layout(
    fsys: WriteFS, *, extension: str, description: str, root: str = ""
) -> None:
    """Write ocfl_layout.json."""
    layout = _OcflLayout(extension=extension, description=description)
    data = msgspec.json.encode(layout)
    write_bytes(fsys, join_path(root, _OCFL_LAYOUT_FILE), data)
