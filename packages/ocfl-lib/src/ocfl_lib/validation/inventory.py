"""OCFL inventory validation — two-phase: raw dict then struct semantics."""

import re
from typing import TYPE_CHECKING, Any

import msgspec

from ocfl_lib.inventory import Inventory

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
    elif not _URI_RE.match(id_val):
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

    return True
