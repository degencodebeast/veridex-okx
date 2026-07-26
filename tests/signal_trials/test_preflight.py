"""Tests for the (chain x bar) matrix preflight (H2.3).

Structure:

1. The FROZEN MANDATED BLOCK from the implementation plan (lines 380-397), reproduced
   byte-identically between the BEGIN/END marker comments below. Under PKT-DEC-C8 those bytes are
   immutable and exempt from lint/type gates; the import header above them is NOT frozen and is
   lint-clean. The markers make the conformance check content-addressed rather than dependent on a
   byte offset, which PKT-EVID-H2-2-PINS recorded as a fragility.

2. ADDED COVERAGE below the block. The mandated five tests hold four things CONSTANT and are
   therefore blind to four hard-codings (PKT-DEC-C18 constancy):

   - ``bar`` is ``"1m"`` in every asserted outcome, so combos 3 and 4 never win and
     ``bar = "1m"`` passes all five;
   - ``min_trials`` is the default 40 in every vector, so ``min_trials = 40`` passes all five;
   - ``test_exploratory_tie_broken_by_frozen_order`` presents ``counts`` in COMBO_ORDER order, so
     "resolve by COMBO_ORDER" and "resolve by iteration order of the counts tuple" COINCIDE and the
     vector cannot distinguish the thing it exists to distinguish;
   - ``rejection_reasons`` is ``{}`` in every vector, so nothing pins that it survives to the
     artifact.

   Every gap is closed by ADDITION. No frozen byte is edited.
"""

import inspect
import json
import re
from dataclasses import fields
from importlib import util as importlib_util
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from veridex.signal_trials import preflight
from veridex.signal_trials.okx_client import BAR_MS, Candle, CandleSeries, OKXAPIError, SignalFilters, SignalPage
from veridex.signal_trials.preflight import (
    COMBO_ORDER,
    ComboCount,
    ComboSelection,
    MatrixProbeResult,
    PreflightError,
    run_matrix_probe,
    select_combo,
    write_preflight_failure,
    write_preflight_result,
)
from veridex.signal_trials.spot_markout import select_settlement_candle

# --- BEGIN FROZEN MANDATED BLOCK (implementation-plan.md lines 380-397) --------------------------

def _r(a, b, c, d, ok=True):
    mk = lambda ch, bar, n: ComboCount(ch, bar, n, {})
    return MatrixProbeResult((mk("196","1m",a), mk("501","1m",b), mk("196","1H",c), mk("501","1H",d)), ok)

def test_qualified_prefers_frozen_order():
    s = select_combo(_r(41, 60, 44, 60)); assert (s.chain_index, s.bar, s.season_status) == ("196", "1m", "qualified")

def test_exploratory_picks_concrete_argmax_combo():
    s = select_combo(_r(12, 39, 17, 30)); assert s.season_status == "exploratory" and (s.chain_index, s.bar) == ("501", "1m")

def test_exploratory_tie_broken_by_frozen_order():
    s = select_combo(_r(20, 20, 5, 5)); assert (s.chain_index, s.bar) == ("196", "1m")

def test_all_zero_is_no_season():
    s = select_combo(_r(0, 0, 0, 0)); assert s.season_status == "no_season" and s.chain_index is None

def test_unconfirmed_direction_forces_no_season():
    s = select_combo(_r(60, 60, 60, 60, ok=False)); assert s.season_status == "no_season"

# --- END FROZEN MANDATED BLOCK ------------------------------------------------------------------


# ==================================================================================================
# Builders for the added coverage.
#
# `_matrix` deliberately does NOT build in COMBO_ORDER: the caller states the order explicitly, so a
# vector can present `counts` in an order that DIFFERS from COMBO_ORDER. That is the only way to
# tell "resolve by COMBO_ORDER" apart from "resolve by iteration order of counts".
# ==================================================================================================

BASE_MS = 1_753_400_000_000
HORIZON_MS = 3_600_000
COOLDOWN_MS = 14_400_000


def _matrix(items, *, ok=True):
    """Build a MatrixProbeResult from an explicit ORDERED sequence of (chain, bar, count[, reasons])."""
    counts = tuple(ComboCount(item[0], item[1], item[2], dict(item[3]) if len(item) > 3 else {}) for item in items)
    return MatrixProbeResult(counts, ok)


def _full(n0, n1, n2, n3, *, ok=True, reasons=None):
    """A COMBO_ORDER-ordered matrix. The four literals are written out, never derived from COMBO_ORDER."""
    per_combo = reasons or [{}, {}, {}, {}]
    return _matrix(
        [
            ("196", "1m", n0, per_combo[0]),
            ("501", "1m", n1, per_combo[1]),
            ("196", "1H", n2, per_combo[2]),
            ("501", "1H", n3, per_combo[3]),
        ],
        ok=ok,
    )


def _count_for(result, chain_index, bar):
    for count in result.counts:
        if (count.chain_index, count.bar) == (chain_index, bar):
            return count
    raise AssertionError(f"no ComboCount for {(chain_index, bar)} in {result.counts}")


# ==================================================================================================
# The frozen surface itself. These are the HARD-CODED anchors that every derived vector needs:
# a vector built from COMBO_ORDER cannot notice COMBO_ORDER moving.
# ==================================================================================================


def test_combo_order_is_the_frozen_predeclared_matrix():
    """Precision (1m) above nativeness (X Layer); nativeness breaks ties within a precision tier."""
    assert COMBO_ORDER == (("196", "1m"), ("501", "1m"), ("196", "1H"), ("501", "1H"))


def test_combo_count_field_order_is_pinned():
    """The frozen helper `_r` constructs ComboCount POSITIONALLY, so field order is load-bearing."""
    assert [f.name for f in fields(ComboCount)] == ["chain_index", "bar", "eligible_settleable", "rejection_reasons"]


def test_matrix_probe_result_field_order_is_pinned():
    assert [f.name for f in fields(MatrixProbeResult)] == ["counts", "direction_semantics_confirmed"]


def test_combo_selection_field_order_is_pinned():
    """Not pinned by the frozen block (it only reads attributes), so it is pinned here."""
    assert [f.name for f in fields(ComboSelection)] == ["chain_index", "bar", "season_status"]


def test_season_status_is_exactly_three_values():
    hints = get_type_hints(ComboSelection)
    assert set(get_args(hints["season_status"])) == {"qualified", "exploratory", "no_season"}


def test_frozen_defaults_match_the_spec():
    """4h same-token cooldown, 1h ranking horizon, 40-trial qualification threshold (spec 5.1/8.3)."""
    signature = inspect.signature(run_matrix_probe)
    assert signature.parameters["cooldown_ms"].default == 14_400_000
    assert signature.parameters["horizon_ms"].default == 3_600_000
    assert signature.parameters["min_trials"].default == 40
    assert inspect.signature(select_combo).parameters["min_trials"].default == 40


# ==================================================================================================
# PKT-DEC-C22 - PIN THE SURFACE, NOT THE BLOB.
#
# This task has exactly ONE cross-lane read-only dependency: the function
# `select_settlement_candle`, owned by LAW at veridex/signal_trials/spot_markout.py:81. It is
# imported by veridex/signal_trials/preflight.py and referenced nowhere else in this task, so the
# blast radius of a Law change is that one symbol.
#
# `Candle` and `CandleSeries` are NOT cross-lane - they are DATA's own types from H2.1, this lane's.
# They are pinned here anyway because the CALL CONTRACT spans them: they are what we hand Law and
# what Law hands back. Pinning them is an intra-lane guard, and it is stated as such rather than
# inflating the cross-lane dependency to three symbols.
#
# C22 requirement 3: the assertion is MECHANICAL - dataclasses.fields names IN ORDER, arity, and
# annotations - and it RUNS AT EVERY GATE because it is an ordinary test.
#
# C22 SCOPE LIMIT, recorded so nothing below is over-credited: a surface check answers "is this lane
# affected by the owner's change?" It does NOT answer "is the owner correct?" A mutant that
# transposed a wire column would leave every assertion here passing. Law's correctness is pinned in
# Law's own suite, never here.
# ==================================================================================================


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (
            Candle,
            [
                ("ts_open_ms", int),
                ("open", float),
                ("high", float),
                ("low", float),
                ("close", float),
                ("vol", float),
                ("vol_usd", float),
                ("confirmed", bool),
            ],
        ),
        (CandleSeries, [("bar", str), ("bar_ms", int), ("candles", tuple[Candle, ...])]),
    ],
    ids=["Candle", "CandleSeries"],
)
def test_settlement_type_surface_names_order_arity_and_types(cls, expected):
    """Names, ORDER, arity and types of the types carried across the H3.2 call boundary."""
    hints = get_type_hints(cls)
    assert [(f.name, hints[f.name]) for f in fields(cls)] == expected


