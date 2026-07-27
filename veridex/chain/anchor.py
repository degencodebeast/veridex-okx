"""On-chain anchor — ONE devnet Solana Memo tx per run over a manifest hash. Test-driven (T8).

NOT per-tick anchoring. Payload = SHA-256 of the run manifest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def run_manifest(
    *,
    run_id: str,
    fixture_or_window_id: str,
    agent_ids: list[str],
    action_evidence_root: str,
    score_root: str,
    proof_mode_map: dict[str, str],
    code_prompt_schema_versions: dict[str, str],
) -> dict[str, Any]:
    """Build the per-run manifest that gets hashed and anchored."""
    return {
        "run_id": run_id,
        "fixture_or_window_id": fixture_or_window_id,
        "agent_ids": agent_ids,
        "action_evidence_root": action_evidence_root,
        "score_root": score_root,
        "proof_mode_map": proof_mode_map,
        "code_prompt_schema_versions": code_prompt_schema_versions,
    }


def run_manifest_hash(manifest: dict[str, Any]) -> str:
    """SHA-256 of the canonically-serialized run manifest.

    Canonical form: json.dumps with sort_keys=True and compact separators — deterministic
    across processes and Python versions.
    """
    # NOT only determinism: the DEFAULT ensure_ascii=True / allow_nan=True here are load-bearing for
    # the receipt verifier's honesty guarantee — see ``veridex.signal_trials.receipts._rehash_reproduces``,
    # whose RecursionError-only guard is complete only while this pair cannot raise a ValueError.
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def memo_payload_for_manifest(manifest: dict[str, Any]) -> str:
    """The exact payload anchored in the Memo tx == the run-manifest hash (gate 4).

    Pins REQ-005/KILL-4: what we anchor on devnet is the manifest hash, not anything unrelated.
    """
    return run_manifest_hash(manifest)


def explorer_tx_url(signature: str | None, *, cluster: str) -> str | None:
    """Build a Solana Explorer URL for an anchored Memo transaction (WD-1 render aid).

    Returns ``None`` when ``signature`` is ``None`` (the run was not anchored — e.g. offline
    replay), so the UI can honestly show ``pending``/``not_applicable`` instead of a dead link.
    Mainnet (``"mainnet-beta"``) takes no ``?cluster`` query; devnet/testnet append it.

    Args:
        signature: The anchored transaction signature, or ``None`` when unanchored.
        cluster: The Solana cluster the anchor lives on (e.g. ``"devnet"``).

    Returns:
        The explorer URL string, or ``None`` when there is no signature to link.
    """
    if signature is None:
        return None
    base = f"https://explorer.solana.com/tx/{signature}"
    if cluster == "mainnet-beta":
        return base
    return f"{base}?cluster={cluster}"


async def anchor_memo(
    manifest_hash: str,
    *,
    rpc_url: str | None = None,
    keypair_path: str | None = None,
    client: object | None = None,
) -> str:
    """Send ONE SPL Memo tx on Solana devnet with ``data == manifest_hash``. Returns tx sig.

    This is the Phase 1 package anchor (B9, REQ-112 / AC-112 / CON-004): ONE Memo per run,
    payload == the run-manifest SHA-256 hash (gate 4). ``solders`` / ``solana`` are imported
    **lazily inside this function** so ``import veridex.chain.anchor`` works without them
    installed (offline test suite / trust-core / light-core principle).

    Args:
        manifest_hash: Exactly 64 valid hex characters — the SHA-256 run-manifest hash.
            This is verbatim the Memo ``data`` payload (gate 4: payload == manifest hash).
        rpc_url: Solana RPC endpoint override. Defaults to ``config.solana_rpc_url``
            (``https://api.devnet.solana.com``).
        keypair_path: Path to the Solana keypair JSON byte-array file. Defaults to
            ``config.require_keypair_path()``, which raises ``ValueError`` when unset.
        client: Injected async RPC client duck-typed as
            ``solana.rpc.async_api.AsyncClient``. When supplied the caller owns the
            client lifecycle (``close`` is **not** called by this function). When
            ``None`` (default) a real ``AsyncClient`` is created, used, and closed.

    Returns:
        The transaction signature as a string.

    Raises:
        ValueError: If ``manifest_hash`` is not exactly 64 valid hex characters, or if
            ``SOLANA_KEYPAIR_PATH`` is unset and ``keypair_path`` is not provided.
    """
    # Validate before any I/O — ValueError is raised immediately, no network involved.
    if len(manifest_hash) != 64:
        raise ValueError(
            f"manifest_hash must be exactly 64 hex characters, got {len(manifest_hash)}: {manifest_hash!r}"
        )
    try:
        bytes.fromhex(manifest_hash)
    except ValueError:
        raise ValueError(f"manifest_hash is not valid hex: {manifest_hash!r}") from None

    # Lazy imports — solders/solana are kept outside the module-load path so that
    # ``import veridex.chain.anchor`` remains lightweight and SDK-free.
    import json as _json
    from pathlib import Path as _Path
    from typing import Any as _Any

    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction, VersionedTransaction

    from veridex.config import get_settings
    from veridex.config import require_keypair_path as _require_kp

    cfg = get_settings()
    url = rpc_url or cfg.solana_rpc_url
    kp_path = keypair_path or _require_kp(cfg)

    kp: _Any = Keypair.from_bytes(bytes(_json.loads(_Path(kp_path).read_text())))

    _MEMO_PROGRAM: _Any = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    ix: _Any = Instruction(
        program_id=_MEMO_PROGRAM,
        data=manifest_hash.encode("utf-8"),
        accounts=[AccountMeta(pubkey=kp.pubkey(), is_signer=True, is_writable=False)],
    )

    _owns_client = client is None
    rpc: _Any = AsyncClient(url) if _owns_client else client

    try:
        blockhash: _Any = (await rpc.get_latest_blockhash()).value.blockhash
        msg: _Any = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
        tx: _Any = Transaction([kp], msg, blockhash)
        vtx: _Any = VersionedTransaction.from_legacy(tx)
        sig: _Any = (await rpc.send_transaction(vtx)).value
        await rpc.confirm_transaction(sig, commitment=Confirmed)
    finally:
        if _owns_client:
            await rpc.close()

    return str(sig)
