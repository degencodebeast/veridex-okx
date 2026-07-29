#!/usr/bin/env python3
"""Fetch exactly one unmodified OKX v6 Signal-list row for the REST live-trial handoff."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Protocol

from veridex.signal_trials.okx_client import (
    OKXCredentials,
    OKXMarketClient,
    SignalFilters,
    SignalPage,
)

DEFAULT_CHAIN_INDEX = "196"
REQUEST_TIMEOUT_SECONDS = 20.0
_CREDENTIAL_VARS = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
_REDACTED = "<redacted>"


class SignalSelectionError(ValueError):
    """The Signal-list page cannot identify exactly one newest valid row."""


class MissingCredentialError(RuntimeError):
    """One or more required OKX credential variables are missing or blank."""


class SignalClient(Protocol):
    """The only market-client operation the producer may perform."""

    async def list_signals(self, filters: SignalFilters) -> SignalPage: ...


ClientFactory = Callable[[OKXCredentials], Any]


def select_unique_latest(rows: Sequence[object]) -> dict[str, Any]:
    """Return the sole row at the maximum timestamp, refusing every ambiguous or malformed page."""

    if not rows:
        raise SignalSelectionError("signal list returned zero rows")

    timestamped: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SignalSelectionError(f"signal row {index} must be a JSON object")
        raw_timestamp = row.get("timestamp")
        if not isinstance(raw_timestamp, str) or not raw_timestamp.isdigit():
            raise SignalSelectionError(f"signal row {index} has a malformed timestamp")
        timestamped.append((int(raw_timestamp), row))

    maximum = max(timestamp for timestamp, _ in timestamped)
    newest = [row for timestamp, row in timestamped if timestamp == maximum]
    if len(newest) != 1:
        raise SignalSelectionError("signal list has a tie for the latest timestamp")
    return newest[0]


def credentials_from_env(env: Mapping[str, str]) -> OKXCredentials:
    """Build credentials after checking every required variable without echoing any value."""

    missing = [name for name in _CREDENTIAL_VARS if not env.get(name, "").strip()]
    if missing:
        raise MissingCredentialError(f"missing or blank OKX credentials: {', '.join(missing)}")
    return OKXCredentials(
        api_key=env["OKX_API_KEY"],
        secret_key=env["OKX_SECRET_KEY"],
        passphrase=env["OKX_PASSPHRASE"],
        base_url=env.get("OKX_BASE_URL", "https://web3.okx.com"),
    )


def redact(text: str, env: Mapping[str, str]) -> str:
    """Replace credential values in a diagnostic while leaving variable names readable."""

    redacted = text
    for name in _CREDENTIAL_VARS:
        value = env.get(name, "")
        if value.strip():
            redacted = redacted.replace(value, _REDACTED)
    return redacted


def httpx_transport_type() -> type[Any]:
    """Load the reviewed sibling HTTP seam by file so by-path container execution remains valid."""

    path = Path(__file__).with_name("run_preflight.py")
    spec = importlib_util.spec_from_file_location("_signal_trials_run_preflight", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the existing HTTP transport seam from {path}")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transport = getattr(module, "HttpxTransport", None)
    if not isinstance(transport, type):
        raise RuntimeError(f"existing HTTP transport seam is absent from {path}")
    return transport


@asynccontextmanager
async def live_client(credentials: OKXCredentials) -> AsyncIterator[SignalClient]:
    """Yield the existing authenticated OKX REST client; importing this module performs no request."""

    import httpx

    async with httpx.AsyncClient(
        base_url=credentials.base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as http:
        yield OKXMarketClient(httpx_transport_type()(http), credentials)


async def fetch_latest_signal(
    credentials: OKXCredentials,
    *,
    chain_index: str = DEFAULT_CHAIN_INDEX,
    client_factory: ClientFactory = live_client,
) -> dict[str, Any]:
    """Fetch one Signal-list page and deterministically select its unique newest row."""

    async with client_factory(credentials) as client:
        page = await client.list_signals(SignalFilters(chain_index=chain_index))
    return select_unique_latest(page.signals)


def main(
    *,
    env: Mapping[str, str] | None = None,
    chain_index: str = DEFAULT_CHAIN_INDEX,
    client_factory: ClientFactory = live_client,
) -> int:
    """Print one raw JSON signal on success; print only redacted diagnostics on refusal."""

    active_env = os.environ if env is None else env
    try:
        credentials = credentials_from_env(active_env)
        signal = asyncio.run(
            fetch_latest_signal(
                credentials,
                chain_index=chain_index,
                client_factory=client_factory,
            )
        )
        rendered = json.dumps(signal, separators=(",", ":"), sort_keys=True)
    except Exception as error:
        print(f"refused: {redact(str(error), active_env)}", file=sys.stderr)
        return 1

    print(rendered)
    print(f"selected one REST signal for chain {chain_index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
