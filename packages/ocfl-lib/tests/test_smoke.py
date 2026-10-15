from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_fixtures_exist(fixtures_root: Path) -> None:
    assert (fixtures_root / "1.0" / "good-objects").is_dir()
    assert (fixtures_root / "1.1" / "good-objects").is_dir()
    assert (fixtures_root / "1.0" / "bad-objects").is_dir()
    assert (fixtures_root / "1.1" / "bad-objects").is_dir()
