import ast
import json
import re
from collections.abc import Iterator
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

_KNOWN_UNREACHED: dict[str, str] = {
    "E026a": "unknown digest type fails at algorithm parse, before validation",
    "E038a": "inventory type mismatch reported via E038b/E038c",
    "E058b": "sidecar filename parsed before validation entry",
    "E050a": "per-digest E050f fires instead of the aggregate",
    "E066d": "prior version state digests reported via E066b",
    "E066e": "prior version state digests reported via E066b",
    "E111": "non-object fixity value reported via E056a",
    "E110": "id change between versions reported as E037b, as ocfl-py does",
    "E049c": "bad created date reported via E049b",
    "L003": "unsupported file type raised by the fs layer, outside validation/",
}

_CODE_RE = re.compile(r"^[EWL]\d+[a-z]?$")


def _validation_module_trees() -> Iterator[ast.Module]:
    for py_file in _VALIDATION_DIR.glob("*.py"):
        yield ast.parse(py_file.read_text(), filename=str(py_file))


def _first_args_of_error_and_warning_calls(tree: ast.Module) -> set[str]:
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("error", "warning")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _code_shaped_string_literals(tree: ast.Module) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _CODE_RE.match(node.value)
    }


def _codes_in_validation_source() -> set[str]:
    codes: set[str] = set()
    for tree in _validation_module_trees():
        codes |= _first_args_of_error_and_warning_calls(tree)
        codes |= _code_shaped_string_literals(tree)
    return codes


def _load_table_codes() -> set[str]:
    data = json.loads(_ERRORS_JSON.read_text())
    return set(data.keys())


class TestCodesRoundtrip:
    def test_all_raised_codes_in_table(self):
        raised = _codes_in_validation_source()
        table = _load_table_codes()
        missing = raised - table
        assert not missing, f"codes raised but not in table: {sorted(missing)}"

    def test_unreached_codes_match_expectation(self):
        raised = _codes_in_validation_source()
        table = _load_table_codes()
        unreached = table - raised
        extra = unreached - _KNOWN_UNREACHED.keys()
        now_raised = {
            code: reason
            for code, reason in _KNOWN_UNREACHED.items()
            if code not in unreached
        }
        assert not extra, f"unreached codes not in _KNOWN_UNREACHED: {sorted(extra)}"
        assert not now_raised, (
            f"_KNOWN_UNREACHED lists codes that are actually raised: {now_raised}"
        )
