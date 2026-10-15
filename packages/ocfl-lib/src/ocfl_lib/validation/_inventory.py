import re
from typing import TYPE_CHECKING, NamedTuple

import msgspec

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._inventory import Inventory
from ocfl_lib._json_value import object_field_or_empty
from ocfl_lib._spec import VERSION_DIR_RE, Spec
from ocfl_lib.validation._limits import (
    MAX_MESSAGE,
    MAX_OBJECT_ID,
    MAX_PATH,
    MAX_USER_NAME,
    note_limits,
    note_overlong,
)

if TYPE_CHECKING:
    from ocfl_lib._json_value import JsonObject, JsonValue
    from ocfl_lib.validation._report import FindingCollector

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

_REQUIRED_KEYS = (
    ("id", "E036a"),
    ("type", "E036b"),
    ("digestAlgorithm", "E036c"),
    ("head", "E036d"),
    ("manifest", "E041a"),
    ("versions", "E041b"),
)


class _VersionKey(NamedTuple):
    number: int
    key: str


_ALLOWED_ALGORITHMS = frozenset({"sha512", "sha256"})

_TYPE_RE = re.compile(r"^https://ocfl\.io/(\d+\.\d+)/spec/#inventory$")

_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*:")


def validate_inventory(
    data: JsonObject,
    result: FindingCollector,
    *,
    lax_digests: bool = False,
    where: str = "root",
) -> Inventory | None:
    if not _structure_is_decodable(data, result, where=where, lax_digests=lax_digests):
        return None

    try:
        inv = msgspec.convert(data, Inventory)
    except Exception:
        result.error("E033", where=where, explanation="failed to decode inventory")
        return None

    return inv


def _structure_is_decodable(
    data: JsonObject,
    result: FindingCollector,
    *,
    where: str,
    lax_digests: bool,
) -> bool:
    missing = [code for key, code in _REQUIRED_KEYS if key not in data]
    for code in missing:
        result.error(code, where=where)
    if missing:
        if data.get("versions") == {}:
            result.error("E008", where=where)
        return False

    for key in data:
        if key not in _KNOWN_KEYS:
            result.error("E102", where=where, key=key)

    _check_id(data["id"], result, where=where)
    _check_type(data["type"], result, where=where)
    _check_digest_algorithm(data["digestAlgorithm"], result, where=where)
    if "contentDirectory" in data:
        _check_content_directory(data["contentDirectory"], result, where=where)

    manifest = data["manifest"]
    if not isinstance(manifest, dict):
        result.error("E041c", where=where)
        return False
    if not isinstance(data["versions"], dict):
        result.error("E044", where=where)
        return False

    alg_name = data["digestAlgorithm"]
    if isinstance(alg_name, str) and alg_name in _ALLOWED_ALGORITHMS:
        _validate_manifest(
            manifest,
            result,
            where=where,
            alg_name=alg_name,
            content_dir=data.get("contentDirectory", "content"),
            lax_digests=lax_digests,
        )
    _validate_versions(data, result, where=where)
    if "fixity" in data:
        _validate_fixity(
            data["fixity"],
            manifest,
            result,
            where=where,
            lax_digests=lax_digests,
        )
    return True


def _check_id(id_val: JsonValue, result: FindingCollector, *, where: str) -> None:
    if not isinstance(id_val, str) or not id_val:
        result.error("E037a", where=where)
        return
    note_limits(result, id_val, where=where, field="id", limit=MAX_OBJECT_ID)
    if not _URI_SCHEME_RE.match(id_val):
        result.warning("W005", where=where, id=id_val)


def _check_type(type_val: JsonValue, result: FindingCollector, *, where: str) -> None:
    if not isinstance(type_val, str):
        result.error("E038d", where=where)
        return
    m = _TYPE_RE.match(type_val)
    if not m:
        result.error("E038b", where=where, got=type_val, assumed_spec_version="1.1")
    elif m.group(1) not in Spec:
        result.error("E038c", where=where, got=type_val, assumed_spec_version="1.1")


def _check_digest_algorithm(
    alg: JsonValue, result: FindingCollector, *, where: str
) -> None:
    if not isinstance(alg, str) or alg not in _ALLOWED_ALGORITHMS:
        result.error("E025a", where=where, digest_algorithm=str(alg))
    elif alg == "sha256":
        result.warning("W004", where=where)