def test_law_settlement_function_surface_is_pinned():
    """The ONE cross-lane symbol. A blob difference is never a finding; this always is.

    `t0_ms` and `horizon_ms` being KEYWORD-ONLY is part of the surface: were either to become
    positional, a caller passing them positionally in the other order would still typecheck.
    """
    signature = inspect.signature(select_settlement_candle)
    assert list(signature.parameters) == ["series", "t0_ms", "horizon_ms"]
    assert signature.parameters["series"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["t0_ms"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["horizon_ms"].kind is inspect.Parameter.KEYWORD_ONLY

    hints = get_type_hints(select_settlement_candle)
    assert hints["series"] is CandleSeries
    assert hints["t0_ms"] is int
    assert hints["horizon_ms"] is int
    assert hints["return"] == (Candle | None)


LAW_OWNED_MODULES = ("spot_markout", "scoring", "leaderboard", "rank_guards")


def test_the_owned_files_reference_exactly_one_law_owned_symbol():
    """The recorded blast radius, asserted rather than described.

    Scans BOTH owned source files and matches ANY reference form - `from X import y`,
    `import X`, `from veridex.signal_trials import spot_markout` - not one import form in one file.
    The narrower earlier version was correct about today's radius while leaving three ways to widen
    it silently (SPEC MINOR-7).
    """
    sources = {
        "veridex/signal_trials/preflight.py": inspect.getsource(preflight),
        "scripts/signal_trials/run_preflight.py": _run_preflight_path().read_text(),
    }
    references = []
    for filename, source in sources.items():
        for module in LAW_OWNED_MODULES:
            for line in source.splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                if re.search(rf"\b{module}\b", stripped):
                    references.append((filename, stripped))
    assert references == [
        (
            "veridex/signal_trials/preflight.py",
            "from veridex.signal_trials.spot_markout import select_settlement_candle",
        )
    ]


# ==================================================================================================
# GAP 1 - `bar` is constant at "1m" across every mandated outcome, and combos 3 and 4 never win.
# An implementation that hard-codes bar="1m", or chain="196", passes all five mandated tests.
# ==================================================================================================


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((41, 60, 44, 60), ("196", "1m")),
        ((12, 41, 44, 60), ("501", "1m")),
        ((12, 39, 44, 60), ("196", "1H")),
        ((12, 39, 17, 41), ("501", "1H")),
    ],
)
def test_qualified_can_be_won_by_each_of_the_four_combos(counts, expected):
    """Each combo in turn is the FIRST to reach min_trials. Expected values are literal, not derived."""
    selection = select_combo(_full(*counts))
    assert (selection.chain_index, selection.bar) == expected
    assert selection.season_status == "qualified"


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((30, 12, 17, 20), ("196", "1m")),
        ((12, 30, 17, 20), ("501", "1m")),
        ((12, 17, 30, 20), ("196", "1H")),
        ((12, 17, 20, 30), ("501", "1H")),
    ],
)
def test_exploratory_argmax_can_land_on_each_of_the_four_combos(counts, expected):
    """A unique argmax in each position in turn. Kills a hard-coded bar and a hard-coded chain."""
    selection = select_combo(_full(*counts))
    assert (selection.chain_index, selection.bar) == expected
    assert selection.season_status == "exploratory"


def test_a_one_hour_combo_can_win_qualified_when_both_minute_combos_are_short():
    """Written out longhand: the 1H fallback is the branch the whole matrix exists to provide."""
    selection = select_combo(_full(39, 39, 40, 0))
    assert selection.chain_index == "196"
    assert selection.bar == "1H"
    assert selection.season_status == "qualified"


# ==================================================================================================
# GAP 2 - `min_trials` is the default 40 in every mandated vector. `min_trials = 40` passes all five.
# ==================================================================================================


def test_identical_counts_change_verdict_when_min_trials_changes():
    """The direct constancy kill: ONE matrix, two thresholds, two different verdicts."""
    matrix = _full(30, 12, 17, 20)
    assert select_combo(matrix, min_trials=20).season_status == "qualified"
    assert select_combo(matrix, min_trials=200).season_status == "exploratory"


@pytest.mark.parametrize("min_trials", [1, 7, 39, 40, 41, 100])
def test_min_trials_boundary_is_inclusive(min_trials):
    """`count >= min_trials`, never `>`. A count of exactly min_trials QUALIFIES."""
    selection = select_combo(_full(min_trials, 0, 0, 0), min_trials=min_trials)
    assert selection.season_status == "qualified"
    assert (selection.chain_index, selection.bar) == ("196", "1m")


@pytest.mark.parametrize("min_trials", [2, 8, 40, 41, 101])
def test_one_below_min_trials_is_exploratory_not_qualified(min_trials):
    selection = select_combo(_full(min_trials - 1, 0, 0, 0), min_trials=min_trials)
    assert selection.season_status == "exploratory"
    assert (selection.chain_index, selection.bar) == ("196", "1m")


def test_min_trials_applies_per_combo_not_to_the_matrix_total():
    """4 x 15 = 60 >= 40 in total, but no single combo reaches 40. A season never pools combos."""
    selection = select_combo(_full(15, 15, 15, 15), min_trials=40)
    assert selection.season_status == "exploratory"


@pytest.mark.parametrize("min_trials", [0, -1, -40])
def test_min_trials_below_one_is_rejected(min_trials):
    """A threshold that cannot gate would manufacture a `qualified` season - spec 5.1 forbids ever
    lowering the threshold. Rejection carries `match=` per PKT-DEC-C25: this is a trust path."""
    with pytest.raises(PreflightError, match="min_trials must be at least 1"):
        select_combo(_full(0, 0, 0, 0), min_trials=min_trials)


# ==================================================================================================
# GAP 3 - the mandated tie-break vector presents `counts` in COMBO_ORDER order, so "by COMBO_ORDER"
# and "by iteration order of counts" agree and it cannot tell them apart. These present `counts` in
# an order that DIFFERS from COMBO_ORDER.
# ==================================================================================================


def test_tie_break_follows_combo_order_not_counts_iteration_order():
    """`counts` reversed, all four tied. A counts-iteration argmax would answer ("501", "1H")."""
    reversed_matrix = _matrix(
        [("501", "1H", 20), ("196", "1H", 20), ("501", "1m", 20), ("196", "1m", 20)],
    )
    selection = select_combo(reversed_matrix, min_trials=40)
    assert (selection.chain_index, selection.bar) == ("196", "1m")
    assert selection.season_status == "exploratory"


def test_tie_between_the_two_one_hour_combos_resolves_to_x_layer():
    """The tie is between combos 3 and 4 and 501/1H is presented FIRST. Answer must still be 196/1H."""
    scrambled = _matrix(
        [("501", "1H", 30), ("196", "1m", 5), ("196", "1H", 30), ("501", "1m", 5)],
    )
    selection = select_combo(scrambled, min_trials=40)
    assert (selection.chain_index, selection.bar) == ("196", "1H")


def test_qualified_also_follows_combo_order_not_counts_order():
    """Two combos qualify and the lower-priority one is presented first."""
    scrambled = _matrix(
        [("501", "1m", 60), ("196", "1m", 41), ("196", "1H", 0), ("501", "1H", 0)],
    )
    selection = select_combo(scrambled, min_trials=40)
    assert (selection.chain_index, selection.bar) == ("196", "1m")


def test_selection_is_invariant_under_every_ordering_of_counts():
    """Same four counts, several presentations, one answer."""
    orderings = [
        [("196", "1m", 12), ("501", "1m", 39), ("196", "1H", 17), ("501", "1H", 30)],
        [("501", "1H", 30), ("196", "1H", 17), ("501", "1m", 39), ("196", "1m", 12)],
        [("196", "1H", 17), ("196", "1m", 12), ("501", "1H", 30), ("501", "1m", 39)],
    ]
    answers = {(select_combo(_matrix(order)).chain_index, select_combo(_matrix(order)).bar) for order in orderings}
    assert answers == {("501", "1m")}


# ==================================================================================================
# The three branches are a CLOSED SET, and `no_season` has two INDEPENDENT routes.
# ==================================================================================================


def test_all_zero_is_no_season_with_both_chain_and_bar_null():
    """The mandated vector asserts chain_index is None but never bar."""
    selection = select_combo(_full(0, 0, 0, 0))
    assert selection.season_status == "no_season"
    assert selection.chain_index is None
    assert selection.bar is None


def test_unconfirmed_direction_is_no_season_with_both_chain_and_bar_null():
    """The mandated vector asserts season_status only."""
    selection = select_combo(_full(60, 60, 60, 60, ok=False))
    assert selection.season_status == "no_season"
    assert selection.chain_index is None
    assert selection.bar is None


def test_unconfirmed_direction_overrides_exploratory_counts():
    """The second no_season route, exercised on counts that would otherwise be EXPLORATORY.

    The mandated unconfirmed-direction vector uses qualified-magnitude counts, so it leaves the
    exploratory interaction unpinned.
    """
    assert select_combo(_full(12, 39, 17, 30, ok=False)).season_status == "no_season"


def test_unconfirmed_direction_overrides_qualified_counts_at_a_non_default_threshold():
    selection = select_combo(_full(5, 5, 5, 5, ok=False), min_trials=5)
    assert selection.season_status == "no_season"
    assert (selection.chain_index, selection.bar) == (None, None)


@pytest.mark.parametrize("min_trials", [1, 2, 40])
def test_all_zero_is_no_season_at_every_threshold(min_trials):
    """With min_trials=1 a naive `count >= min_trials` scan would call combo 1 QUALIFIED at zero.
    The all-zero gate outranks the qualification scan."""
    selection = select_combo(_full(0, 0, 0, 0), min_trials=min_trials)
    assert selection.season_status == "no_season"
    assert (selection.chain_index, selection.bar) == (None, None)


def test_the_two_no_season_routes_are_independent():
    """Each route alone produces no_season; removing either gate leaves the other reachable."""
    assert select_combo(_full(0, 0, 0, 0, ok=True)).season_status == "no_season"
    assert select_combo(_full(60, 60, 60, 60, ok=False)).season_status == "no_season"


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((1, 0, 0, 0), ("196", "1m")),
        ((0, 1, 0, 0), ("501", "1m")),
        ((0, 0, 1, 0), ("196", "1H")),
        ((0, 0, 0, 1), ("501", "1H")),
    ],
)
def test_a_single_nonzero_combo_is_exploratory_not_no_season(counts, expected):
    """`some > 0` is the boundary between exploratory and no_season, in each position in turn."""
    selection = select_combo(_full(*counts))
    assert selection.season_status == "exploratory"
    assert (selection.chain_index, selection.bar) == expected


