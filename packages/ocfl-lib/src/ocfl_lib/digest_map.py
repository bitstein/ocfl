"""Operations on OCFL digest maps (digest → [paths])."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

type DigestMap = dict[str, list[str]]
type PathMap = dict[str, str]


def normalize(dm: DigestMap) -> DigestMap:
    """Lowercase digests, sort path lists, merge case-colliding keys."""
    result: DigestMap = {}
    for d, ps in dm.items():
        key = d.lower()
        existing = result.get(key, [])
        existing.extend(ps)
        result[key] = existing
    return {d: sorted(ps) for d, ps in result.items()}


def to_path_map(dm: DigestMap) -> PathMap:
    """Invert: digest→[paths] to path→digest."""
    return {p: d for d, ps in dm.items() for p in ps}


def from_path_map(pm: PathMap) -> DigestMap:
    """Invert: path→digest to digest→[paths]."""
    dm: DigestMap = {}
    for path, digest in pm.items():
        dm.setdefault(digest, []).append(path)
    return dm


def merge(base: DigestMap, overlay: DigestMap, *, replace: bool = False) -> DigestMap:
    """Merge overlay into base.

    If replace=True, overlay paths override base paths (by path).
    Otherwise, paths are combined per digest.
    """
    if not replace:
        result: DigestMap = {d: list(ps) for d, ps in base.items()}
        for d, ps in overlay.items():
            existing = result.get(d, [])
            existing.extend(p for p in ps if p not in existing)
            result[d] = existing
        return result

    # Replace mode: overlay's path→digest wins over base
    base_pm = to_path_map(base)
    overlay_pm = to_path_map(overlay)
    base_pm.update(overlay_pm)
    return from_path_map(base_pm)


def paths(dm: DigestMap) -> Iterator[tuple[str, str]]:
    """Yield (path, digest) for every path in the map."""
    for digest, ps in dm.items():
        for p in ps:
            yield p, digest


def _rename_path(path: str, src: str, dst: str) -> str:
    """Replace path component prefix src with dst."""
    if path == src:
        return dst
    prefix = src + "/"
    if path.startswith(prefix):
        return dst + "/" + path[len(prefix) :]
    return path


def rename_paths(dm: DigestMap, src: str, dst: str) -> DigestMap:
    """Replace path prefix src with dst (component-aware).

    Raises ValueError if rename would produce duplicate paths.
    """
    result: DigestMap = {
        digest: [_rename_path(p, src, dst) for p in ps] for digest, ps in dm.items()
    }
    seen: set[str] = set()
    for ps in result.values():
        for p in ps:
            if p in seen:
                raise ValueError(f"rename collision at path {p!r}")
            seen.add(p)
    return result


def remove_path(dm: DigestMap, path: str) -> DigestMap:
    """Remove a path, dropping the digest entry if no paths remain."""
    result: DigestMap = {}
    for digest, ps in dm.items():
        filtered = [p for p in ps if p != path]
        if filtered:
            result[digest] = filtered
    return result


def eq(a: DigestMap, b: DigestMap) -> bool:
    """Normalized equality: case-insensitive digests, sorted paths."""
    return normalize(a) == normalize(b)


def validate(dm: DigestMap) -> list[str]:
    """Structural validation. Returns list of error descriptions."""
    errors: list[str] = []
    seen_paths: dict[str, str] = {}
    for digest, ps in dm.items():
        if not digest:
            errors.append("empty digest key")
        if not ps:
            errors.append(f"digest {digest!r} has empty path list")
        for p in ps:
            if not p:
                errors.append(f"digest {digest!r} has empty path string")
            elif p in seen_paths:
                errors.append(
                    f"path {p!r} appears under both {seen_paths[p]!r} and {digest!r}"
                )
            else:
                seen_paths[p] = digest
    return errors
