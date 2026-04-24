"""Typed exception hierarchy for ocfl_lib.

Single-rooted at OcflError. OcflNotFoundError dual-inherits LookupError so
KeyError/IndexError-style callers still match; no other multiple inheritance.
"""


class OcflError(Exception):
    """Base class for all ocfl_lib raised exceptions."""


class OcflNotFoundError(OcflError, LookupError):
    """Base class for missing-entity errors (LookupError-compatible)."""


class ObjectNotFound(OcflNotFoundError):
    """Object ID does not exist in the storage root."""

    def __init__(self, object_id: str) -> None:
        self.object_id = object_id
        super().__init__(f"object {object_id!r} not found")


class VersionNotFound(OcflNotFoundError):
    """Version key does not exist in the inventory."""

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"version {version} not found")


class NotAnOcflRoot(OcflError):
    """Path is missing a NAMASTE root declaration."""


class MultipleRootDeclarations(OcflError):
    """More than one NAMASTE root declaration found."""

    def __init__(self, filenames: list[str]) -> None:
        self.filenames = filenames
        super().__init__(f"multiple OCFL root declarations: {', '.join(filenames)}")


class LayoutNotConfigured(OcflError):
    """Storage root has no layout extension configured."""

    def __init__(self) -> None:
        super().__init__("no layout configured")


class InventorySidecarMismatch(OcflError):
    """Inventory sidecar digest does not match the inventory bytes."""

    def __init__(self, path: str, expected: str, actual: str) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"inventory sidecar digest mismatch in {path}: "
            f"expected {expected[:16]}..., got {actual[:16]}..."
        )


class OcflFileTypeError(OcflError):
    """Non-regular, non-directory entry encountered (symlink, fifo, etc.)."""


class RevertCompletedError(OcflError):
    """Refused to revert a fully-completed plan.

    Defensive guard against undoing a successful commit. Mirrors
    ocfl-go's ``ErrRevertUpdate``.
    """


class RevertError(OcflError):
    """One or more revert steps failed.

    `failures` is a tuple of (step_name, error_message) pairs.
    """

    def __init__(self, failures: tuple[tuple[str, str], ...]) -> None:
        self.failures = failures
        detail = "; ".join(f"{name}: {err}" for name, err in failures)
        super().__init__(f"revert failed for {len(failures)} step(s): {detail}")


class ExtensionError(OcflError):
    """Base class for OCFL extension structural violations."""


class CompanionMissingError(ExtensionError):
    """Expected companion file (e.g. `.ots`) is absent from an object."""


class EmbeddedDigestMismatchError(ExtensionError):
    """Digest embedded in a companion file does not match its paired content."""


class OtsParseError(ExtensionError):
    """Raw `.ots` bytes could not be parsed."""
