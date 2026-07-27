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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from veridex.signal_trials.challenge_spec import CanonicalSignal, evidence_hash, normalize_signal
from veridex.signal_trials.okx_client import WS_SIGNAL_CHANNEL, WSTransport, subscribe_one_signal

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

#: The one sentence an operator must see whenever a trial MIGHT exist. Shared by both routes that
#: can reach that state — an exception raised after the handoff began, and a nonzero child status —
#: because they are the same situation and a reader who learns the phrase from one must recognise
#: it from the other. Two spellings of one warning is how the two paths drift apart.
INDETERMINATE_WARNING = (
    "a live trial MAY ALREADY EXIST for this signal; check the data dir before opening a REST-sourced one"
)

#: Receive bound, in seconds. `WSTransport` states that a real implementation MUST set one, because
#: `_ws_converse` loops with no frame budget. Generous rather than tight: a signal push is a market
#: event and may legitimately be minutes away, so this is the "the socket has gone quiet and the
#: demo needs an answer" bound, not a latency SLA. An expiry becomes a clean `refused:` line.
RECV_TIMEOUT_S = 90.0

#: Handshake and close bounds, passed explicitly rather than left to library defaults — the same
#: choice `scripts/smoke_public_ws.py::_default_connect` makes.
OPEN_TIMEOUT_S = 10.0
CLOSE_TIMEOUT_S = 5.0

#: How the connection is opened. Injected so tests never reach a socket — see `_run`.
ConnectFactory = Callable[[str], Any]


class Handoff(Protocol):
    """How the open-trial script gets run. Injected so tests never spawn a process."""

    def __call__(self, argv: list[str], stdin_text: str) -> int: ...


