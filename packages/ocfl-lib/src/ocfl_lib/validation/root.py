"""OCFL storage root validation."""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.spec import Namaste
from ocfl_lib.validation.object import validate_object

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FileWalker
    from ocfl_lib.validation.result import ValidationResult

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


@dataclass
class RootValidationSummary:
    """Summary of storage root validation."""

    num_objects: int = 0
    valid_objects: int = 0
    object_errors: list[tuple[str, Any]] = field(default_factory=list)
    truncated: bool = False


def validate_root(
    fsys: FileWalker,
    path: str,
    result: ValidationResult,
    *,
    validate_objects: bool = True,
    check_digests: bool = True,
    max_errors: int = 100,
) -> RootValidationSummary:
    """Validate an OCFL storage root."""
    summary = RootValidationSummary()

    # 1. Root NAMASTE declaration
    try:
        entries = list(fsys.read_dir(path))
    except OSError:
        result.error("E069a", assumed_version="1.1")
        return summary

    root_decls = []
    for entry in entries:
        n = Namaste.parse(entry.name)
        if n is not None and n.kind == "root":
            root_decls.append(n)

    if not root_decls:
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
        decl_path = _join(path, decl.filename)
        try:
            content = fs_utils.read_text(fsys, decl_path)
            if content != decl.content:
                result.error("E069e", filename=decl.filename)
        except OSError:
            pass

    # 2. ocfl_layout.json
    layout_path = _join(path, "ocfl_layout.json")
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
    ext_path = _join(path, "extensions")
    try:
        ext_entries = list(fsys.read_dir(ext_path))
        for entry in ext_entries:
            if not entry.is_dir:
                result.error("E086", entry=entry.name)
            elif entry.name not in _KNOWN_ROOT_EXTENSIONS:
                result.warning("W901", entry=entry.name)
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
        )

    return summary


def _walk_and_validate(
    fsys: FileWalker,
    root_path: str,
    result: ValidationResult,
    summary: RootValidationSummary,
    *,
    check_digests: bool,
    max_errors: int,
) -> None:
    """Walk storage root hierarchy, find objects, validate them."""
    from ocfl_lib.validation.result import ValidationResult as VR

    obj_paths = _walk_hierarchy(fsys, root_path, result)

    for obj_path in obj_paths:
        summary.num_objects += 1
        obj_result = VR()
        validate_object(
            fsys,
            obj_path,
            obj_result,
            check_digests=check_digests,
        )
        if obj_result.is_valid:
            summary.valid_objects += 1
        else:
            summary.object_errors.append((obj_path, obj_result))
            if len(summary.object_errors) >= max_errors:
                summary.truncated = True
                break


def _walk_hierarchy(
    fsys: FileWalker,
    root_path: str,
    result: ValidationResult,
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

        # Check if this dir is an object root (has object NAMASTE)
        has_obj_namaste = any(
            (n := Namaste.parse(e.name)) is not None and n.kind == "object"
            for e in entries
        )
        if has_obj_namaste:
            obj_paths.append(dir_path)
            continue  # don't descend into objects

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
            queue.append(_join(dir_path, d.name))

    return obj_paths


def _join(*parts: str) -> str:
    """Join path parts with /, filtering empty parts."""
    return "/".join(p for p in parts if p)
