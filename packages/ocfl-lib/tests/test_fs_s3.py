"""Tests for S3FS backend."""

from io import BytesIO
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

from ocfl_lib.fs import CopyFS

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from ocfl_lib.fs.s3 import S3FS

BUCKET = "test-bucket"
REGION = "us-east-1"


@pytest.fixture
def s3_client():
    with mock_aws():
        client: S3Client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _seed(client: S3Client) -> None:
    """Seed standard test objects directly via boto3."""
    client.put_object(Bucket=BUCKET, Key="a.txt", Body=b"hello")
    client.put_object(Bucket=BUCKET, Key="sub/b.txt", Body=b"world")
    client.put_object(Bucket=BUCKET, Key="sub/deep/c.txt", Body=b"\x00\x01\x02")


@pytest.fixture
def fs(s3_client: S3Client) -> S3FS:
    from ocfl_lib.fs.s3 import S3FS

    _seed(s3_client)
    return S3FS(BUCKET, client=s3_client)


class TestProtocolConformance:
    def test_is_copy_fs(self, fs: S3FS):
        assert isinstance(fs, CopyFS)


class TestOpenFile:
    def test_read_file(self, fs: S3FS):
        with fs.open_file("a.txt") as f:
            assert f.read() == b"hello"

    def test_read_nested(self, fs: S3FS):
        with fs.open_file("sub/b.txt") as f:
            assert f.read() == b"world"

    def test_not_found(self, fs: S3FS):
        with pytest.raises(FileNotFoundError):
            fs.open_file("nope.txt")


class TestReadDir:
    def test_root(self, fs: S3FS):
        entries = sorted(fs.read_dir(""), key=lambda e: e.name)
        names = [e.name for e in entries]
        assert "a.txt" in names
        assert "sub" in names

    def test_subdir(self, fs: S3FS):
        entries = sorted(fs.read_dir("sub"), key=lambda e: e.name)
        names = [e.name for e in entries]
        assert names == ["b.txt", "deep"]

    def test_entry_types(self, fs: S3FS):
        entries = {e.name: e for e in fs.read_dir("")}
        assert entries["a.txt"].is_file is True
        assert entries["a.txt"].is_dir is False
        assert entries["sub"].is_dir is True
        assert entries["sub"].is_file is False
        assert entries["sub"].size is None

    def test_file_size(self, fs: S3FS):
        entries = {e.name: e for e in fs.read_dir("")}
        assert entries["a.txt"].size == 5  # len("hello")

    def test_not_found(self, fs: S3FS):
        with pytest.raises(FileNotFoundError):
            list(fs.read_dir("nope"))

    def test_pagination(self, s3_client: S3Client):
        """Verify read_dir paginates correctly with >1000 keys."""
        from ocfl_lib.fs.s3 import S3FS

        for i in range(1050):
            s3_client.put_object(Bucket=BUCKET, Key=f"many/{i:04d}.txt", Body=b"x")
        s3fs = S3FS(BUCKET, client=s3_client)
        entries = list(s3fs.read_dir("many"))
        file_entries = [e for e in entries if e.is_file]
        assert len(file_entries) == 1050


class TestWrite:
    def test_write_new_file(self, fs: S3FS, s3_client: S3Client):
        n = fs.write("new.txt", BytesIO(b"content"))
        assert n == 7
        body = s3_client.get_object(Bucket=BUCKET, Key="new.txt")["Body"].read()
        assert body == b"content"

    def test_write_overwrite(self, fs: S3FS, s3_client: S3Client):
        fs.write("a.txt", BytesIO(b"replaced"))
        body = s3_client.get_object(Bucket=BUCKET, Key="a.txt")["Body"].read()
        assert body == b"replaced"

    def test_write_nested(self, fs: S3FS, s3_client: S3Client):
        n = fs.write("newdir/f.txt", BytesIO(b"data"))
        assert n == 4
        body = s3_client.get_object(Bucket=BUCKET, Key="newdir/f.txt")["Body"].read()
        assert body == b"data"


class TestRemove:
    def test_remove_file(self, fs: S3FS, s3_client: S3Client):
        fs.remove("a.txt")
        with pytest.raises(s3_client.exceptions.NoSuchKey):
            s3_client.get_object(Bucket=BUCKET, Key="a.txt")

    def test_remove_not_found(self, fs: S3FS):
        with pytest.raises(FileNotFoundError):
            fs.remove("nope.txt")


