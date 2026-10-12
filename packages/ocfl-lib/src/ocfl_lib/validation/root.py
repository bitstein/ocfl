"""OCFL storage root validation."""

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ocfl_lib._path import join_path
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.spec import Namaste, Spec
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.report import FindingCollector

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FileWalker
    from ocfl_lib.validation.report import ValidationReport

# Known OCFL community extensions (for storage root)
_KNOWN_ROOT_EXTENSIONS = frozenset(
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
) -> ValidationReport:
    """Validate an OCFL storage root and (optionally) every object in it.

    Keeps every object's findings, valid or not. Stops after `max_errors`
    invalid objects and sets `truncated`.
    """
    result = FindingCollector(path=path)
    summary = _RootSummary()
    _validate_root(
        fsys,
        path,
        result,
        summary,
        validate_objects=validate_objects,
        check_digests=check_digests,
        max_errors=max_errors,
    )
    return result.report(
        objects_checked=summary.objects_checked,
        objects_valid=summary.objects_valid,
        truncated=summary.truncated,
    )


def _validate_root(
    fsys: FileWalker,
    path: str,
    result: FindingCollector,
    summary: _RootSummary,
    *,
    validate_objects: bool,
    check_digests: bool,
    max_errors: int,
) -> None:
    root_spec: Spec | None = None

    # 1. Root NAMASTE declaration
    try:
        entries = list(fsys.read_dir(path))
    except OSError:
        result.error("E069a", assumed_version="1.1")
        return

    raw_zero_files = [e for e in entries if e.name.startswith("0=")]
    root_decls = []
    for entry in entries:
        n = Namaste.parse(entry.name)
        if n is not None and n.kind == "root":
            root_decls.append(n)

    # Fire E069d for each unparseable 0= file
    parsed_names = {d.filename for d in root_decls}
    for zf in raw_zero_files:
        if zf.name not in parsed_names:
            result.error("E069d", filename=zf.name)

    if not root_decls:
        if raw_zero_files:
            result.error("E069c", assumed_version="1.1")
        else:
            result.error("E069a", assumed_version="1.1")
    elif len(root_decls) > 1:
        result.error(
            "E069b",
            files=", ".join(d.filename for d in root_decls),
            using_version=str(root_decls[0].spec),
        )
    else:
        # Validate contents
        decl = root_decls[0]
        root_spec = decl.spec
        decl_path = join_path(path, decl.filename)
        try:
            content = fs_utils.read_text(fsys, decl_path)
            if content != decl.content:
                result.error("E069e", filename=decl.filename)
        except OSError:
            pass

    # 2. ocfl_layout.json
    layout_path = join_path(path, "ocfl_layout.json")
    try:
        layout_bytes = fs_utils.read_all(fsys, layout_path)
        try:
            layout_data = json.loads(layout_bytes)
        except (json.JSONDecodeError, ValueError) as e:
            result.error(
                "E070",
                path=path or ".",
                explanation=str(e),
            )
            layout_data = None

        if layout_data is not None and not isinstance(layout_data, dict):
            result.error("E071", path=path or ".")
    except OSError:
        pass  # ocfl_layout.json is optional

    # 3. extensions/ directory
    ext_path = join_path(path, "extensions")
    try:
        ext_entries = list(fsys.read_dir(ext_path))
        for entry in ext_entries:
            if not entry.is_dir:
                result.error("E112", entry=entry.name)
            elif entry.name not in _KNOWN_ROOT_EXTENSIONS:
                result.warning("W016", entry=entry.name)
    except OSError:
        pass

    # 4. Walk hierarchy for objects and validate
    if validate_objects:
        _walk_and_validate(
            fsys,
            path,
            result,
            summary,
            check_digests=check_digests,
            max_errors=max_errors,
            root_spec=root_spec,
        )


def _walk_and_validate(
    fsys: FileWalker,
    root_path: str,
    result: FindingCollector,
    summary: _RootSummary,
    *,
    check_digests: bool,
    max_errors: int,
    root_spec: Spec | None,
) -> None:
    """Walk storage root hierarchy, find objects, validate them."""
    obj_paths = _walk_hierarchy(fsys, root_path, result, root_spec)

    for obj_path in obj_paths:
        summary.objects_checked += 1
        obj_report = validate_object(fsys, obj_path, check_digests=check_digests)
        result.extend(obj_report.findings)
        if obj_report.is_valid:
            summary.objects_valid += 1
        else:
            summary.objects_invalid += 1
            if summary.objects_invalid >= max_errors:
                summary.truncated = True
                break


def _walk_hierarchy(
    fsys: FileWalker,
    root_path: str,
    result: FindingCollector,
    root_spec: Spec | None,
) -> list[str]:
    """Walk root hierarchy via read_dir. Emit E073 (empty dirs) and E072
    (non-object dirs with files). Return discovered object root paths.

    Matches ocfl-py's storage_root.py traversal approach.
    """
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
            # Check for object declarations (files starting with "0=")
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
            # E073: empty directories must not appear under a storage root
            if not entries:
                result.error("E073", path=dir_path)
                continue

            # E072: hierarchy must not contain files not part of an object
            if files:
                result.error("E072", path=dir_path)

        for d in sub_dirs:
            queue.append(join_path(dir_path, d.name))

    return obj_paths