def _check_content_directory(
    cd: JsonValue, result: FindingCollector, *, where: str
) -> None:
    if not isinstance(cd, str) or "/" in cd:
        result.error("E017", where=where)
    elif cd in (".", ".."):
        result.error("E018", where=where)


def _validate_manifest(
    manifest: JsonObject,
    result: FindingCollector,
    *,
    where: str,
    alg_name: str,
    content_dir: JsonValue,
    lax_digests: bool,
) -> None:
    try:
        alg = DigestAlgorithm.from_ocfl_name(alg_name)
    except ValueError:
        return

    digest_re = alg.regex
    original_by_lower: dict[str, str] = {}
    all_content_paths: list[str] = []

    for digest, paths in manifest.items():
        if not lax_digests and not digest_re.match(digest):
            result.error("E025b", where=where, digest=digest, algorithm=alg_name)

        lower = digest.lower()
        if lower in original_by_lower and original_by_lower[lower] != digest:
            result.error("E096", where=where, digest=digest)
        original_by_lower[lower] = digest

        if not isinstance(paths, list):
            result.error("E091", where=where, digest=digest)
            continue

        for path in paths:
            if not isinstance(path, str):
                result.error("E091", where=where, digest=digest)
                continue

            note_limits(
                result, path, where=where, field="manifest path", limit=MAX_PATH
            )
            segments = _path_segments(
                result, path, slash_code="E100", segment_code="E099", where=where
            )
            if segments is None:
                continue

            if len(segments) < 3:
                result.error("E042a", where=where, path=path)
                continue

            if not VERSION_DIR_RE.match(segments[0]):
                result.error("E042b", where=where, path=path)
                continue

            if segments[1] != content_dir:
                result.error(
                    "E042c", where=where, path=path, content_directory=content_dir
                )
                continue

            all_content_paths.append(path)

    path_set: set[str] = set()
    for path in all_content_paths:
        if path in path_set:
            result.error("E101a", where=where, path=path)
        path_set.add(path)

    for path in _path_prefix_conflicts(path_set):
        result.error("E101b", where=where, path=path)


def _validate_versions(
    data: JsonObject,
    result: FindingCollector,
    *,
    where: str,
) -> None:
    versions = object_field_or_empty(data, "versions")
    manifest = object_field_or_empty(data, "manifest")
    head = data["head"]

    vnums: list[_VersionKey] = []
    pad_widths: set[int | None] = set()
    for vkey in versions:
        m = VERSION_DIR_RE.match(vkey)
        if not m:
            result.error("E046b", dir=vkey)
            continue
        digits = vkey[1:]
        num = int(digits)
        pad_widths.add(len(digits) if digits[0] == "0" else None)
        vnums.append(_VersionKey(num, vkey))

    if not vnums:
        result.error("E008", where=where)
        return

    has_padded = any(w is not None for w in pad_widths)
    has_unpadded = None in pad_widths
    if has_padded and has_unpadded:
        result.error("E011", where=where)
        result.error("E012", where=where)
        result.error("E013", where=where)
    elif has_padded and len(pad_widths) > 1:
        result.error("E012", where=where)
    elif has_padded:
        result.warning("W001", where=where)

    vnums.sort()

    if vnums[0].number != 1:
        result.error("E009", where=where)

    for i in range(1, len(vnums)):
        if vnums[i].number != vnums[i - 1].number + 1:
            result.error("E010", where=where)
            break

    highest_key = vnums[-1].key
    if head != highest_key:
        result.error("E040", where=where, got=head, expected=highest_key)

    all_state_digests: set[str] = set()

    for _, vkey in vnums:
        vblock = versions[vkey]
        if not isinstance(vblock, dict):
            continue

        if "created" not in vblock:
            result.error("E048", where=where, version=vkey)
        else:
            created = vblock["created"]
            if not isinstance(created, str):
                result.error("E049d", where=where, version=vkey)
            else:
                _validate_created(created, result, where=where, version=vkey)

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

        if "message" not in vblock:
            result.warning("W007a", where=where, version=vkey)
        elif not isinstance(vblock["message"], str):
            result.error("E094", where=where, version=vkey)
        else:
            note_overlong(
                result,
                vblock["message"],
                where=where,
                field=f"{vkey} message",
                limit=MAX_MESSAGE,
            )

        if "user" not in vblock:
            result.warning("W007b", where=where, version=vkey)
        else:
            _validate_user(vblock["user"], result, where=where, version=vkey)

    for digest in manifest:
        if digest.lower() not in all_state_digests:
            result.error("E107", where=where, digests=digest)


