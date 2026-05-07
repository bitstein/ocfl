"""Static contract tests: production code must not drive raw SQLite tx ops.

The autocommit contract says only ``db.immediate_transaction`` and
``scanner.scan`` may issue ``BEGIN``, ``COMMIT``, or ``ROLLBACK``. Anything
else risks half-committed schema rebuilds or stranded writer locks.
"""

import re
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "ocfl_index"
_ALLOWED_FILES = {"db.py", "scanner.py"}
_RAW_TX_PATTERN = re.compile(
    r"""['"](?:BEGIN(?:\s+IMMEDIATE)?|COMMIT|ROLLBACK)['"]""",
    re.IGNORECASE,
)


def test_no_raw_transaction_ops_outside_db_or_scanner() -> None:
    offenders: list[str] = []
    for path in _SRC_DIR.rglob("*.py"):
        if path.name in _ALLOWED_FILES:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RAW_TX_PATTERN.search(line):
                offenders.append(f"{path.relative_to(_SRC_DIR.parent.parent)}:{lineno}")

    assert offenders == [], (
        "Raw BEGIN/COMMIT/ROLLBACK outside db.py or scanner.py — "
        "use db.immediate_transaction instead:\n  " + "\n  ".join(offenders)
    )
