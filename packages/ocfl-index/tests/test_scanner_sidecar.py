import logging
from pathlib import Path

import pytest
from index_fixtures import FixtureRoot

from ocfl_index.scanner import _read_sidecar
from ocfl_lib import DigestAlgorithm


def _object_relpath(fixture: FixtureRoot, object_id: str) -> str:
    return fixture.root.object_path(object_id)


def test_sha256_preferred_over_sha512(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)
    relpath = _object_relpath(fixture, "alpha")

    sha512_sidecar = fixture.root_dir / relpath / "inventory.json.sha512"
    sha512_sidecar.write_text("0000000000000000 inventory.json\n")

    result = _read_sidecar(fixture.fs, relpath)
    assert result is not None
    alg, digest = result
    assert alg is DigestAlgorithm.SHA256
    assert digest != "0000000000000000"


def test_sha512_only_object_resolves(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("beta", {"b.txt": "hi"}, algorithm=DigestAlgorithm.SHA512)
    relpath = _object_relpath(fixture, "beta")

    result = _read_sidecar(fixture.fs, relpath)
    assert result is not None
    alg, digest = result
    assert alg is DigestAlgorithm.SHA512
    assert len(digest) == 128


def test_missing_sidecar_warns_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("gamma", {"c.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    relpath = _object_relpath(fixture, "gamma")

    (fixture.root_dir / relpath / "inventory.json.sha256").unlink()

    with caplog.at_level(logging.WARNING, logger="ocfl_index.scanner"):
        assert _read_sidecar(fixture.fs, relpath) is None
    assert any("no inventory sidecar" in m for m in caplog.messages)


@pytest.mark.parametrize(
    ("text", "expected"), [("", ""), (" \n", ""), ("ABCDEF inventory.json\n", "abcdef")]
)
def test_sidecar_digest_is_first_word_lowercased(
    tmp_path: Path, text: str, expected: str
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("gamma", {"c.txt": "x"}, algorithm=DigestAlgorithm.SHA512)
    relpath = _object_relpath(fixture, "gamma")
    (fixture.root_dir / relpath / "inventory.json.sha512").write_text(text)

    assert _read_sidecar(fixture.fs, relpath) == (DigestAlgorithm.SHA512, expected)
