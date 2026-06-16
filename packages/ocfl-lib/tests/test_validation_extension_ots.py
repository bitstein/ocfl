"""Tests for NNNN-ots-timestamps extension validation."""

import hashlib
import json
from pathlib import Path

import pytest

from ocfl_lib.fs.local import LocalFS
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.result import ValidationResult
from tests.helpers.ots import build_attestation

_EXT_NAME = "NNNN-ots-timestamps"


def _hex(alg: str, data: bytes) -> str:
    return hashlib.new(alg, data).hexdigest()


def _build_ots_object(
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
    include_companions: bool = True,
    include_extension_dir: bool = True,
    include_inventory_v1_ots: bool = True,
    digest_algorithm: str = "sha256",
    extra_content_files: dict[str, bytes] | None = None,
    companion_overrides: dict[str, bytes] | None = None,
    inventory_ots_override: bytes | None = None,
    pending_inventory_ots: bool = False,
    pending_companions: set[str] | None = None,
) -> Path:
    """Build a minimal SHA-256 OCFL object on disk for ots-extension tests.

    Knobs let individual tests break one invariant at a time.
    """
    files = {"file.txt": b"hello world"} if files is None else files
    pending_companions = pending_companions or set()
    companion_overrides = companion_overrides or {}
    extra_content_files = extra_content_files or {}

    manifest: dict[str, list[str]] = {}
    state: dict[str, list[str]] = {}

    def _add(content_path: str, state_path: str, data: bytes) -> None:
        d = _hex(digest_algorithm, data)
        manifest.setdefault(d, []).append(content_path)
        state.setdefault(d, []).append(state_path)

    companion_bytes: dict[str, bytes] = {}
    for name, data in files.items():
        _add(f"v1/content/{name}", name, data)
        if include_companions:
            file_sha256 = hashlib.sha256(data).digest()
            if name in companion_overrides:
                ots_bytes = companion_overrides[name]
            elif name in pending_companions:
                ots_bytes = build_attestation(
                    file_sha256, pending_uri="https://a.pool.opentimestamps.org"
                )
            else:
                ots_bytes = build_attestation(file_sha256, block_height=800_000)
            companion_bytes[name] = ots_bytes
            _add(f"v1/content/{name}.ots", f"{name}.ots", ots_bytes)

    for path, data in extra_content_files.items():
        _add(f"v1/content/{path}", path, data)

    inv = {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": digest_algorithm,
        "head": "v1",
        "manifest": manifest,
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": state,
                "message": "init",
                "user": {"name": "t", "address": "mailto:t@e.org"},
            },
        },
    }
    inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
    inv_digest = _hex(digest_algorithm, inv_bytes)

    (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    (tmp_path / "inventory.json").write_bytes(inv_bytes)
    (tmp_path / f"inventory.json.{digest_algorithm}").write_text(
        f"{inv_digest} inventory.json\n"
    )

    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "inventory.json").write_bytes(inv_bytes)
    (v1 / f"inventory.json.{digest_algorithm}").write_text(
        f"{inv_digest} inventory.json\n"
    )
    content = v1 / "content"
    content.mkdir()
    for name, data in files.items():
        (content / name).write_bytes(data)
        if include_companions:
            (content / f"{name}.ots").write_bytes(companion_bytes[name])
    for path, data in extra_content_files.items():
        fp = content / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)

    if include_extension_dir:
        ext_dir = tmp_path / "extensions" / _EXT_NAME
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text(json.dumps({"extensionName": _EXT_NAME}))
        if include_inventory_v1_ots:
            inv_sha256 = hashlib.sha256(inv_bytes).digest()
            if inventory_ots_override is not None:
                inventory_ots = inventory_ots_override
            elif pending_inventory_ots:
                inventory_ots = build_attestation(
                    inv_sha256,
                    pending_uri="https://a.pool.opentimestamps.org",
                )
            else:
                inventory_ots = build_attestation(inv_sha256, block_height=800_000)
            (ext_dir / "inventory.json.v1.ots").write_bytes(inventory_ots)

    return tmp_path


def _validate(tmp_path: Path) -> ValidationResult:
    fsys = LocalFS(tmp_path)
    r = ValidationResult()
    validate_object(fsys, "", r)
    return r


def test_ots_extension_is_known(tmp_path: Path) -> None:
    """Object with the extension config does NOT emit W013 (unknown ext)."""
    _build_ots_object(tmp_path)
    r = _validate(tmp_path)
    w013 = [w for w in r.warnings if w.code == "W013" and _EXT_NAME in w.message]
    assert not w013, f"W013 fired for known extension: {[w.message for w in w013]}"


def test_sha512_with_ots_extension_rejected(tmp_path: Path) -> None:
    """SHA-512 manifest + ots extension config → E901."""
    _build_ots_object(
        tmp_path,
        digest_algorithm="sha512",
        include_companions=False,
        include_inventory_v1_ots=False,
    )
    r = _validate(tmp_path)
    assert r.has_code("E901"), f"expected E901, got errors={[e.code for e in r.errors]}"