def test_season_status_is_only_ever_one_of_the_three_branches():
    vectors = [_full(60, 0, 0, 0), _full(1, 0, 0, 0), _full(0, 0, 0, 0), _full(60, 60, 60, 60, ok=False)]
    assert {select_combo(v).season_status for v in vectors} == {"qualified", "exploratory", "no_season"}


def test_a_named_combo_always_has_a_positive_count_behind_it():
    """A selection that names a chain/bar must never point at a zero-count combo."""
    for vector in [_full(41, 60, 44, 60), _full(0, 0, 30, 0), _full(0, 0, 0, 5)]:
        selection = select_combo(vector)
        assert _count_for(vector, selection.chain_index, selection.bar).eligible_settleable > 0


# ==================================================================================================
# A malformed matrix must FAIL CLOSED. `select_combo` decides the season; a partial or contradictory
# matrix must never yield a confident answer. Every rejection carries `match=` (PKT-DEC-C25).
# ==================================================================================================


def test_a_matrix_missing_a_combo_is_rejected():
    partial = _matrix([("196", "1m", 41), ("501", "1m", 0), ("196", "1H", 0)])
    with pytest.raises(PreflightError, match="is missing 1 of the frozen combos"):
        select_combo(partial)


def test_a_matrix_with_a_duplicated_combo_is_rejected():
    duplicated = _matrix(
        [("196", "1m", 41), ("196", "1m", 0), ("501", "1m", 0), ("196", "1H", 0), ("501", "1H", 0)],
    )
    with pytest.raises(PreflightError, match="duplicate entry for combo"):
        select_combo(duplicated)


def test_a_matrix_carrying_an_unknown_combo_is_rejected():
    """A chain outside the frozen matrix would settle the season against an unpredeclared market."""
    unknown = _matrix(
        [("196", "1m", 0), ("501", "1m", 0), ("196", "1H", 0), ("501", "1H", 0), ("999", "1m", 99)],
    )
    with pytest.raises(PreflightError, match="unknown combo"):
        select_combo(unknown)


def test_a_matrix_carrying_an_unknown_bar_is_rejected():
    unknown_bar = _matrix([("196", "1m", 0), ("501", "1m", 0), ("196", "1H", 0), ("501", "4H", 41)])
    with pytest.raises(PreflightError, match="unknown combo"):
        select_combo(unknown_bar)


def test_a_negative_count_is_rejected():
    with pytest.raises(PreflightError, match="negative eligible_settleable count"):
        select_combo(_full(-1, 0, 0, 0))


# ==================================================================================================
# GAP 4 - `rejection_reasons` is `{}` in every mandated vector, so nothing pins that it carries real
# content, survives to the artifact, or aggregates correctly.
# ==================================================================================================

RESULT_KEYS = [
    "probe_status",
    "season_status",
    "chain_index",
    "bar",
    "counts",
    "rejection_reasons",
    "direction_semantics_confirmed",
    "failure_reason",
]


def _read(path):
    return json.loads(path.read_text())


def test_per_combo_rejection_reasons_reach_the_artifact(tmp_path):
    matrix = _full(
        41,
        3,
        0,
        0,
        reasons=[
            {"same_token_cooldown": 7},
            {"settlement_window_gap": 11, "null_trigger_price": 2},
            {"settlement_window_gap": 40},
            {"settlement_window_gap": 40},
        ],
    )
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)

    payload = _read(out)
    by_combo = {(c["chain_index"], c["bar"]): c["rejection_reasons"] for c in payload["counts"]}
    assert by_combo[("196", "1m")] == {"same_token_cooldown": 7}
    assert by_combo[("501", "1m")] == {"settlement_window_gap": 11, "null_trigger_price": 2}


def test_top_level_rejection_reasons_aggregate_across_all_four_combos(tmp_path):
    matrix = _full(
        41,
        3,
        0,
        0,
        reasons=[
            {"same_token_cooldown": 7},
            {"settlement_window_gap": 11, "null_trigger_price": 2},
            {"settlement_window_gap": 40},
            {"settlement_window_gap": 1},
        ],
    )
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)

    assert _read(out)["rejection_reasons"] == {
        "same_token_cooldown": 7,
        "settlement_window_gap": 52,
        "null_trigger_price": 2,
    }


def test_a_reason_present_in_only_one_combo_still_reaches_the_aggregate(tmp_path):
    matrix = _full(0, 0, 0, 5, reasons=[{}, {}, {}, {"non_positive_trigger_price": 3}])
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)
    assert _read(out)["rejection_reasons"] == {"non_positive_trigger_price": 3}


def test_empty_rejection_reasons_stay_empty_and_are_not_invented(tmp_path):
    matrix = _full(41, 0, 0, 0)
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)
    payload = _read(out)
    assert payload["rejection_reasons"] == {}
    assert all(c["rejection_reasons"] == {} for c in payload["counts"])


# ==================================================================================================
# `write_preflight_result` - ABSENCE, EMPTINESS and FAILURE are three distinguishable states.
#
# Data's Gate 1 MAJOR was `list_signals` returning a well-formed empty page indistinguishable from
# genuine absence. Here it is worse: the artifact is read by a LATER STAGE that cannot see the run.
# A completed no_season probe and a failed probe must never produce the same artifact.
# ==================================================================================================


def test_a_completed_result_declares_itself_completed(tmp_path):
    matrix = _full(41, 0, 0, 0)
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)
    payload = _read(out)
    assert payload["probe_status"] == "completed"
    assert payload["failure_reason"] is None


def test_a_failure_declares_itself_failed_and_makes_no_season_claim(tmp_path):
    out = tmp_path / "preflight_result.json"
    write_preflight_failure("OKXAPIError: OKX returned non-success code '51000'", out)
    payload = _read(out)
    assert payload["probe_status"] == "failed"
    assert payload["season_status"] is None
    assert payload["chain_index"] is None
    assert payload["bar"] is None
    assert payload["counts"] == []
    assert payload["direction_semantics_confirmed"] is False
    assert "51000" in payload["failure_reason"]


def test_emptiness_and_failure_are_distinguishable_on_every_load_bearing_field(tmp_path):
    """A no_season season is an OBSERVATION. A failed probe is not. They must not read alike."""
    empty_matrix = _full(0, 0, 0, 0)
    empty_path = tmp_path / "empty.json"
    failed_path = tmp_path / "failed.json"
    write_preflight_result(select_combo(empty_matrix), empty_matrix, empty_path)
    write_preflight_failure("transport refused the connection", failed_path)

    empty, failed = _read(empty_path), _read(failed_path)
    assert empty != failed
    assert (empty["probe_status"], failed["probe_status"]) == ("completed", "failed")
    assert (empty["season_status"], failed["season_status"]) == ("no_season", None)
    assert len(empty["counts"]) == 4 and failed["counts"] == []
    assert empty["failure_reason"] is None and failed["failure_reason"]


def test_both_writers_emit_the_same_key_set_in_the_same_order(tmp_path):
    """A reader must be able to reach `probe_status` on EVERY artifact without a KeyError."""
    matrix = _full(41, 0, 0, 0)
    ok_path, failed_path = tmp_path / "ok.json", tmp_path / "failed.json"
    write_preflight_result(select_combo(matrix), matrix, ok_path)
    write_preflight_failure("boom", failed_path)
    assert list(_read(ok_path)) == RESULT_KEYS
    assert list(_read(failed_path)) == RESULT_KEYS


def test_absence_is_the_third_state_and_a_refused_write_leaves_no_artifact(tmp_path):
    """ABSENCE means the run did not reach the writer. A refused write must not fabricate a file."""
    out = tmp_path / "preflight_result.json"
    assert not out.exists()
    matrix = _full(0, 0, 0, 0)
    with pytest.raises(PreflightError):
        write_preflight_result(ComboSelection("196", "1m", "qualified"), matrix, out)
    assert not out.exists()


def test_a_refused_write_does_not_clobber_an_existing_artifact(tmp_path):
    out = tmp_path / "preflight_result.json"
    good = _full(41, 0, 0, 0)
    write_preflight_result(select_combo(good), good, out)
    before = out.read_bytes()

    contradictory = _full(0, 0, 0, 0)
    with pytest.raises(PreflightError):
        write_preflight_result(ComboSelection("196", "1m", "qualified"), contradictory, out)
    assert out.read_bytes() == before


def test_a_successful_write_leaves_no_temporary_file_behind(tmp_path):
    """The write is atomic, so a crash leaves the previous artifact or none - never a truncated one."""
    out = tmp_path / "preflight_result.json"
    matrix = _full(41, 0, 0, 0)
    write_preflight_result(select_combo(matrix), matrix, out)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["preflight_result.json"]


def test_a_failure_write_also_leaves_no_temporary_file_behind(tmp_path):
    out = tmp_path / "preflight_result.json"
    write_preflight_failure("boom", out)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["preflight_result.json"]


