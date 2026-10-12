"""Typed exception hierarchy for ocfl_lib.

Single-rooted at OcflError. Every class carries an `ErrorClass` (how a
caller should react) and a stable snake_case `code` (part of the CLI's JSON
contract). Classes replacing a builtin raise also inherit that builtin
(`ValueError`, `KeyError`) so existing `except` clauses still match.
"""

import enum
from typing import ClassVar


class ErrorClass(enum.StrEnum):
    """How a caller should react to an error. Mirrors ocfl-rs ADR 0004."""

    USAGE = "usage"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    SERVICE = "service"
    STATE = "state"
    BUSY = "busy"
    HALT = "halt"
    CONFIG = "config"
    INTERRUPTED = "interrupted"


class OcflError(Exception):
    """Base class for all ocfl_lib raised exceptions."""

    code: ClassVar[str] = "ocfl_error"
    error_class: ClassVar[ErrorClass] = ErrorClass.INVALID


class OcflNotFoundError(OcflError, LookupError):
    """Base class for missing-entity errors (LookupError-compatible)."""

    code = "not_found"
    error_class = ErrorClass.NOT_FOUND


class ObjectNotFound(OcflNotFoundError):
    """Object ID does not exist in the storage root."""

    code = "object_not_found"
    error_class = ErrorClass.NOT_FOUND

    def __init__(self, object_id: str) -> None:
        self.object_id = object_id
        super().__init__(f"object {object_id!r} not found")


class VersionNotFound(OcflNotFoundError):
    """Version key does not exist in the inventory."""

    code = "version_not_found"
    error_class = ErrorClass.NOT_FOUND

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"version {version} not found")


class LogicalPathNotFound(OcflNotFoundError, KeyError):
    """Logical path is not in a version's state."""

    code = "logical_path_not_found"
    error_class = ErrorClass.NOT_FOUND

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class ContentNotFound(OcflNotFoundError, KeyError):
    """No content source can supply a digest."""

    code = "content_not_found"
    error_class = ErrorClass.NOT_FOUND

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class UnknownLayout(OcflNotFoundError, KeyError):
    """Layout extension name is not in the layout registry."""

    code = "unknown_layout"
    error_class = ErrorClass.NOT_FOUND

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown layout extension: {name!r}")

    def __str__(self) -> str:
        return str(self.args[0])


class NotAnOcflRoot(OcflError):
    """Path is missing a NAMASTE root declaration."""

    code = "not_an_ocfl_root"
    error_class = ErrorClass.NOT_FOUND


class RootExistsError(OcflError, ValueError):
    """Storage root is already initialized."""

    code = "root_exists"
    error_class = ErrorClass.STATE


class MultipleRootDeclarations(OcflError):
    """More than one NAMASTE root declaration found."""

    code = "multiple_root_declarations"
    error_class = ErrorClass.INVALID

    def __init__(self, filenames: list[str]) -> None:
        self.filenames = filenames
        super().__init__(f"multiple OCFL root declarations: {', '.join(filenames)}")


class SpecVersionError(OcflError):
    """Requested object spec version breaks a spec version rule.

    E081: an object must not be newer than its storage root.
    E103: a version must not use an older spec than the previous one.
    """

    code = "spec_version"
    error_class = ErrorClass.INVALID


class UnknownInventoryType(OcflError, ValueError):
    """Inventory `type` is not a known OCFL inventory type URL."""

    code = "unknown_inventory_type"
    error_class = ErrorClass.INVALID


class InvalidVersionNumber(OcflError, ValueError):
    """Version number is malformed or out of range."""

    code = "invalid_version_number"
    error_class = ErrorClass.INVALID


class UnknownDigestAlgorithm(OcflError, ValueError):
    """Digest algorithm name is not supported."""

    code = "unknown_digest_algorithm"
    error_class = ErrorClass.INVALID


class DigestAlgorithmMismatch(OcflError, ValueError):
    """Combined inputs use different digest algorithms."""

    code = "digest_algorithm_mismatch"
    error_class = ErrorClass.INVALID


class InvalidPath(OcflError, ValueError):
    """Object ID, logical path or content path breaks a path rule."""

    code = "invalid_path"
    error_class = ErrorClass.INVALID


class PathCollision(OcflError, ValueError):
    """Two entries map to the same logical path."""

    code = "path_collision"
    error_class = ErrorClass.INVALID


class InvalidLayoutConfig(OcflError, ValueError):
    """Layout extension config is missing or out of range."""

    code = "invalid_layout_config"
    error_class = ErrorClass.CONFIG


class LayoutNotConfigured(OcflError):
    """Storage root has no layout extension configured."""

    code = "layout_not_configured"
    error_class = ErrorClass.CONFIG

    def __init__(self) -> None:
        super().__init__("no layout configured")


class InventoryBuildError(OcflError, ValueError):
    """Inventory builder was given incomplete or inconsistent input."""

    code = "inventory_build"
    error_class = ErrorClass.INVALID


class InventorySidecarMismatch(OcflError):
    """Inventory sidecar digest does not match the inventory bytes."""

    code = "inventory_sidecar_mismatch"
    error_class = ErrorClass.INVALID

    def __init__(self, path: str, expected: str, actual: str) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"inventory sidecar digest mismatch in {path}: "
            f"expected {expected[:16]}..., got {actual[:16]}..."
        )


class PlanMismatchError(OcflError, ValueError):
    """Serialized update plan cannot be read or does not match its rebuild."""

    code = "plan_mismatch"
    error_class = ErrorClass.STATE


class StaleHeadError(OcflError):
    """Object root inventory changed after an update plan was built.

    This signals optimistic stale-head detection, not a multi-file atomic
    publish guarantee. Backends without conditional writes can still expose a
    short transient mismatch while inventory.json and its sidecar are being
    replaced.
    """

    code = "stale_head"
    error_class = ErrorClass.BUSY

    def __init__(
        self,
        *,
        object_path: str,
        expected_digest: str | None,
        actual_digest: str | None,
    ) -> None:
        self.object_path = object_path
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        expected = _short_digest(expected_digest)
        actual = _short_digest(actual_digest)
        super().__init__(
            f"stale object head at {object_path or '.'}: "
            f"expected {expected}, got {actual}"
        )


def _short_digest(digest: str | None) -> str:
    if digest is None:
        return "missing"
    return f"{digest[:16]}..."


class OcflFileTypeError(OcflError):
    """Non-regular, non-directory entry encountered (symlink, fifo, etc.)."""

    code = "unsupported_file_type"
    error_class = ErrorClass.INVALID


class RevertCompletedError(OcflError):
    """Refused to revert a fully-completed plan.

    Defensive guard against undoing a successful commit. Mirrors
    ocfl-go's ``ErrRevertUpdate``.
    """

    code = "revert_completed"
    error_class = ErrorClass.STATE


class RevertError(OcflError):
    """One or more revert steps failed.

    `failures` is a tuple of (step_name, error_message) pairs.
    """

    code = "revert_failed"
    error_class = ErrorClass.HALT

    def __init__(self, failures: tuple[tuple[str, str], ...]) -> None:
        self.failures = failures
        detail = "; ".join(f"{name}: {err}" for name, err in failures)
        super().__init__(f"revert failed for {len(failures)} step(s): {detail}")


class OtsParseError(OcflError):
    """Raw `.ots` bytes could not be parsed."""

    code = "ots_parse"
    error_class = ErrorClass.INVALID
