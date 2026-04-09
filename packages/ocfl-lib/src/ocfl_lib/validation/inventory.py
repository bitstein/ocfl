"""OCFL inventory validation — two-phase: raw dict then struct semantics."""

import re
from typing import TYPE_CHECKING, Any

import msgspec

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.inventory import Inventory
from ocfl_lib.validation.limits import (
    MAX_MESSAGE,
    MAX_OBJECT_ID,
    MAX_PATH,
    MAX_USER_NAME,
    contains_null,
)

if TYPE_CHECKING:
    from ocfl_lib.validation.result import ValidationResult

_KNOWN_KEYS = frozenset(
    {
        "id",
        "type",
        "digestAlgorithm",
        "head",
        "manifest",
        "versions",
        "contentDirectory",
        "fixity",
    }
)

_ALLOWED_ALGORITHMS = frozenset({"sha512", "sha256"})

_TYPE_RE = re.compile(r"^https://ocfl\.io/(\d+\.\d+)/spec/#inventory$")
_SUPPORTED_VERSIONS = frozenset({"1.0", "1.1"})

# Simple URI check: has a scheme followed by :
_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*:")

_VNUM_RE = re.compile(r"^v\d+$")


def validate_inventory(
    data: dict[str, Any],
    result: ValidationResult,
    *,
    lax_digests: bool = False,
    where: str = "root",
) -> Inventory | None:
    """Validate an inventory from a raw dict.

    Phase 1: structural/type checks on the raw dict.
    Phase 2: decode to Inventory struct, semantic checks.

    Returns parsed Inventory on success, None on fatal structural error.
    """
    # Phase 1: structural checks
    if not _validate_structure(data, result, where=where, lax_digests=lax_digests):
        return None

    # Phase 2: decode to struct
    try:
        inv = msgspec.convert(data, Inventory)
    except Exception:
        result.error("E033", where=where, explanation="failed to decode inventory")
        return None

    return inv


def _validate_structure(
    data: dict[str, Any],
    result: ValidationResult,
    *,
    where: str,
    lax_digests: bool,
) -> bool:
    """Phase 1: raw dict structural validation. Returns False on fatal error."""
    ok = True

    # Required keys
    for key, code in [
        ("id", "E036a"),
        ("type", "E036b"),
        ("digestAlgorithm", "E036c"),
        ("head", "E036d"),
        ("manifest", "E041a"),
        ("versions", "E041b"),
    ]:
        if key not in data:
            result.error(code, where=where)
            ok = False

    if not ok:
        return False

    # Unknown keys
    for key in data:
        if key not in _KNOWN_KEYS:
            result.error("E102", where=where, key=key)

    # id validation
    id_val = data["id"]
    if not isinstance(id_val, str) or not id_val:
        result.error("E037a", where=where)
    else:
        if contains_null(id_val):
            result.error("L001", where=where, field="id")
        if len(id_val) > MAX_OBJECT_ID:
            result.error(
                "L002",
                where=where,
                field="id",
                limit=MAX_OBJECT_ID,
                actual=len(id_val),
            )
        if not _URI_RE.match(id_val):
            result.warning("W005", where=where, id=id_val)

    # type validation
    type_val = data["type"]
    if not isinstance(type_val, str):
        result.error("E038d", where=where)
    else:
        m = _TYPE_RE.match(type_val)
        if not m:
            result.error("E038b", where=where, got=type_val, assumed_spec_version="1.1")
        else:
            ver = m.group(1)
            if ver not in _SUPPORTED_VERSIONS:
                result.error(
                    "E038c", where=where, got=type_val, assumed_spec_version="1.1"
                )

    # digestAlgorithm validation
    alg = data["digestAlgorithm"]
    if not isinstance(alg, str) or alg not in _ALLOWED_ALGORITHMS:
        result.error("E025a", where=where, digest_algorithm=str(alg))
    elif alg == "sha256":
        result.warning("W004", where=where)

    # contentDirectory validation
    if "contentDirectory" in data:
        cd = data["contentDirectory"]
        if not isinstance(cd, str) or "/" in cd:
            result.error("E017", where=where)
        elif cd in (".", ".."):
            result.error("E018", where=where)

    # manifest must be object
    if not isinstance(data["manifest"], dict):
        result.error("E041c", where=where)
        return False

    # versions must be object
    if not isinstance(data["versions"], dict):
        result.error("E044", where=where)
        return False

    # manifest validation
    alg_name = data["digestAlgorithm"]
    content_dir = data.get("contentDirectory", "content")
    if isinstance(alg_name, str) and alg_name in _ALLOWED_ALGORITHMS:
        _validate_manifest(
            data["manifest"],
            result,
            where=where,
            alg_name=alg_name,
            content_dir=content_dir,
            lax_digests=lax_digests,
        )

    # versions validation
    _validate_versions(data, result, where=where)

    # fixity validation
    if "fixity" in data:
        _validate_fixity(
            data["fixity"],
            data["manifest"],
            result,
            where=where,
            lax_digests=lax_digests,
        )

    return True


