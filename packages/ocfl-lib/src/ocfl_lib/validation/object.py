"""OCFL object validation."""

import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

from ocfl_lib.digest import DigestAlgorithm, bytes_digest, stream_digest
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.spec import Namaste
from ocfl_lib.validation.inventory import (
    validate_inventory,
    validate_inventory_against_prior,
)

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS, FileWalker
    from ocfl_lib.validation.result import ValidationResult

_VNUM_RE = re.compile(r"^v\d+$")
_SIDECAR_RE = re.compile(r"^inventory\.json\.(\S+)$")

# Known OCFL community extensions
_KNOWN_EXTENSIONS = frozenset(
    {
        "0001-digest-algorithms",
        "0002-flat-direct-storage-layout",
        "0003-hash-and-id-n-tuple-storage-layout",
        "0004-hashed-n-tuple-storage-layout",
        "0005-mutable-head",
        "0006-flat-omit-prefix-storage-layout",
        "0007-n-tuple-omit-prefix-storage-layout",
    }
)


def validate_object(
    fsys: FileWalker,
    path: str,
    result: ValidationResult,
    *,
    check_digests: bool = True,
    lax_digests: bool = False,
) -> None:
    """Validate an OCFL object at the given path."""
    # 1. Read root entries
    try:
        entries = list(fsys.read_dir(path))
    except OSError:
        result.error("E003e", path=path or ".")
        return

    if not entries:
        result.error("E003e", path=path or ".")
        return

    # 2. Find and validate NAMASTE declaration
    decl_files = []
    namaste = None
    for entry in entries:
        n = Namaste.parse(entry.name)
        if n is not None and n.kind == "object":
            decl_files.append(n)

    if not decl_files:
        result.error("E003a", assumed_version="1.1")
    elif len(decl_files) > 1:
        result.error(
            "E003b",
            files=", ".join(d.filename for d in decl_files),
            using_version=str(decl_files[0].spec),
        )
        namaste = decl_files[0]
    else:
        namaste = decl_files[0]

    # Validate declaration contents
    if namaste is not None:
        decl_path = _join(path, namaste.filename)
        try:
            content = fs_utils.read_text(fsys, decl_path)
            if content != namaste.content:
                result.error("E007", filename=namaste.filename)
        except OSError:
            pass

    # 3. Read and validate root inventory.json
    inv_path = _join(path, "inventory.json")
    try:
        inv_bytes = fs_utils.read_all(fsys, inv_path)
    except OSError:
        result.error("E063")
        return

    try:
        inv_data = json.loads(inv_bytes)
    except (json.JSONDecodeError, ValueError) as e:
        result.error("E033", where="root", explanation=str(e))
        return

    if not isinstance(inv_data, dict):
        result.error("E034", where="root")
        return

    inv = validate_inventory(inv_data, result, lax_digests=lax_digests, where="root")
    if inv is None:
        return

    # 4. Validate root inventory sidecar
    alg_name = inv_data.get("digestAlgorithm", "sha512")
    try:
        alg = DigestAlgorithm.from_ocfl_name(alg_name)
    except ValueError:
        return

    sidecar_name = f"inventory.json.{alg.ocfl_name}"
    sidecar_path = _join(path, sidecar_name)
    _validate_sidecar(fsys, sidecar_path, inv_bytes, alg, result, where="root")

    # 5. Determine content directory and version list
    content_dir = inv_data.get("contentDirectory", "content")

    # Parse version numbers from inventory
    inv_versions: dict[str, int] = {}
    for vkey in inv_data.get("versions", {}):
        m = _VNUM_RE.match(vkey)
        if m:
            inv_versions[vkey] = int(vkey[1:])

    # 6. Validate object root structure
    # extensions/ and logs/ are allowed per spec
    allowed_root = {"inventory.json", sidecar_name, "extensions", "logs"}
    if namaste:
        allowed_root.add(namaste.filename)
    for vkey in inv_versions:
        allowed_root.add(vkey)

    for entry in entries:
        if entry.name in allowed_root:
            continue
        if Namaste.parse(entry.name) is not None:
            continue
        if entry.is_dir and _VNUM_RE.match(entry.name):
            if entry.name not in inv_versions:
                result.error("E046b", dir=entry.name)
            continue
        if entry.is_file:
            result.error("E001a", file=entry.name)
        elif entry.is_dir:
            result.error("E001b", dir=entry.name)
        else:
            result.error("E001c", entry=entry.name)

    # 7. Check extensions directory
    ext_path = _join(path, "extensions")
    try:
        ext_entries = list(fsys.read_dir(ext_path))
        for entry in ext_entries:
            if not entry.is_dir:
                result.error("E067", entry=entry.name)
            elif entry.name not in _KNOWN_EXTENSIONS:
                result.warning("W013", entry=entry.name)
    except OSError:
        pass

    # 8. Validate each version directory
    for vkey, _vnum in sorted(inv_versions.items(), key=lambda x: x[1]):
        vdir = _join(path, vkey)
        _validate_version_dir(
            fsys,
            vdir,
            vkey,
            content_dir,
            inv_data,
            inv_bytes,
            alg,
            result,
            lax_digests=lax_digests,
        )

    # 9. Validate content files against manifest
    manifest = inv_data.get("manifest", {})
    manifest_paths: set[str] = set()
    for paths in manifest.values():
        if isinstance(paths, list):
            manifest_paths.update(p for p in paths if isinstance(p, str))

    actual_files: set[str] = set()
    prefix_len = len(path) + 1 if path else 0
    for vkey in inv_versions:
        content_path = _join(path, vkey, content_dir)
        try:
            for ref in fsys.walk_files(content_path):
                # Strip object path prefix to get manifest-relative path
                rel = ref.path[prefix_len:] if prefix_len else ref.path
                actual_files.add(rel)
        except OSError:
            pass

    extra = actual_files - manifest_paths
    if extra:
        result.error("E023a", where="root", extra_files=", ".join(sorted(extra)))

    missing = manifest_paths - actual_files
    if missing:
        result.error("E092b", where="root", content_path=", ".join(sorted(missing)))

    # 10. If check_digests: verify content file digests
    if check_digests:
        _verify_digests(fsys, path, manifest, alg, result, where="root")
        fixity = inv_data.get("fixity")
        if fixity and isinstance(fixity, dict):
            _verify_fixity_digests(fsys, path, fixity, result, where="root")


