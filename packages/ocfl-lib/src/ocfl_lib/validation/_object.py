"""OCFL object validation."""

import contextlib
import json
import re
from typing import TYPE_CHECKING

from ocfl_lib import ots
from ocfl_lib._digest import DigestAlgorithm, bytes_digest, stream_digest
from ocfl_lib._json_value import as_str, object_field, parse_json
from ocfl_lib._path import join_path
from ocfl_lib._spec import VERSION_DIR_RE, Namaste
from ocfl_lib.fs import _utils as fs_utils
from ocfl_lib.validation._extension_ots import check_chain, validate_ots_extension
from ocfl_lib.validation._inventory import (
    validate_inventory,
    validate_inventory_against_prior,
)
from ocfl_lib.validation._report import FindingCollector

if TYPE_CHECKING:
    from ocfl_lib._json_value import JsonObject
    from ocfl_lib.fs._protocols import FS, FileWalker, FsEntry
    from ocfl_lib.validation._report import ValidationReport

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
        ots.EXTENSION_NAME,
    }
)


def validate_object(
    fsys: FileWalker,
    path: str,
    *,
    check_digests: bool = True,
    lax_digests: bool = False,
    chain: ots.ChainCheck | None = None,
) -> ValidationReport:
    """Validate an OCFL object at the given path.

    With `chain`, the proofs of the OTS extension are also checked against
    Bitcoin (E906, W903); errors from its source propagate.
    """
    result = FindingCollector(path=path)
    _validate_object(
        fsys, path, result, check_digests=check_digests, lax_digests=lax_digests
    )
    if chain is not None:
        check_chain(fsys, path, chain, result)
    return result.report()