def _validate_manifest(
    manifest: dict[str, Any],
    result: ValidationResult,
    *,
    where: str,
    alg_name: str,
    content_dir: str,
    lax_digests: bool,
) -> None:
    """Validate manifest block: digest format, paths, duplicates."""
    try:
        alg = DigestAlgorithm.from_ocfl_name(alg_name)
    except ValueError:
        return

    digest_re = alg.regex
    seen_digests: dict[str, str] = {}  # lowercase → original
    all_content_paths: list[str] = []

    for digest, paths in manifest.items():
        # Digest format check
        if not lax_digests and not digest_re.match(digest):
            result.error("E025b", where=where, digest=digest, algorithm=alg_name)

        # Case-insensitive duplicate check
        lower = digest.lower()
        if lower in seen_digests and seen_digests[lower] != digest:
            result.error("E096", where=where, digest=digest)
        seen_digests[lower] = digest

        # Values must be arrays of strings
        if not isinstance(paths, list):
            result.error("E091", where=where, digest=digest)
            continue

        for path in paths:
            if not isinstance(path, str):
                result.error("E091", where=where, digest=digest)
                continue

            # Null byte + length sanity checks (non-spec)
            if contains_null(path):
                result.error("L001", where=where, field="manifest path")
                continue
            if len(path) > MAX_PATH:
                result.error(
                    "L002",
                    where=where,
                    field="manifest path",
                    limit=MAX_PATH,
                    actual=len(path),
                )
                continue

            # Path segment validation
            if path.startswith("/") or path.endswith("/"):
                result.error("E100", where=where, path=path)
                continue

            segments = path.split("/")
            if any(s in ("", ".", "..") for s in segments):
                result.error("E099", where=where, path=path)
                continue

            # Content path must be v{N}/{contentDir}/...
            if len(segments) < 3:
                result.error("E042a", where=where, path=path)
                continue

            if not _VNUM_RE.match(segments[0]):
                result.error("E042b", where=where, path=path)
                continue

            if segments[1] != content_dir:
                result.error(
                    "E042c", where=where, path=path, content_directory=content_dir
                )
                continue

            all_content_paths.append(path)

    # Duplicate content path check
    path_set: set[str] = set()
    for path in all_content_paths:
        if path in path_set:
            result.error("E101a", where=where, path=path)
        path_set.add(path)

    # Path prefix conflict check (path used as both file and directory)
    sorted_paths = sorted(path_set)
    for i in range(len(sorted_paths) - 1):
        if sorted_paths[i + 1].startswith(sorted_paths[i] + "/"):
            result.error("E101b", where=where, path=sorted_paths[i])


