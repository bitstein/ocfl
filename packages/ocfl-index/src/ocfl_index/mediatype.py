import magic

OCTET_STREAM = "application/octet-stream"


def sniff(buf: bytes) -> str:
    if not buf:
        return ""
    return magic.from_buffer(buf, mime=True)
