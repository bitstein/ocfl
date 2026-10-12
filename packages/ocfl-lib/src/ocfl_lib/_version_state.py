"""Version State: a version's logical state plus where its content is."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ocfl_lib._digest import DigestAlgorithm, stream_multi_digest
from ocfl_lib._errors import ContentNotFound, DigestAlgorithmMismatch
from ocfl_lib._sources import ContentSource, FixitySource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ocfl_lib._digest_map import DigestMap
    from ocfl_lib.fs._protocols import FS, FileWalker


@dataclass
class VersionState:
    """Logical state of one object version plus where its content is."""

    state: DigestMap
    digest_algorithm: DigestAlgorithm
    content_source: ContentSource
    fixity_source: FixitySource | None = None

    def overlay(self, *others: VersionState) -> VersionState:
        """Merge other version states on top of this one.

        Later states override earlier ones by path. All must
        share the same digest_algorithm. Content and fixity sources
        are chained: tries each in order.
        """
        from ocfl_lib._digest_map import merge

        for other in others:
            if other.digest_algorithm != self.digest_algorithm:
                msg = (
                    f"digest_algorithm mismatch: "
                    f"{self.digest_algorithm} vs {other.digest_algorithm}"
                )
                raise DigestAlgorithmMismatch(msg)

        # Merge states: overlay paths win
        merged_state = dict(self.state)
        for other in others:
            merged_state = merge(merged_state, other.state, replace=True)

        # Chain content sources
        all_sources = [self.content_source, *(o.content_source for o in others)]
        content_source = _ChainContentSource(all_sources)

        # Chain fixity sources
        all_fixity = [
            s.fixity_source for s in [self, *others] if s.fixity_source is not None
        ]
        fixity_source: FixitySource | None = None
        if all_fixity:
            fixity_source = _ChainFixitySource(all_fixity)

        return VersionState(
            state=merged_state,
            digest_algorithm=self.digest_algorithm,
            content_source=content_source,
            fixity_source=fixity_source,
        )


class _ChainContentSource(ContentSource):
    """ContentSource that tries multiple sources in order."""

    def __init__(self, sources: list[ContentSource]):
        self._sources = sources

    def get_content(self, digest: str) -> tuple[FS, str]:
        for src in self._sources:
            try:
                return src.get_content(digest)
            except KeyError:
                continue
        raise ContentNotFound(f"digest {digest!r} not found in any content source")


class _ChainFixitySource(FixitySource):
    """FixitySource that tries multiple sources in order."""

    def __init__(self, sources: list[FixitySource]):
        self._sources = sources

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
        for src in self._sources:
            result = src.get_fixity(digest)
            if result:
                return result
        return {}


class _DirContentSource(ContentSource):
    """ContentSource backed by a directory in an FS."""

    def __init__(self, fsys: FS, path_by_digest: dict[str, str]):
        self._fsys = fsys
        self._paths = path_by_digest

    def get_content(self, digest: str) -> tuple[FS, str]:
        return self._fsys, self._paths[digest]


class _DictFixitySource(FixitySource):
    """FixitySource backed by a precomputed dict."""

    def __init__(self, fixity: dict[str, dict[DigestAlgorithm, str]]):
        self._fixity = fixity

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
        return self._fixity.get(digest, {})


def scan_dir(
    fsys: FileWalker,
    dir: str,
    alg: DigestAlgorithm,
    *,
    fixity_algs: Sequence[DigestAlgorithm] = (),
) -> VersionState:
    """Scan all files in a directory into a VersionState.

    Walks dir, computes digests, builds state and content source.
    Optionally computes fixity digests for additional algorithms.
    """
    from ocfl_lib._concurrent import bounded_map

    dir = dir.rstrip("/")

    # Collect all file paths
    files = list(fsys.walk_files(dir))
    if not files:
        return VersionState(
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
        with fsys.open_file(walk_path) as f:
            digests = stream_multi_digest(f, all_algs)
        fixity = {a: digests[a] for a in fixity_algs}
        return state_path, walk_path, digests[alg], fixity

    # Parallel digest computation
    results = list(bounded_map(lambda fr: _digest_file(fr.path), files))

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

    return VersionState(
        state=state,
        digest_algorithm=alg,
        content_source=_DirContentSource(fsys, path_by_digest),
        fixity_source=fixity_source,
    )
