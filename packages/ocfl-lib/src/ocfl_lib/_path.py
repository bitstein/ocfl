def join_path(*parts: str) -> str:
    return "/".join(p for p in parts if p)
