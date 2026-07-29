"""Score a sealed pack and publish the season — the offline half of the H1.2 API contract.

    .venv/bin/python scripts/signal_trials/score_and_publish.py \
        --pack-dir data/signal_trials/packs/season-2026-07 --data-dir data/signal_trials

The season is BUILT here and SERVED by the router, in different processes at different times, with
``veridex.signal_trials.published`` as the only thing between them. This script therefore owns two
obligations that are invisible from either side alone.

**The ``no_season`` branch, and why it is checked FIRST.** ``published`` distinguishes two states
that both mean "no season is served": ``not_built`` says the scorer never ran, ``no_season`` says it
ran and declined. This script is the scorer that distinction refers to. Under a declined preflight
there is no pack to read, so the state is consulted BEFORE anything touches the pack directory and
the script exits 0 with ``skipped: no_season``. Exiting non-zero would make an intentional refusal
look like a broken pipeline, and reading the pack first would turn it into a crash — which is a
third, false, story about what happened. ``not_built`` is the ordinary first run and does NOT skip.

**Write order is payload-then-state, and it is this side's half of a contract.**
``published.read_season`` silently absorbs a payload sitting under a stale state, but REFUSES a
state asserting a season whose payload is missing — so a crash between the two writes must land in
the absorbable arrangement. :func:`publish_season` writes ``season.json`` before ``state.json`` for
exactly that reason; reversing the two lines would be a durability defect that no single-process
test would notice.

Importing this module reads nothing, opens nothing and scores nothing: every effect happens inside
``main``, matching ``run_preflight.py``. It touches no credential and makes no network call — the
pack it reads was sealed by an earlier, operator-run stage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from veridex.signal_trials.pack import PackRef, load_pack
from veridex.signal_trials.published import read_state, write_season, write_state
from veridex.signal_trials.scoring import SeasonResult, score_season, season_document

#: The published state under which the scorer declines rather than runs. Distinct from
#: ``not_built``, which is an unpublished directory awaiting its first run.
NO_SEASON = "no_season"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score a sealed signal-trials pack and publish the season document.",
    )
    parser.add_argument(
        "--pack-dir",
        type=Path,
        required=True,
        help="The sealed pack directory to score. Not read at all under a no_season state.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="The signal-trials data directory holding the published-season repository.",
    )
    parser.add_argument(
        "--expected-content-hash",
        default=None,
        help="The exact sealed-pack content hash approved by the named human.",
    )
    return parser.parse_args(argv)


def publish_season(
    data_dir: Path | str,
    season: SeasonResult,
    approved_pack_content_hash: str,
) -> None:
    """Publish ``season`` to the repository the router serves.

    The payload is written BEFORE the state that advertises it. That order is not stylistic: writes
    are atomic per FILE, not across the pair, so a crash between them leaves either a payload under
    a stale state — which ``read_season`` absorbs — or a state asserting a season that is not there,
    which it refuses. Only one of those two is a recoverable arrangement.

    Args:
        data_dir: The signal-trials data directory (created on demand).
        season: The scored season.
        approved_pack_content_hash: The verified human-approved pack hash retained in local state
            provenance. It is not added to the public season payload.
    """
    write_season(data_dir, season_document(season))
    write_state(
        data_dir,
        season.season_status,  # type: ignore[arg-type]  # narrowed by score_season, which refuses no_season
        {
            "season_id": season.season_id,
            "sample_size": season.sample_size,
            "row_count": len(season.rows),
            "qualified_rows": sum(1 for row in season.rows if row.qualified),
            "approved_pack_content_hash": approved_pack_content_hash,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    # FIRST, before the pack directory is touched: a declined preflight has no pack to read.
    state = read_state(args.data_dir)["state"]
    if state == NO_SEASON:
        print("skipped: no_season")
        print(
            "the preflight declined this season; the scorer records nothing and leaves the state as it found it",
            file=sys.stderr,
        )
        return 0

    if not args.expected_content_hash:
        print(
            "scoring REFUSED: --expected-content-hash is required before a pack may be scored",
            file=sys.stderr,
        )
        return 2

    try:
        approved_ref = PackRef(
            dir=args.pack_dir,
            content_hash=str(args.expected_content_hash),
        )
        season = score_season(load_pack(approved_ref))
    except Exception as exc:  # noqa: BLE001 - every failure must be reported, not just the known ones
        print(f"scoring FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("nothing was published; the previous published state is unchanged", file=sys.stderr)
        return 1

    publish_season(args.data_dir, season, approved_ref.content_hash)
    qualified = sum(1 for row in season.rows if row.qualified)
    print(
        f"published season {season.season_id!r} status={season.season_status!r} "
        f"sample_size={season.sample_size} rows={len(season.rows)} qualified={qualified}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
