import ast
import re
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "ocfl_index"
_RAW_TX_PATTERN = re.compile(
    r"""['"](?:BEGIN(?:\s+IMMEDIATE)?|COMMIT|ROLLBACK)['"]""",
    re.IGNORECASE,
)


def test_no_raw_transaction_ops_outside_db() -> None:
    offenders: list[str] = []
    for path in _SRC_DIR.rglob("*.py"):
        if path == _SRC_DIR / "db.py":
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RAW_TX_PATTERN.search(line):
                offenders.append(f"{path.relative_to(_SRC_DIR.parent.parent)}:{lineno}")

    assert offenders == [], (
        "Raw BEGIN/COMMIT/ROLLBACK outside db.py; "
        "use db.immediate_transaction instead:\n  " + "\n  ".join(offenders)
    )


def test_only_immediate_transaction_mints_tx() -> None:
    forged: list[str] = []
    for path in [*_SRC_DIR.rglob("*.py"), *Path(__file__).parent.rglob("*.py")]:
        if path == _SRC_DIR / "db.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = ast.unparse(node.func)
            is_cast = func in {"cast", "typing.cast"} and any(
                "Tx" in ast.unparse(a) for a in node.args[:1]
            )
            if func in {"Tx", "db.Tx"} or is_cast:
                forged.append(f"{path.name}:{node.lineno}")

    assert forged == [], (
        "Tx proves an open BEGIN IMMEDIATE; take it from "
        "`with db.immediate_transaction(conn) as tx:` instead:\n  "
        + "\n  ".join(forged)
    )
