"""Validation result accumulator."""

from dataclasses import dataclass

from ocfl_lib.validation.codes import get_code


@dataclass
class ValidationError:
    """A single validation error, warning or info."""

    code: str
    message: str
    is_warning: bool


class ValidationResult:
    """Accumulates validation errors, warnings and infos.

    Validity depends on spec errors only. Infos carry non-spec
    implementation notes (L-codes) and never affect validity.
    """

    def __init__(self) -> None:
        self.errors: list[ValidationError] = []
        self.warnings: list[ValidationError] = []
        self.infos: list[ValidationError] = []

    @staticmethod
    def _message(code: str, params: dict[str, object]) -> str:
        vc = get_code(code)
        return vc.description.format(**params) if params else vc.description

    def error(self, code: str, **params: object) -> None:
        msg = self._message(code, params)
        self.errors.append(ValidationError(code=code, message=msg, is_warning=False))

    def warning(self, code: str, **params: object) -> None:
        msg = self._message(code, params)
        self.warnings.append(ValidationError(code=code, message=msg, is_warning=True))

    def info(self, code: str, **params: object) -> None:
        msg = self._message(code, params)
        self.infos.append(ValidationError(code=code, message=msg, is_warning=False))

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
        """Check if a code (exact or base prefix) is present in any finding."""
        return any(
            e.code == code or e.code.startswith(code)
            for e in (*self.errors, *self.warnings, *self.infos)
        )
