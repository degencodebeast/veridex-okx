#!/usr/bin/env python3
"""Exhibit ONE live signal arriving over the OKX WS push channel, and open a trial over it.

    python scripts/signal_trials/ws_exhibition.py --ws-url wss://... --chain-index 501 \
        --data-dir ./data [--dry-run]

The composition is script-level and deliberately so: this script subscribes, takes the first
pushed signal, and hands the RAW signal to the payments-lane ``open_live_trial.py`` on stdin. It
edits nothing that lane owns and imports nothing from it. ``open_live_trial.py`` does its own
canonicalization, so the leakage boundary stays in ``challenge_spec.normalize_signal`` where a
reviewer of that boundary will look for it, rather than being quietly relocated here.

**The source is a recorded fact, not an operator's assertion.**

The frozen plan's truth rule reads: *if live WS is unreliable at demo time, the demo says
"REST-sourced live trial" — REST is never labeled a WS arrival.* The obligation that rule creates
for this script is structural rather than editorial, so it is met structurally:

* There is **no ``--source`` flag and no ``source=`` parameter**, on any surface. The one thing an
  operator under demo-time pressure could do to produce a false label is not offered.
* ``ws`` is a module constant emitted on a path that can only be reached by having consumed a real
  WS push frame, because :func:`~veridex.signal_trials.okx_client.subscribe_one_signal` refuses
  every frame that is not a push on the signal channel — including a REST envelope replayed onto
  the WS wire, which is otherwise indistinguishable by payload alone.
* :class:`ExhibitionSummary` derives ``source`` rather than storing it, so no construction path
  writes it and no caller can reassign it.

Consequently there is no code path in this program that turns a REST arrival into a WS-labelled
record. If WS is unreliable at demo time the honest fallback is to run ``open_live_trial.py``
directly with ``--source rest``, which labels it truthfully.

**This script reads no credential.** ``subscribe_one_signal`` authenticates nothing — the WS
handshake belongs to the transport — so nothing here touches ``OKX_API_KEY`` or its siblings. A
process that never reads a secret cannot leak one into a demo transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from veridex.signal_trials.challenge_spec import CanonicalSignal, evidence_hash, normalize_signal
from veridex.signal_trials.okx_client import OKXClientError, WSTransport, subscribe_one_signal

#: The transport tag for everything this script produces. A constant, never an argument: see the
#: module docstring. A mutation of this value is killed by
#: `test_the_handoff_labels_the_signal_ws_and_there_is_no_argv_that_says_rest`.
#:
#: `Final[Literal["ws"]]` rather than `str`, and that is the truth rule expressed in the type
#: system: the declared type of this module's source tag admits exactly one value, so a type check
#: rejects any edit that widens it — including the one that would let `"rest"` be written here.
WS_SOURCE: Final[Literal["ws"]] = "ws"

#: The payments-lane script this one composes with. Resolved as a path and never imported, so this
#: script holds no code-level coupling to a module another lane owns.
OPEN_LIVE_TRIAL_SCRIPT = Path(__file__).resolve().parent / "open_live_trial.py"


class Handoff(Protocol):
    """How the open-trial script gets run. Injected so tests never spawn a process."""

    def __call__(self, argv: list[str], stdin_text: str) -> int: ...


@dataclass(frozen=True)
class ExhibitionSummary:
    """What one exhibition did — the operator-facing record of a WS arrival.

    ``source`` is a property, not a field. Frozen would already block reassignment, but a field
    would still be settable AT CONSTRUCTION, which is enough for a future caller to record a WS
    arrival as something else. Deriving it means the value has exactly one origin.
    """

    evidence_hash: str
    t0_ms: int
    evidence_fields: tuple[str, ...]
    handoff_status: int
    dry_run: bool

    @property
    def source(self) -> Literal["ws"]:
        """Where this signal came from. Always ``ws``: nothing else can reach this class."""
        return WS_SOURCE

    @property
    def published(self) -> bool:
        """Whether a live trial actually exists now.

        False on a non-zero handoff status, and false for a dry run. A summary that claimed a
        publication the open-trial script refused would be the demo asserting a live trial exists
        when none does.
        """
        return self.handoff_status == 0 and not self.dry_run

    def render(self) -> dict[str, Any]:
        """The JSON-safe view printed to stdout.

        Market-data evidence VALUES are deliberately absent — only the field NAMES and the hash —
        matching ``open_live_trial.py``'s own rendering. The payload is public through the free
        read; a terminal transcript is not where it should be published, and the hash is what an
        operator compares against a receipt.

        ``t0_ms`` is the one exception and is TRIAL METADATA rather than market data: it is when
        the trial opens, ``open_live_trial.py`` prints it for the same reason, and an operator
        cannot check a deadline without it. ``token_address`` used to be rendered here and is not
        any more — it is market data, so printing it made the sentence above untrue. The line
        between the two is what ``test_the_rendered_summary_carries_evidence_FIELD_NAMES_but_not_
        evidence_VALUES`` pins.
        """
        return {
            "source": self.source,
            "evidence_hash": self.evidence_hash,
            "t0_ms": self.t0_ms,
            "evidence_fields": list(self.evidence_fields),
            "handoff_status": self.handoff_status,
            "dry_run": self.dry_run,
            "published": self.published,
        }


def build_handoff_argv(*, data_dir: str, dry_run: bool) -> list[str]:
    """The argv for the open-trial script. Carries ``--source ws`` and no way to say otherwise."""
    argv = [str(OPEN_LIVE_TRIAL_SCRIPT), "--data-dir", data_dir, "--source", WS_SOURCE]
    if dry_run:
        argv.append("--dry-run")
    return argv


def _subprocess_handoff(argv: list[str], stdin_text: str) -> int:
    """Spawn the open-trial script under this same interpreter and return its exit status."""
    # No shell, and every element of `argv` is built by `build_handoff_argv` from this module's own
    # constants plus `--data-dir` — there is no operator-supplied string that could become a word.
    completed = subprocess.run(
        [sys.executable, *argv],
        input=stdin_text,
        text=True,
        check=False,
    )
    return completed.returncode


async def exhibit_one_signal(
    ws_transport: WSTransport,
    chain_index: str,
    *,
    data_dir: str,
    dry_run: bool = False,
    handoff: Handoff = _subprocess_handoff,
) -> ExhibitionSummary:
    """Subscribe once, normalize the arrival as ``ws``, and hand it to the open-trial script.

    ``normalize_signal`` runs here as well as inside the handoff, and the duplication is the point:
    a signal that cannot be canonicalized must be refused BEFORE anything is published, so a
    malformed arrival costs the exhibition rather than producing a half-opened trial. Its refusal
    propagates — there is no partial-success path — and the handoff never fires.

    Raises:
        OKXClientError: the WS conversation failed or the frame was not a signal push.
        ValueError: the arrived signal failed canonicalization (including leakage refusals).
    """
    raw = await subscribe_one_signal(ws_transport, chain_index)
    signal: CanonicalSignal = normalize_signal(raw, WS_SOURCE)

    status = handoff(
        build_handoff_argv(data_dir=data_dir, dry_run=dry_run),
        json.dumps(raw, separators=(",", ":"), sort_keys=True),
    )
    return ExhibitionSummary(
        evidence_hash=evidence_hash(signal),
        t0_ms=signal.t0_ms,
        evidence_fields=tuple(sorted(signal.model_dump())),
        handoff_status=status,
        dry_run=dry_run,
    )


def exit_status(summary: ExhibitionSummary) -> int:
    """The process status for ``summary``. Non-zero unless a trial really was opened.

    A dry run is a success (nothing was meant to be published), so it maps to 0; a refused handoff
    does not, so a shell driving this cannot read a printed summary as a published trial.
    """
    return 0 if summary.handoff_status == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Note the absence of any option that names a transport source — see the docstring."""
    parser = argparse.ArgumentParser(
        description=(
            "Exhibit one WS-arriving OKX signal and open a live trial over it. "
            "Signals exhibited by this script are recorded as WS arrivals because they are; "
            "there is no option to label them otherwise."
        ),
    )
    parser.add_argument("--ws-url", required=True, help="WS endpoint to connect to.")
    parser.add_argument("--chain-index", required=True, help="Chain to subscribe the signal channel for.")
    parser.add_argument("--data-dir", required=True, help="Signal-trials data directory; the API must serve it.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render what would be published and write nothing.",
    )
    return parser


class _WebsocketsTransport:
    """Adapter over a `websockets` client connection, satisfying :class:`WSTransport`.

    Exists to keep the byte/str ambiguity at the edge: `recv` can yield either, and the frame
    parsing upstream is written against exactly one input type. Only ``main`` constructs this —
    no test touches it, and nothing in this module opens a connection on any other path.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def send(self, message: str) -> None:
        await self._connection.send(message)

    async def recv(self) -> str:
        frame = await self._connection.recv()
        return frame.decode("utf-8") if isinstance(frame, bytes) else str(frame)

    async def close(self) -> None:
        await self._connection.close()


async def _run(args: argparse.Namespace) -> int:
    """Connect, exhibit once, print the summary. The only place a real socket is opened."""
    import websockets

    async with websockets.connect(args.ws_url) as connection:
        summary = await exhibit_one_signal(
            _WebsocketsTransport(connection),
            args.chain_index,
            data_dir=args.data_dir,
            dry_run=args.dry_run,
        )
    print(json.dumps(summary.render(), indent=2, sort_keys=True))
    return exit_status(summary)


def main(argv: list[str] | None = None) -> int:
    """Run one exhibition. Refusals go to stderr and exit non-zero."""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, OKXClientError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