def test_the_artifact_is_replaced_atomically_not_written_in_place(tmp_path):
    """The mechanism behind the ABSENCE state, pinned DIRECTLY rather than via "no temp file left".

    "No temp file left behind" is satisfied by a plain in-place write too, so replacing the
    mkstemp-and-os.replace with one survived the suite (SPEC G19, recorded under MINOR-6). A
    read-only destination discriminates them: os.replace onto it SUCCEEDS, because POSIX rename
    permission depends on the containing DIRECTORY, while opening it "w" in place raises
    PermissionError. Measured both ways before this test was written.

    What is actually at stake: an in-place write TRUNCATES the previous artifact before it can fail,
    which manufactures a fourth state - a half-written file that reads as a corrupt version of one of
    the three, with no way for a reader to tell.
    """
    out = tmp_path / "preflight_result.json"
    first = _full(41, 0, 0, 0)
    write_preflight_result(select_combo(first), first, out)
    out.chmod(0o444)

    second = _full(12, 39, 17, 30)
    write_preflight_result(select_combo(second), second, out)
    assert _read(out)["season_status"] == "exploratory"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["preflight_result.json"]


def test_the_writer_creates_missing_parent_directories(tmp_path):
    out = tmp_path / "nested" / "deeper" / "preflight_result.json"
    matrix = _full(41, 0, 0, 0)
    write_preflight_result(select_combo(matrix), matrix, out)
    assert _read(out)["season_status"] == "qualified"


def test_a_blank_failure_reason_is_refused(tmp_path):
    """A failure with no reason is indistinguishable from a sloppy success."""
    out = tmp_path / "preflight_result.json"
    with pytest.raises(PreflightError, match="failure_reason must be non-empty"):
        write_preflight_failure("   ", out)
    assert not out.exists()


def test_counts_are_serialized_in_combo_order_whatever_order_they_arrive_in(tmp_path):
    reversed_matrix = _matrix(
        [("501", "1H", 1), ("196", "1H", 2), ("501", "1m", 3), ("196", "1m", 4)],
    )
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(reversed_matrix), reversed_matrix, out)
    payload = _read(out)
    assert [(c["chain_index"], c["bar"]) for c in payload["counts"]] == [
        ("196", "1m"),
        ("501", "1m"),
        ("196", "1H"),
        ("501", "1H"),
    ]
    assert [c["eligible_settleable"] for c in payload["counts"]] == [4, 3, 2, 1]


def test_the_artifact_records_the_selected_combo_and_the_direction_flag(tmp_path):
    matrix = _full(12, 39, 17, 30)
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)
    payload = _read(out)
    assert payload["season_status"] == "exploratory"
    assert (payload["chain_index"], payload["bar"]) == ("501", "1m")
    assert payload["direction_semantics_confirmed"] is True


def test_an_unconfirmed_direction_reaches_the_artifact(tmp_path):
    matrix = _full(60, 60, 60, 60, ok=False)
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)
    payload = _read(out)
    assert payload["direction_semantics_confirmed"] is False
    assert payload["season_status"] == "no_season"


# --- the writer refuses to emit a selection its own matrix does not support -----------------------


def test_a_qualified_selection_over_an_unconfirmed_matrix_is_refused(tmp_path):
    with pytest.raises(PreflightError, match="direction_semantics_confirmed is False"):
        write_preflight_result(
            ComboSelection("196", "1m", "qualified"), _full(60, 60, 60, 60, ok=False), tmp_path / "x.json"
        )


def test_a_selection_naming_a_combo_absent_from_the_matrix_is_refused(tmp_path):
    with pytest.raises(PreflightError, match="is not present in the probe matrix"):
        write_preflight_result(ComboSelection("999", "1m", "qualified"), _full(41, 0, 0, 0), tmp_path / "x.json")


def test_a_selection_naming_a_zero_count_combo_is_refused(tmp_path):
    with pytest.raises(PreflightError, match="zero eligible_settleable count"):
        write_preflight_result(ComboSelection("501", "1H", "qualified"), _full(41, 0, 0, 0), tmp_path / "x.json")


@pytest.mark.parametrize(
    "matrix",
    [_full(0, 0, 0, 0, ok=True), _full(41, 0, 0, 0, ok=False)],
    ids=["all-zero", "unconfirmed-direction"],
)
def test_a_no_season_selection_that_still_names_a_combo_is_refused(tmp_path, matrix):
    """Both vectors are matrices on which no_season is REACHABLE, so the mirror guard cannot fire and
    this guard is pinned in isolation. `match=` carries the guard's own words: under a looser pattern
    this test passed while the guard was deleted, because the mirror guard's message also said
    "no_season" (PKT-DEC-C25 ruling 2 — a rejection pin must discriminate the rejection's IDENTITY)."""
    with pytest.raises(PreflightError, match="must name neither chain nor bar"):
        write_preflight_result(ComboSelection("196", "1m", "no_season"), matrix, tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()


@pytest.mark.parametrize("status", ["qualified", "exploratory"])
def test_a_seasoned_selection_with_a_null_combo_is_refused(tmp_path, status):
    with pytest.raises(PreflightError, match="must name both chain_index and bar"):
        write_preflight_result(ComboSelection(None, None, status), _full(41, 0, 0, 0), tmp_path / "x.json")


def test_a_no_season_artifact_over_a_live_matrix_is_refused(tmp_path):
    """The mirror direction: no_season has exactly two routes, and this matrix took neither.
    Downstream such an artifact reads as a legitimately barren season."""
    with pytest.raises(PreflightError, match="no_season is unreachable"):
        write_preflight_result(ComboSelection(None, None, "no_season"), _full(41, 0, 0, 0), tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()


@pytest.mark.parametrize(
    "matrix",
    [_full(0, 0, 0, 0, ok=True), _full(41, 0, 0, 0, ok=False)],
    ids=["all-zero", "unconfirmed-direction"],
)
def test_each_no_season_route_is_accepted_by_the_writer(tmp_path, matrix):
    out = tmp_path / "preflight_result.json"
    write_preflight_result(ComboSelection(None, None, "no_season"), matrix, out)
    assert _read(out)["season_status"] == "no_season"


def test_an_unrecognized_season_status_is_refused(tmp_path):
    with pytest.raises(PreflightError, match="unrecognized season_status"):
        write_preflight_result(ComboSelection(None, None, "provisional"), _full(0, 0, 0, 0), tmp_path / "x.json")


def test_the_writer_refuses_a_malformed_matrix(tmp_path):
    partial = _matrix([("196", "1m", 41), ("501", "1m", 0), ("196", "1H", 0)])
    with pytest.raises(PreflightError, match="is missing 1 of the frozen combos"):
        write_preflight_result(ComboSelection("196", "1m", "qualified"), partial, tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()


# ==================================================================================================
# `run_matrix_probe`. NO REAL CLIENT IS EVER CONSTRUCTED HERE - the probe takes a structural
# protocol and every test injects an in-memory fake holding literal pages and candle series.
# ==================================================================================================


class FakeMarketClient:
    """In-memory stand-in for OKXMarketClient. Holds literal payloads; performs no I/O of any kind."""

    def __init__(self, pages_by_chain=None, series_by_key=None, *, fail_on=None):
        self.pages_by_chain = pages_by_chain or {}
        self.series_by_key = series_by_key or {}
        self.fail_on = fail_on
        self.signal_calls = []
        self.candle_calls = []

    async def list_signals(self, f, cursor=None):
        if self.fail_on == "list_signals":
            raise OKXAPIError("51000", "Invalid parameter")
        self.signal_calls.append((f.chain_index, f, cursor))
        pages = self.pages_by_chain.get(f.chain_index, [])
        index = 0 if cursor is None else int(cursor)
        if index >= len(pages):
            return SignalPage((), None)
        return pages[index]

    async def get_candles(self, chain_index, token, bar, *, before_ms=None, limit=100):
        if self.fail_on == "get_candles":
            raise OKXAPIError("51000", "Invalid parameter")
        # `limit` and `before_ms` are RECORDED, not ignored: CANDLE_LIMIT sets the depth of the
        # retention answer and no fake observed it until SPEC's G1 mutant survived (MINOR-4).
        self.candle_calls.append((chain_index, token, bar, limit, before_ms))
        return self.series_by_key.get((chain_index, token, bar), CandleSeries(bar, BAR_MS[bar], ()))


def _sig(
    *,
    t0=BASE_MS,
    chain="501",
    token="TOK",
    price="1.0",
    amount="1500",
    wallet_count="3",
    market_cap="2000000",
    wallet_type="1",
    direction="buy",
    drop=(),
):
    """One raw wire signal that PASSES the default SignalFilters unless a caller weakens a field."""
    raw = {
        "timestamp": str(t0),
        "chainIndex": chain,
        "price": price,
        "walletType": wallet_type,
        "triggerWalletCount": wallet_count,
        "triggerWalletAddress": "0xa,0xb,0xc",
        "amountUsd": amount,
        "direction": direction,
        "token": {
            "tokenAddress": token,
            "symbol": "TOK",
            "name": "Token",
            "marketCapUsd": market_cap,
            "holders": "900",
            "top10HolderPercent": "31.5",
        },
    }
    for key in drop:
        raw.pop(key, None)
    return raw


def _page(signals, next_index=None):
    rows = list(signals)
    if next_index is not None and rows:
        rows[-1] = {**rows[-1], "cursor": str(next_index)}
    return SignalPage(tuple(rows), None if next_index is None else str(next_index))


def _candle(ts_open_ms, *, close=1.5, confirmed=True):
    """Built by KEYWORD, never positionally.

    PKT-DEC-C17-R2-A1 / C22: `Candle` field ORDER is the property that breaks SILENTLY, and it does
    so precisely through positional construction. A fixture that constructs positionally inherits
    that silent failure mode for no benefit, since order is pinned mechanically by
    test_settlement_type_surface_names_order_arity_and_types instead.
    """
    return Candle(
        ts_open_ms=ts_open_ms,
        open=1.0,
        high=1.6,
        low=0.9,
        close=close,
        vol=5.0,
        vol_usd=5.5,
        confirmed=confirmed,
    )


def _settling_series(bar, *t0s, horizon_ms=HORIZON_MS, close=1.5, confirmed=True):
    """A series carrying one candle whose close lands exactly on t0 + horizon, for each t0 (H3.2)."""
    candles = tuple(_candle(t0 + horizon_ms - BAR_MS[bar], close=close, confirmed=confirmed) for t0 in t0s)
    return CandleSeries(bar, BAR_MS[bar], candles)


def _filters():
    return {"196": SignalFilters(chain_index="196"), "501": SignalFilters(chain_index="501")}


def _both_bars(chain, token, *t0s, **kw):
    return {
        (chain, token, "1m"): _settling_series("1m", *t0s, **kw),
        (chain, token, "1H"): _settling_series("1H", *t0s, **kw),
    }


async def test_probe_counts_a_settleable_trial_under_both_bars():
    client = FakeMarketClient({"501": [_page([_sig()])]}, _both_bars("501", "TOK", BASE_MS))
    result = await run_matrix_probe(client, _filters())
    assert _count_for(result, "501", "1m").eligible_settleable == 1
    assert _count_for(result, "501", "1H").eligible_settleable == 1
    assert _count_for(result, "196", "1m").eligible_settleable == 0


async def test_probe_returns_counts_in_combo_order():
    client = FakeMarketClient()
    result = await run_matrix_probe(client, _filters())
    assert [(c.chain_index, c.bar) for c in result.counts] == list(COMBO_ORDER)


async def test_counts_differ_per_bar_when_only_one_bar_settles():
    """Kills a probe that settles every combo under a hard-coded bar."""
    series = {("501", "TOK", "1m"): _settling_series("1m", BASE_MS)}
    client = FakeMarketClient({"501": [_page([_sig()])]}, series)
    result = await run_matrix_probe(client, _filters())
    assert _count_for(result, "501", "1m").eligible_settleable == 1
    assert _count_for(result, "501", "1H").eligible_settleable == 0
    assert _count_for(result, "501", "1H").rejection_reasons == {"no_candles_returned": 1}


async def test_counts_differ_per_chain():
    """Kills a probe that queries one hard-coded chain and reports it under both."""
    pages = {"196": [_page([_sig(chain="196", token="XL1"), _sig(chain="196", token="XL2", t0=BASE_MS + COOLDOWN_MS)])]}
    series = {
        **_both_bars("196", "XL1", BASE_MS),
        **_both_bars("196", "XL2", BASE_MS + COOLDOWN_MS),
    }
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "196", "1m").eligible_settleable == 2
    assert _count_for(result, "501", "1m").eligible_settleable == 0


async def test_probe_sends_each_chain_its_own_filters():
    client = FakeMarketClient()
    await run_matrix_probe(client, _filters())
    assert sorted(chain for chain, _, _ in client.signal_calls) == ["196", "501"]
    for chain, filters, _ in client.signal_calls:
        assert filters.chain_index == chain


async def test_filters_registered_under_the_wrong_chain_are_rejected():
    """The sharpest failure this lane has: probing 501 while recording it as 196 would settle every
    trial of the season against the wrong market with every count looking plausible."""
    crossed = {"196": SignalFilters(chain_index="501"), "501": SignalFilters(chain_index="501")}
    with pytest.raises(PreflightError, match="carry chain_index"):
        await run_matrix_probe(FakeMarketClient(), crossed)


async def test_missing_filters_for_a_probed_chain_are_rejected():
    with pytest.raises(PreflightError, match="has no entry for chain"):
        await run_matrix_probe(FakeMarketClient(), {"501": SignalFilters(chain_index="501")})


async def test_probe_rejects_min_trials_below_one_before_doing_any_work():
    """An unusable threshold must fail BEFORE a 30-minute live probe, not after it."""
    client = FakeMarketClient()
    with pytest.raises(PreflightError, match="min_trials must be at least 1"):
        await run_matrix_probe(client, _filters(), min_trials=0)
    assert client.signal_calls == []


async def test_probe_rejects_a_negative_cooldown():
    with pytest.raises(PreflightError, match="cooldown_ms must be non-negative"):
        await run_matrix_probe(FakeMarketClient(), _filters(), cooldown_ms=-1)


@pytest.mark.parametrize("horizon_ms", [0, -3_600_000])
async def test_probe_rejects_a_non_positive_horizon(horizon_ms):
    """A zero horizon would settle every trial at its own open price - a guaranteed zero markout."""
    with pytest.raises(PreflightError, match="horizon_ms must be positive"):
        await run_matrix_probe(FakeMarketClient(), _filters(), horizon_ms=horizon_ms)


# --- the cooldown dedup ---------------------------------------------------------------------------


async def test_cooldown_collapses_same_token_signals_and_the_argument_is_honoured():
    """ONE signal set, two cooldowns, two counts. Kills a hard-coded 4h."""
    pages = {"501": [_page([_sig(t0=BASE_MS), _sig(t0=BASE_MS + 3_600_000)])]}
    series = _both_bars("501", "TOK", BASE_MS, BASE_MS + 3_600_000)
    deduped = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), cooldown_ms=COOLDOWN_MS)
    assert _count_for(deduped, "501", "1H").eligible_settleable == 1
    assert _count_for(deduped, "501", "1H").rejection_reasons == {"same_token_cooldown": 1}

    kept = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), cooldown_ms=0)
    assert _count_for(kept, "501", "1H").eligible_settleable == 2
    assert _count_for(kept, "501", "1H").rejection_reasons.get("same_token_cooldown") is None


