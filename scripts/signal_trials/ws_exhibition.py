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
from veridex.signal_trials.okx_client import WSTransport, subscribe_one_signal

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

        **Evidence VALUES are absent, with NO exception** — only the field NAMES and the hash. The
        payload is public through the free read; a terminal transcript is not where it should be
        published, and the hash is what an operator compares against a receipt.

        The rule is exceptionless on purpose, and it did not start that way. Two fields were
        printed here and both were wrong to print. ``token_address`` went first. ``t0_ms`` was then
        RETAINED under a written exception claiming it was "trial metadata rather than market
        data" — a claim that was false three ways, none of which had been measured:

        * It is a HASHED EVIDENCE FIELD. ``visible_at_decision`` returns the whole
          ``CanonicalSignal`` model dump, so ``t0_ms`` is inside ``evidence_hash``. This dict was
          therefore listing ``"t0_ms"`` in ``evidence_fields`` — naming it as evidence — while
          printing its value under a docstring saying evidence values are absent.
        * It is NOT the trial-open instant. ``live.py`` is explicit that ``LiveTrial.t0_ms`` is
          ``now_ms`` and *"not from ``sig.t0_ms``… the two are usually equal and are allowed to"*
          differ; ``live.py`` has a guard that exists solely because they can. Two different
          quantities under one name.
        * It could not serve the deadline check it was justified by. Measured on one signal in one
          invocation, the two renderings were ~31.7 billion ms apart, and this script prints no
          deadline of its own.

        So the honest fix is to print neither, rather than to relabel one. **An operator who needs
        the deadline already has it:** the handoff does not capture the child's stdout, so
        ``open_live_trial.py`` prints its own summary — carrying the authoritative ``t0_ms`` AND
        ``commit_deadline_ms`` — into the same terminal, in the same run. Printing a second
        ``t0_ms`` under the same key with a different value would not add information; it would
        put a contradiction in front of the operator. The script that owns the deadline prints the
        deadline, and this one does not guess at it.

        Pinned by ``test_the_rendered_summary_publishes_NO_evidence_VALUE_without_exception``,
        which walks EVERY field of the signal rather than a hand-kept list — the exceptionless rule
        is what makes that general guard possible, and a hand-kept exception list is what produced
        both defects above.
        """
        return {
            "source": self.source,
            "evidence_hash": self.evidence_hash,
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
    """Run one exhibition. Refusals go to stderr and exit non-zero.

    **The catch is deliberately broad, and the narrow version was measurably wrong.** This
    previously caught ``(OSError, ValueError, OKXClientError)``, a tuple written by reasoning about
    which exceptions a connection failure raises. Measured against the installed ``websockets``
    15.0.1, ``InvalidURI``, ``InvalidHandshake`` and ``ConnectionClosed`` are subclasses of
    ``WebSocketException`` and of NEITHER ``OSError`` nor ``ValueError`` — so not one of the
    failures this script exists to survive was caught, and the documented refusal degraded to a
    traceback. ``import websockets`` failing adds ``ModuleNotFoundError`` to the same list.

    Re-enumerating a third-party hierarchy is the move that just failed, so this does not do it
    again. ``Exception`` leaves ``KeyboardInterrupt`` and ``SystemExit`` alone (they are
    ``BaseException``), so Ctrl-C still behaves, and the exception TYPE is printed alongside the
    message so a genuine defect stays diagnosable rather than being flattened into a bare string.

    This matters beyond tidiness: the scenario it fails on — live WS unreliable at demo time — is
    the exact scenario the plan's truth rule is written for. An operator who gets a clean
    ``refused:`` line falls back to a REST-sourced trial and says so; one who gets a traceback is
    being invited to improvise.
    """
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as error:
        print(f"refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
