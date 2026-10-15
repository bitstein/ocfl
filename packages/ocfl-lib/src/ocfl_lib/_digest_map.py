from ocfl_lib._errors import PathCollision

type DigestMap = dict[str, list[str]]
type PathMap = dict[str, str]


def to_path_map(dm: DigestMap) -> PathMap:
    """Invert: digest→[paths] to path→digest."""
    return {p: d for d, ps in dm.items() for p in ps}


def from_path_map(pm: PathMap) -> DigestMap:
    dm: DigestMap = {}
    for path, digest in pm.items():
        dm.setdefault(digest, []).append(path)
    return dm


def overlay_paths(base: DigestMap, overlay: DigestMap) -> DigestMap:
    pm = to_path_map(base)
    pm.update(to_path_map(overlay))
    return from_path_map(pm)


def _rename_path(path: str, src: str, dst: str) -> str:
    if path == src:
        return dst
    prefix = src + "/"
    if path.startswith(prefix):
        return dst + "/" + path[len(prefix) :]
    return path


def rename_paths(dm: DigestMap, src: str, dst: str) -> DigestMap:
    result: DigestMap = {
        digest: [_rename_path(p, src, dst) for p in ps] for digest, ps in dm.items()
    }
    seen: set[str] = set()
    for ps in result.values():
        for p in ps:
            if p in seen:
                raise PathCollision(f"rename collision at path {p!r}")
            seen.add(p)
    return result
