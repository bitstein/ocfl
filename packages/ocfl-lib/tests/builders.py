from pathlib import Path

from digests import D1

from ocfl_lib import DigestAlgorithm, LocalFS, VersionState, scan_dir

FIXTURES_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


class ProcessDied(BaseException):
    pass


def write_tree(base: Path, files: dict[str, str]) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return base


def stage_files(
    tmp_path: Path,
    files: dict[str, str],
    *,
    name: str = "src",
    alg: DigestAlgorithm = DigestAlgorithm.SHA512,
) -> VersionState:
    write_tree(tmp_path / name, files)
    return scan_dir(LocalFS(tmp_path), name, alg)


def version_block(**overrides: object) -> dict:
    return {
        "created": "2024-01-01T00:00:00Z",
        "state": {D1: ["file.txt"]},
        "message": "init",
        "user": {"name": "test", "address": "mailto:test@example.org"},
        **overrides,
    }


def inventory(**overrides: object) -> dict:
    return {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": "v1",
        "manifest": {D1: ["v1/content/file.txt"]},
        "versions": {"v1": version_block()},
        **overrides,
    }