def _validate_versions(
    data: dict[str, Any],
    result: ValidationResult,
    *,
    where: str,
) -> None:
    """Validate versions block: keys, sequencing, head, version blocks."""
    versions = data["versions"]
    manifest = data["manifest"]
    head = data["head"]

    # Parse and validate version keys
    vnums: list[tuple[int, str]] = []  # (number, key)
    pad_lengths: set[int] = set()  # 0 = unpadded, >0 = padded width
    for vkey in versions:
        m = _VNUM_RE.match(vkey)
        if not m:
            result.error("E046b", dir=vkey)
            continue
        digits = vkey[1:]
        num = int(digits)
        pad_lengths.add(len(digits) if digits[0] == "0" else 0)
        vnums.append((num, vkey))

    if not vnums:
        result.error("E008", where=where)
        return

    # E011/E012/E013: padding consistency
    has_padded = any(p > 0 for p in pad_lengths)
    has_unpadded = 0 in pad_lengths
    if has_padded and has_unpadded:
        # Mixed padded and unpadded — E011 + E012 + E013
        result.error("E011", where=where)
        result.error("E012", where=where)
        result.error("E013", where=where)
    elif has_padded and len(pad_lengths) > 1:
        # All padded but inconsistent widths — E012
        result.error("E012", where=where)
    elif has_padded:
        result.warning("W001", where=where)

    vnums.sort()

    # Must start at 1
    if vnums[0][0] != 1:
        result.error("E009", where=where)

    # Must be contiguous
    for i in range(1, len(vnums)):
        if vnums[i][0] != vnums[i - 1][0] + 1:
            result.error("E010", where=where)
            break

    # head must equal highest version
    highest_key = vnums[-1][1]
    if head != highest_key:
        result.error("E040", where=where, got=head, expected=highest_key)

    # Collect all state digests to check manifest usage later
    all_state_digests: set[str] = set()

    # Validate each version block
    for _, vkey in vnums:
        vblock = versions[vkey]
        if not isinstance(vblock, dict):
            continue

        # created
        if "created" not in vblock:
            result.error("E048", where=where, version=vkey)
        else:
            created = vblock["created"]
            if not isinstance(created, str):
                result.error("E049d", where=where, version=vkey)
            else:
                _validate_created(created, result, where=where, version=vkey)

        # state
        if "state" not in vblock:
            result.error("E048c", where=where, version=vkey)
        else:
            state = vblock["state"]
            if not isinstance(state, dict):
                result.error("E050c", where=where, version=vkey)
            else:
                _validate_version_state(
                    state,
                    manifest,
                    result,
                    where=where,
                    version=vkey,
                )
                for digest in state:
                    if isinstance(digest, str):
                        all_state_digests.add(digest.lower())

        # message
        if "message" not in vblock:
            result.warning("W007a", where=where, version=vkey)
        elif not isinstance(vblock["message"], str):
            result.error("E094", where=where, version=vkey)
        elif len(vblock["message"]) > MAX_MESSAGE:
            result.error(
                "L002",
                where=where,
                field=f"{vkey} message",
                limit=MAX_MESSAGE,
                actual=len(vblock["message"]),
            )

        # user
        if "user" not in vblock:
            result.warning("W007b", where=where, version=vkey)
        else:
            _validate_user(vblock["user"], result, where=where, version=vkey)

    # E107: all manifest digests must be referenced by at least one version state
    for digest in manifest:
        if digest.lower() not in all_state_digests:
            result.error("E107", where=where, digests=digest)


def _validate_created(
    created: str,
    result: ValidationResult,
    *,
    where: str,
    version: str,
) -> None:
    """Validate created datetime string per OCFL: ISO 8601, with tz, ≥seconds."""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(created)
    except ValueError:
        result.error("E049b", where=where, version=version)
        return

    if dt.tzinfo is None:
        result.error("E049a", where=where, version=version)

    # Granularity to seconds: must contain T and HH:MM:SS
    if "T" not in created or len(created) < 19:
        result.error("E049b", where=where, version=version)