def _validate_version_dir(
    fsys: FS,
    vdir: str,
    vkey: str,
    content_dir: str,
    root_inv_data: dict[str, Any],
    root_inv_bytes: bytes,
    root_alg: DigestAlgorithm,
    result: ValidationResult,
    *,
    lax_digests: bool,
) -> None:
    """Validate a single version directory."""
    try:
        entries = list(fsys.read_dir(vdir))
    except OSError:
        result.error("E046a", version_dir=vkey)
        return

    # Detect version inventory sidecar algorithm from actual files
    version_sidecar_alg = None
    for entry in entries:
        m = _SIDECAR_RE.match(entry.name)
        if m:
            with contextlib.suppress(ValueError):
                version_sidecar_alg = DigestAlgorithm.from_ocfl_name(m.group(1))

    # Allowed entries: inventory.json, its sidecar, content dir
    allowed = {"inventory.json", content_dir}
    if version_sidecar_alg:
        allowed.add(f"inventory.json.{version_sidecar_alg.ocfl_name}")
    else:
        # Accept any sidecar
        for entry in entries:
            if _SIDECAR_RE.match(entry.name):
                allowed.add(entry.name)

    for entry in entries:
        if entry.name in allowed:
            continue
        if entry.is_dir:
            result.warning("W002", where=vkey, entry=entry.name)
        elif entry.is_file:
            result.error("E015", where=vkey, entry=entry.name)

    # Check version inventory
    vinv_path = _join(vdir, "inventory.json")
    try:
        vinv_bytes = fs_utils.read_all(fsys, vinv_path)
    except OSError:
        result.warning("W010", where=vkey)
        return

    # Validate version inventory sidecar (use detected alg or root alg)
    sidecar_alg = version_sidecar_alg or root_alg
    vsidecar_path = _join(vdir, f"inventory.json.{sidecar_alg.ocfl_name}")
    _validate_sidecar(fsys, vsidecar_path, vinv_bytes, sidecar_alg, result, where=vkey)

    # For head version: must be byte-identical to root
    head = root_inv_data.get("head", "")
    if vkey == head and vinv_bytes != root_inv_bytes:
        result.error(
            "E064",
            root_inv_file="inventory.json",
            inv_file=f"{vkey}/inventory.json",
        )

    # Parse version inventory
    try:
        vinv_data = json.loads(vinv_bytes)
    except json.JSONDecodeError, ValueError:
        return

    if not isinstance(vinv_data, dict):
        return

    vinv = validate_inventory(vinv_data, result, lax_digests=lax_digests, where=vkey)
    if vinv is None:
        return

    # E040: version inventory head must match version directory
    vinv_head = vinv_data.get("head", "")
    if vinv_head != vkey:
        result.error("E040", where=vkey, got=vinv_head, expected=vkey)

    # E103: version inventory spec must not be older than a later version's spec
    _check_spec_regression(vinv_data, root_inv_data, result, vkey=vkey)

    # E092: when version alg differs from root, verify version manifest digests
    vinv_alg_name = vinv_data.get("digestAlgorithm", "")
    if vinv_alg_name != root_inv_data.get("digestAlgorithm", ""):
        try:
            vinv_alg = DigestAlgorithm.from_ocfl_name(vinv_alg_name)
        except ValueError:
            vinv_alg = None
        if vinv_alg is not None:
            obj_path = vdir.rsplit("/", 1)[0] if "/" in vdir else ""
            vinv_manifest = vinv_data.get("manifest", {})
            if isinstance(vinv_manifest, dict):
                _verify_digests(
                    fsys, obj_path, vinv_manifest, vinv_alg, result, where=vkey
                )

    # Cross-validate: version inventory is prior, root inventory is current
    if vkey != head:
        validate_inventory_against_prior(root_inv_data, vinv_data, result, where=vkey)

    # W011: check version metadata consistency
    _check_version_metadata(vinv_data, root_inv_data, result, vkey=vkey)


