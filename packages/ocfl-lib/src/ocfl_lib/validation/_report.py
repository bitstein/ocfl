"""Validation findings and the report that holds them.

A Finding carries data only. Its message is rendered from the code's
description template only for display (`Finding.render`).
"""

import enum
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from ocfl_lib.validation._codes import get_code

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class Severity(enum.StrEnum):
    """Spec errors decide validity. Warnings and infos never do."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Finding:
    """One validation finding.

    `path` is the object root (or storage root) the finding is about.
    `object_id` is the object's id when its inventory could be read.
    `detail` holds the code's template parameters.
    """

    code: str
    severity: Severity
    object_id: str | None
    path: str
    detail: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __hash__(self) -> int:
        return hash(
            (
                self.code,
                self.severity,
                self.object_id,
                self.path,
                tuple(sorted(self.detail.items())),
            )
        )

    def render(self) -> str:
        """Human-readable message from the code's description template."""
        description = get_code(self.code).description
        return description.format(**self.detail) if self.detail else description


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Immutable result of validating an object or a storage root.

    For a root, `objects_checked` / `objects_valid` count the objects
    validated, and `truncated` is set when validation stopped early at
    `max_errors` invalid objects.
    """

    findings: tuple[Finding, ...] = ()
    objects_checked: int = 0
    objects_valid: int = 0
    truncated: bool = False

    def _with(self, severity: Severity) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is severity)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return self._with(Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return self._with(Severity.WARNING)

    @property
    def infos(self) -> tuple[Finding, ...]:
        return self._with(Severity.INFO)

    @property
    def is_valid(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def num_errors(self) -> int:
        return len(self.errors)

    @property
    def num_warnings(self) -> int:
        return len(self.warnings)

    def has_code(self, code: str) -> bool:
        """Check if a code (exact or base prefix) is present in any finding."""
        return any(f.code.startswith(code) for f in self.findings)

    def with_findings(self, findings: Iterable[Finding]) -> ValidationReport:
        """Return a copy with `findings` appended."""
        return replace(self, findings=(*self.findings, *findings))


class FindingCollector:
    """Mutable accumulator that validators write to; `report()` freezes it.

    `object_id` may be set once the inventory is read; it is stamped on
    every finding this collector made.
    """

    def __init__(self, path: str = "", object_id: str | None = None) -> None:
        self.path = path
        self.object_id = object_id
        self._own: list[tuple[str, Severity, dict[str, str]]] = []
        self._other: list[Finding] = []

    def _add(self, code: str, severity: Severity, params: dict[str, object]) -> None:
        get_code(code)  # unknown codes fail fast
        self._own.append((code, severity, {k: str(v) for k, v in params.items()}))

    def error(self, code: str, **params: object) -> None:
        self._add(code, Severity.ERROR, params)

    def warning(self, code: str, **params: object) -> None:
        self._add(code, Severity.WARNING, params)

    def info(self, code: str, **params: object) -> None:
        self._add(code, Severity.INFO, params)

    def extend(self, findings: Iterable[Finding]) -> None:
        """Add findings made elsewhere (e.g. a contained object's report)."""
        self._other.extend(findings)

    @property
    def findings(self) -> tuple[Finding, ...]:
        own = (
            Finding(
                code=code,
                severity=severity,
                object_id=self.object_id,
                path=self.path,
                detail=MappingProxyType(detail),
            )
            for code, severity, detail in self._own
        )
        return (*own, *self._other)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return self.report().errors

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return self.report().warnings

    @property
    def infos(self) -> tuple[Finding, ...]:
        return self.report().infos

    @property
    def is_valid(self) -> bool:
        return self.report().is_valid

    def has_code(self, code: str) -> bool:
        return self.report().has_code(code)

    def report(
        self,
        *,
        objects_checked: int = 0,
        objects_valid: int = 0,
        truncated: bool = False,
    ) -> ValidationReport:
        return ValidationReport(
            findings=self.findings,
            objects_checked=objects_checked,
            objects_valid=objects_valid,
            truncated=truncated,
        )
