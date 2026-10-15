import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from ocfl_lib.fs._local import LocalFS

if TYPE_CHECKING:
    from ocfl_lib.fs.s3 import S3FS


def open_fs(location: str) -> LocalFS | S3FS:
    """The backend for `location`: a local path or an `s3://bucket/prefix` URI.

    S3 needs the `[s3]` extra; it is imported only for an `s3://` location.
    """
    if location.startswith("s3://"):
        from ocfl_lib.fs.s3 import S3FS

        parsed = urllib.parse.urlparse(location)
        return S3FS(parsed.hostname or "", parsed.path.lstrip("/"))
    return LocalFS(Path(location))