def _check_version_metadata(
    vinv_data: dict[str, Any],
    root_inv_data: dict[str, Any],
    result: ValidationResult,
    *,
    vkey: str,
) -> None:
    """W011: version metadata in prior inventory should match root."""
    vinv_versions = vinv_data.get("versions", {})
    root_versions = root_inv_data.get("versions", {})
    prior_head = vinv_data.get("head", "")

    for ver_key in vinv_versions:
        if ver_key not in root_versions:
            continue
        v_block = vinv_versions[ver_key]
        r_block = root_versions[ver_key]
        if not isinstance(v_block, dict) or not isinstance(r_block, dict):
            continue

        for meta_key in ("created", "message", "user"):
            v_val = v_block.get(meta_key)
            r_val = r_block.get(meta_key)
            if v_val != r_val:
                result.warning(
                    "W011",
                    key=meta_key,
                    version=ver_key,
                    prior_head=str(prior_head),
                    where="root",
                )


_SPEC_RE = re.compile(r"https://ocfl\.io/(\d+\.\d+)/spec/#inventory")


def _check_spec_regression(
    vinv_data: dict[str, Any],
    root_inv_data: dict[str, Any],
    result: ValidationResult,
    *,
    vkey: str,
) -> None:
    """E103: version inventory spec must not be older than root spec."""
    vtype = vinv_data.get("type", "")
    rtype = root_inv_data.get("type", "")
    vm = _SPEC_RE.match(vtype) if isinstance(vtype, str) else None
    rm = _SPEC_RE.match(rtype) if isinstance(rtype, str) else None
    if vm and rm and vm.group(1) < rm.group(1):
        result.error(
            "E103",
            where=vkey,
            this_spec_version=vm.group(1),
            prev_version_dir="root",
            prev_spec_version=rm.group(1),
        )


def _validate_sidecar(
    fsys: FS,
    sidecar_path: str,
    inv_bytes: bytes,
    alg: DigestAlgorithm,
    result: ValidationResult,
    *,
    where: str,
) -> None:
    """Validate inventory sidecar digest file."""
    try:
        sidecar_text = fs_utils.read_text(fsys, sidecar_path)
    except OSError:
        result.error("E058a", where=where, path=sidecar_path)
        return

    parts = sidecar_text.strip().split(maxsplit=1)
    if len(parts) < 2:
        result.error("E061", description="sidecar must contain digest and filename")
        return

    recorded_digest = parts[0].strip()
    if not recorded_digest:
        result.error("E061", description="no digest in sidecar")
        return

    actual_digest = bytes_digest(inv_bytes, alg)

    if recorded_digest.lower() != actual_digest.lower():
        result.error(
            "E060",
            inv_file=f"{where}/inventory.json",
            actual=actual_digest,
            recorded=recorded_digest,
            inv_digest_file=sidecar_path,
        )


def _verify_digests(
    fsys: FS,
    obj_path: str,
    manifest: dict[str, Any],
    alg: DigestAlgorithm,
    result: ValidationResult,
    *,
    where: str,
) -> None:
    """Verify content file digests match manifest."""
    for digest, paths in manifest.items():
        if not isinstance(paths, list):
            continue
        for content_path in paths:
            if not isinstance(content_path, str):
                continue
            full_path = _join(obj_path, content_path)
            try:
                with fsys.open_file(full_path) as f:
                    actual = stream_digest(f, alg)
                if actual.lower() != digest.lower():
                    result.error(
                        "E092a",
                        where=where,
                        digest_algorithm=alg.ocfl_name,
                        digest=digest,
                        content_path=content_path,
                        content_digest=actual,
                    )
            except OSError, ValueError:
                pass


def _verify_fixity_digests(
    fsys: FS,
    obj_path: str,
    fixity: dict[str, Any],
    result: ValidationResult,
    *,
    where: str,
) -> None:
    """Verify fixity block digests."""
    for alg_name, digest_map in fixity.items():
        if not isinstance(digest_map, dict):
            continue
        try:
            alg = DigestAlgorithm.from_ocfl_name(alg_name)
        except ValueError:
            continue
        for digest, paths in digest_map.items():
            if not isinstance(paths, list):
                continue
            for content_path in paths:
                if not isinstance(content_path, str):
                    continue
                full_path = _join(obj_path, content_path)
                try:
                    with fsys.open_file(full_path) as f:
                        actual = stream_digest(f, alg)
                    if actual.lower() != digest.lower():
                        result.error(
                            "E093a",
                            where=where,
                            digest_algorithm=alg.ocfl_name,
                            digest=digest,
                            content_path=content_path,
                            content_digest=actual,
                        )
                except OSError, ValueError:
                    pass


def _join(*parts: str) -> str:
    """Join path parts with /, filtering empty parts."""
    return "/".join(p for p in parts if p)