def _validate_created(
    created: str,
    result: FindingCollector,
    *,
    where: str,
    version: str,
) -> None:
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(created)
    except ValueError:
        result.error("E049b", where=where, version=version)
        return

    if dt.tzinfo is None:
        result.error("E049a", where=where, version=version)

    if not _created_has_seconds(created):
        result.error("E049b", where=where, version=version)


def _created_has_seconds(created: str) -> bool:
    if "T" not in created:
        return False

    time_part = created.split("T", 1)[1]
    if time_part.endswith("Z"):
        time_part = time_part[:-1]
    else:
        for sign in ("+", "-"):
            offset_start = time_part.rfind(sign)
            if offset_start > 0:
                time_part = time_part[:offset_start]
                break

    parts = time_part.split(":")
    return len(parts) >= 3 and parts[2] != ""


def _validate_version_state(
    state: JsonObject,
    manifest: JsonObject,
    result: FindingCollector,
    *,
    where: str,
    version: str,
) -> None:
    all_logical_paths: list[str] = []
    manifest_lower = {d.lower(): d for d in manifest}

    for digest, paths in state.items():
        if isinstance(digest, str):
            lower = digest.lower()
            if lower not in manifest_lower:
                result.error("E050f", where=where, version=version, digest=digest)
            elif digest != manifest_lower[lower]:
                result.error("E050d", where=where, version=version, digest=digest)

        if not isinstance(paths, list):
            result.error("E050e", where=where, version=version, digest=str(digest))
            continue

        for path in paths:
            if not isinstance(path, str):
                continue

            note_limits(result, path, where=where, field="logical path", limit=MAX_PATH)
            segments = _path_segments(
                result,
                path,
                slash_code="E053",
                segment_code="E052",
                where=where,
                version=version,
            )
            if segments is None:
                continue

            all_logical_paths.append(path)

    seen: set[str] = set()
    for path in all_logical_paths:
        if path in seen:
            result.error("E095a", where=where, version=version, path=path)
        seen.add(path)

    for path in _path_prefix_conflicts(seen):
        result.error("E095b", where=where, version=version, path=path)


def _path_prefix_conflicts(paths: set[str]) -> list[str]:
    conflicts: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in paths:
                conflicts.add(prefix)
    return sorted(conflicts)


def _path_segments(
    result: FindingCollector,
    path: str,
    *,
    slash_code: str,
    segment_code: str,
    **params: object,
) -> list[str] | None:
    if path.startswith("/") or path.endswith("/"):
        result.error(slash_code, **params, path=path)
        return None
    segments = path.split("/")
    if any(s in ("", ".", "..") for s in segments):
        result.error(segment_code, **params, path=path)
        return None
    return segments


def listed_paths(digest_map: JsonObject) -> set[str]:
    paths: set[str] = set()
    for listed in digest_map.values():
        if isinstance(listed, list):
            paths.update(p for p in listed if isinstance(p, str))
    return paths


def _digest_by_path(digest_map: JsonObject) -> dict[str, str]:
    digest_of: dict[str, str] = {}
    for digest, listed in digest_map.items():
        if isinstance(listed, list):
            for p in listed:
                if isinstance(p, str):
                    digest_of[p] = str(digest)
    return digest_of


def _validate_user(
    user: JsonValue,
    result: FindingCollector,
    *,
    where: str,
    version: str,
) -> None:
    if not isinstance(user, dict):
        result.error("E054a", where=where, version=version)
        return

    name = user.get("name")
    if not isinstance(name, str):
        result.error("E054b", where=where, version=version)
    else:
        note_overlong(
            result,
            name,
            where=where,
            field=f"{version} user name",
            limit=MAX_USER_NAME,
        )

    if "address" not in user:
        result.warning("W008", where=where, version=version)
    elif isinstance(user["address"], str):
        if not _URI_SCHEME_RE.match(user["address"]):
            result.warning("W009", where=where, version=version)
    elif not isinstance(user["address"], str):
        result.error("E054c", where=where, version=version)


