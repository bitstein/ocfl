"""S3 filesystem backend.

This module is not part of the stable API. It is implemented and tested,
but the public surface (``fs/__init__.py``) does not export it. Code that
imports directly from this module accepts that the API may change without
notice.
"""

import io
import posixpath
from typing import TYPE_CHECKING, BinaryIO, cast

import boto3
from botocore.exceptions import ClientError

from ocfl_lib._errors import UnsupportedBackendError
from ocfl_lib.fs._protocols import (
    ConditionalFS,
    CopyFS,
    FileRef,
    FileWalker,
    FsEntry,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ocfl_lib.fs._protocols import Readable


def _not_found(e: ClientError) -> bool:
    """Return True if the ClientError indicates a missing object or bucket."""
    code = e.response["Error"]["Code"]
    return code in ("404", "NoSuchKey", "NoSuchBucket")


def _precondition_failed(e: ClientError) -> bool:
    """Return True if a conditional write was refused (412 / 409)."""
    code = e.response["Error"]["Code"]
    return code in ("412", "PreconditionFailed", "ConditionalRequestConflict")


class S3FileReader(io.RawIOBase):
    """Wrap an S3 StreamingBody as a RawIOBase for use as BinaryIO."""

    def __init__(self, body, content_length: int) -> None:
        self._body = body
        self._length = content_length

    def read(self, size: int = -1) -> bytes:
        if size == -1 or size is None:
            return self._body.read()
        return self._body.read(size)

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._body.close()
        super().close()


def _is_dir_marker(key: str) -> bool:
    return key.endswith("/")


class _CountingReader:
    def __init__(self, data: Readable) -> None:
        self._data = data
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._data.read(size)
        self.bytes_read += len(chunk)
        return chunk


class S3FS(ConditionalFS, CopyFS, FileWalker):
    """S3 filesystem implementing WriteFS, CopyFS, and FileWalker."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client=None,
        **boto_kwargs,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client or boto3.client("s3", **boto_kwargs)

    def _key(self, name: str) -> str:
        if not name or name == ".":
            return self._prefix
        if self._prefix:
            return f"{self._prefix}/{name}"
        return name

    def _resolve(self, name: str) -> str:
        """Validate path safety and return the S3 key."""
        if name and posixpath.isabs(name):
            raise ValueError(f"absolute path not allowed: {name!r}")
        normed = posixpath.normpath(name) if name else "."
        if normed.startswith("..") or "/../" in f"/{normed}/":
            raise ValueError(f"path escapes outside root: {name!r}")
        if normed == ".":
            return self._prefix
        return self._key(normed)

    def exists(self, name: str) -> bool:
        """Return True if an object or prefix exists at name."""
        key = self._resolve(name)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as e:
            if not _not_found(e):
                raise
        prefix = f"{key}/" if key else ""
        resp = self._client.list_objects_v2(
            Bucket=self._bucket, Prefix=prefix, MaxKeys=1
        )
        return resp.get("KeyCount", 0) > 0

    def open_file(self, name: str) -> BinaryIO:
        key = self._resolve(name)
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _not_found(e):
                raise FileNotFoundError(f"s3://{self._bucket}/{key}") from e
            raise
        return cast("BinaryIO", S3FileReader(resp["Body"], resp["ContentLength"]))

    def read_dir(self, name: str) -> Iterator[FsEntry]:
        key = self._resolve(name)
        prefix = f"{key}/" if key else ""

        paginator = self._client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self._bucket,
            Prefix=prefix,
            Delimiter="/",
        )

        found_any = False
        for page in pages:
            for cp in page.get("CommonPrefixes", ()):
                found_any = True
                dir_name = cp["Prefix"][len(prefix) :].rstrip("/")
                if dir_name:
                    yield FsEntry(
                        name=dir_name,
                        is_dir=True,
                        is_file=False,
                        size=None,
                    )
            for obj in page.get("Contents", ()):
                obj_key = obj["Key"]
                if _is_dir_marker(obj_key):
                    continue
                found_any = True
                entry_name = obj_key[len(prefix) :]
                if entry_name:
                    yield FsEntry(
                        name=entry_name,
                        is_dir=False,
                        is_file=True,
                        size=obj["Size"],
                    )

        if not found_any:
            raise FileNotFoundError(f"s3://{self._bucket}/{key}")

    def write(self, name: str, data: Readable) -> int:
        key = self._resolve(name)
        counter = _CountingReader(data)
        self._client.upload_fileobj(counter, self._bucket, key)
        return counter.bytes_read

    def remove(self, name: str) -> None:
        key = self._resolve(name)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _not_found(e):
                raise FileNotFoundError(f"s3://{self._bucket}/{key}") from e
            raise
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def remove_all(self, name: str) -> None:
        """Remove a file or directory tree."""
        key = self._resolve(name)
        prefix = f"{key}/" if key else ""

        to_delete: list[str] = []

        if key:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as e:
                if not _not_found(e):
                    raise
            else:
                to_delete.append(key)

        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", ()):
                to_delete.append(obj["Key"])

        if not to_delete:
            raise FileNotFoundError(f"s3://{self._bucket}/{key}")

        unique = list(dict.fromkeys(to_delete))

        # Batch delete in groups of 1000 (S3 limit).
        for i in range(0, len(unique), 1000):
            batch = [{"Key": delete_key} for delete_key in unique[i : i + 1000]]
            self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": batch, "Quiet": True},
            )

    def mkdir(self, name: str) -> None:
        """Create directory (no-op for S3)."""
        self._resolve(name)

    def read_tagged(self, name: str) -> tuple[bytes, str]:
        """Read a file with its ETag. FileNotFoundError if missing."""
        key = self._resolve(name)
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _not_found(e):
                raise FileNotFoundError(f"s3://{self._bucket}/{key}") from e
            raise
        return resp["Body"].read(), resp["ETag"]

    def write_new(self, name: str, data: bytes) -> str:
        """Create a file with If-None-Match: *. FileExistsError if it exists."""
        key = self._resolve(name)
        try:
            resp = self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, IfNoneMatch="*"
            )
        except ClientError as e:
            if _precondition_failed(e):
                raise FileExistsError(f"s3://{self._bucket}/{key}") from e
            raise
        return resp["ETag"]

    def write_if_match(self, name: str, data: bytes, tag: str) -> str | None:
        """Replace a file with If-Match: tag. None if the tag no longer matches."""
        key = self._resolve(name)
        try:
            resp = self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, IfMatch=tag
            )
        except ClientError as e:
            if _precondition_failed(e) or _not_found(e):
                return None
            raise
        return resp["ETag"]

    def check_conditional_writes(self, probe: str) -> None:
        """Raise UnsupportedBackendError if the store ignores the conditions.

        Writes the key `probe` create-only twice and with a wrong If-Match;
        the store must refuse both, then the probe is removed.
        """
        self.write_new(probe, b"probe")
        try:
            try:
                self.write_new(probe, b"probe")
            except FileExistsError:
                pass
            else:
                raise UnsupportedBackendError(
                    f"s3://{self._bucket} ignores If-None-Match; "
                    "it cannot claim versions"
                )
            if self.write_if_match(probe, b"probe", f'"{"0" * 32}"') is not None:
                raise UnsupportedBackendError(
                    f"s3://{self._bucket} ignores If-Match; it cannot publish safely"
                )
        finally:
            self._client.delete_object(Bucket=self._bucket, Key=self._resolve(probe))

    def copy(self, src: str, dst: str) -> int:
        src_key = self._resolve(src)
        dst_key = self._resolve(dst)
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                CopySource={"Bucket": self._bucket, "Key": src_key},
                Key=dst_key,
            )
        except ClientError as e:
            if _not_found(e):
                raise FileNotFoundError(f"s3://{self._bucket}/{src_key}") from e
            raise
        resp = self._client.head_object(Bucket=self._bucket, Key=dst_key)
        return resp["ContentLength"]

    def __repr__(self) -> str:
        if self._prefix:
            return f"S3FS({self._bucket}, {self._prefix})"
        return f"S3FS({self._bucket})"

    def walk_files(self, name: str) -> Iterator[FileRef]:
        """Recursively yield FileRef for every file under name."""
        key = self._resolve(name)
        prefix = f"{key}/" if key else ""

        paginator = self._client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self._bucket, Prefix=prefix)

        root_prefix = f"{self._prefix}/" if self._prefix else ""
        for page in pages:
            for obj in page.get("Contents", ()):
                obj_key = obj["Key"]
                if _is_dir_marker(obj_key):
                    continue
                rel = obj_key[len(root_prefix) :]
                yield FileRef(path=rel, size=obj["Size"])
