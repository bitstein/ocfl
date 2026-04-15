"""Walk an OCFL root and update the index cache."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ocfl_lib import DigestAlgorithm
from ocfl_lib import fs as ocfl_fs

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS

_log = logging.getLogger(__name__)

_SIDECAR_PROBE_ORDER: tuple[DigestAlgorithm, ...] = (
    DigestAlgorithm.SHA256,
    DigestAlgorithm.SHA512,
)


def _read_sidecar(fs: FS, relpath: str) -> tuple[DigestAlgorithm, str] | None:
    """Find the inventory sidecar for an object and return (algo, digest).

    Probes sha256 then sha512. Returns None if neither sidecar is present.
    """
    for alg in _SIDECAR_PROBE_ORDER:
        sidecar = f"{relpath}/inventory.json.{alg.ocfl_name}"
        try:
            text = ocfl_fs.read_text(fs, sidecar)
        except FileNotFoundError:
            continue
        digest = text.strip().split()[0]
        return alg, digest
    _log.warning("no inventory sidecar for %s; skipping", relpath)
    return None