def test_companion_file_missing_for_content(tmp_path: Path) -> None:
    """Content file with no `.ots` companion → E902."""
    _build_ots_object(tmp_path, include_companions=False)
    r = _validate(tmp_path)
    assert r.has_code("E902"), f"expected E902, got errors={[e.code for e in r.errors]}"


def test_stray_ots_ots_file_rejected(tmp_path: Path) -> None:
    """`<file>.ots.ots` companion-of-companion → E903."""
    file_data = b"hello world"
    file_sha256 = hashlib.sha256(file_data).digest()
    inner_ots = build_attestation(file_sha256, block_height=800_000)
    inner_sha256 = hashlib.sha256(inner_ots).digest()
    stray_ots = build_attestation(inner_sha256, block_height=800_000)
    _build_ots_object(
        tmp_path,
        files={"file.txt": file_data},
        extra_content_files={"file.txt.ots.ots": stray_ots},
    )
    r = _validate(tmp_path)
    assert r.has_code("E903"), f"expected E903, got errors={[e.code for e in r.errors]}"


def test_inventory_version_ots_missing(tmp_path: Path) -> None:
    """Extension present but no `inventory.json.v1.ots` → E904."""
    _build_ots_object(tmp_path, include_inventory_v1_ots=False)
    r = _validate(tmp_path)
    assert r.has_code("E904"), f"expected E904, got errors={[e.code for e in r.errors]}"


def test_inventory_ots_digest_mismatch(tmp_path: Path) -> None:
    """Inventory `.ots` committing to wrong digest → E905."""
    bad_ots = build_attestation(
        hashlib.sha256(b"different bytes").digest(),
        block_height=800_000,
    )
    _build_ots_object(tmp_path, inventory_ots_override=bad_ots)

    r = _validate(tmp_path)

    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_corrupt_inventory_ots_reports_e905(tmp_path: Path) -> None:
    """Unparseable inventory `.ots` bytes → E905."""
    _build_ots_object(tmp_path, inventory_ots_override=b"not an ots file")

    r = _validate(tmp_path)

    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_inventory_ots_match_no_error(tmp_path: Path) -> None:
    """Inventory `.ots` committing to correct digest → no E905."""
    _build_ots_object(tmp_path)
    r = _validate(tmp_path)
    assert not r.has_code("E905"), (
        f"unexpected E905, errors={[e.code for e in r.errors]}"
    )


def test_pending_inventory_ots_does_not_warn(tmp_path: Path) -> None:
    """Pending inventory `.ots` is structurally valid and does not emit W902."""
    _build_ots_object(tmp_path, pending_inventory_ots=True)

    r = _validate(tmp_path)

    assert not r.has_code("W902"), (
        f"unexpected W902, warnings={[w.code for w in r.warnings]}"
    )
    pending_errors = [e for e in r.errors if e.code in _OTS_ERROR_CODES]
    assert not pending_errors, (
        "pending inventory proof must not error, "
        f"got {[e.code for e in pending_errors]}"
    )


def test_embedded_digest_mismatch(tmp_path: Path) -> None:
    """`.ots` commits to wrong digest → E905."""
    file_data = b"hello world"
    wrong_digest = hashlib.sha256(b"a different file").digest()
    bad_ots = build_attestation(wrong_digest, block_height=800_000)
    _build_ots_object(
        tmp_path,
        files={"file.txt": file_data},
        companion_overrides={"file.txt": bad_ots},
    )
    r = _validate(tmp_path)
    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_corrupt_companion_file_reports_e905(tmp_path: Path) -> None:
    """Unparseable `.ots` companion bytes → E905."""
    _build_ots_object(
        tmp_path,
        companion_overrides={"file.txt": b"not an ots file"},
    )

    r = _validate(tmp_path)

    assert r.has_code("E905"), f"expected E905, got errors={[e.code for e in r.errors]}"


def test_embedded_digest_match_no_error(tmp_path: Path) -> None:
    """`.ots` committing to correct digest → no E905."""
    _build_ots_object(tmp_path)
    r = _validate(tmp_path)
    assert not r.has_code("E905"), (
        f"unexpected E905, errors={[e.code for e in r.errors]}"
    )


_OTS_ERROR_CODES = frozenset({"E901", "E902", "E903", "E904", "E905"})


def test_pending_attestation_warns_not_error(tmp_path: Path) -> None:
    """Companion with only PendingAttestation → W902 warn, not error."""
    _build_ots_object(tmp_path, pending_companions={"file.txt"})
    r = _validate(tmp_path)
    assert r.has_code("W902"), (
        f"expected W902 warning, got warnings={[w.code for w in r.warnings]}"
    )
    pending_errors = [e for e in r.errors if e.code in _OTS_ERROR_CODES]
    assert not pending_errors, (
        f"pending attestation must not error, got {[e.code for e in pending_errors]}"
    )


@pytest.mark.parametrize(
    "code",
    ["E901", "E902", "E903", "E904", "E905", "W902"],
)
def test_ots_codes_registered(code: str) -> None:
    """Codes used by the extension must be registered in validation-errors.json."""
    from ocfl_lib.validation.codes import get_code

    get_code(code)