async def test_cooldown_boundary_is_exclusive_at_exactly_the_cooldown():
    """Exactly `cooldown_ms` apart is OUTSIDE the cooldown and both trials survive."""
    second = BASE_MS + COOLDOWN_MS
    pages = {"501": [_page([_sig(t0=BASE_MS), _sig(t0=second)])]}
    series = _both_bars("501", "TOK", BASE_MS, second)
    at_boundary = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), cooldown_ms=COOLDOWN_MS)
    assert _count_for(at_boundary, "501", "1H").eligible_settleable == 2
    assert _count_for(at_boundary, "501", "1H").rejection_reasons == {}

    one_ms_inside = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), cooldown_ms=COOLDOWN_MS + 1)
    assert _count_for(one_ms_inside, "501", "1H").rejection_reasons == {"same_token_cooldown": 1}


async def test_dedup_keeps_the_earliest_of_a_cluster_regardless_of_wire_order():
    """The feed is latest-first. A dedup that trusted wire order would keep a different trial and
    could report a different COUNT on a re-fetch that paginated differently."""
    early, late = BASE_MS, BASE_MS + 3_600_000
    series = {**_both_bars("501", "TOK", early)}
    forward = {"501": [_page([_sig(t0=early), _sig(t0=late)])]}
    backward = {"501": [_page([_sig(t0=late), _sig(t0=early)])]}
    a = await run_matrix_probe(FakeMarketClient(forward, series), _filters())
    b = await run_matrix_probe(FakeMarketClient(backward, series), _filters())
    assert _count_for(a, "501", "1H").eligible_settleable == _count_for(b, "501", "1H").eligible_settleable == 1


async def test_dedup_is_scoped_to_one_token():
    """Two DIFFERENT tokens inside one cooldown window are two trials, not one."""
    pages = {"501": [_page([_sig(token="A"), _sig(token="B", t0=BASE_MS + 60_000)])]}
    series = {**_both_bars("501", "A", BASE_MS), **_both_bars("501", "B", BASE_MS + 60_000)}
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 2


async def test_dedup_is_scoped_to_one_chain():
    """The same token address on two chains is two markets, never one trial.

    The two chains' series carry DIFFERENT closes. Byte-identical fixtures would make this vector
    blind to a chain-blind candle cache - it would reuse one chain's series for the other and still
    report 1 and 1 (SPEC MAJOR-1). The distinguishing pin is
    test_the_candle_cache_is_keyed_by_market_identity below; this one no longer holds the series
    content constant across the dimension its own name says it varies.
    """
    pages = {
        "196": [_page([_sig(chain="196", token="SAME")])],
        "501": [_page([_sig(chain="501", token="SAME")])],
    }
    series = {**_both_bars("196", "SAME", BASE_MS, close=2.5), **_both_bars("501", "SAME", BASE_MS, close=7.5)}
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "196", "1H").eligible_settleable == 1
    assert _count_for(result, "501", "1H").eligible_settleable == 1


# --- MAJOR-1 (SPEC at 06f5269): market identity on the counting path -------------------------------
#
# `cache_key = (chain_index, token, bar)`. Dropping `chain_index` survived the ENTIRE suite: the test
# above had the right idea - one token address on both chains - but built byte-identical series for
# each, so reusing one for the other produced the same settlement and the vector could not tell a
# chain-scoped cache from a chain-blind one.
#
# This is the packet's own named sharpest failure mode - "settle every trial of the season against
# the wrong market, with counts that look entirely plausible" - recurring at the cache layer, and it
# is the C18 constancy defect: the fixture held constant the very thing the test's name varies.


