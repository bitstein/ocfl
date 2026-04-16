from collections.abc import Callable
from pathlib import Path

from ocfl_index import Index
from ocfl_index.mediatype import OCTET_STREAM
from ocfl_index.scanner import scan, sniff_pass
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def test_sniff_sets_media_type(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha", {"hello.txt": "Hello, world\n"}, algorithm=DigestAlgorithm.SHA256
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        media = idx._conn.execute("SELECT media_type FROM blobs").fetchone()[0]
        assert media is not None
        assert media != ""


def test_octet_stream_triggers_1mib_retry(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"\x00" * (16 * 1024)
    fixture.commit("alpha", {"f.bin": payload}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        idx._conn.execute("UPDATE blobs SET media_type = NULL")

        calls: list[int] = []

        def sniffer(buf: bytes) -> str:
            calls.append(len(buf))
            return OCTET_STREAM if len(calls) == 1 else "text/plain"

        sniff_pass(idx._conn, idx.storage_root.fs, sniffer=sniffer)
        assert len(calls) == 2
        assert calls[0] == 8 * 1024
        assert calls[1] > 8 * 1024
        media = idx._conn.execute("SELECT media_type FROM blobs").fetchone()[0]
        assert media == "text/plain"


def test_unchanged_blobs_not_resniffed(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        calls = 0

        def sniffer(buf: bytes) -> str:
            nonlocal calls
            calls += 1
            return "text/plain"

        sniff_pass(idx._conn, idx.storage_root.fs, sniffer=sniffer)
        assert calls == 0


def test_indeterminate_stored_as_octet_stream(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha", {"f.bin": b"\x00\x01\x02"}, algorithm=DigestAlgorithm.SHA256
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        idx._conn.execute("UPDATE blobs SET media_type = NULL")

        always_octet: Callable[[bytes], str] = lambda _: OCTET_STREAM  # noqa: E731
        sniff_pass(idx._conn, idx.storage_root.fs, sniffer=always_octet)
        media = idx._conn.execute("SELECT media_type FROM blobs").fetchone()[0]
        assert media == OCTET_STREAM