class TestRemoveAll:
    def test_remove_dir(self, fs: S3FS, s3_client: S3Client):
        fs.remove_all("sub")
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="sub/")
        assert resp.get("KeyCount", 0) == 0

    def test_remove_single_file(self, fs: S3FS, s3_client: S3Client):
        fs.remove_all("a.txt")
        with pytest.raises(s3_client.exceptions.NoSuchKey):
            s3_client.get_object(Bucket=BUCKET, Key="a.txt")

    def test_remove_not_found(self, fs: S3FS):
        with pytest.raises(FileNotFoundError):
            fs.remove_all("nope")

    def test_batch_deletion(self, s3_client: S3Client):
        """Verify batch deletion works for >1000 keys."""
        from ocfl_lib.fs.s3 import S3FS

        for i in range(1050):
            s3_client.put_object(Bucket=BUCKET, Key=f"bulk/{i:04d}.txt", Body=b"x")
        s3fs = S3FS(BUCKET, client=s3_client)
        s3fs.remove_all("bulk")
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="bulk/")
        assert resp.get("KeyCount", 0) == 0


class TestMkdir:
    def test_noop(self, fs: S3FS, s3_client: S3Client):
        """mkdir is a no-op on S3 -- should not create any objects."""
        fs.mkdir("newdir")
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="newdir/")
        assert resp.get("KeyCount", 0) == 0

    def test_nested(self, fs: S3FS, s3_client: S3Client):
        fs.mkdir("a/b/c")
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="a/b/c/")
        assert resp.get("KeyCount", 0) == 0

    def test_existing_ok(self, fs: S3FS):
        fs.mkdir("sub")  # already has keys under sub/ -- should not raise


class TestCopy:
    def test_copy_file(self, fs: S3FS, s3_client: S3Client):
        n = fs.copy("a.txt", "copy.txt")
        assert n == 5
        body = s3_client.get_object(Bucket=BUCKET, Key="copy.txt")["Body"].read()
        assert body == b"hello"
        # Source unchanged
        src = s3_client.get_object(Bucket=BUCKET, Key="a.txt")["Body"].read()
        assert src == b"hello"

    def test_copy_nested(self, fs: S3FS, s3_client: S3Client):
        fs.copy("sub/b.txt", "out/b_copy.txt")
        body = s3_client.get_object(Bucket=BUCKET, Key="out/b_copy.txt")["Body"].read()
        assert body == b"world"

    def test_copy_not_found(self, fs: S3FS):
        with pytest.raises(FileNotFoundError):
            fs.copy("nope.txt", "dst.txt")


class TestWalkFiles:
    def test_walk_all(self, fs: S3FS):
        refs = sorted(fs.walk_files(""), key=lambda r: r.path)
        paths = [r.path for r in refs]
        assert paths == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]

    def test_walk_subdir(self, fs: S3FS):
        refs = sorted(fs.walk_files("sub"), key=lambda r: r.path)
        paths = [r.path for r in refs]
        assert paths == ["sub/b.txt", "sub/deep/c.txt"]

    def test_sizes(self, fs: S3FS):
        refs = {r.path: r for r in fs.walk_files("")}
        assert refs["a.txt"].size == 5
        assert refs["sub/b.txt"].size == 5
        assert refs["sub/deep/c.txt"].size == 3

    def test_empty_bucket(self, s3_client: S3Client):
        from ocfl_lib.fs.s3 import S3FS

        s3_client.create_bucket(Bucket="empty-bucket")
        s3fs = S3FS("empty-bucket", client=s3_client)
        assert list(s3fs.walk_files("")) == []


class TestPathValidation:
    def test_reject_absolute(self, fs: S3FS):
        with pytest.raises(ValueError, match="absolute"):
            fs.open_file("/etc/passwd")

    def test_reject_dotdot(self, fs: S3FS):
        with pytest.raises(ValueError, match="outside"):
            fs.open_file("../etc/passwd")

    def test_reject_dotdot_nested(self, fs: S3FS):
        with pytest.raises(ValueError, match="outside"):
            fs.open_file("sub/../../etc/passwd")

    def test_reject_dotdot_write(self, fs: S3FS):
        with pytest.raises(ValueError, match="outside"):
            fs.write("../escape.txt", BytesIO(b"bad"))

    def test_reject_dotdot_remove(self, fs: S3FS):
        with pytest.raises(ValueError, match="outside"):
            fs.remove("../escape.txt")

    def test_reject_absolute_mkdir(self, fs: S3FS):
        with pytest.raises(ValueError, match="absolute"):
            fs.mkdir("/tmp/evil")


