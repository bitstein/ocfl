"""Validation for the NNNN-ots-timestamps OCFL extension."""

from typing import TYPE_CHECKING, Any

from ocfl_lib import ots
from ocfl_lib.errors import OtsParseError
from ocfl_lib.fs import utils as fs_utils

if TYPE_CHECKING:
    from ocfl_lib.digest import DigestAlgorithm
    from ocfl_lib.fs.protocols import FS
    from ocfl_lib.validation.result import ValidationResult


EXTENSION_NAME = "NNNN-ots-timestamps"


def validate_ots_extension(
    fsys: FS,
    obj_path: str,
    inv_data: dict[str, Any],
    alg: DigestAlgorithm,
    inv_versions: dict[str, int],
    result: ValidationResult,
) -> None:
    """Validate the NNNN-ots-timestamps extension.

    Activated by presence of ``extensions/NNNN-ots-timestamps/config.json``.
    """
    ext_root = _join(obj_path, "extensions", EXTENSION_NAME)
    if not fsys.exists(_join(ext_root, "config.json")):
        return

    if alg.ocfl_name == "sha512":
        result.error("E901")
        return

    versions = inv_data.get("versions") or {}
    manifest = inv_data.get("manifest") or {}

    for vkey in sorted(inv_versions):
        _validate_version_state(vkey, versions.get(vkey), result)
        vots_rel = f"inventory.json.{vkey}.ots"
        if not fsys.exists(_join(ext_root, vots_rel)):
            result.error("E904", where=vkey, path=vots_rel)

    if isinstance(manifest, dict):
        _check_embedded_digests(fsys, obj_path, manifest, result)


def _validate_version_state(
    vkey: str,
    vblock: Any,
    result: ValidationResult,
) -> None:
    if not isinstance(vblock, dict):
        return
    state = vblock.get("state")
    if not isinstance(state, dict):
        return

    path_set: set[str] = {
        p
        for plist in state.values()
        if isinstance(plist, list)
        for p in plist
        if isinstance(p, str)
    }

    for p in sorted(path_set):
        if p.endswith(".ots.ots"):
            result.error("E903", where=vkey, path=p)
            continue
        if p.endswith(".ots"):
            continue
        if f"{p}.ots" not in path_set:
            result.error("E902", where=vkey, path=p)


def _check_embedded_digests(
    fsys: FS,
    obj_path: str,
    manifest: dict[str, Any],
    result: ValidationResult,
) -> None:
    path_to_digest: dict[str, str] = {
        p: digest
        for digest, plist in manifest.items()
        if isinstance(plist, list)
        for p in plist
        if isinstance(p, str)
    }

    for ots_path in sorted(path_to_digest):
        if not ots_path.endswith(".ots") or ots_path.endswith(".ots.ots"):
            continue
        companion = ots_path.removesuffix(".ots")
        companion_digest = path_to_digest.get(companion)
        if companion_digest is None:
            continue

        try:
            ots_bytes = fs_utils.read_all(fsys, _join(obj_path, ots_path))
        except OSError:
            continue

        try:
            parsed = ots.parse(ots_bytes)
        except OtsParseError as exc:
            result.error("E905", path=ots_path, reason=str(exc))
            continue

        try:
            expected = bytes.fromhex(companion_digest)
        except ValueError:
            continue

        if parsed.commitment_digest != expected:
            result.error(
                "E905",
                path=ots_path,
                reason=(
                    f"commitment_digest {parsed.commitment_digest.hex()} "
                    f"does not match manifest digest {companion_digest}"
                ),
            )
            continue

        if parsed.block_height is None:
            result.warning("W902", path=ots_path)


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)
