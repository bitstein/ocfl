"""Generate the E9xx/W9xx validation codes from the OTS extension spec.

Reads the "Validation Codes" table of docs/ext/NNNN-ots-timestamps.md and
rewrites the E9xx/W9xx entries of ocfl_lib/_data/validation-errors.json.
Inside a backticked span, the placeholders `vN`/`vK`, `P` and `X` become
the template parameters {version}, {path} and {proof}; backticks are dropped.

    python packages/ocfl-lib/scripts/gen_ots_codes.py          # rewrite
    python packages/ocfl-lib/scripts/gen_ots_codes.py --check  # exit 1 if stale
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "docs/ext/NNNN-ots-timestamps.md"
CODES = REPO / "packages/ocfl-lib/src/ocfl_lib/_data/validation-errors.json"

PLACEHOLDERS = {"vN": "version", "vK": "version", "P": "path", "X": "proof"}
_ROW = re.compile(r"\| ([EW]9\d\d) \| (?:error|warning) \| (.+) \|")
_OURS = re.compile(r"[EW]9\d\d")


def spec_codes(text: str) -> dict[str, dict[str, object]]:
    """{code: {"params": [...], "description": ...}} in table order."""
    out: dict[str, dict[str, object]] = {}
    for line in text.splitlines():
        m = _ROW.fullmatch(line)
        if m is None:
            continue
        params: list[str] = []

        def placeholder(pm: re.Match[str], params: list[str] = params) -> str:
            name = PLACEHOLDERS[pm.group(0)]
            if name not in params:
                params.append(name)
            return "{" + name + "}"

        def span(sm: re.Match[str]) -> str:
            return re.sub(r"\b(vN|vK|P|X)\b", placeholder, sm.group(1))

        description = re.sub(r"`([^`]*)`", span, m.group(2))
        out[m.group(1)] = {"params": params, "description": description}
    if not out:
        raise SystemExit(f"no E9xx/W9xx rows in {SPEC}")
    return out


def render(current: dict[str, object], generated: dict[str, object]) -> str:
    merged = {k: v for k, v in current.items() if not _OURS.fullmatch(k)}
    merged.update(generated)
    return json.dumps(merged, indent=2) + "\n"


def main(argv: list[str]) -> int:
    generated = spec_codes(SPEC.read_text())
    old = CODES.read_text()
    new = render(json.loads(old), generated)
    if "--check" in argv:
        if new != old:
            print(f"{CODES} is stale; run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    CODES.write_text(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
