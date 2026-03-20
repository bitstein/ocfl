"""OCFL staging for version assembly."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ocfl_lib.digest import DigestAlgorithm, stream_digest, stream_multi_digest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ocfl_lib.digest_map import DigestMap
    from ocfl_lib.fs.protocols import FS, FileWalker
    from ocfl_lib.update_plan import ContentSource


@runtime_checkable
class FixitySource(Protocol):
    """Protocol for resolving fixity digests by primary digest."""

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]: ...


@dataclass
class Stage:
    """Staged content for creating or updating an OCFL object version."""

    state: DigestMap
    digest_algorithm: DigestAlgorithm
    content_source: ContentSource
    fixity_source: FixitySource | None = None


class _DirContentSource:
    """ContentSource backed by a directory in an FS."""

    def __init__(self, fsys: FS, path_by_digest: dict[str, str]):
        self._fsys = fsys
        self._paths = path_by_digest

    def get_content(self, digest: str) -> tuple[FS, str]:
        return self._fsys, self._paths[digest]


class _DictFixitySource:
    """FixitySource backed by a precomputed dict."""

    def __init__(self, fixity: dict[str, dict[DigestAlgorithm, str]]):
        self._fixity = fixity

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
        return self._fixity.get(digest, {})


def stage_dir(
    fsys: FileWalker,
    dir: str,
    alg: DigestAlgorithm,
    *,
    fixity_algs: Sequence[DigestAlgorithm] = (),
) -> Stage:
    """Stage all files in a directory.

    Walks dir, computes digests, builds state and content source.
    Optionally computes fixity digests for additional algorithms.
    """
    from concurrent.futures import ThreadPoolExecutor

    dir = dir.rstrip("/")

    # Collect all file paths
    files = list(fsys.walk_files(dir))
    if not files:
        return Stage(
            state={},
            digest_algorithm=alg,
            content_source=_DirContentSource(fsys, {}),
        )

    all_algs = [alg, *fixity_algs]
    compute_fixity = len(fixity_algs) > 0

    # walk_files returns paths relative to FS root; strip dir prefix for state
    dir_prefix = f"{dir}/" if dir else ""

    def _digest_file(
        walk_path: str,
    ) -> tuple[str, str, str, dict[DigestAlgorithm, str]]:
        """Return (state_path, walk_path, primary_digest, fixity_digests)."""
        state_path = walk_path.removeprefix(dir_prefix)
        if compute_fixity:
            with fsys.open_file(walk_path) as f:
                digests = stream_multi_digest(f, all_algs)
            primary = digests[alg]
            fixity = {a: digests[a] for a in fixity_algs}
            return state_path, walk_path, primary, fixity
        else:
            with fsys.open_file(walk_path) as f:
                primary = stream_digest(f, alg)
            return state_path, walk_path, primary, {}

    # Parallel digest computation
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda fr: _digest_file(fr.path), files))

    # Build state (digest → [paths]) and path_by_digest
    state: dict[str, list[str]] = {}
    path_by_digest: dict[str, str] = {}
    fixity_map: dict[str, dict[DigestAlgorithm, str]] = {}

    for state_path, walk_path, primary, fixity in results:
        state.setdefault(primary, []).append(state_path)
        if primary not in path_by_digest:
            path_by_digest[primary] = walk_path
        if fixity:
            fixity_map[primary] = fixity

    # Sort paths within each digest for determinism
    for paths in state.values():
        paths.sort()

    fixity_source: FixitySource | None = None
    if compute_fixity:
        fixity_source = _DictFixitySource(fixity_map)

    return Stage(
        state=state,
        digest_algorithm=alg,
        content_source=_DirContentSource(fsys, path_by_digest),
        fixity_source=fixity_source,
    )
