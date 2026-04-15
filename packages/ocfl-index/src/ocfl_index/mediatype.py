"""Media-type sniffing via libmagic."""

import magic

OCTET_STREAM = "application/octet-stream"


def sniff(buf: bytes) -> str:
    """Return libmagic mime guess for ``buf``. Empty buf returns ''."""
    if not buf:
        return ""
    return magic.from_buffer(buf, mime=True)
