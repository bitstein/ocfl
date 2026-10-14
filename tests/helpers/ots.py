import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from bitcoin.core import CBlock
from opentimestamps.bitcoin import make_timestamp_from_block
from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from ocfl_lib.ots import EXTENSION_NAME

if TYPE_CHECKING:
    from tests.helpers.regtest import RegtestNode


def build_attestation(
    file_digest: bytes,
    *,
    block_height: int | None = None,
    pending_uri: str | None = None,
) -> bytes:
    if block_height is None and pending_uri is None:
        raise ValueError("must provide block_height or pending_uri")

    timestamp = Timestamp(file_digest)
    if pending_uri is not None:
        timestamp.attestations.add(PendingAttestation(pending_uri))
    if block_height is not None:
        timestamp.attestations.add(BitcoinBlockHeaderAttestation(block_height))

    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes()


def build_committed_attestation(
    file_digest: bytes,
    *,
    block_height: int,
) -> tuple[bytes, bytes]:
    """Build ``.ots`` bytes with one operation between the leaf and the
    Bitcoin attestation, so the *attested* merkle root differs from the file's
    *commitment* digest.

    ``build_attestation`` attaches the attestation directly to the leaf, which
    collapses commitment_digest == attested_digest and makes the two distinct
    checks in ``verify_attestation`` indistinguishable. Here the attestation
    sits one OpSHA256 below the leaf, mirroring a real merkle path.

    Returns ``(ots_bytes, attested_digest)``. Register ``attested_digest``
    (reversed, display hex) as the block's merkle root for a match.
    """
    timestamp = Timestamp(file_digest)
    attested_node = timestamp.ops.add(OpSHA256())
    attested_node.attestations.add(BitcoinBlockHeaderAttestation(block_height))
    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes(), attested_node.msg


def build_regtest_attestation(
    node: RegtestNode,
    digest: bytes,
    *,
    wallet: str = "ocfltest",
) -> tuple[bytes, int, str]:
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes (SHA-256)")

    raw_hex = node.wallet_rpc(
        wallet,
        "createrawtransaction",
        [[], [{"data": digest.hex()}]],
    )
    funded = node.wallet_rpc(wallet, "fundrawtransaction", [raw_hex])
    signed = node.wallet_rpc(wallet, "signrawtransactionwithwallet", [funded["hex"]])
    if not signed.get("complete"):
        raise RuntimeError(f"sign incomplete: {signed.get('errors')}")
    txid = node.wallet_rpc(wallet, "sendrawtransaction", [signed["hex"]])

    addr = node.wallet_rpc(wallet, "getnewaddress")
    block_hash = node.rpc("generatetoaddress", [1, addr])[0]
    header = node.rpc("getblockheader", [block_hash])
    height = int(header["height"])

    block_txids = node.rpc("getblock", [block_hash])["tx"]
    if txid not in block_txids:
        raise RuntimeError(f"tx {txid} not in block {block_hash}")

    block_hex = node.rpc("getblock", [block_hash, 0])
    block = CBlock.deserialize(bytes.fromhex(block_hex))
    timestamp = make_timestamp_from_block(digest, block, height)
    if timestamp is None:
        raise RuntimeError("make_timestamp_from_block could not commit digest")

    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes(), height, block_hash


def build_ots_object(
    obj_dir: Path,
    *,
    obj_id: str = "http://example.org/obj1",
    files: dict[str, bytes] | None = None,
    include_companions: bool = True,
    include_extension_dir: bool = True,
    include_inventory_v1_ots: bool = True,
    digest_algorithm: str = "sha256",
    content_block_height: int = 800_000,
    extra_content_files: dict[str, bytes] | None = None,
    companion_overrides: dict[str, bytes] | None = None,
    inventory_ots_override: bytes | None = None,
    pending_inventory_ots: bool = False,
    pending_companions: set[str] | None = None,
) -> Path:
    files = {"file.txt": b"hello world"} if files is None else files
    pending_companions = pending_companions or set()
    companion_overrides = companion_overrides or {}
    extra_content_files = extra_content_files or {}

    manifest: dict[str, list[str]] = {}
    state: dict[str, list[str]] = {}

    def _add(content_path: str, state_path: str, data: bytes) -> None:
        d = hashlib.new(digest_algorithm, data).hexdigest()
        manifest.setdefault(d, []).append(content_path)
        state.setdefault(d, []).append(state_path)

    companion_bytes: dict[str, bytes] = {}
    for name, data in files.items():
        _add(f"v1/content/{name}", name, data)
        if include_companions:
            file_sha256 = hashlib.sha256(data).digest()
            if name in companion_overrides:
                ots_bytes = companion_overrides[name]
            elif name in pending_companions:
                ots_bytes = build_attestation(
                    file_sha256, pending_uri="https://a.pool.opentimestamps.org"
                )
            else:
                ots_bytes = build_attestation(
                    file_sha256, block_height=content_block_height
                )
            companion_bytes[name] = ots_bytes
            _add(f"v1/content/{name}.ots", f"{name}.ots", ots_bytes)

    for path, data in extra_content_files.items():
        _add(f"v1/content/{path}", path, data)

    inv = {
        "id": obj_id,
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": digest_algorithm,
        "head": "v1",
        "manifest": manifest,
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": state,
                "message": "init",
                "user": {"name": "t", "address": "mailto:t@e.org"},
            },
        },
    }
    inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
    inv_digest = hashlib.new(digest_algorithm, inv_bytes).hexdigest()

    obj_dir.mkdir(parents=True, exist_ok=True)
    (obj_dir / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    (obj_dir / "inventory.json").write_bytes(inv_bytes)
    (obj_dir / f"inventory.json.{digest_algorithm}").write_text(
        f"{inv_digest} inventory.json\n"
    )

    v1 = obj_dir / "v1"
    v1.mkdir()
    (v1 / "inventory.json").write_bytes(inv_bytes)
    (v1 / f"inventory.json.{digest_algorithm}").write_text(
        f"{inv_digest} inventory.json\n"
    )
    content = v1 / "content"
    content.mkdir()
    for name, data in files.items():
        (content / name).write_bytes(data)
        if include_companions:
            (content / f"{name}.ots").write_bytes(companion_bytes[name])
    for path, data in extra_content_files.items():
        fp = content / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)

    if include_extension_dir:
        ext_dir = obj_dir / "extensions" / EXTENSION_NAME
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text(
            json.dumps({"extensionName": EXTENSION_NAME})
        )
        if include_inventory_v1_ots:
            inv_sha256 = hashlib.sha256(inv_bytes).digest()
            if inventory_ots_override is not None:
                inventory_ots = inventory_ots_override
            elif pending_inventory_ots:
                inventory_ots = build_attestation(
                    inv_sha256,
                    pending_uri="https://a.pool.opentimestamps.org",
                )
            else:
                inventory_ots = build_attestation(inv_sha256, block_height=800_000)
            (ext_dir / "inventory.json.v1.ots").write_bytes(inventory_ots)

    return obj_dir