def _validate_version_state(
    state: dict[str, Any],
    manifest: dict[str, Any],
    result: ValidationResult,
    *,
    where: str,
    version: str,
) -> None:
    """Validate a single version's state block."""
    all_logical_paths: list[str] = []
    manifest_lower = {d.lower(): d for d in manifest}

    for digest, paths in state.items():
        # Check digest is in manifest (case-insensitive), then exact case
        if isinstance(digest, str):
            lower = digest.lower()
            if lower not in manifest_lower:
                result.error("E050f", where=where, version=version, digest=digest)
            elif digest != manifest_lower[lower]:
                result.error("E050d", where=where, version=version, digest=digest)

        # Values must be list
        if not isinstance(paths, list):
            result.error("E050e", where=where, version=version, digest=str(digest))
            continue

        for path in paths:
            if not isinstance(path, str):
                continue

            # Null byte + length sanity checks (non-spec)
            if contains_null(path):
                result.error("L001", where=where, field="logical path")
                continue
            if len(path) > MAX_PATH:
                result.error(
                    "L002",
                    where=where,
                    field="logical path",
                    limit=MAX_PATH,
                    actual=len(path),
                )
                continue

            # Check for leading/trailing slash
            if path.startswith("/") or path.endswith("/"):
                result.error("E052", where=where, version=version, path=path)
                continue

            # Check for ., .., empty segments
            segments = path.split("/")
            if any(s in ("", ".", "..") for s in segments):
                result.error("E053", where=where, version=version, path=path)
                continue

            all_logical_paths.append(path)

    # Duplicate logical paths
    seen: set[str] = set()
    for path in all_logical_paths:
        if path in seen:
            result.error("E095a", where=where, version=version, path=path)
        seen.add(path)

    # Path prefix conflicts
    sorted_paths = sorted(seen)
    for i in range(len(sorted_paths) - 1):
        if sorted_paths[i + 1].startswith(sorted_paths[i] + "/"):
            result.error("E095b", where=where, version=version, path=sorted_paths[i])


def _validate_user(
    user: Any,
    result: ValidationResult,
    *,
    where: str,
    version: str,
) -> None:
    """Validate user block in a version."""
    if not isinstance(user, dict):
        result.error("E054a", where=where, version=version)
        return

    if "name" not in user or not isinstance(user.get("name"), str):
        result.error("E054b", where=where, version=version)
    elif len(user["name"]) > MAX_USER_NAME:
        result.error(
            "L002",
            where=where,
            field=f"{version} user name",
            limit=MAX_USER_NAME,
            actual=len(user["name"]),
        )

    if "address" not in user:
        result.warning("W008", where=where, version=version)
    elif isinstance(user["address"], str):
        if not _URI_RE.match(user["address"]):
            result.warning("W009", where=where, version=version)
    elif not isinstance(user["address"], str):
        result.error("E054c", where=where, version=version)


def _validate_fixity(
    fixity: Any,
    manifest: dict[str, Any],
    result: ValidationResult,
    *,
    where: str,
    lax_digests: bool,
) -> None:
    """Validate fixity block."""
    if not isinstance(fixity, dict):
        result.error("E056a", where=where)
        return

    # Collect all manifest content paths for cross-reference
    manifest_paths: set[str] = set()
    for paths in manifest.values():
        if isinstance(paths, list):
            manifest_paths.update(p for p in paths if isinstance(p, str))

    for alg_name, digest_map in fixity.items():
        # Check algorithm is known
        try:
            alg = DigestAlgorithm.from_ocfl_name(alg_name)
        except ValueError:
            if not lax_digests:
                result.error("E056b", where=where, algorithm=alg_name)
            continue

        if not isinstance(digest_map, dict):
            result.error("E057a", where=where, algorithm=alg_name)
            continue

        digest_re = alg.regex
        seen_digests: dict[str, str] = {}

        for digest, paths in digest_map.items():
            # Digest format check
            if not digest_re.match(digest):
                result.error("E057b", where=where, algorithm=alg_name, digest=digest)

            # Case-insensitive duplicate check
            lower = digest.lower()
            if lower in seen_digests and seen_digests[lower] != digest:
                result.error("E097", where=where, algorithm=alg_name, digest=digest)
            seen_digests[lower] = digest

            # Values must be list
            if not isinstance(paths, list):
                result.error("E057c", where=where, algorithm=alg_name, digest=digest)
                continue

            # Each path must be in manifest
            for path in paths:
                if isinstance(path, str) and path not in manifest_paths:
                    result.error(
                        "E057d",
                        where=where,
                        algorithm=alg_name,
                        digest=digest,
                        path=path,
                    )


