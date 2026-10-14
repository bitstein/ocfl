import ast
import json
import re
from pathlib import Path

_VALIDATION_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "ocfl_lib" / "validation"
)
_ERRORS_JSON = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ocfl_lib"
    / "_data"
    / "validation-errors.json"
)

_KNOWN_UNREACHED: frozenset[str] = frozenset(
    {
        # Content path empty segment — subsumed by E099 segment check
        "E024",
        # Digest type unknown — caught at algorithm parse, not in validation
        "E026a",
        # Inventory type/version mismatches — E038a covered by E038b/c path
        "E038a",
        # Sidecar filename extraction — handled before validation entry
        "E058b",
        # State digest aggregate — individual E050f fires instead
        "E050a",
        # Prior inventory version state digest checks — E066b covers the paths
        "E066d",
        "E066e",
        # Fixity object type — E056a covers this path
        "E111",
        # Inventory cross-version id check — E110 is unreachable (E037b used instead)
        "E110",
        # Created date format variant — E049b covers this path
        "E049c",
        # Unsupported file type — raised outside validation/ (fs layer)
        "L003",
    },
)


_CODE_RE = re.compile(r"^[EWL]\d+[a-z]?$")


def _collect_raised_codes() -> set[str]:
    """AST-walk validation/*.py and collect error/warning codes.

    Detects both direct calls like ``result.error("E001a", ...)`` and
    loop-variable patterns like ``for key, code in [("id", "E036a"), ...]``.
    """
    codes: set[str] = set()
    for py_file in _VALIDATION_DIR.glob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            # Direct: result.error("CODE", ...) / result.warning("CODE", ...)
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("error", "warning")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    codes.add(node.args[0].value)
            # Indirect: string constants in list/tuple literals that match
            # code pattern (catches `for key, code in [(..., "E036a"), ...]`)
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _CODE_RE.match(node.value)
            ):
                codes.add(node.value)
    return codes


def _load_table_codes() -> set[str]:
    data = json.loads(_ERRORS_JSON.read_text())
    return set(data.keys())


class TestCodesRoundtrip:
    def test_all_raised_codes_in_table(self):
        raised = _collect_raised_codes()
        table = _load_table_codes()
        missing = raised - table
        assert not missing, f"codes raised but not in table: {sorted(missing)}"

    def test_unreached_codes_match_expectation(self):
        raised = _collect_raised_codes()
        table = _load_table_codes()
        unreached = table - raised
        extra = unreached - _KNOWN_UNREACHED
        missing = _KNOWN_UNREACHED - unreached
        assert not extra, f"unreached codes not in _KNOWN_UNREACHED: {sorted(extra)}"
        assert not missing, (
            f"_KNOWN_UNREACHED lists codes that are actually raised: {sorted(missing)}"
        )