def _validate_fixity(
    fixity: JsonValue,
    manifest: JsonObject,
    result: FindingCollector,
    *,
    where: str,
    lax_digests: bool,
) -> None:
    if not isinstance(fixity, dict):
        result.error("E056a", where=where)
        return

    manifest_paths = listed_paths(manifest)

    for alg_name, digest_map in fixity.items():
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
        original_by_lower: dict[str, str] = {}

        for digest, paths in digest_map.items():
            if not digest_re.match(digest):
                result.error("E057b", where=where, algorithm=alg_name, digest=digest)

            lower = digest.lower()
            if lower in original_by_lower and original_by_lower[lower] != digest:
                result.error("E097", where=where, algorithm=alg_name, digest=digest)
            original_by_lower[lower] = digest

            if not isinstance(paths, list):
                result.error("E057c", where=where, algorithm=alg_name, digest=digest)
                continue

            for path in paths:
                if not isinstance(path, str):
                    continue

                if (
                    _path_segments(
                        result,
                        path,
                        slash_code="E100",
                        segment_code="E099",
                        where=where,
                    )
                    is None
                ):
                    continue

                if path not in manifest_paths:
                    result.error(
                        "E057d",
                        where=where,
                        algorithm=alg_name,
                        digest=digest,
                        path=path,
                    )


def validate_inventory_against_prior(
    current: JsonObject,
    prior: JsonObject,
    result: FindingCollector,
    *,
    where: str = "root",
) -> None:
    cur_id = current.get("id", "")
    pri_id = prior.get("id", "")
    if cur_id != pri_id:
        result.error("E037b", where=where, root_id=str(cur_id), version_id=str(pri_id))

    cur_versions = current.get("versions", {})
    pri_versions = prior.get("versions", {})
    if not isinstance(cur_versions, dict) or not isinstance(pri_versions, dict):
        return

    prior_head = str(prior.get("head", ""))
    if any(vkey not in cur_versions for vkey in pri_versions):
        result.error("E066a", prior_head=prior_head, where=where)
        return

    _check_prior_manifest_covers(current, prior, pri_versions, result, where=where)

    same_alg = current.get("digestAlgorithm") == prior.get("digestAlgorithm")
    for vkey, prior_block in pri_versions.items():
        _check_shared_version(
            prior_block,
            cur_versions[vkey],
            result,
            version=vkey,
            prior_head=prior_head,
            where=where,
            compare_digests=same_alg,
        )


def _check_prior_manifest_covers(
    current: JsonObject,
    prior: JsonObject,
    prior_versions: JsonObject,
    result: FindingCollector,
    *,
    where: str,
) -> None:
    cur_manifest = current.get("manifest", {})
    pri_manifest = prior.get("manifest", {})
    if not isinstance(cur_manifest, dict) or not isinstance(pri_manifest, dict):
        return
    pri_content = listed_paths(pri_manifest)
    pri_prefixes = tuple(vkey + "/" for vkey in prior_versions)
    missing = {
        p
        for p in listed_paths(cur_manifest)
        if p.startswith(pri_prefixes) and p not in pri_content
    }
    if missing:
        result.error("E023b", where=where, missing_filepaths=", ".join(sorted(missing)))


def _check_shared_version(
    prior_block: JsonValue,
    current_block: JsonValue,
    result: FindingCollector,
    *,
    version: str,
    prior_head: str,
    where: str,
    compare_digests: bool,
) -> None:
    pri_state = prior_block.get("state", {}) if isinstance(prior_block, dict) else {}
    cur_state = (
        current_block.get("state", {}) if isinstance(current_block, dict) else {}
    )
    if not isinstance(pri_state, dict) or not isinstance(cur_state, dict):
        return

    pri_digest_of = _digest_by_path(pri_state)
    cur_digest_of = _digest_by_path(cur_state)
    only_in_prior = pri_digest_of.keys() - cur_digest_of.keys()
    only_in_current = cur_digest_of.keys() - pri_digest_of.keys()
    if only_in_prior:
        result.error(
            "E066b",
            prior_head=prior_head,
            version=version,
            where=where,
            logical_paths=", ".join(sorted(only_in_prior)),
            only_in=prior_head,
        )
    if only_in_current:
        result.error(
            "E066b",
            prior_head=prior_head,
            version=version,
            where=where,
            logical_paths=", ".join(sorted(only_in_current)),
            only_in=where,
        )

    if not compare_digests:
        return
    for lpath, pd in pri_digest_of.items():
        cd = cur_digest_of.get(lpath)
        if cd is not None and pd.lower() != cd.lower():
            result.error(
                "E066c",
                prior_head=prior_head,
                version=version,
                logical_path=lpath,
                prior_content=pd,
                where=where,
                current_content=cd,
            )