async def test_the_candle_cache_is_keyed_by_market_identity():
    """One token address, two chains, candles for 501 ONLY. A chain-blind cache mis-settles.

    196 is probed first and caches its EMPTY series under the token+bar alone; 501 then reuses it and
    its genuinely settleable trial vanishes. HEAD: (501,1m)=1. Chain-blind: (501,1m)=0.
    """
    pages = {
        "196": [_page([_sig(chain="196", token="SAME")])],
        "501": [_page([_sig(chain="501", token="SAME")])],
    }
    series = _both_bars("501", "SAME", BASE_MS)  # nothing for 196 at all
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "501", "1m").eligible_settleable == 1
    assert _count_for(result, "501", "1H").eligible_settleable == 1
    assert _count_for(result, "196", "1m").eligible_settleable == 0
    assert _count_for(result, "196", "1H").eligible_settleable == 0


async def test_the_candle_cache_does_not_lend_one_chains_candles_to_another():
    """The mirror direction: candles for 196 ONLY. A chain-blind cache OVER-counts 501 off 196's
    series. Both directions are pinned because either one settles against the wrong market."""
    pages = {
        "196": [_page([_sig(chain="196", token="SAME")])],
        "501": [_page([_sig(chain="501", token="SAME")])],
    }
    series = _both_bars("196", "SAME", BASE_MS)  # nothing for 501 at all
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "196", "1H").eligible_settleable == 1
    assert _count_for(result, "501", "1H").eligible_settleable == 0
    assert _count_for(result, "501", "1H").rejection_reasons == {"no_candles_returned": 1}


async def test_each_chain_is_fetched_its_own_candles():
    """The cache must not suppress the second chain's fetch. Recorded calls name both chains."""
    pages = {
        "196": [_page([_sig(chain="196", token="SAME")])],
        "501": [_page([_sig(chain="501", token="SAME")])],
    }
    client = FakeMarketClient(pages, {**_both_bars("196", "SAME", BASE_MS), **_both_bars("501", "SAME", BASE_MS)})
    await run_matrix_probe(client, _filters())
    assert sorted({(chain, token, bar) for chain, token, bar, _, _ in client.candle_calls}) == [
        ("196", "SAME", "1H"),
        ("196", "SAME", "1m"),
        ("501", "SAME", "1H"),
        ("501", "SAME", "1m"),
    ]


# --- the horizon ----------------------------------------------------------------------------------


async def test_horizon_argument_moves_the_settlement_point():
    """ONE candle set, two horizons, two counts. Kills a hard-coded 3_600_000."""
    series = {("501", "TOK", "1H"): _settling_series("1H", BASE_MS, horizon_ms=HORIZON_MS)}
    pages = {"501": [_page([_sig()])]}
    at_one_hour = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), horizon_ms=HORIZON_MS)
    assert _count_for(at_one_hour, "501", "1H").eligible_settleable == 1

    at_four_hours = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), horizon_ms=4 * HORIZON_MS)
    assert _count_for(at_four_hours, "501", "1H").eligible_settleable == 0
    assert _count_for(at_four_hours, "501", "1H").rejection_reasons == {"settlement_window_gap": 1}


async def test_an_unconfirmed_candle_does_not_settle_a_trial():
    """H3.2's law: an in-progress bar's close is a moving number."""
    series = {("501", "TOK", "1H"): _settling_series("1H", BASE_MS, confirmed=False)}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 0
    assert _count_for(result, "501", "1H").rejection_reasons == {"no_confirmed_candle": 1}


async def test_a_non_positive_settlement_close_is_not_settleable():
    series = {("501", "TOK", "1H"): _settling_series("1H", BASE_MS, close=0.0)}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 0
    assert _count_for(result, "501", "1H").rejection_reasons == {"non_positive_settlement_price": 1}


# --- PKT-TASK-H2-3-A2 obligation 1: guarantee ts_open_ms uniqueness UPSTREAM ------------------------
#
# H3.2 selects on close_ts and states plainly that duplicate ts_open_ms TIE, that `min` keeps
# whichever row the wire put first, and that when their closes differ THE SETTLEMENT PRICE FOLLOWS
# WIRE ORDER. Deduping is not H3.2's job; a caller needing cross-re-fetch determinism must guarantee
# uniqueness upstream. This probe is such a caller, so a non-deterministic count here would be a
# defect in THIS module, not in H3.2.


def _duplicate_ts_series(bar, t0, *, closes, horizon_ms=HORIZON_MS):
    """Two rows sharing one ts_open_ms - exactly the tie H3.2 documents as wire-order dependent."""
    ts_open = t0 + horizon_ms - BAR_MS[bar]
    return CandleSeries(bar, BAR_MS[bar], tuple(_candle(ts_open, close=close) for close in closes))


async def test_duplicate_open_times_with_differing_closes_are_refused_not_settled():
    """The settlement price would otherwise be decided by which row the wire happened to put first."""
    series = {("501", "TOK", "1H"): _duplicate_ts_series("1H", BASE_MS, closes=[1.5, 9.9])}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 0
    assert _count_for(result, "501", "1H").rejection_reasons == {"ambiguous_settlement_candle": 1}


async def test_the_count_is_the_same_whichever_order_the_wire_puts_the_duplicates_in():
    """THE property, stated directly: a re-fetch that paginates differently must not change a count.

    Presented one way a wire-order-dependent implementation settles at 1.5, the other way at 9.9.
    Both are counted, so a naive implementation reports 1 BOTH times and looks stable while the
    settlement PRICE silently differs. Refusing the series is what makes the two runs agree for the
    right reason.
    """
    forward = {("501", "TOK", "1H"): _duplicate_ts_series("1H", BASE_MS, closes=[1.5, 9.9])}
    backward = {("501", "TOK", "1H"): _duplicate_ts_series("1H", BASE_MS, closes=[9.9, 1.5])}
    a = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, forward), _filters())
    b = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, backward), _filters())
    assert _count_for(a, "501", "1H") == _count_for(b, "501", "1H")
    assert _count_for(a, "501", "1H").rejection_reasons == {"ambiguous_settlement_candle": 1}


async def test_identical_duplicate_rows_collapse_and_still_settle():
    """Identical rows carry no ambiguity - whichever wins, the settlement is the same. Refusing them
    would throw away a settleable trial for a wire quirk that cannot change the answer."""
    series = {("501", "TOK", "1H"): _duplicate_ts_series("1H", BASE_MS, closes=[1.5, 1.5])}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 1
    assert _count_for(result, "501", "1H").rejection_reasons == {}


async def test_every_series_handed_to_h32_has_unique_open_times(monkeypatch):
    """The caller obligation as a POSTCONDITION, asserted at the call boundary itself.

    The probe's COUNT cannot pin this. With identical duplicate rows the settlement is the same
    whether or not they were collapsed, so a count-based assertion passes either way and the collapse
    goes unobserved - which is exactly what the mutation battery caught. What actually has to hold is
    that H3.2 is never handed a series outside the regime its determinism guarantee covers, so that
    is asserted where it happens rather than inferred from a downstream number.
    """
    handed = []
    real = preflight.select_settlement_candle

    def spy(series, *, t0_ms, horizon_ms):
        handed.append(series)
        return real(series, t0_ms=t0_ms, horizon_ms=horizon_ms)

    monkeypatch.setattr(preflight, "select_settlement_candle", spy)
    series = {("501", "TOK", "1H"): _duplicate_ts_series("1H", BASE_MS, closes=[1.5, 1.5])}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())

    assert handed, "H3.2 was never called - this vector does not exercise the boundary it claims to"
    for series_handed in handed:
        open_times = [candle.ts_open_ms for candle in series_handed.candles]
        assert len(open_times) == len(set(open_times)), f"duplicate ts_open_ms reached H3.2: {open_times}"
    assert _count_for(result, "501", "1H").eligible_settleable == 1


async def test_a_duplicate_differing_only_in_confirmed_is_also_refused():
    """Equality is compared over the WHOLE Candle, not over the fields selection reads today. A row
    differing only in `confirmed` changes ELIGIBILITY rather than price, and is just as ambiguous."""
    ts_open = BASE_MS
    series = {
        ("501", "TOK", "1H"): CandleSeries(
            "1H", BAR_MS["1H"], (_candle(ts_open, confirmed=True), _candle(ts_open, confirmed=False))
        )
    }
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())
    assert _count_for(result, "501", "1H").rejection_reasons == {"ambiguous_settlement_candle": 1}


async def test_distinct_open_times_are_never_treated_as_ambiguous():
    """The guard must not fire on the ordinary case - H3.2 IS deterministic given distinct ts."""
    series = _both_bars("501", "TOK", BASE_MS, BASE_MS + COOLDOWN_MS)
    pages = {"501": [_page([_sig(t0=BASE_MS), _sig(t0=BASE_MS + COOLDOWN_MS)])]}
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 2
    assert _count_for(result, "501", "1H").rejection_reasons == {}


# --- PKT-TASK-H2-3-A2 obligation 2: the THREE UNSCORED causes must not collapse into one bucket -----


@pytest.mark.parametrize(
    ("series_for_token", "expected_reason"),
    [
        (None, "no_candles_returned"),
        ("unconfirmed", "no_confirmed_candle"),
        ("out_of_window", "settlement_window_gap"),
    ],
    ids=["empty-series", "only-unconfirmed", "gap-past-T"],
)
async def test_each_unscored_cause_gets_its_own_reason(series_for_token, expected_reason):
    """H3.2 returns None from three causes. Each is a DIFFERENT operator finding: absence of candles
    is retention, an unconfirmed tail is timing, a gap past T is liquidity."""
    if series_for_token is None:
        series = {}
    elif series_for_token == "unconfirmed":
        series = {("501", "TOK", "1H"): _settling_series("1H", BASE_MS, confirmed=False)}
    else:
        series = {("501", "TOK", "1H"): _settling_series("1H", BASE_MS - 10 * HORIZON_MS)}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([_sig()])]}, series), _filters())
    combo = _count_for(result, "501", "1H")
    assert combo.eligible_settleable == 0
    assert combo.rejection_reasons == {expected_reason: 1}


