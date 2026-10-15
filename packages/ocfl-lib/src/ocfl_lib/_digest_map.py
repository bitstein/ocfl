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