class PostHandoffError(RuntimeError):
    """A failure that happened AFTER the open-trial handoff fired — so a trial MAY EXIST.

    This type exists because of what ``main``'s ``refused:`` line instructs an operator to do. The
    module docstring's fallback is "say REST-sourced and open a REST trial instead", and that is
    correct advice for a failure BEFORE the handoff, when nothing was published. Applied to a
    failure AFTER it, the same advice produces a SECOND live trial for one signal — one labelled
    ``ws``, one labelled ``rest``. A single word in a stderr line is the difference.

    The window is small but it is not empty: the summary construction, the connection ``__aexit__``
    and the final ``print`` all run after the child has been spawned. Low probability is a reason
    to make the message precise, not a reason to let it be wrong — the guarantee the docstring
    makes about what ``refused:`` MEANS is the thing being kept honest here.

    ``RuntimeError`` rather than a bare ``Exception`` so it is still caught by ``main``'s broad
    handler with no special-casing needed for the exit status; only the WORDING differs.
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


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
        """Whether a live trial DEFINITELY exists now.

        True only for a non-dry-run handoff that returned 0. A summary claiming a publication the
        open-trial script refused would be the demo asserting a live trial exists when none does.
        """
        return self.handoff_status == 0 and not self.dry_run

    @property
    def publication_indeterminate(self) -> bool:
        """Whether a trial MIGHT exist despite the handoff failing — the false-NEGATIVE case.

        ``published`` guards one direction: never claim a publication that did not happen. That
        left the inverse unguarded, and the inverse is the one that produces a second trial.

        **A nonzero child status does not mean nothing was written.** ``open_live_trial.py``
        publishes durably FIRST and prints its summary AFTERWARDS, so a terminal-output failure —
        a closed pipe, a full disk — leaves a real trial on disk while the child exits nonzero.
        Reported as ``published: false``, that tells an operator to open a REST-sourced fallback
        for a signal that already has a WS-sourced trial: one signal, two live trials, contradictory
        source labels. Exactly what the truth rule exists to prevent.

        So the honest report has THREE states, not two: definitely published, definitely not
        (nothing was ever spawned, or it was a dry run), and INDETERMINATE — the child began and
        did not cleanly succeed, and only the data directory can settle it.
        """
        return self.handoff_status != 0 and not self.dry_run

    def render(self) -> dict[str, Any]:
        """The JSON-safe view printed to stdout.

        **Evidence VALUES are absent, with NO exception** — only the field NAMES and the hash. The
        payload is public through the free read; a terminal transcript is not where it should be
        published, and the hash is what an operator compares against a receipt.

        **The "no exception" is the load-bearing half.** Two fields were printed here and both were
        wrong to print; the second, ``t0_ms``, was RETAINED for a round under a written exception
        that read plausibly and was false in three ways, none of which had been measured. Anyone
        reaching for a fresh exception should assume theirs reads just as plausibly. The rule is
        also what makes the guard general: ``test_the_rendered_summary_publishes_NO_evidence_VALUE_
        without_exception`` walks EVERY field of the signal, which a hand-kept exception list would
        make impossible.

        An operator who needs the deadline already has it — the handoff does not capture the
        child's stdout, so ``open_live_trial.py`` prints the authoritative ``t0_ms`` and
        ``commit_deadline_ms`` into the same terminal, in the same run.

        Full history — the three false claims, the measurements that refuted them, and why
        dropping beat relabelling — is in PKT-EVID-H2-5-MUTATION-3f9a4c7.txt SECTION 3, the commit
        message, and the test's own docstring.
        """
        return {
            "source": self.source,
            "evidence_hash": self.evidence_hash,
            "evidence_fields": list(self.evidence_fields),
            "handoff_status": self.handoff_status,
            "dry_run": self.dry_run,
            "published": self.published,
            "publication_indeterminate": self.publication_indeterminate,
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
    handoff: Handoff | None = None,
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
    # Resolved at CALL time for the same reason `_WebsocketsTransport._recv_timeout` is:
    # `handoff: Handoff = _subprocess_handoff` in the signature FREEZES the function object at
    # import, so rebinding `module._subprocess_handoff` is silently ignored and the real
    # subprocess runs anyway. Measured — a test that patched the module attribute spawned
    # `open_live_trial.py` for real while reporting that its fake had been used.
    run_handoff: Handoff = _subprocess_handoff if handoff is None else handoff

    raw = await subscribe_one_signal(ws_transport, chain_index)
    signal: CanonicalSignal = normalize_signal(raw, WS_SOURCE)

    # Both prepared BEFORE the boundary, so that a failure to BUILD the call is still an ordinary
    # pre-handoff refusal — nothing has been spawned yet at that point.
    argv = build_handoff_argv(data_dir=data_dir, dry_run=dry_run)
    payload = json.dumps(raw, separators=(",", ":"), sort_keys=True)

    # THE BOUNDARY CONTAINS THE CALL, and that is the correction. It previously began on the line
    # AFTER `run_handoff(...)`, so an exception raised BY the handoff itself — a broken pipe while
    # writing the child's stdin, say — escaped as an ordinary pre-handoff refusal. The comment
    # naming the hazard sat one line below the statement that creates it.
    #
    # `dry_run` is the only exemption, and it is a real one: the child is invoked with
    # `--dry-run`, writes nothing, so a failure there genuinely leaves nothing behind.
    try:
        status = run_handoff(argv, payload)
        return ExhibitionSummary(
            evidence_hash=evidence_hash(signal),
            evidence_fields=tuple(sorted(signal.model_dump())),
            handoff_status=status,
            dry_run=dry_run,
        )
    except Exception as error:
        if dry_run:
            raise
        raise PostHandoffError(error) from error


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

    Exists for two reasons. First, to keep the byte/str ambiguity at the edge: `recv` can yield
    either, and the frame parsing upstream is written against exactly one input type.

    Second, and this is the load-bearing one, **to honour the receive-timeout MUST that
    ``WSTransport``'s docstring states.** ``_ws_converse`` is a ``while True`` with no frame budget
    BY DESIGN — the protocol layer cannot know what a reasonable wait is, so the whole safety of
    that loop rests on the transport imposing a bound. This class is the repo's ONLY real
    implementation of that Protocol, and it previously did not impose one: `recv` was a bare
    ``await``, which made the module's own stated MUST false at the only place it applied.

    Why a bare await is not enough, measured rather than assumed: websockets 15.0.1's
    ``ClientConnection.recv`` has signature ``(self, decode)`` — **there is no timeout parameter**,
    so the bound can only come from the caller. ``connect()``'s ``open_timeout`` bounds the
    handshake and ``ping_interval``/``ping_timeout`` detect a DEAD peer, but a peer that is alive
    and answering Pings while pushing no signal blocks ``recv`` with nothing to interrupt it. That
    is exactly the heartbeats-forever case, and on the H6.1 demo path it is a hung terminal in
    front of an audience — the one state worse than the traceback ``main`` was fixed to avoid.

    ``asyncio.wait_for`` is the same mechanism ``scripts/smoke_public_ws.py`` already uses for its
    own WS reads; this is an in-repo pattern, not a novel demand. The expiry surfaces as
    ``TimeoutError``, which ``main``'s existing ``except Exception`` renders as a clean ``refused:``
    line — the documented operator behaviour.
    """

    def __init__(self, connection: Any, *, recv_timeout: float | None = None) -> None:
        self._connection = connection
        # Resolved at CALL time, not as a default argument. `recv_timeout=RECV_TIMEOUT_S` in the
        # signature would freeze the constant at import, so an operator override or a test that
        # rebinds the module attribute would be silently ignored — the default would already have
        # been captured. Measured: a test that set it to 0.05 waited the full 90s default.
        self._recv_timeout = RECV_TIMEOUT_S if recv_timeout is None else recv_timeout

    async def send(self, message: str) -> None:
        await self._connection.send(message)

    async def recv(self) -> str:
        try:
            frame = await asyncio.wait_for(self._connection.recv(), timeout=self._recv_timeout)
        except TimeoutError as error:
            # `str(TimeoutError())` is the EMPTY STRING, so `main`'s formatter rendered the bare
            # `refused: TimeoutError: ` with nothing after the colon — a refusal that tells an
            # operator only that something did not happen. The detail is added HERE, at the one
            # place that knows the bound and what waiting on it meant.
            raise TimeoutError(
                f"no WS frame arrived within {self._recv_timeout:g}s on the "
                f"{WS_SIGNAL_CHANNEL!r} channel; the socket is open but the peer is pushing "
                "nothing. Fall back to a REST-sourced trial and label it rest."
            ) from error
        return frame.decode("utf-8") if isinstance(frame, bytes) else str(frame)

    async def close(self) -> None:
        await self._connection.close()


