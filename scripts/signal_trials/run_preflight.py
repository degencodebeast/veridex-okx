"""Operator entrypoint for the (chain x bar) matrix preflight — GATE B, live, operator-run.

    OKX_API_KEY=… OKX_SECRET_KEY=… OKX_PASSPHRASE=… \
        .venv/bin/python scripts/signal_trials/run_preflight.py --out preflight_result.json

This is the ONLY place in the codebase that constructs a real OKX transport. Everything it
orchestrates — ``run_matrix_probe``, ``select_combo``, ``write_preflight_result`` — is exercised in
tests against in-memory fakes, so importing this module must never read a credential, open a
connection, or run a probe. It does not: every one of those happens inside ``main``.

Three operator-facing properties:

**It prints all four counts and their rejection reasons**, not just the winner. "0 eligible" and
"0 eligible because nothing settled" are different findings, and only the second one says that
retention rather than density is the constraint — which is the answer §5.1 wants from this run.

**It always leaves an artifact, and the artifact says which kind of run it was.** A completed probe
writes ``probe_status="completed"``; a failed one writes ``probe_status="failed"`` with a reason and
no counts. Absence of the file means the run never reached the writer. A later stage reading
``preflight_result.json`` can tell the three apart without seeing this terminal.

**Credential values never reach a rendered string.** ``OKXCredentials`` redacts its own ``repr``,
and every failure reason is passed through ``redact`` before it is printed or written — an upstream
error message that echoed a key would otherwise be committed into an artifact an operator attaches
to a review.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from veridex.signal_trials.okx_client import OKXCredentials, OKXMarketClient, SignalFilters
from veridex.signal_trials.preflight import (
    COMBO_ORDER,
    ComboSelection,
    MatrixProbeResult,
    run_matrix_probe,
    select_combo,
    write_preflight_failure,
    write_preflight_result,
)

REDACTED = "***"
DEFAULT_BASE_URL = "https://web3.okx.com"
REQUEST_TIMEOUT_SECONDS = 30.0

_CREDENTIAL_VARS: tuple[str, ...] = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")


class MissingCredentialError(RuntimeError):
    """A required OKX credential is absent from the environment.

    Names the VARIABLE and never its value: this message is printed to a terminal an operator may
    screenshot into a review.
    """


class HttpxTransport:
    """The concrete OKX HTTP seam, satisfying ``okx_client.Transport`` structurally.

    ``params`` is passed straight through as a dict so httpx serializes it in iteration order — the
    OKX signature covers the query string, so a transport that reordered it would produce a URL the
    ``OK-ACCESS-SIGN`` no longer matches.

    ``raise_for_status`` handles transport-level failures; OKX signals APPLICATION errors inside an
    HTTP 200 envelope, and those are the client's business, not this transport's.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json_body: object | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = await self._http.request(method, path, params=params, json=json_body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(f"OKX response must be a JSON object, got {type(payload).__name__}")
        return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_preflight",
        description="Probe the frozen (chain x bar) matrix and write preflight_result.json.",
    )
    parser.add_argument("--out", type=Path, required=True, help="destination for preflight_result.json")
    parser.add_argument(
        "--min-trials",
        dest="min_trials",
        type=int,
        default=40,
        help="qualification threshold; the frozen season gate is 40 (default: %(default)s)",
    )
    parser.add_argument(
        "--cooldown-ms",
        dest="cooldown_ms",
        type=int,
        default=14_400_000,
        help="same-token dedup window in ms; frozen at 4h (default: %(default)s)",
    )
    parser.add_argument(
        "--horizon-ms",
        dest="horizon_ms",
        type=int,
        default=3_600_000,
        help="ranked settlement horizon in ms; frozen at 1h (default: %(default)s)",
    )
    return parser.parse_args(argv)


def credentials_from_env(env: Mapping[str, str]) -> OKXCredentials:
    """Read the OKX credentials, naming any that are missing or blank.

    Raises:
        MissingCredentialError: Listing the missing VARIABLE NAMES only.
    """
    missing = [name for name in _CREDENTIAL_VARS if not env.get(name, "").strip()]
    if missing:
        raise MissingCredentialError(f"missing or blank OKX credentials in the environment: {', '.join(missing)}")
    return OKXCredentials(
        api_key=env["OKX_API_KEY"],
        secret_key=env["OKX_SECRET_KEY"],
        passphrase=env["OKX_PASSPHRASE"],
        base_url=env.get("OKX_BASE_URL", DEFAULT_BASE_URL),
    )


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every credential value in ``text``.

    Blank and whitespace-only entries are skipped: replacing the empty string would rewrite every
    character boundary in the message, which destroys the diagnostic while looking like redaction.
    """
    redacted = text
    for secret in secrets:
        if secret.strip():
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def filters_by_chain() -> dict[str, SignalFilters]:
    """The predeclared MVP dataset filters (§5.1), one entry per chain in the frozen matrix."""
    return {chain_index: SignalFilters(chain_index=chain_index) for chain_index, _ in COMBO_ORDER}


def render_summary(sel: ComboSelection, result: MatrixProbeResult) -> str:
    """All four combos with their counts and rejection reasons, then the decision."""
    by_combo = {(count.chain_index, count.bar): count for count in result.counts}
    lines = [
        "(chain x bar) matrix preflight",
        f"direction_semantics_confirmed: {result.direction_semantics_confirmed}",
        "",
    ]
    for chain_index, bar in COMBO_ORDER:
        count = by_combo.get((chain_index, bar))
        if count is None:
            lines.append(f"  chain {chain_index} bar {bar}: MISSING FROM MATRIX")
            continue
        rendered = " ".join(f"{reason}={n}" for reason, n in sorted(count.rejection_reasons.items())) or "none"
        lines.append(
            f"  chain {chain_index} bar {bar}: eligible_settleable={count.eligible_settleable}  rejections: {rendered}"
        )
    lines.extend(
        [
            "",
            f"season_status: {sel.season_status}",
            f"chain_index:   {sel.chain_index}",
            f"bar:           {sel.bar}",
        ]
    )
    return "\n".join(lines)


async def _probe(creds: OKXCredentials, args: argparse.Namespace) -> MatrixProbeResult:
    """Construct the live transport and run the probe. The only network path in this repository."""
    async with httpx.AsyncClient(base_url=creds.base_url, timeout=REQUEST_TIMEOUT_SECONDS) as http:
        client = OKXMarketClient(HttpxTransport(http), creds)
        return await run_matrix_probe(
            client,
            filters_by_chain(),
            cooldown_ms=args.cooldown_ms,
            horizon_ms=args.horizon_ms,
            min_trials=args.min_trials,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        creds = credentials_from_env(os.environ)
    except MissingCredentialError as exc:
        # No artifact: nothing was probed, so ABSENCE is the honest state. Writing a failure record
        # here would claim a probe was attempted.
        print(f"preflight aborted before any request: {exc}", file=sys.stderr)
        return 2

    secrets = [creds.api_key, creds.secret_key, creds.passphrase]
    try:
        result = asyncio.run(_probe(creds, args))
        selection = select_combo(result, min_trials=args.min_trials)
    except Exception as exc:  # noqa: BLE001 - every failure must reach the artifact, not just known ones
        reason = redact(f"{type(exc).__name__}: {exc}", secrets)
        write_preflight_failure(reason, args.out)
        print(f"preflight FAILED: {reason}", file=sys.stderr)
        print(f"failure artifact written to {args.out}", file=sys.stderr)
        return 1

    print(render_summary(selection, result))
    write_preflight_result(selection, result, args.out)
    print(f"\npreflight artifact written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
