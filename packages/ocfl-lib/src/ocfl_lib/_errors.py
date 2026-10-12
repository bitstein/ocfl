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


class ContentDigestMismatch(OcflError):
    """Content from a content source does not match its manifest digest."""

    code = "content_digest_mismatch"
    error_class = ErrorClass.INVALID


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
    """Refused to revert a version whose root inventory is already published.

    Readers may already see the version. Finish it with a resume instead.
    Mirrors ocfl-go's ``ErrRevertUpdate``.
    """

    code = "revert_completed"
    error_class = ErrorClass.STATE


class ClaimLostError(OcflError):
    """Another writer claimed the version first (ADR-0001).

    The loser has written nothing into the object.
    """

    code = "claim_lost"
    error_class = ErrorClass.STATE


class RecoveryRequiredError(OcflError):
    """An unfinished commit or a stray claim blocks the operation.

    Run `recover` on the object first.
    """

    code = "recovery_required"
    error_class = ErrorClass.STATE


class RecoveryActionError(OcflError):
    """The recovery action does not apply to the object's partial state."""

    code = "recovery_action"
    error_class = ErrorClass.USAGE


class ObjectLockedError(OcflError):
    """Another process holds the object's commit lock."""

    code = "object_locked"
    error_class = ErrorClass.BUSY


class UnsupportedBackendError(OcflError):
    """The filesystem cannot claim a version atomically.

    Local claims need Linux renameat2(RENAME_NOREPLACE); S3 claims need a
    store that honors If-None-Match and If-Match. There is no fallback.
    """

    code = "unsupported_backend"
    error_class = ErrorClass.CONFIG


class OtsParseError(OcflError):
    """Raw `.ots` bytes could not be parsed."""

    code = "ots_parse"
    error_class = ErrorClass.INVALID


class OtsConfigError(OcflError, ValueError):
    """The OTS extension's `config.json` is missing or invalid (E908)."""

    code = "ots_config"
    error_class = ErrorClass.INVALID


class BitcoinRpcError(OcflError):
    """A Bitcoin node's RPC call failed (`ocfl_lib.bitcoin`)."""

    code = "bitcoin_rpc"
    error_class = ErrorClass.SERVICE


class BitcoinRpcAuthError(BitcoinRpcError):
    """The Bitcoin node refused the RPC credentials."""

    code = "bitcoin_rpc_auth"
    error_class = ErrorClass.CONFIG


class BitcoinRpcTransientError(BitcoinRpcError):
    """A retryable RPC failure (loading, 429, 5xx, transport) that outlasted
    the retries."""

    code = "bitcoin_rpc_transient"
    error_class = ErrorClass.SERVICE


class BitcoinNoBlockError(BitcoinRpcError):
    """The node's best chain has no block at the requested height."""

    code = "bitcoin_no_block"
    error_class = ErrorClass.NOT_FOUND


class MissingInventoryProofError(OcflError, ValueError):
    """The object's OTS extension requires an inventory proof (E904)."""

    code = "missing_inventory_proof"
    error_class = ErrorClass.INVALID


class MissingContentProofError(OcflError, ValueError):
    """A path of the new version has no content proof `P.ots` (E902)."""

    code = "missing_content_proof"
    error_class = ErrorClass.INVALID


class InvalidProofError(OcflError, ValueError):
    """A proof does not parse, has no sha256 file hash, or commits to
    another digest (E905)."""

    code = "invalid_proof"
    error_class = ErrorClass.INVALID


class PendingProofError(OcflError, ValueError):
    """A content proof holds no Bitcoin attestation yet (E907)."""

    code = "pending_proof"
    error_class = ErrorClass.STATE
