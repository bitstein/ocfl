def rfind_casefold_span(text: str, needle: str) -> tuple[int, int] | None:
    """Return the original-string span for the rightmost casefolded match."""
    needle_cf = needle.casefold()
    last_match: tuple[int, int] | None = None

    for start in range(len(text)):
        folded = ""
        for end in range(start, len(text)):
            folded += text[end].casefold()
            if folded == needle_cf:
                last_match = (start, end + 1)
                break
            if not needle_cf.startswith(folded):
                break

    return last_match
