"""Local checks of the NNNN-ots-timestamps extension (spec "Local Checks").

The extension is in use when its directory exists in the object root. The
checks run without network access; Bitcoin checks are separate.
"""

import hashlib
from typing import TYPE_CHECKING, NamedTuple

from ocfl_lib import ots
from ocfl_lib._errors import InvalidProofError, OtsConfigError
from ocfl_lib._json_value import as_object, object_field, parse_json
from ocfl_lib._path import join_path
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from ocfl_lib._digest import DigestAlgorithm
    from ocfl_lib._json_value import JsonObject
    from ocfl_lib.fs._protocols import FS
    from ocfl_lib.validation._report import FindingCollector

_SHA256 = "sha256"


class _VersionInventory(NamedTuple):
    raw: bytes
    digest_algorithm: str | None


def validate_ots_extension(
    fsys: FS,
    obj_path: str,
    inv_data: JsonObject,
    alg: DigestAlgorithm,
    inv_versions: dict[str, int],
    result: FindingCollector,
) -> None:
    """Run the extension's local checks on the object at `obj_path`."""
    ext_dir = join_path(obj_path, ots.EXTENSION_DIR)
    if not fsys.exists(ext_dir):
        return
    try:
        cfg = ots.read_config(fsys, obj_path)
        covered = ots.covered_versions(cfg, inv_versions) if cfg else []
    except OtsConfigError as e:
        result.error("E908", reason=str(e))
        return
    if cfg is None:
        return
    if cfg.unknown:
        result.warning("W904", properties=", ".join(cfg.unknown))
    ordered = sorted(inv_versions, key=inv_versions.__getitem__)

    version_inventories = _read_version_inventories(fsys, obj_path, ordered)
    if (
        cfg.proof_digest_algorithm != _SHA256
        or alg.ocfl_name != _SHA256
        or any(
            vi.digest_algorithm not in (None, _SHA256)
            for vi in version_inventories.values()
        )
    ):
        result.error("E901")
        return

    for v in covered:
        if v not in version_inventories:
            result.error("E909", version=v)

    if cfg.timestamp_content:
        _check_content_proofs(fsys, obj_path, inv_data, covered, result)
    _check_inventory_proofs(
        fsys, obj_path, cfg, ordered, covered, version_inventories, result
    )


def _read_version_inventories(
    fsys: FS, obj_path: str, versions: list[str]
) -> dict[str, _VersionInventory]:
    found: dict[str, _VersionInventory] = {}
    for v in versions:
        try:
            raw = fs_utils.read_all(fsys, join_path(obj_path, v, "inventory.json"))
        except FileNotFoundError:
            continue
        try:
            data = as_object(parse_json(raw))
        except ValueError:
            data = None
        dalg = data.get("digestAlgorithm") if data is not None else None
        found[v] = _VersionInventory(raw, dalg if isinstance(dalg, str) else None)
    return found


def _check_content_proofs(
    fsys: FS,
    obj_path: str,
    inv_data: JsonObject,
    covered: list[str],
    result: FindingCollector,
) -> None:
    """Content-proof rules 1-3 for every covered version (E902, E905, E907)."""
    versions = object_field(inv_data, "versions")
    content_path = _content_paths(object_field(inv_data, "manifest"))
    parsed_by_cpath: dict[str, ots.ParsedOts | None] = {}

    for v in covered:
        state = object_field(as_object(versions.get(v)) or {}, "state")
        digest_of = {
            p: d
            for d, plist in state.items()
            if isinstance(plist, list)
            for p in plist
            if isinstance(p, str)
        }
        ordinary, proofs = ots.split_state(digest_of)
        for p in sorted(ordinary):
            if f"{p}{ots.PROOF_SUFFIX}" not in proofs:
                result.error("E902", version=v, path=p)
        for proof, companion in sorted(proofs.items()):
            cpath = content_path.get(digest_of[proof])
            if cpath is None:
                continue  # E050 is reported by the inventory checks
            if cpath not in parsed_by_cpath:
                parsed_by_cpath[cpath] = _check_proof(
                    fsys, obj_path, cpath, digest_of[companion], result
                )
            parsed = parsed_by_cpath[cpath]
            if parsed is not None and not parsed.is_final:
                result.error("E907", version=v, path=companion)


def _check_inventory_proofs(
    fsys: FS,
    obj_path: str,
    cfg: ots.OtsConfig,
    ordered: list[str],
    covered: list[str],
    version_inventories: dict[str, _VersionInventory],
    result: FindingCollector,
) -> None:
    """Inventory-proof rules (E904, E905, W902) and moved timestampFrom (W905)."""
    for v in ordered:
        proof = ots.inventory_proof_path(v)
        present = fsys.exists(join_path(obj_path, proof))
        if v not in covered:
            if present:
                result.warning("W905", version=v)
            continue
        if not present:
            if cfg.timestamp_inventory:
                result.error("E904", version=v)
            continue
        if v not in version_inventories:
            continue  # E909
        raw = version_inventories[v].raw
        parsed = _check_proof(
            fsys, obj_path, proof, hashlib.sha256(raw).hexdigest(), result
        )
        if parsed is not None and not parsed.is_final:
            result.warning("W902", version=v)


def _check_proof(
    fsys: FS,
    obj_path: str,
    proof: str,
    expected_hex: str,
    result: FindingCollector,
) -> ots.ParsedOts | None:
    """Parse the proof at object-relative `proof` and check what it commits to.

    Reports E905 and returns None for a proof that does not parse, uses the
    wrong file-hash operation, or commits to another digest.
    """
    try:
        raw = fs_utils.read_all(fsys, join_path(obj_path, proof))
    except FileNotFoundError:
        return None  # a missing content file is E092
    try:
        return ots.verify_proof(raw, expected_hex)
    except InvalidProofError as e:
        result.error("E905", proof=proof, reason=str(e))
        return None


def _content_paths(manifest: JsonObject) -> dict[str, str]:
    """{digest: first content path} from the manifest."""
    out: dict[str, str] = {}
    for digest, plist in manifest.items():
        if isinstance(plist, list) and plist and isinstance(plist[0], str):
            out[digest] = plist[0]
    return out


def check_chain(
    fsys: FS, obj_path: str, chain: ots.ChainCheck, result: FindingCollector
) -> None:
    """Bitcoin checks of every proof of the covered versions (E906, W903).

    A proof that the local checks reject (E905, E908) or a pending one gets
    no finding here.
    """
    try:
        results = ots.check_object_chain(fsys, obj_path, chain)
    except OtsConfigError, OSError, ValueError:
        return
    for r in results:
        if r.status is ots.AttestationStatus.MISMATCH:
            heights = ", ".join(
                str(c.height)
                for c in r.checks
                if c.status is ots.AttestationStatus.MISMATCH
            )
            result.error("E906", proof=r.proof.path, reason=f"block {heights}")
        elif r.status is ots.AttestationStatus.SHALLOW:
            result.warning("W903", proof=r.proof.path)
