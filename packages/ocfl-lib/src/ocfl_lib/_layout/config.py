from typing import Any

import msgspec

from ocfl_lib._path import join_path
from ocfl_lib.fs import FS, WriteFS, read_all, write_bytes

_EXTENSIONS_DIR = "extensions"
_OCFL_LAYOUT_FILE = "ocfl_layout.json"


class OcflLayout(msgspec.Struct):
    extension: str
    description: str = ""


def _config_dir(root_path: str, layout_name: str) -> str:
    return join_path(root_path, _EXTENSIONS_DIR, layout_name)


def read_layout_config(
    fsys: FS, layout_name: str, *, root_path: str = ""
) -> dict[str, Any] | None:
    path = join_path(_config_dir(root_path, layout_name), "config.json")
    try:
        data = read_all(fsys, path)
    except FileNotFoundError:
        return None
    return msgspec.json.decode(data)


def write_layout_config(
    fsys: WriteFS, layout_name: str, config: dict[str, Any], *, root_path: str = ""
) -> None:
    dir_path = _config_dir(root_path, layout_name)
    fsys.mkdir(dir_path)
    data = msgspec.json.encode(config)
    write_bytes(fsys, join_path(dir_path, "config.json"), data)


def read_ocfl_layout(fsys: FS, *, root_path: str = "") -> OcflLayout | None:
    try:
        data = read_all(fsys, join_path(root_path, _OCFL_LAYOUT_FILE))
    except FileNotFoundError:
        return None
    return msgspec.json.decode(data, type=OcflLayout)


def write_ocfl_layout(
    fsys: WriteFS, *, extension: str, description: str, root_path: str = ""
) -> None:
    layout = OcflLayout(extension=extension, description=description)
    data = msgspec.json.encode(layout)
    write_bytes(fsys, join_path(root_path, _OCFL_LAYOUT_FILE), data)