def _validate_object(
    fsys: FileWalker,
    path: str,
    result: FindingCollector,
    *,
    check_digests: bool,
    lax_digests: bool,
) -> None:
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
    raw_zero_files = [e for e in entries if e.name.startswith("0=")]
    decl_files = []
    namaste = None
    for entry in entries:
        n = Namaste.parse(entry.name)
        if n is not None and n.kind == "object":
            decl_files.append(n)

    # Fire E006 for each unparseable 0= file
    parsed_names = {d.filename for d in decl_files}
    for zf in raw_zero_files:
        if zf.name not in parsed_names:
            result.error("E006", filename=zf.name)

    if not decl_files:
        if raw_zero_files:
            result.error("E003c", assumed_version="1.1")
        else:
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
        decl_path = join_path(path, namaste.filename)
        try:
            content = fs_utils.read_text(fsys, decl_path)
            if content != namaste.content:
                result.error("E007", filename=namaste.filename)
        except OSError:
            pass

    # 3. Read and validate root inventory.json
    inv_path = join_path(path, "inventory.json")
    try:
        inv_bytes = fs_utils.read_all(fsys, inv_path)
    except OSError:
        result.error("E063")
        return

    try:
        inv_data = parse_json(inv_bytes)
    except (json.JSONDecodeError, ValueError) as e:
        result.error("E033", where="root", explanation=str(e))
        return

    if not isinstance(inv_data, dict):
        result.error("E034", where="root")
        return
    if isinstance(object_id := inv_data.get("id"), str):
        result.object_id = object_id

    inv = validate_inventory(inv_data, result, lax_digests=lax_digests, where="root")
    if inv is None:
        return

    # 4. Validate root inventory sidecar
    try:
        alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    except ValueError:
        return

    sidecar_name = f"inventory.json.{alg.ocfl_name}"
    sidecar_path = join_path(path, sidecar_name)
    _validate_sidecar(fsys, sidecar_path, inv_bytes, alg, result, where="root")

    # 5. Determine content directory and version list
    content_dir = (
        inv.content_directory if inv.content_directory is not None else "content"
    )

    # Parse version numbers from inventory
    inv_versions: dict[str, int] = {}
    for vkey in inv.versions:
        m = VERSION_DIR_RE.match(vkey)
        if m:
            inv_versions[vkey] = int(vkey[1:])

    _check_first_version_content_dir(fsys, path, inv_data, inv_versions, result)

    # 6. Validate object root structure
    _check_version_dir_sequence(entries, result)

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
        if entry.is_dir and VERSION_DIR_RE.match(entry.name):
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
    ext_path = join_path(path, "extensions")
    try:
        ext_entries = list(fsys.read_dir(ext_path))
        for entry in ext_entries:
            if not entry.is_dir:
                result.error("E067", entry=entry.name)
            elif entry.name not in _KNOWN_EXTENSIONS:
                result.warning("W013", entry=entry.name)
    except OSError:
        pass

    validate_ots_extension(fsys, path, inv_data, alg, inv_versions, result)

    # 8. Validate each version directory. E103: each version's spec must be
    # the same as or later than the preceding version's; root is the last.
    prev_spec: tuple[str, str] | None = None  # (version dir, spec version)
    for vkey, _vnum in sorted(inv_versions.items(), key=lambda x: x[1]):
        vdir = join_path(path, vkey)
        vspec = _validate_version_dir(
            fsys,
            vdir,
            vkey,
            content_dir,
            inv_data,
            inv_bytes,
            alg,
            result,
            check_digests=check_digests,
            lax_digests=lax_digests,
        )
        prev_spec = _check_spec_sequence(prev_spec, vkey, vspec, result)
    _check_spec_sequence(prev_spec, "root", _inventory_spec(inv_data), result)

    # 9. Validate content files against manifest
    manifest = object_field(inv_data, "manifest")
    manifest_paths: set[str] = set()
    for paths in manifest.values():
        if isinstance(paths, list):
            manifest_paths.update(p for p in paths if isinstance(p, str))

    actual_files: set[str] = set()
    prefix_len = len(path) + 1 if path else 0
    for vkey in inv_versions:
        content_path = join_path(path, vkey, content_dir)
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
    root_inv_data: JsonObject,
    root_inv_bytes: bytes,
    root_alg: DigestAlgorithm,
    result: FindingCollector,
    *,
    check_digests: bool,
    lax_digests: bool,
) -> str | None:
    """Validate a single version directory."""
    try:
        entries = list(fsys.read_dir(vdir))
    except OSError:
        result.error("E046a", version_dir=vkey)
        return None

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

    # W003: empty content directory
    content_path = join_path(vdir, content_dir)
    try:
        content_entries = list(fsys.read_dir(content_path))
        if not content_entries:
            result.warning("W003", where=vkey)
    except OSError:
        pass

    # Check version inventory
    vinv_path = join_path(vdir, "inventory.json")
    try:
        vinv_bytes = fs_utils.read_all(fsys, vinv_path)
    except OSError:
        result.warning("W010", where=vkey)
        return None

    # Validate version inventory sidecar (use detected alg or root alg)
    sidecar_alg = version_sidecar_alg or root_alg
    vsidecar_path = join_path(vdir, f"inventory.json.{sidecar_alg.ocfl_name}")
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
        vinv_data = parse_json(vinv_bytes)
    except (json.JSONDecodeError, ValueError) as e:
        result.error("E033", where=vkey, explanation=str(e))
        return None

    if not isinstance(vinv_data, dict):
        result.error("E034", where=vkey)
        return None

    vinv = validate_inventory(vinv_data, result, lax_digests=lax_digests, where=vkey)
    if vinv is None:
        return None

    # E019/E020: contentDirectory consistency between version and root
    root_cd = root_inv_data.get("contentDirectory")
    ver_cd = vinv_data.get("contentDirectory")
    if ver_cd is not None and "contentDirectory" not in root_inv_data:
        result.error("E019", where=vkey)
    elif ver_cd is not None and root_cd is not None and ver_cd != root_cd:
        result.error(
            "E020",
            where=vkey,
            version_content_directory=ver_cd,
            root_content_directory=root_cd,
        )

    # E040: version inventory head must match version directory
    vinv_head = vinv_data.get("head", "")
    if vinv_head != vkey:
        result.error("E040", where=vkey, got=vinv_head, expected=vkey)

    # E092: non-head version inventories must describe their content correctly.
    # Under check_digests, verify every non-head version. Always verify when the
    # version alg differs from root, even in lax mode, so a mismatched non-head
    # alg can never go unchecked.
    vinv_alg_name = vinv_data.get("digestAlgorithm", "")
    alg_differs = vinv_alg_name != root_inv_data.get("digestAlgorithm", "")
    if vkey != head and (check_digests or alg_differs):
        try:
            vinv_alg = DigestAlgorithm.from_ocfl_name(as_str(vinv_alg_name) or "")
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

    return _inventory_spec(vinv_data)


