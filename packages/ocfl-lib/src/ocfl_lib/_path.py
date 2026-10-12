"""Storage path helpers. Paths are "/"-separated and relative to an FS root."""


def join_path(*parts: str) -> str:
    """Join path parts with "/", skipping empty parts (an empty base is the root)."""
    return "/".join(p for p in parts if p)