async def test_the_three_unscored_causes_are_mutually_distinguishable():
    """One probe, three tokens, three causes - they must land in three separate buckets, not one.
    A single bucket would answer "nothing settled" and hide which of the three it was."""
    pages = {
        "501": [
            _page(
                [
                    _sig(token="EMPTY"),
                    _sig(token="UNCONF", t0=BASE_MS + COOLDOWN_MS),
                    _sig(token="GAP", t0=BASE_MS + 2 * COOLDOWN_MS),
                ]
            )
        ]
    }
    series = {
        ("501", "UNCONF", "1H"): _settling_series("1H", BASE_MS + COOLDOWN_MS, confirmed=False),
        ("501", "GAP", "1H"): _settling_series("1H", BASE_MS - 10 * HORIZON_MS),
    }
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "501", "1H").rejection_reasons == {
        "no_candles_returned": 1,
        "no_confirmed_candle": 1,
        "settlement_window_gap": 1,
    }


# --- the eligibility rules -------------------------------------------------------------------------


async def test_a_null_trigger_price_is_rejected_with_its_own_reason():
    pages = {"501": [_page([_sig(drop=("price",))])]}
    result = await run_matrix_probe(FakeMarketClient(pages, _both_bars("501", "TOK", BASE_MS)), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"null_trigger_price": 1}
    assert _count_for(result, "501", "1H").rejection_reasons == {"null_trigger_price": 1}


async def test_an_explicitly_null_trigger_price_is_rejected():
    pages = {"501": [_page([{**_sig(), "price": None}])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"null_trigger_price": 1}


@pytest.mark.parametrize("price", ["0", "-1.5"])
async def test_a_non_positive_trigger_price_is_rejected(price):
    """A zero or negative entry can never be scored - spot_markout rejects it outright."""
    pages = {"501": [_page([_sig(price=price)])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"non_positive_trigger_price": 1}


async def test_an_unparseable_signal_is_rejected_with_its_own_reason():
    pages = {"501": [_page([_sig(drop=("token",))])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"unparseable_signal": 1}


async def test_a_signal_from_another_chain_is_rejected():
    """The feed answering with a different chain must never be counted under the probed one."""
    pages = {"501": [_page([_sig(chain="196")])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"chain_mismatch": 1}
    assert _count_for(result, "196", "1m").eligible_settleable == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"wallet_count": "2"}, None),
        ({"wallet_count": "1"}, "min_address_count"),
        ({"amount": "1000"}, None),
        ({"amount": "999"}, "min_amount_usd"),
        ({"market_cap": "100000"}, None),
        ({"market_cap": "99999"}, "min_market_cap_usd"),
    ],
)
async def test_numeric_filter_thresholds_are_inclusive(kwargs, reason):
    """Exactly-at-threshold PASSES and one below is rejected, for each filter in turn. `>=`, not `>`."""
    pages = {"501": [_page([_sig(**kwargs)])]}
    client = FakeMarketClient(pages, _both_bars("501", "TOK", BASE_MS))
    result = await run_matrix_probe(client, _filters())
    combo = _count_for(result, "501", "1H")
    if reason is None:
        assert combo.eligible_settleable == 1 and combo.rejection_reasons == {}
    else:
        assert combo.eligible_settleable == 0 and combo.rejection_reasons == {reason: 1}


async def test_thresholds_track_the_filters_actually_supplied():
    """Kills thresholds hard-coded to the frozen manifest values instead of read from SignalFilters."""
    pages = {"501": [_page([_sig(amount="1500")])]}
    series = _both_bars("501", "TOK", BASE_MS)
    strict = {"196": SignalFilters(chain_index="196"), "501": SignalFilters(chain_index="501", min_amount_usd=5000)}
    result = await run_matrix_probe(FakeMarketClient(pages, series), strict)
    assert _count_for(result, "501", "1H").rejection_reasons == {"min_amount_usd": 1}


@pytest.mark.parametrize("wallet_type", ["2", "2,3"])
async def test_a_signal_missing_the_filtered_wallet_type_is_rejected(wallet_type):
    pages = {"501": [_page([_sig(wallet_type=wallet_type)])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"wallet_type_filter": 1}


async def test_a_wallet_type_code_that_merely_contains_the_filter_is_rejected():
    """`walletType="11"` must not satisfy a filter of `"1"`.

    Membership is over comma-separated CODES, not substrings. SPEC argued a substring match
    equivalent because §5.1 pins single-digit codes, so the collision is unreachable on the
    documented domain - a sound argument that expires the day OKX emits a two-digit code. Pinning it
    costs one vector and makes the argument unnecessary.
    """
    pages = {"501": [_page([_sig(wallet_type="11")])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"wallet_type_filter": 1}


async def test_a_multi_category_wallet_type_that_includes_the_filter_is_kept():
    pages = {"501": [_page([_sig(wallet_type="1,2")])]}
    client = FakeMarketClient(pages, _both_bars("501", "TOK", BASE_MS))
    result = await run_matrix_probe(client, _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 1


async def test_rejection_reasons_accumulate_counts_rather_than_flags():
    pages = {"501": [_page([_sig(token="A", price="0"), _sig(token="B", price="0"), _sig(token="C", price="0")])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {"non_positive_trigger_price": 3}


# --- pagination ------------------------------------------------------------------------------------


async def test_probe_follows_the_cursor_across_pages():
    pages = {
        "501": [
            _page([_sig(token="A", t0=BASE_MS)], next_index=1),
            _page([_sig(token="B", t0=BASE_MS + COOLDOWN_MS)]),
        ]
    }
    series = {**_both_bars("501", "A", BASE_MS), **_both_bars("501", "B", BASE_MS + COOLDOWN_MS)}
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters())
    assert _count_for(result, "501", "1H").eligible_settleable == 2


class EndlessCursorClient:
    """Always answers with another cursor, so pagination never terminates on its own."""

    def __init__(self):
        self.pages_served = 0

    async def list_signals(self, f, cursor=None):
        self.pages_served += 1
        return SignalPage((), str(self.pages_served))

    async def get_candles(self, chain_index, token, bar, *, before_ms=None, limit=100):
        return CandleSeries(bar, BAR_MS[bar], ())


async def test_pagination_past_max_pages_fails_rather_than_reporting_a_floor(monkeypatch):
    """Spec 5.1 requires the per-combo counts to be AUDITABLE. Returning what had been collected so
    far would report a floor as a count - the one number a preflight must never produce."""
    monkeypatch.setattr(preflight, "MAX_PAGES", 3)
    client = EndlessCursorClient()
    with pytest.raises(PreflightError, match="exceeded MAX_PAGES"):
        await run_matrix_probe(client, _filters())
    assert client.pages_served == 3


async def test_candles_are_fetched_once_per_token_and_bar():
    """Two signals on one token must not cost four candle fetches; the series is bar-and-token keyed."""
    pages = {"501": [_page([_sig(t0=BASE_MS), _sig(t0=BASE_MS + COOLDOWN_MS)])]}
    client = FakeMarketClient(pages, _both_bars("501", "TOK", BASE_MS))
    await run_matrix_probe(client, _filters())
    assert sorted((c, t, b) for c, t, b, _, _ in client.candle_calls) == [("501", "TOK", "1H"), ("501", "TOK", "1m")]


async def test_the_frozen_candle_limit_reaches_the_client():
    """CANDLE_LIMIT sets the DEPTH of the live retention answer, and no fake observed it.

    Mutating it 100 -> 5 survived the whole suite (SPEC MINOR-4). The module's own docstring calls
    the resulting window "the retention observation §5.1 asks for" and that answer decides H6.1, so
    one number carries it. Asserted against a LITERAL as well as the constant: a vector derived from
    the constant under test cannot detect that constant moving.
    """
    client = FakeMarketClient({"501": [_page([_sig()])]}, _both_bars("501", "TOK", BASE_MS))
    await run_matrix_probe(client, _filters())
    assert client.candle_calls, "no candle was fetched - this vector does not exercise the boundary"
    for _, _, _, limit, before_ms in client.candle_calls:
        assert limit == 100
        # `before_ms` is deliberately never passed: its wire semantics are not pinned by the frozen
        # contract, and guessing a pagination direction would skew every close_ts. Pinned so the
        # deliberate omission cannot become an accidental inclusion.
        assert before_ms is None
    assert preflight.CANDLE_LIMIT == 100


async def test_a_client_error_is_never_absorbed_into_an_empty_probe():
    """A failed probe must NOT be representable as a legitimate zero-count matrix - that is exactly
    this lane's Gate 1 MAJOR, one stage further downstream."""
    with pytest.raises(OKXAPIError, match="51000"):
        await run_matrix_probe(FakeMarketClient(fail_on="list_signals"), _filters())


async def test_a_candle_error_is_never_absorbed_either():
    pages = {"501": [_page([_sig()])]}
    with pytest.raises(OKXAPIError, match="51000"):
        await run_matrix_probe(FakeMarketClient(pages, fail_on="get_candles"), _filters())


# --- direction semantics ----------------------------------------------------------------------------


async def test_direction_is_confirmed_only_when_every_signal_declares_buy():
    pages = {"501": [_page([_sig(direction="buy"), _sig(token="B", direction="buy")])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is True


@pytest.mark.parametrize("direction", ["sell", "short", "", "2"])
async def test_a_single_non_buy_signal_unconfirms_the_whole_probe(direction):
    """Never guess polarity: one signal we cannot read as a buy takes the whole matrix to unconfirmed."""
    pages = {"501": [_page([_sig(direction="buy"), _sig(token="B", direction=direction)])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is False


@pytest.mark.parametrize(
    ("buy_key", "sell_key"),
    [("direction", "side"), ("side", "signalType"), ("signalType", "tradeDirection"), ("tradeDirection", "direction")],
)
async def test_contradictory_direction_markers_unconfirm_the_probe(buy_key, sell_key):
    """A payload contradicting itself says we do not understand its schema.

    Parametrized across all four keys rather than the `direction`/`side` pair alone: with only that
    pair exercised, deleting `signalType` or `tradeDirection` from _DIRECTION_KEYS survived the whole
    suite, and under that mutant a payload contradicting under the deleted key would CONFIRM buy
    polarity - fail-open on the polarity surface (SPEC MINOR-3).
    """
    raw = {**_sig(drop=("direction",)), buy_key: "buy", sell_key: "sell"}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([raw])]}), _filters())
    assert result.direction_semantics_confirmed is False


@pytest.mark.parametrize("key", ["direction", "side", "signalType", "tradeDirection"])
async def test_every_direction_key_is_actually_consulted(key):
    """Each of the four keys in _DIRECTION_KEYS must be read, in BOTH directions.

    Removing any one from the tuple makes a payload labelled only under it read as "no marker
    present": the buy case then yields False (fail-closed, caught here) and a contradiction under it
    yields True (fail-OPEN, caught by the test above). The tuple's declared REACH is the property.
    """
    buy = {**_sig(drop=("direction",)), key: "buy"}
    confirmed = await run_matrix_probe(FakeMarketClient({"501": [_page([buy])]}), _filters())
    assert confirmed.direction_semantics_confirmed is True

    sell = {**_sig(drop=("direction",)), key: "sell"}
    contradicted = await run_matrix_probe(FakeMarketClient({"501": [_page([sell])]}), _filters())
    assert contradicted.direction_semantics_confirmed is False


@pytest.mark.parametrize("marker", ["buy", "b", "long", "BUY", "Long", "  buy  "])
async def test_every_recognized_buy_marker_confirms(marker):
    """Each member of _BUY_MARKERS, plus the case-folding and stripping the reader applies.

    Only lowercase `"buy"` was exercised, so removing `"long"` or `"b"` from the set survived, and so
    did dropping `.casefold()` - `"BUY"` would then read as unrecognized. Both fail CLOSED, which is
    why they rank below the fail-open gap above, but an unpinned marker set is still an unpinned
    constant that enumeration category (a) claims to cover.
    """
    raw = {**_sig(drop=("direction",)), "direction": marker}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([raw])]}), _filters())
    assert result.direction_semantics_confirmed is True