def validate_inventory_against_prior(
    current: dict[str, Any],
    prior: dict[str, Any],
    result: ValidationResult,
    *,
    where: str = "root",
) -> None:
    """Validate current inventory against a prior version's inventory."""
    # ID must not change
    cur_id = current.get("id", "")
    pri_id = prior.get("id", "")
    if cur_id != pri_id:
        result.error("E110", where=where, current_id=str(cur_id), prior_id=str(pri_id))

    # Prior versions must be subset of current
    cur_versions = current.get("versions", {})
    pri_versions = prior.get("versions", {})

    if not isinstance(cur_versions, dict) or not isinstance(pri_versions, dict):
        return

    prior_head = prior.get("head", "")

    for vkey in pri_versions:
        if vkey not in cur_versions:
            result.error("E066a", prior_head=str(prior_head), where=where)
            return

    # E023b: prior manifest must include all content paths under its version scope
    cur_manifest = current.get("manifest", {})
    pri_manifest = prior.get("manifest", {})
    if isinstance(cur_manifest, dict) and isinstance(pri_manifest, dict):
        cur_content: set[str] = set()
        for paths in cur_manifest.values():
            if isinstance(paths, list):
                cur_content.update(p for p in paths if isinstance(p, str))
        pri_content: set[str] = set()
        for paths in pri_manifest.values():
            if isinstance(paths, list):
                pri_content.update(p for p in paths if isinstance(p, str))
        pri_prefixes = [vkey + "/" for vkey in pri_versions]
        missing = {
            p
            for p in cur_content
            if any(p.startswith(pfx) for pfx in pri_prefixes) and p not in pri_content
        }
        if missing:
            result.error(
                "E023b", where=where, missing_filepaths=", ".join(sorted(missing))
            )

    # For shared versions: state must match
    for vkey in pri_versions:
        if vkey not in cur_versions:
            continue

        pri_state = (
            pri_versions[vkey].get("state", {})
            if isinstance(pri_versions[vkey], dict)
            else {}
        )
        cur_state = (
            cur_versions[vkey].get("state", {})
            if isinstance(cur_versions[vkey], dict)
            else {}
        )

        if not isinstance(pri_state, dict) or not isinstance(cur_state, dict):
            continue

        # Compare logical paths per digest
        pri_paths: set[str] = set()
        cur_paths: set[str] = set()
        for paths in pri_state.values():
            if isinstance(paths, list):
                pri_paths.update(p for p in paths if isinstance(p, str))
        for paths in cur_state.values():
            if isinstance(paths, list):
                cur_paths.update(p for p in paths if isinstance(p, str))

        only_in_prior = pri_paths - cur_paths
        only_in_current = cur_paths - pri_paths

        if only_in_prior:
            result.error(
                "E066b",
                prior_head=str(prior_head),
                version=vkey,
                where=where,
                logical_paths=", ".join(sorted(only_in_prior)),
                only_in=str(prior_head),
            )
        if only_in_current:
            result.error(
                "E066b",
                prior_head=str(prior_head),
                version=vkey,
                where=where,
                logical_paths=", ".join(sorted(only_in_current)),
                only_in=where,
            )

        # E066c: same logical path must map to same digest (skip if alg differs)
        same_alg = current.get("digestAlgorithm") == prior.get("digestAlgorithm")
        if not same_alg:
            continue
        pri_path_dig: dict[str, str] = {}
        for digest, paths in pri_state.items():
            if isinstance(paths, list):
                for p in paths:
                    if isinstance(p, str):
                        pri_path_dig[p] = str(digest)
        cur_path_dig: dict[str, str] = {}
        for digest, paths in cur_state.items():
            if isinstance(paths, list):
                for p in paths:
                    if isinstance(p, str):
                        cur_path_dig[p] = str(digest)

        for lpath in pri_path_dig:
            if lpath in cur_path_dig:
                pd = pri_path_dig[lpath]
                cd = cur_path_dig[lpath]
                if pd.lower() != cd.lower():
                    result.error(
                        "E066c",
                        prior_head=str(prior_head),
                        version=vkey,
                        logical_path=lpath,
                        prior_content=pd,
                        where=where,
                        current_content=cd,
                    )
