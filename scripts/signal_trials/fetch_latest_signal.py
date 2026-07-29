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
_REDACTED = "<redacted>"


class SignalSelectionError(ValueError):
    """The Signal-list page cannot identify exactly one newest valid row."""


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


def redact(text: str, credentials: OKXCredentials | None) -> str:
    """Replace loaded credential values while leaving missing-variable diagnostics readable."""

    redacted = text
    if credentials is None:
        return redacted
    distinct_nonblank = dict.fromkeys(
        value
        for value in (credentials.api_key, credentials.secret_key, credentials.passphrase)
        if value.strip()
    )
    for value in sorted(distinct_nonblank, key=len, reverse=True):
        redacted = redacted.replace(value, _REDACTED)
    return redacted


def run_preflight_seam() -> Any:
    """Load the reviewed sibling credential and HTTP seams without relying on repository sys.path."""
    path = Path(__file__).with_name("run_preflight.py")
    spec = importlib_util.spec_from_file_location("_signal_trials_run_preflight", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the existing credential/HTTP seam from {path}")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "credentials_from_env", None)):
        raise RuntimeError(f"existing credential seam is absent from {path}")
    if not isinstance(getattr(module, "HttpxTransport", None), type):
        raise RuntimeError(f"existing HTTP transport seam is absent from {path}")
    return module


def httpx_transport_type() -> type[Any]:
    """Resolve the existing HTTP transport from the same production-safe sibling seam."""

    transport = run_preflight_seam().HttpxTransport
    if not isinstance(transport, type):
        raise RuntimeError("existing HTTP transport seam is not a type")
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
    seam_loader: Callable[[], Any] = run_preflight_seam,
) -> int:
    """Print one raw JSON signal on success; print only redacted diagnostics on refusal."""

    active_env = os.environ if env is None else env
    credentials: OKXCredentials | None = None
    try:
        credentials = seam_loader().credentials_from_env(active_env)
        signal = asyncio.run(
            fetch_latest_signal(
                credentials,
                chain_index=chain_index,
                client_factory=client_factory,
            )
        )
        rendered = json.dumps(signal, separators=(",", ":"), sort_keys=True)
    except Exception as error:
        print(f"refused: {redact(str(error), credentials)}", file=sys.stderr)
        return 1

    print(rendered)
    print(f"selected one REST signal for chain {chain_index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
