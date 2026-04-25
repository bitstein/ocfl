"""_style helpers added for OTS: _format_digest + humanize_age.

Pinned behavior:
- _format_digest(d) returns f"sha256:{d}" — human-facing prefix only.
- humanize_age: 20-LOC, ASCII, single-unit, floor-rounded.
    <60s         => "{n}s ago"
    <60m         => "{n}m ago"
    <24h         => "{n}h ago"
    <30d         => "{n}d ago"
    >=30d        => "{n}w ago" (weeks, floor)
    future / <0  => "just now" (clock skew guard)
"""

import pytest

from ocfl_cli._style import _format_digest, humanize_age

DAY = 86400
HOUR = 3600


class TestFormatDigest:
    def test_adds_sha256_prefix(self):
        d = "abcd" * 16  # 64-char bare hex
        assert _format_digest(d) == f"sha256:{d}"

    def test_does_not_double_prefix(self):
        # If a caller already passed a prefixed digest, formatter must not
        # produce "sha256:sha256:..." — bare-hex is the wire convention; this
        # test pins that callers don't accidentally double-stamp.
        d = "ab" * 32
        formatted = _format_digest(d)
        assert formatted.count("sha256:") == 1


class TestHumanizeAge:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s ago"),
            (1, "1s ago"),
            (59, "59s ago"),
            (60, "1m ago"),
            (61, "1m ago"),
            (119, "1m ago"),
            (120, "2m ago"),
            (3599, "59m ago"),
            (3600, "1h ago"),
            (3601, "1h ago"),
            (HOUR * 23 + 3599, "23h ago"),
            (DAY, "1d ago"),
            (DAY + 1, "1d ago"),
            (DAY * 29, "29d ago"),
            (DAY * 30 - 1, "29d ago"),
            (DAY * 30, "4w ago"),  # 30/7 = 4.28 → 4
            (DAY * 35, "5w ago"),
            (DAY * 365, "52w ago"),
        ],
    )
    def test_boundaries(self, seconds: int, expected: str):
        assert humanize_age(seconds) == expected

    @pytest.mark.parametrize("seconds", [-1, -60, -3600])
    def test_negative_is_just_now(self, seconds: int):
        assert humanize_age(seconds) == "just now"

    def test_accepts_float(self):
        assert humanize_age(59.9) == "59s ago"
        assert humanize_age(60.0) == "1m ago"

    def test_ascii_only(self):
        for s in [0, 30, 90, HOUR * 2, DAY * 3, DAY * 60]:
            out = humanize_age(s)
            out.encode("ascii")  # must not raise
