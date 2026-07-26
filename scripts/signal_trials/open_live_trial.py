#!/usr/bin/env python3
"""Open one live trial over an already-captured signal, and publish it for free discovery.

Run by an operator, offline. **This script makes no network request of any kind.** The signal
arrives as JSON on a file or on stdin, already captured by whatever pulled it from OKX, because
opening a trial and fetching a signal are separate concerns with separate failure modes: a fetch
that half-succeeded must not be able to publish a trial over partial evidence.

    python scripts/signal_trials/open_live_trial.py --signal-file sig.json --data-dir ./data
    okx_signal_dump | python scripts/signal_trials/open_live_trial.py --data-dir ./data

The published trial is what ``GET /signal-trials/open-trial`` serves and what a paid commit binds
to, so three things are refused rather than worked around:

* **A signal that fails canonicalization.** ``normalize_signal`` is the leakage boundary — it
  rejects unknown wallet types, missing fields and any forbidden evidence field. A trial opened
  over a payload that "mostly" normalized would seal evidence nobody can re-derive.
* **Evidence observed after the open instant.** :func:`~veridex.signal_trials.live.open_live_trial`
  refuses it: the commit-before-outcome seal requires that every committer could have seen the
  evidence, and a clock skew must cost a trial rather than the seal.
* **Re-publishing an id with different terms.** The repository refuses to move a deadline or swap
  evidence under commitments already placed against it.

``--dry-run`` renders exactly what would be published and writes nothing, which is the safe way
to check a capture before it becomes the thing agents pay to commit against.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from veridex.signal_trials.challenge_spec import normalize_signal
from veridex.signal_trials.live import DECISION_WINDOW_MS, LiveTrialRepository, open_live_trial

#: Where the repository lives under the data dir. Must match the API's own composition, or the
#: script publishes into a directory nothing serves.
LIVE_SUBDIR = "live"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Open and publish one live signal trial. Reads a captured signal; makes no network request.",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Signal-trials data directory; the API must serve the same one.",
    )
    parser.add_argument(
        "--signal-file",
        default=None,
        help="JSON file holding one raw signal. Omit to read stdin.",
    )
    parser.add_argument(
        "--source",
        choices=("rest", "ws"),
        default="rest",
        help="Which transport the signal was captured from; decides alias normalization.",
    )
    parser.add_argument(
        "--trial-id",
        default=None,
        help="Explicit trial id. Omit to derive one from the evidence hash and open instant.",
    )
    parser.add_argument(
        "--now-ms",
        type=int,
        default=None,
        help="Open instant in epoch milliseconds. Omit to use the real clock.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be published and write nothing.",
    )
    return parser.parse_args(argv)


def _load_signal(signal_file: str | None) -> dict[str, Any]:
    """Load one raw signal object from ``signal_file`` or stdin.

    Raises:
        ValueError: The input is not a single JSON object. A list is refused explicitly rather
            than silently taking its first element: a capture holding several signals is an
            ambiguous instruction about which trial to open, and guessing would publish a trial
            the operator did not choose.
    """
    text = Path(signal_file).read_text(encoding="utf-8") if signal_file else sys.stdin.read()
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError(
            f"expected ONE signal as a JSON object, got {type(loaded).__name__}; "
            "open one trial per invocation so the choice of signal is always explicit"
        )
    return loaded


def main(argv: list[str] | None = None) -> int:
    """Open a live trial and publish it. Returns a process exit status.

    Refusals are reported on stderr and exit non-zero, so a shell driving this cannot mistake a
    rejected capture for a published trial.
    """
    args = _parse_args(argv)
    try:
        raw = _load_signal(args.signal_file)
        sig = normalize_signal(raw, source=args.source)
        now_ms = int(time.time() * 1000) if args.now_ms is None else args.now_ms
        trial = open_live_trial(sig, now_ms=now_ms, trial_id=args.trial_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1

    summary = {
        "trial_id": trial.trial_id,
        "trial_mode": trial.trial_mode,
        "t0_ms": trial.t0_ms,
        "commit_deadline_ms": trial.commit_deadline_ms,
        "decision_window_ms": DECISION_WINDOW_MS,
        "evidence_hash": trial.evidence_hash,
        "evidence_fields": sorted(trial.evidence),
        "published": not args.dry_run,
    }
    if not args.dry_run:
        try:
            LiveTrialRepository(Path(args.data_dir) / LIVE_SUBDIR).publish(trial)
        except (OSError, ValueError) as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
    # The evidence VALUES are deliberately not printed — only its field names and its hash. The
    # payload is public through the free read; a terminal transcript is not the place to publish
    # it, and the hash is what an operator actually needs to compare against a receipt.
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