def _default_connect(ws_url: str) -> Any:
    """Open the real connection. The ONLY place in this module that opens a socket.

    Bounds the handshake explicitly rather than relying on defaults, matching
    ``scripts/smoke_public_ws.py``'s ``_default_connect``.
    """
    import websockets

    return websockets.connect(ws_url, open_timeout=OPEN_TIMEOUT_S, close_timeout=CLOSE_TIMEOUT_S)


async def _run(args: argparse.Namespace, *, connect_factory: ConnectFactory | None = None) -> int:
    """Connect, exhibit once, print the summary.

    ``connect_factory`` is INJECTED for the same reason ``handoff`` is, and the omission was a real
    gap rather than a style point. While the connection was resolved by a function-local
    ``import websockets``, the only way to intercept it was global ``sys.modules`` state — so
    "no real socket is opened" rested on every future test remembering to patch it, with no
    structural backstop, and three decision points here were disclosed as unbindable when they were
    merely un-injected. ``scripts/smoke_public_ws.py`` already ships this seam; H2.5 had not
    picked it up.

    **Resolved at CALL time, and this is the THIRD seam in this file to need that correction.**
    ``connect_factory: ConnectFactory = _default_connect`` in the signature freezes the function
    object at import, so rebinding ``module._default_connect`` is silently ignored — and unlike the
    other two, what escapes is not a stale value but the REAL LIBRARY: a genuine resolver call
    (``gaierror``), from the seam whose whole purpose is that nothing reaches a socket.

    The inconsistency was the trap, more than the individual defect. Two seams here taught that
    module-attribute rebinding is the idiom that works; a third silently did not, and it was the
    one that reaches the network. One rule for all three is worth more than a frozen default.
    """
    connect = _default_connect if connect_factory is None else connect_factory
    summary: ExhibitionSummary | None = None
    try:
        async with connect(args.ws_url) as connection:
            summary = await exhibit_one_signal(
                _WebsocketsTransport(connection),
                args.chain_index,
                data_dir=args.data_dir,
                dry_run=args.dry_run,
            )
    except Exception as error:
        # `summary` is bound only once `exhibit_one_signal` has RETURNED, which it cannot do before
        # the handoff has fired. So a live `summary` here means the failure is the connection
        # teardown — inside the may-have-published window — rather than the connect or the
        # exhibition itself. `PostHandoffError` passes through unchanged; it already says so.
        if summary is not None and not isinstance(error, PostHandoffError):
            raise PostHandoffError(error) from error
        raise

    # ONE BOUNDARY OVER THE WHOLE TAIL, rather than one per statement, and the shape is the fix.
    #
    # Twice the correction enclosed everything that EXISTED and left what it ADDED outside: round 1
    # wrapped the summary construction and left the handoff call out; round 2 wrapped the handoff
    # call and left this warning print out. Wrapping statements individually is what produced that,
    # because each new statement is a new decision nobody is prompted to make. Enclosing the REGION
    # means anything added here later is inside by default, and the enumeration in
    # `h25_postboundary.py` reports the region rather than the known gaps.
    #
    # Everything below runs after the child has started AND returned, so a failure here is
    # post-handoff by construction — including `exit_status`, which the reviewer did not name and
    # which has no business being reasoned about individually.
    try:
        print(json.dumps(summary.render(), indent=2, sort_keys=True))

        # THE NONZERO-RETURN PATH. It never raises, so no exception handler could ever have covered
        # the CHILD's failure — the child simply reports it, having published BEFORE it printed. The
        # operator gets the same sentence here as on the raising path, from the same constant.
        if summary.publication_indeterminate:
            print(
                f"handoff returned {summary.handoff_status} AFTER starting -- {INDETERMINATE_WARNING}",
                file=sys.stderr,
            )
        return exit_status(summary)
    except Exception as error:
        raise PostHandoffError(error) from error


def main(argv: list[str] | None = None, *, connect_factory: ConnectFactory | None = None) -> int:
    """Run one exhibition. Refusals go to stderr and exit non-zero.

    ``connect_factory`` is passed straight through to ``_run``. Without it ``main`` had NO connect
    seam at all, so its two returns were reachable only by faking ``sys.modules`` — the exact
    dependency the seam was introduced to remove. A test name in this file certified that removal
    while two tests in the same file still needed the fake, and needed it precisely BECAUSE ``main``
    had no seam. A name asserting a file-wide absence its own file refutes is worse than no name.

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
        return asyncio.run(_run(args, connect_factory=connect_factory))
    except PostHandoffError as error:
        # A DIFFERENT SENTENCE, because the operator's correct next action is different. "refused"
        # invites the documented REST fallback; after the handoff has fired that fallback would
        # open a SECOND trial for one signal. This line tells them to check before acting.
        print(
            f"refused AFTER handoff: {error} -- {INDETERMINATE_WARNING}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(f"refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