class TestRepr:
    def test_repr_no_prefix(self, fs: S3FS):
        assert repr(fs) == f"S3FS({BUCKET})"

    def test_repr_with_prefix(self, s3_client: S3Client):
        from ocfl_lib.fs.s3 import S3FS

        s3fs = S3FS(BUCKET, prefix="data/ocfl", client=s3_client)
        assert repr(s3fs) == f"S3FS({BUCKET}, data/ocfl)"


class TestPrefix:
    """Verify S3FS with prefix correctly scopes all operations."""

    @pytest.fixture
    def prefixed_fs(self, s3_client: S3Client) -> S3FS:
        from ocfl_lib.fs.s3 import S3FS

        # Seed objects under the prefix via raw boto3
        s3_client.put_object(Bucket=BUCKET, Key="myprefix/a.txt", Body=b"hello")
        s3_client.put_object(Bucket=BUCKET, Key="myprefix/sub/b.txt", Body=b"world")
        s3_client.put_object(
            Bucket=BUCKET, Key="myprefix/sub/deep/c.txt", Body=b"\x00\x01\x02"
        )
        # Also put an object outside the prefix to verify isolation
        s3_client.put_object(Bucket=BUCKET, Key="other/outside.txt", Body=b"nope")
        return S3FS(BUCKET, prefix="myprefix", client=s3_client)

    def test_open_file(self, prefixed_fs: S3FS):
        with prefixed_fs.open_file("a.txt") as f:
            assert f.read() == b"hello"

    def test_open_nested(self, prefixed_fs: S3FS):
        with prefixed_fs.open_file("sub/b.txt") as f:
            assert f.read() == b"world"

    def test_open_not_found(self, prefixed_fs: S3FS):
        with pytest.raises(FileNotFoundError):
            prefixed_fs.open_file("outside.txt")

    def test_read_dir_root(self, prefixed_fs: S3FS):
        entries = sorted(prefixed_fs.read_dir(""), key=lambda e: e.name)
        names = [e.name for e in entries]
        assert "a.txt" in names
        assert "sub" in names
        # Object outside prefix must not appear
        assert "other" not in names
        assert "outside.txt" not in names

    def test_read_dir_subdir(self, prefixed_fs: S3FS):
        entries = sorted(prefixed_fs.read_dir("sub"), key=lambda e: e.name)
        names = [e.name for e in entries]
        assert names == ["b.txt", "deep"]

    def test_write(self, prefixed_fs: S3FS, s3_client: S3Client):
        prefixed_fs.write("new.txt", BytesIO(b"data"))
        body = s3_client.get_object(Bucket=BUCKET, Key="myprefix/new.txt")[
            "Body"
        ].read()
        assert body == b"data"

    def test_remove(self, prefixed_fs: S3FS, s3_client: S3Client):
        prefixed_fs.remove("a.txt")
        with pytest.raises(s3_client.exceptions.NoSuchKey):
            s3_client.get_object(Bucket=BUCKET, Key="myprefix/a.txt")

    def test_remove_all(self, prefixed_fs: S3FS, s3_client: S3Client):
        prefixed_fs.remove_all("sub")
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="myprefix/sub/")
        assert resp.get("KeyCount", 0) == 0

    def test_copy(self, prefixed_fs: S3FS, s3_client: S3Client):
        prefixed_fs.copy("a.txt", "a_copy.txt")
        body = s3_client.get_object(Bucket=BUCKET, Key="myprefix/a_copy.txt")[
            "Body"
        ].read()
        assert body == b"hello"

    def test_walk_files(self, prefixed_fs: S3FS):
        refs = sorted(prefixed_fs.walk_files(""), key=lambda r: r.path)
        paths = [r.path for r in refs]
        assert paths == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]

    def test_walk_subdir(self, prefixed_fs: S3FS):
        refs = sorted(prefixed_fs.walk_files("sub"), key=lambda r: r.path)
        paths = [r.path for r in refs]
        assert paths == ["sub/b.txt", "sub/deep/c.txt"]

    def test_mkdir_noop(self, prefixed_fs: S3FS, s3_client: S3Client):
        prefixed_fs.mkdir("newdir")
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix="myprefix/newdir/")
        assert resp.get("KeyCount", 0) == 0


class TestExists:
    def test_exists_object(self, fs):
        fs.write("file.txt", BytesIO(b"hi"))
        assert fs.exists("file.txt") is True

    def test_exists_missing(self, fs):
        assert fs.exists("nope.txt") is False

    def test_exists_prefix(self, fs):
        fs.write("d/sub.txt", BytesIO(b"hi"))
        assert fs.exists("d") is True
