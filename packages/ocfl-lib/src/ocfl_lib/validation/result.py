"""Validation result accumulator."""

from dataclasses import dataclass

from ocfl_lib.validation.codes import get_code


@dataclass
class ValidationError:
    """A single validation error or warning."""

    code: str
    message: str
    is_warning: bool


class ValidationResult:
    """Accumulates validation errors and warnings."""

    def __init__(self) -> None:
        self.errors: list[ValidationError] = []
        self.warnings: list[ValidationError] = []

    def error(self, code: str, **params: object) -> None:
        vc = get_code(code)
        msg = vc.description.format(**params) if params else vc.description
        self.errors.append(ValidationError(code=code, message=msg, is_warning=False))

    def warning(self, code: str, **params: object) -> None:
        vc = get_code(code)
        msg = vc.description.format(**params) if params else vc.description
        self.warnings.append(ValidationError(code=code, message=msg, is_warning=True))

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def num_errors(self) -> int:
        return len(self.errors)

    @property
    def num_warnings(self) -> int:
        return len(self.warnings)

    def has_code(self, code: str) -> bool:
        """Check if a code (exact or base prefix) is present in errors or warnings."""
        return any(
            e.code == code or e.code.startswith(code)
            for e in (*self.errors, *self.warnings)
        )
