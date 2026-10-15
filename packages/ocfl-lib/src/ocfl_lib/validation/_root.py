import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ocfl_lib._json_value import parse_json
from ocfl_lib._path import join_path
from ocfl_lib._spec import Spec
from ocfl_lib.fs import _utils as fs_utils
from ocfl_lib.validation._object import (
    REGISTERED_EXTENSIONS,
    REGISTERED_LAYOUTS,
    check_extensions_dir,
    declaration_reads_wrong,
    split_declarations,
    validate_object,
)
from ocfl_lib.validation._report import FindingCollector
from ocfl_lib.validation._unsupported import RegularEntries

if TYPE_CHECKING:
    from ocfl_lib import ots
    from ocfl_lib.fs._protocols import FS, FileWalker, FsEntry
    from ocfl_lib.validation._report import ValidationReport

_OBJ_DECL_RE = re.compile(r"^0=ocfl_object_(\d+\.\d+)$")


@dataclass
class _RootSummary:
    objects_checked: int = 0
    objects_valid: int = 0
    objects_invalid: int = 0
    truncated: bool = False


def validate_root(
    fsys: FileWalker,
    path: str,
    *,
    validate_objects: bool = True,
    check_digests: bool = True,
    max_errors: int = 100,
    chain: ots.ChainCheck | None = None,
) -> ValidationReport:
    """Validate an OCFL storage root and (optionally) every object in it.

    Keeps every object's findings, valid or not. Stops after `max_errors`
    invalid objects and sets `truncated`. With `chain`, each object's OTS
    proofs are also checked against Bitcoin, as in `validate_object`.
    """
    result = FindingCollector(path=path)
    summary = _RootSummary()
    regular = RegularEntries(fsys)
    obj_paths = _validate_root(
        regular,
        path,
        result,
        summary,
        validate_objects=validate_objects,
        check_digests=check_digests,
        max_errors=max_errors,
        chain=chain,
    )
    regular.report(result, skip_dirs=obj_paths)
    return result.report(
        objects_checked=summary.objects_checked,
        objects_valid=summary.objects_valid,
        truncated=summary.truncated,
    )


def _validate_root(
    fsys: RegularEntries,
    path: str,
    result: FindingCollector,
    summary: _RootSummary,
    *,
    validate_objects: bool,
    check_digests: bool,
    max_errors: int,
    chain: ots.ChainCheck | None,
) -> list[str]:
    """Paths of the objects found, which report their own unsupported entries."""
    try:
        entries = list(fsys.read_dir(path))
    except OSError:
        result.error("E069a", assumed_version="1.1")
        return []

    root_spec = _check_root_declaration(fsys, path, entries, result)
    _check_layout_file(fsys, path, result)
    check_extensions_dir(
        fsys,
        path,
        result,
        known=REGISTERED_EXTENSIONS,
        not_dir_code="E112",
        unknown_code="W016",
    )
    if not validate_objects:
        return []
    return _walk_and_validate(
        fsys,
        path,
        result,
        summary,
        check_digests=check_digests,
        max_errors=max_errors,
        root_spec=root_spec,
        chain=chain,
    )


def _check_root_declaration(
    fsys: FS, path: str, entries: list[FsEntry], result: FindingCollector
) -> Spec | None:
    unparsed, decls = split_declarations(entries, "root")
    for name in unparsed:
        result.error("E069d", filename=name)
    if not decls:
        result.error("E069c" if unparsed else "E069a", assumed_version="1.1")
        return None
    if len(decls) > 1:
        result.error(
            "E069b",
            files=", ".join(d.filename for d in decls),
            using_version=str(decls[0].spec),
        )
        return None
    decl = decls[0]
    if declaration_reads_wrong(fsys, path, decl):
        result.error("E069e", filename=decl.filename)
    return decl.spec


def _check_layout_file(fsys: FS, path: str, result: FindingCollector) -> None:
    try:
        layout_bytes = fs_utils.read_all(fsys, join_path(path, "ocfl_layout.json"))
    except OSError:
        return
    root = path or "."
    try:
        layout_data = parse_json(layout_bytes)
    except (json.JSONDecodeError, ValueError) as e:
        result.error("E070", path=root, explanation=f"not valid JSON: {e}")
        return
    if not isinstance(layout_data, dict):
        result.error("E070", path=root, explanation="not a JSON object")
        return
    missing = [k for k in ("extension", "description") if k not in layout_data]
    if missing:
        result.error(
            "E070", path=root, explanation=f"missing key(s): {', '.join(missing)}"
        )
        return
    extension = layout_data["extension"]
    if extension not in REGISTERED_LAYOUTS:
        result.error("E071", path=root, extension=json.dumps(extension))


def _walk_and_validate(
    fsys: RegularEntries,
    root_path: str,
    result: FindingCollector,
    summary: _RootSummary,
    *,
    check_digests: bool,
    max_errors: int,
    root_spec: Spec | None,
    chain: ots.ChainCheck | None,
) -> list[str]:
    obj_paths = _walk_hierarchy(fsys, root_path, result, root_spec)

    for obj_path in obj_paths:
        summary.objects_checked += 1
        obj_report = validate_object(
            fsys.inner, obj_path, check_digests=check_digests, chain=chain
        )
        result.extend(obj_report.findings)
        if obj_report.is_valid:
            summary.objects_valid += 1
        else:
            summary.objects_invalid += 1
            if summary.objects_invalid >= max_errors:
                summary.truncated = True
                break
    return obj_paths


def _walk_hierarchy(
    fsys: FileWalker,
    root_path: str,
    result: FindingCollector,
    root_spec: Spec | None,
) -> list[str]:
    obj_paths: list[str] = []
    root_skip_dirs = {"extensions"}
    queue = [root_path]

    while queue:
        dir_path = queue.pop()
        try:
            entries = list(fsys.read_dir(dir_path))
        except OSError:
            continue

        is_root = dir_path == root_path

        if not is_root:
            zero_eqs = [e for e in entries if e.name.startswith("0=")]
            if len(zero_eqs) > 1:
                result.error("E003d", path=dir_path)
                continue
            if len(zero_eqs) == 1:
                m = _OBJ_DECL_RE.match(zero_eqs[0].name)
                if m:
                    version = m.group(1)
                    if version in Spec:
                        obj_paths.append(dir_path)
                        if root_spec is not None and Spec(version).is_newer_than(
                            root_spec
                        ):
                            result.error(
                                "E081",
                                path=dir_path,
                                object_spec_version=version,
                                root_spec_version=str(root_spec),
                            )
                    else:
                        result.error("E004a", path=dir_path, version=version)
                else:
                    result.error(
                        "E004b",
                        path=dir_path,
                        declaration=zero_eqs[0].name,
                    )
                continue

        sub_dirs = [e for e in entries if e.is_dir]
        files = [e for e in entries if e.is_file]

        if is_root:
            sub_dirs = [e for e in sub_dirs if e.name not in root_skip_dirs]
        else:
            if not entries:
                result.error("E073", path=dir_path)
                continue

            if files:
                result.error("E072", path=dir_path)

        for d in sub_dirs:
            queue.append(join_path(dir_path, d.name))

    return obj_paths