@pytest.mark.parametrize("marker", ["sell", "short", "1", "0", "buy_maybe", ""])
async def test_no_unrecognized_marker_is_read_as_a_buy(marker):
    """The closed half of the same constant: nothing outside the set may confirm polarity. `"1"` and
    `"0"` are here deliberately - a numeric side code is an ENCODING GUESS and must never confirm."""
    raw = {**_sig(drop=("direction",)), "direction": marker}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([raw])]}), _filters())
    assert result.direction_semantics_confirmed is False


async def test_direction_is_unconfirmed_when_the_feed_carries_no_direction_field():
    pages = {"501": [_page([_sig(drop=("direction",))])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is False


async def test_a_partially_labelled_feed_is_unconfirmed():
    pages = {"501": [_page([_sig(direction="buy"), _sig(token="B", drop=("direction",))])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is False


async def test_an_empty_probe_confirms_nothing():
    """Zero observations is not evidence of buy semantics, and an empty probe must land on no_season."""
    result = await run_matrix_probe(FakeMarketClient(), _filters())
    assert result.direction_semantics_confirmed is False
    assert select_combo(result).season_status == "no_season"


async def test_direction_is_read_from_the_raw_feed_not_only_from_eligible_trials():
    """A signal our filters drop still tells us what the FEED means. Judging polarity only on the
    survivors would let one filtered sell signal go unnoticed."""
    pages = {"501": [_page([_sig(direction="buy"), _sig(token="B", amount="1", direction="sell")])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is False


async def test_an_unconfirmed_direction_survives_a_fully_qualified_matrix():
    """End-to-end: real counts, unreadable polarity, no season."""
    tokens = [f"T{i}" for i in range(3)]
    pages = {
        "501": [_page([_sig(token=t, t0=BASE_MS + i * COOLDOWN_MS, drop=("direction",)) for i, t in enumerate(tokens)])]
    }
    series = {}
    for i, token in enumerate(tokens):
        series.update(_both_bars("501", token, BASE_MS + i * COOLDOWN_MS))
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), min_trials=2)
    assert _count_for(result, "501", "1H").eligible_settleable == 3
    assert select_combo(result, min_trials=2).season_status == "no_season"


# ==================================================================================================
# Structural guards: the probe module is transport-free, and the operator script has no import-time
# side effects. STEP 5 IS OPERATOR-ONLY - nothing here constructs a real client or a real transport.
# ==================================================================================================


def test_the_preflight_module_imports_no_http_client():
    source = inspect.getsource(preflight)
    assert not re.search(r"^\s*(import|from)\s+(httpx|requests|aiohttp|urllib)\b", source, re.MULTILINE)


def _run_preflight_path():
    return Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "run_preflight.py"


def _load_run_preflight():
    script = _run_preflight_path()
    assert script.exists(), f"operator script missing at {script}"
    spec = importlib_util.spec_from_file_location("run_preflight_under_test", script)
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_operator_script_imports_without_credentials_or_network(monkeypatch):
    """Importing must not read credentials, construct a transport, or touch the network. If it did,
    this test would itself become the live request the packet prohibits."""
    for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    module = _load_run_preflight()
    assert hasattr(module, "main")


def test_the_operator_script_parses_the_frozen_defaults():
    module = _load_run_preflight()
    args = module.parse_args(["--out", "preflight_result.json"])
    assert args.out == Path("preflight_result.json")
    assert (args.min_trials, args.cooldown_ms, args.horizon_ms) == (40, 14_400_000, 3_600_000)


def test_the_operator_script_requires_an_output_path():
    module = _load_run_preflight()
    with pytest.raises(SystemExit):
        module.parse_args([])


def test_the_operator_summary_prints_all_four_counts_and_their_reasons():
    module = _load_run_preflight()
    matrix = _full(
        41,
        3,
        0,
        0,
        reasons=[{"same_token_cooldown": 7}, {"settlement_window_gap": 11}, {}, {"chain_mismatch": 2}],
    )
    summary = module.render_summary(select_combo(matrix), matrix)
    for chain, bar in COMBO_ORDER:
        assert f"{chain}" in summary and f"{bar}" in summary
    assert "same_token_cooldown=7" in summary
    assert "settlement_window_gap=11" in summary
    assert "chain_mismatch=2" in summary
    assert "qualified" in summary


def test_the_operator_summary_states_the_direction_flag():
    module = _load_run_preflight()
    matrix = _full(60, 60, 60, 60, ok=False)
    summary = module.render_summary(select_combo(matrix), matrix)
    assert "direction_semantics_confirmed" in summary
    assert "no_season" in summary


def test_a_missing_credential_names_the_variable_and_never_its_value():
    module = _load_run_preflight()
    env = {"OKX_API_KEY": "SENTINEL-KEY-DO-NOT-LEAK", "OKX_SECRET_KEY": "SENTINEL-SECRET-DO-NOT-LEAK"}
    with pytest.raises(module.MissingCredentialError, match="OKX_PASSPHRASE") as excinfo:
        module.credentials_from_env(env)
    assert "SENTINEL" not in str(excinfo.value)


def test_a_blank_credential_is_treated_as_missing():
    """An empty string is not a credential. Accepting it would send an unauthenticated live request."""
    module = _load_run_preflight()
    env = {"OKX_API_KEY": "SENTINEL-KEY-DO-NOT-LEAK", "OKX_SECRET_KEY": "   ", "OKX_PASSPHRASE": "p"}
    with pytest.raises(module.MissingCredentialError, match="OKX_SECRET_KEY"):
        module.credentials_from_env(env)


def test_credential_values_never_reach_a_rendered_string():
    module = _load_run_preflight()
    env = {
        "OKX_API_KEY": "SENTINEL-KEY-DO-NOT-LEAK",
        "OKX_SECRET_KEY": "SENTINEL-SECRET-DO-NOT-LEAK",
        "OKX_PASSPHRASE": "SENTINEL-PASS-DO-NOT-LEAK",
    }
    creds = module.credentials_from_env(env)
    assert "SENTINEL" not in repr(creds)


def test_the_failure_reason_redacts_every_credential_value():
    """An upstream error message that echoed a credential would be written verbatim into an artifact
    the operator is expected to attach to a review."""
    module = _load_run_preflight()
    secrets = ["SENTINEL-KEY-DO-NOT-LEAK", "SENTINEL-PASS-DO-NOT-LEAK"]
    redacted = module.redact("auth failed for SENTINEL-KEY-DO-NOT-LEAK with SENTINEL-PASS-DO-NOT-LEAK", secrets)
    assert "SENTINEL" not in redacted
    assert "auth failed" in redacted


def test_redaction_ignores_empty_secrets():
    """An empty secret must not turn every character of the message into a redaction marker."""
    module = _load_run_preflight()
    assert module.redact("connection refused", ["", "   "]) == "connection refused"
