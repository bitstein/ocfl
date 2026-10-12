"""Validation for the NNNN-ots-timestamps OCFL extension."""

import hashlib
from typing import TYPE_CHECKING

from ocfl_lib import ots
from ocfl_lib._errors import OtsParseError
from ocfl_lib._json_value import object_field
from ocfl_lib._path import join_path
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from ocfl_lib._digest import DigestAlgorithm
    from ocfl_lib._json_value import JsonObject, JsonValue
    from ocfl_lib.fs._protocols import FS
    from ocfl_lib.validation._report import FindingCollector


EXTENSION_NAME = "NNNN-ots-timestamps"


def validate_ots_extension(
    fsys: FS,
    obj_path: str,
    inv_data: JsonObject,
    alg: DigestAlgorithm,
    inv_versions: dict[str, int],
    result: FindingCollector,
) -> None:
    """Validate the NNNN-ots-timestamps extension.

    Activated by presence of ``extensions/NNNN-ots-timestamps/config.json``.
    """
    ext_root = join_path(obj_path, "extensions", EXTENSION_NAME)
    if not fsys.exists(join_path(ext_root, "config.json")):
        return

    if alg.ocfl_name == "sha512":
        result.error("E901")
        return

    versions = object_field(inv_data, "versions")
    manifest = object_field(inv_data, "manifest")

    for vkey in sorted(inv_versions):
        _validate_version_state(vkey, versions.get(vkey), result)
        vots_rel = f"inventory.json.{vkey}.ots"
        if not fsys.exists(join_path(ext_root, vots_rel)):
            result.error("E904", where=vkey, path=vots_rel)
        else:
            _check_inventory_proof(fsys, obj_path, vkey, vots_rel, result)

    if isinstance(manifest, dict):
        _check_embedded_digests(fsys, obj_path, manifest, result)


def _validate_version_state(
    vkey: str,
    vblock: JsonValue,
    result: FindingCollector,
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


def _check_inventory_proof(
    fsys: FS,
    obj_path: str,
    vkey: str,
    vots_rel: str,
    result: FindingCollector,
) -> None:
    ext_root = join_path(obj_path, "extensions", EXTENSION_NAME)
    try:
        ots_bytes = fs_utils.read_all(fsys, join_path(ext_root, vots_rel))
        vinv_path = join_path(obj_path, vkey, "inventory.json")
        vinv_bytes = fs_utils.read_all(fsys, vinv_path)
    except OSError:
        return

    expected = hashlib.sha256(vinv_bytes).digest()

    try:
        parsed = ots.parse(ots_bytes)
    except OtsParseError as exc:
        result.error("E905", path=vots_rel, reason=str(exc))
        return

    if parsed.commitment_digest != expected:
        result.error(
            "E905",
            path=vots_rel,
            reason=(
                f"commitment_digest {parsed.commitment_digest.hex()} "
                f"does not match inventory digest {expected.hex()}"
            ),
        )


def _check_embedded_digests(
    fsys: FS,
    obj_path: str,
    manifest: JsonObject,
    result: FindingCollector,
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
            ots_bytes = fs_utils.read_all(fsys, join_path(obj_path, ots_path))
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
