import hashlib
from pathlib import Path

import pytest

from ocfl_lib.fs._local import LocalFS
from ocfl_lib.ots import EXTENSION_NAME
from ocfl_lib.validation._object import validate_object
from ocfl_lib.validation._report import ValidationReport
from tests.helpers.ots import build_attestation, build_ots_object


def _validate(tmp_path: Path) -> ValidationReport:
    fsys = LocalFS(tmp_path)
    r = validate_object(fsys, "")
    return r


def test_ots_extension_is_known(tmp_path: Path) -> None:
    """Object with the extension config does NOT emit W013 (unknown ext)."""
    build_ots_object(tmp_path)
    r = _validate(tmp_path)
    w013 = [w for w in r.warnings if w.code == "W013" and EXTENSION_NAME in w.render()]
    assert not w013, f"W013 fired for known extension: {[w.render() for w in w013]}"


def test_sha512_with_ots_extension_rejected(tmp_path: Path) -> None:
    """SHA-512 manifest + ots extension config → E901."""
    build_ots_object(
        tmp_path,
        digest_algorithm="sha512",
        include_companions=False,
        include_inventory_v1_ots=False,
    )
    r = _validate(tmp_path)
    assert r.has_code("E901"), f"expected E901, got errors={[e.code for e in r.errors]}"


def test_companion_file_missing_for_content(tmp_path: Path) -> None:
    """Content file with no `.ots` companion → E902."""
    build_ots_object(tmp_path, include_companions=False)
    r = _validate(tmp_path)
    assert r.has_code("E902"), f"expected E902, got errors={[e.code for e in r.errors]}"


def test_proof_of_a_proof_is_allowed(tmp_path: Path) -> None:
    file_data = b"hello world"
    file_sha256 = hashlib.sha256(file_data).digest()
    inner_ots = build_attestation(file_sha256, block_height=800_000)
    inner_sha256 = hashlib.sha256(inner_ots).digest()
    outer_ots = build_attestation(inner_sha256, block_height=800_000)
    build_ots_object(
        tmp_path,
        files={"file.txt": file_data},
        extra_content_files={"file.txt.ots.ots": outer_ots},
    )
    r = _validate(tmp_path)
    assert r.is_valid, [e.code for e in r.errors]


def test_inventory_version_ots_missing(tmp_path: Path) -> None:
    """Extension present but no `inventory.json.v1.ots` → E904."""
    build_ots_object(tmp_path, include_inventory_v1_ots=False)
    r = _validate(tmp_path)
    assert r.has_code("E904"), f"expected E904, got errors={[e.code for e in r.errors]}"


def test_missing_version_inventory_is_one_e909(tmp_path: Path) -> None:
    build_ots_object(tmp_path)
    (tmp_path / "v1" / "inventory.json").unlink()
    r = _validate(tmp_path)
    assert [e.code for e in r.errors if e.code.startswith("E9")] == ["E909"]


def test_inventory_ots_digest_mismatch(tmp_path: Path) -> None:
    """Inventory `.ots` committing to wrong digest → E905."""
    bad_ots = build_attestation(
        hashlib.sha256(b"different bytes").digest(),
        block_height=800_000,
    )
    build_ots_object(tmp_path, inventory_ots_override=bad_ots)

    r = _validate(tmp_path)

    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_corrupt_inventory_ots_reports_e905(tmp_path: Path) -> None:
    build_ots_object(tmp_path, inventory_ots_override=b"not an ots file")

    r = _validate(tmp_path)

    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_inventory_ots_match_no_error(tmp_path: Path) -> None:
    """Inventory `.ots` committing to correct digest → no E905."""
    build_ots_object(tmp_path)
    r = _validate(tmp_path)
    assert not r.has_code("E905"), (
        f"unexpected E905, errors={[e.code for e in r.errors]}"
    )


def test_pending_inventory_ots_warns_w902(tmp_path: Path) -> None:
    build_ots_object(tmp_path, pending_inventory_ots=True)

    r = _validate(tmp_path)

    assert r.has_code("W902"), [w.code for w in r.warnings]
    pending_errors = [e for e in r.errors if e.code in _OTS_ERROR_CODES]
    assert not pending_errors, [e.code for e in pending_errors]


def test_embedded_digest_mismatch(tmp_path: Path) -> None:
    """`.ots` commits to wrong digest → E905."""
    file_data = b"hello world"
    wrong_digest = hashlib.sha256(b"a different file").digest()
    bad_ots = build_attestation(wrong_digest, block_height=800_000)
    build_ots_object(
        tmp_path,
        files={"file.txt": file_data},
        companion_overrides={"file.txt": bad_ots},
    )
    r = _validate(tmp_path)
    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_corrupt_companion_file_reports_e905(tmp_path: Path) -> None:
    build_ots_object(
        tmp_path,
        companion_overrides={"file.txt": b"not an ots file"},
    )

    r = _validate(tmp_path)

    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_embedded_digest_match_no_error(tmp_path: Path) -> None:
    """`.ots` committing to correct digest → no E905."""
    build_ots_object(tmp_path)
    r = _validate(tmp_path)
    assert not r.has_code("E905"), (
        f"unexpected E905, errors={[e.code for e in r.errors]}"
    )


_OTS_ERROR_CODES = frozenset(
    {"E901", "E902", "E904", "E905", "E906", "E907", "E908", "E909"}
)


def test_pending_content_proof_is_e907(tmp_path: Path) -> None:
    build_ots_object(tmp_path, pending_companions={"file.txt"})
    r = _validate(tmp_path)
    assert r.has_code("E907"), [e.code for e in r.errors]
    assert not r.has_code("W902")


@pytest.mark.parametrize(
    "code",
    [
        "E901",
        "E902",
        "E904",
        "E905",
        "E906",
        "E907",
        "E908",
        "E909",
        "W902",
        "W903",
        "W904",
        "W905",
    ],
)
def test_ots_codes_registered(code: str) -> None:
    from ocfl_lib.validation._codes import get_code

    get_code(code)