def _check_version_metadata(
    vinv_data: JsonObject,
    root_inv_data: JsonObject,
    result: FindingCollector,
    *,
    vkey: str,
) -> None:
    """W011: version metadata in prior inventory should match root."""
    vinv_versions = object_field(vinv_data, "versions")
    root_versions = object_field(root_inv_data, "versions")
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


def _check_version_dir_sequence(
    entries: list[FsEntry], result: FindingCollector
) -> None:
    """E010: version directories present on disk must be sequential."""
    vnums: list[int] = []
    for entry in entries:
        if not entry.is_dir:
            continue
        if VERSION_DIR_RE.match(entry.name):
            vnums.append(int(entry.name[1:]))

    if not vnums:
        return

    vnums.sort()
    for i in range(1, len(vnums)):
        if vnums[i] != vnums[i - 1] + 1:
            result.error("E010", where="root")
            return


def _check_first_version_content_dir(
    fsys: FS,
    path: str,
    root_inv_data: JsonObject,
    inv_versions: dict[str, int],
    result: FindingCollector,
) -> None:
    """E019: contentDirectory must be set consistently from the first version."""
    if "contentDirectory" not in root_inv_data or not inv_versions:
        return

    first_vkey = min(inv_versions.items(), key=lambda item: item[1])[0]
    vinv_path = join_path(path, first_vkey, "inventory.json")
    try:
        vinv_bytes = fs_utils.read_all(fsys, vinv_path)
        vinv_data = parse_json(vinv_bytes)
    except OSError, json.JSONDecodeError, ValueError:
        return

    if not isinstance(vinv_data, dict):
        return

    first_cd = vinv_data.get("contentDirectory")
    root_cd = root_inv_data.get("contentDirectory")
    if first_cd != root_cd:
        result.error("E019", where="root")


_SPEC_RE = re.compile(r"https://ocfl\.io/(\d+\.\d+)/spec/#inventory")


def _inventory_spec(inv_data: JsonObject) -> str | None:
    """Spec version ("1.1") from an inventory's type URI, or None."""
    itype = inv_data.get("type", "")
    m = _SPEC_RE.match(itype) if isinstance(itype, str) else None
    return m.group(1) if m else None


def _check_spec_sequence(
    prev: tuple[str, str] | None,
    where: str,
    spec: str | None,
    result: FindingCollector,
) -> tuple[str, str] | None:
    """E103: *spec* must not be older than the preceding version's *prev*.

    Returns the new (where, spec) to compare the next version against.
    """
    if spec is None:
        return prev
    if prev is not None and _spec_tuple(spec) < _spec_tuple(prev[1]):
        result.error(
            "E103",
            where=where,
            this_spec_version=spec,
            prev_version_dir=prev[0],
            prev_spec_version=prev[1],
        )
    return (where, spec)


def _spec_tuple(version: str) -> tuple[int, int]:
    major, _, minor = version.partition(".")
    return (int(major), int(minor) if minor else 0)


def _validate_sidecar(
    fsys: FS,
    sidecar_path: str,
    inv_bytes: bytes,
    alg: DigestAlgorithm,
    result: FindingCollector,
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
    manifest: JsonObject,
    alg: DigestAlgorithm,
    result: FindingCollector,
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
            full_path = join_path(obj_path, content_path)
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
    fixity: JsonObject,
    result: FindingCollector,
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
                full_path = join_path(obj_path, content_path)
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
                except OSError:
                    result.error(
                        "E093b",
                        where=where,
                        digest_algorithm=alg.ocfl_name,
                        digest=digest,
                        content_path=content_path,
                    )
                except ValueError:
                    pass
