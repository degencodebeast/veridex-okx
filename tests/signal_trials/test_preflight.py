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

import ast
import functools
import inspect
import json
import os
import pathlib
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


def _run_preflight_path():
    return Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "run_preflight.py"


@functools.lru_cache(maxsize=1)
def _load_run_preflight():
    """Load the operator script BY PATH, once.

    By path because importing scripts.signal_trials.run_preflight would need a
    scripts/signal_trials/__init__.py this task does not own. Cached because eleven call sites each
    running a full exec_module is eleven executions of a module whose whole point is that importing
    it does nothing (QUALITY NIT-1).
    """
    script = _run_preflight_path()
    assert script.exists(), f"operator script missing at {script}"
    spec = importlib_util.spec_from_file_location("run_preflight_under_test", script)
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_the_polarity_constants_are_a_CLOSED_set():
    """Membership, not just behaviour over a sample.

    Both constants were exercised by parametrized families over hard-coded literals - correct per
    C18 - but neither constant's MEMBERSHIP was asserted, so widening either survived the whole
    suite: `_DIRECTION_KEYS += ("tradeSide",)` and `_BUY_MARKERS |= {"bid"}` both passed 463 tests
    (QUALITY MINOR-1). That is the fail-OPEN direction on "never guess polarity", the one property
    the module docstring elevates above all others, and the file already applies exactly this
    discipline to COMBO_ORDER, CANDLE_LIMIT and RESULT_KEYS. Applying it unevenly is what lets a
    fifth key land precisely where the lock does not reach.
    """
    assert preflight._DIRECTION_KEYS == ("direction", "side", "signalType", "tradeDirection")
    assert frozenset({"buy", "b", "long"}) == preflight._BUY_MARKERS


def test_the_frozen_pagination_bound_is_pinned():
    """MAX_PAGES was the one frozen constant with no literal pin while CANDLE_LIMIT had one.

    It is a runaway bound rather than a correctness constant, so the stakes are lower - but the
    asymmetry is the same shape as MINOR-1 (QUALITY NIT-2), and it is one line.
    """
    assert preflight.MAX_PAGES == 100


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


# The packet names FOUR Law surfaces: veridex/scoring.py, veridex/leaderboard.py,
# veridex/rank_guards.py and veridex/law/** - the last of which exists in the tree with edge.py
# and recompute.py, and was missing here. The test's NAME asserts the general property, so
# enumerating three of four made the name overclaim (QUALITY MINOR-6).
LAW_OWNED_MODULES = ("spot_markout", "scoring", "leaderboard", "rank_guards", "law")


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
    """An all-zero matrix is no_season at every threshold.

    CORRECTED (QUALITY MINOR-3). This docstring previously claimed that with min_trials=1 a naive
    `count >= min_trials` scan "would call combo 1 QUALIFIED at zero". That is arithmetically FALSE -
    `0 >= 1` is False, so such a scan falls through and returns EXPLORATORY, not qualified.

    And the ordering the old name claimed is NOT pinned here, because it cannot be: given the
    `min_trials >= 1` guard the qualification scan can never fire on an all-zero matrix, so moving
    the all-zero gate below the scan is BEHAVIOURALLY EQUIVALENT at this head. There is no vector
    that makes "outranks" true. Saying so is the honest resolution; the ordering becomes
    load-bearing only if the threshold guard is ever relaxed, and that change would need its own pin.
    """
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
    "policy",
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


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="the discriminator is the file-mode permission check on open(path, 'w'), which POSIX "
    "defines as bypassed for euid 0 - as root the in-place mutant would succeed and this test "
    "would pass while pinning nothing (QUALITY MINOR-8 / OBS-1)",
)
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
    """In-memory stand-in for the frozen Signal List source. Holds literal payloads; no I/O at all.

    It DECLARES `signal_source_direction` because that is what it is standing in for: a client bound
    to the documented buy-direction endpoint. A fake that forgets the declaration fails closed, which
    is the point - the declaration is the thing being relied on, so a stand-in has to state it too.
    """

    signal_source_direction = "buy"

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
    direction=None,
    drop=(),
):
    """One WIRE-REALISTIC Signal List row that passes the default SignalFilters.

    The members are exactly the documented return fields of
    `POST /api/v6/dex/market/signal/list` - timestamp, chainIndex, price, walletType,
    triggerWalletCount, triggerWalletAddress, amountUsd, soldRatioPercent, token.* and cursor.

    `direction` DEFAULTS TO None, i.e. ABSENT, because the endpoint returns no such member. An
    earlier revision injected `"direction": "buy"` unconditionally, and that single line held "a
    direction field is present" CONSTANT across every vector in the file - so the one dimension
    never varied was WHAT A REAL OKX ROW LOOKS LIKE, and the suite could not see that a conforming
    response was unpublishable. Callers that need a marker now pass one explicitly, which is also
    what makes those vectors visible as the non-default case they are.
    """
    raw = {
        "timestamp": str(t0),
        "chainIndex": chain,
        "price": price,
        "walletType": wallet_type,
        "triggerWalletCount": wallet_count,
        "triggerWalletAddress": "0xa,0xb,0xc",
        "amountUsd": amount,
        "soldRatioPercent": "0",
        "cursor": "c0",
        "token": {
            "tokenAddress": token,
            "symbol": "TOK",
            "name": "Token",
            "logo": "https://example.invalid/logo.png",
            "marketCapUsd": market_cap,
            "holders": "900",
            "top10HolderPercent": "31.5",
        },
    }
    if direction is not None:
        raw["direction"] = direction
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


async def test_the_cooldown_rejection_is_bumped_by_the_BATCH_not_by_one():
    """The `dropped` amount is an argument, and nothing distinguished it from a hard-coded 1.

    `test_rejection_reasons_accumulate_counts_rather_than_flags` pins "counts, not flags" - but with
    three SEPARATE rejections that each bump by 1, which cannot tell an amount argument from a
    literal. The cooldown path is the only site that bumps by a batch, and every cooldown vector in
    the suite dropped exactly one signal, so `_bump(..., dropped)` -> `_bump(..., 1)` was invisible
    across 463 tests (QUALITY MINOR-4). Three same-token signals inside one window drop TWO.

    The number is operator-facing: it feeds the density-versus-retention answer that decides H6.1.
    """
    pages = {
        "501": [
            _page(
                [
                    _sig(t0=BASE_MS),
                    _sig(t0=BASE_MS + 3_600_000),
                    _sig(t0=BASE_MS + 7_200_000),
                ]
            )
        ]
    }
    series = _both_bars("501", "TOK", BASE_MS)
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), cooldown_ms=COOLDOWN_MS)
    assert _count_for(result, "501", "1H").eligible_settleable == 1
    assert _count_for(result, "501", "1H").rejection_reasons == {"same_token_cooldown": 2}


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"price": "0", "wallet_type": "2"}, "non_positive_trigger_price"),
        ({"wallet_type": "2", "amount": "1"}, "wallet_type_filter"),
        ({"amount": "1", "market_cap": "1"}, "min_amount_usd"),
        ({"chain": "196", "price": "0"}, "chain_mismatch"),
    ],
    ids=["price-before-wallet", "wallet-before-amount", "amount-before-marketcap", "chain-before-price"],
)
async def test_a_signal_breaking_two_rules_is_attributed_to_the_FIRST(kwargs, expected_reason):
    """`_screen` documents a first-match order and no vector exercised it (SPEC G9 / QUALITY MINOR-5).

    Every existing vector breaks exactly ONE rule, so hoisting the wallet_type check above the
    non-positive-price check survived 463 tests. Counts are unaffected - the signal is rejected
    either way - but the REASON LABEL is not, and rejection_reasons is the operator-facing number
    that answers 5.1's density-versus-retention question. The module docstring calls the attribution
    a responsibility: "a rejected signal is never merely dropped".
    """
    pages = {"501": [_page([_sig(**kwargs)])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert _count_for(result, "501", "1m").rejection_reasons == {expected_reason: 1}


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

    signal_source_direction = "buy"

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
    raw = {**_sig(), buy_key: "buy", sell_key: "sell"}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([raw])]}), _filters())
    assert result.direction_semantics_confirmed is False


@pytest.mark.parametrize("key", ["direction", "side", "signalType", "tradeDirection"])
async def test_every_direction_key_is_actually_consulted(key):
    """Each of the four keys in _DIRECTION_KEYS must be read, in BOTH directions.

    Removing any one from the tuple makes a payload labelled only under it read as "no marker
    present": the buy case then yields False (fail-closed, caught here) and a contradiction under it
    yields True (fail-OPEN, caught by the test above). The tuple's declared REACH is the property.
    """
    buy = {**_sig(), key: "buy"}
    confirmed = await run_matrix_probe(FakeMarketClient({"501": [_page([buy])]}), _filters())
    assert confirmed.direction_semantics_confirmed is True

    sell = {**_sig(), key: "sell"}
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
    raw = {**_sig(), "direction": marker}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([raw])]}), _filters())
    assert result.direction_semantics_confirmed is True


@pytest.mark.parametrize("marker", ["sell", "short", "1", "0", "buy_maybe", ""])
async def test_no_unrecognized_marker_is_read_as_a_buy(marker):
    """The closed half of the same constant: nothing outside the set may confirm polarity. `"1"` and
    `"0"` are here deliberately - a numeric side code is an ENCODING GUESS and must never confirm."""
    raw = {**_sig(), "direction": marker}
    result = await run_matrix_probe(FakeMarketClient({"501": [_page([raw])]}), _filters())
    assert result.direction_semantics_confirmed is False


# The documented Signal List response schema, transcribed field-for-field from
# .agents/skills/okx-dex-market/references/signal-cli-reference.md section 3 "Return fields".
# NOTHING is added. This list is the thing the fixture is checked against, and it is written out
# rather than derived from the fixture - a vector derived from the thing under test cannot detect
# that thing drifting.
DOCUMENTED_SIGNAL_ROW_FIELDS = {
    "timestamp",
    "chainIndex",
    "price",
    "walletType",
    "triggerWalletCount",
    "triggerWalletAddress",
    "amountUsd",
    "soldRatioPercent",
    "cursor",
    "token",
}
DOCUMENTED_SIGNAL_TOKEN_FIELDS = {
    "tokenAddress",
    "symbol",
    "name",
    "logo",
    "marketCapUsd",
    "holders",
    "top10HolderPercent",
}


def test_the_fixture_matches_the_DOCUMENTED_upstream_schema_field_for_field():
    """STANDING LESSON 94. At least one vector must be the documented row with nothing invented.

    The previous fixture injected a synthetic `direction` key, so every vector in this file held
    "a direction field is present" CONSTANT - and the dimension never varied was what a real row
    looks like. This assertion is what would have caught the milestone MAJOR-1, and it is written
    as an exact set comparison so an invented member cannot be added without failing here.
    """
    row = _sig()
    assert set(row) == DOCUMENTED_SIGNAL_ROW_FIELDS
    assert set(row["token"]) == DOCUMENTED_SIGNAL_TOKEN_FIELDS
    for invented in ("direction", "side", "signalType", "tradeDirection"):
        assert invented not in row, f"the default fixture invented a {invented!r} member"


class BareSource:
    """A source that OMITS the attestation attribute entirely - the realistic non-declaring shape.

    Deliberately NOT a FakeMarketClient subclass: inheriting would inherit the attestation, which is
    exactly how the two cases below missed the `getattr` DEFAULT. `OKXMarketClient` carries no such
    attribute either (0 hits), which is the whole reason FrozenSignalListSource exists - so an object
    with no attribute is what a real non-declaring source looks like, and `= None` is a shape nobody
    would write.
    """

    def __init__(self, pages_by_chain=None, series_by_key=None, *, fail_on=None):
        self._inner = FakeMarketClient(pages_by_chain, series_by_key, fail_on=fail_on)

    async def list_signals(self, f, cursor=None):
        return await self._inner.list_signals(f, cursor)

    async def get_candles(self, chain_index, token, bar, *, before_ms=None, limit=100):
        return await self._inner.get_candles(chain_index, token, bar, before_ms=before_ms, limit=limit)


class AttestsNoneSource(FakeMarketClient):
    """A source that DEFINES the attribute and sets it to None. Real, but not the realistic case."""

    signal_source_direction = None


class SellSideSource(FakeMarketClient):
    """A source that attests a polarity other than the frozen buy contract."""

    signal_source_direction = "sell"


def test_the_bare_source_really_omits_the_attestation_attribute():
    """The precondition the parametrized vector below depends on.

    If BareSource ever grew the attribute - by inheritance or by edit - the `omits-attribute` case
    would silently become a third copy of `attests-none`, and the getattr DEFAULT would go unpinned
    again without any test failing. This is the assertion that would notice.
    """
    assert not hasattr(BareSource, "signal_source_direction")
    assert not hasattr(BareSource(), "signal_source_direction")


@pytest.mark.parametrize(
    "source_cls",
    [BareSource, AttestsNoneSource, SellSideSource],
    ids=["omits-attribute", "attests-none", "declares-sell"],
)
async def test_a_source_that_does_not_attest_the_buy_contract_FAILS_CLOSED(source_cls):
    """THE THIRD OUTCOME. Confirmation comes from the source's contract, never from clean rows.

    Absence of a marker under a DECLARED buy-direction source is confirmation by contract;
    a contradicting marker is refusal; and an UNDECLARED source is refusal too. These rows are
    byte-identical to the ones that confirm in the test below - the ONLY difference is whether the
    source attests what endpoint it is bound to. Without this, generalising the client to a source
    whose polarity is not contractually fixed would silently inherit a guarantee it no longer has.

    THREE cases, and the first one is the one that was missing. Both original vectors DEFINED the
    attribute, so the `getattr` DEFAULT was never consulted by any test and mutating it from None to
    the frozen contract - making a source that declares NOTHING confirm buy polarity - survived all
    499 tests. That is the same structural error as the milestone MAJOR-1 one level down and on the
    fail-open side: every vector held the discriminating dimension (is the attribute DEFINED?)
    constant, while the parametrize id claimed the dimension it did not reach.
    """
    pages = {"501": [_page([_sig()])]}
    result = await run_matrix_probe(source_cls(pages), _filters())
    assert result.direction_semantics_confirmed is False


async def test_a_documented_signal_list_row_CONFIRMS_buy_semantics():
    """THE regression test for the milestone MAJOR-1, and it inverts what this test used to assert.

    The documented Signal List row carries NO direction member; polarity is fixed by the endpoint
    contract ("Get latest buy-direction token signals"). The previous revision required a per-row
    marker, so this vector asserted `is False` - a PASSING TEST FOR THE DEFECT. Together with
    test_unconfirmed_direction_forces_no_season it demonstrated that a conforming response could
    never publish a season, which is the entire purpose of the task failing closed.
    """
    pages = {"501": [_page([_sig()])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is True


async def test_a_wire_realistic_probe_can_reach_a_QUALIFIED_season():
    """End-to-end on documented rows only: the probe must be able to PUBLISH.

    Nothing in this vector invents a wire member. If a per-row direction marker is ever required
    again, this goes to no_season and the task's purpose fails closed once more.
    """
    tokens = [f"T{i}" for i in range(3)]
    pages = {"501": [_page([_sig(token=t, t0=BASE_MS + i * COOLDOWN_MS) for i, t in enumerate(tokens)])]}
    series = {}
    for i, token in enumerate(tokens):
        series.update(_both_bars("501", token, BASE_MS + i * COOLDOWN_MS))
    result = await run_matrix_probe(FakeMarketClient(pages, series), _filters(), min_trials=3)

    assert result.direction_semantics_confirmed is True
    assert _count_for(result, "501", "1m").eligible_settleable == 3
    assert _count_for(result, "501", "1H").eligible_settleable == 3
    selection = select_combo(result, min_trials=3)
    assert selection.season_status == "qualified"
    # 501/1m, not 501/1H: both bars settle here and COMBO_ORDER ranks precision above the fallback.
    assert (selection.chain_index, selection.bar) == ("501", "1m")


async def test_a_partially_labelled_feed_is_still_confirmed():
    """A source that labels SOME rows contradicts nothing: the unlabelled ones are the documented
    shape and the labelled one agrees with the contract."""
    pages = {"501": [_page([_sig(direction="buy"), _sig(token="B")])]}
    result = await run_matrix_probe(FakeMarketClient(pages), _filters())
    assert result.direction_semantics_confirmed is True


async def test_one_contradicting_row_unconfirms_a_partially_labelled_feed():
    """The fail-closed half, kept. One explicit non-buy poisons the whole probe."""
    pages = {"501": [_page([_sig(), _sig(token="B", direction="sell")])]}
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
        "501": [_page([_sig(token=t, t0=BASE_MS + i * COOLDOWN_MS, direction="sell") for i, t in enumerate(tokens)])]
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


def _imported_module_names(path):
    """Every module name imported by `path`, parsed from its AST rather than pattern-matched."""
    tree = ast.parse(pathlib.Path(path).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_the_preflight_module_imports_exactly_this_closed_set():
    """The probe module is transport-free BY STRUCTURE - asserted as a CLOSED SET, not a denylist.

    The previous guard was a four-name denylist (httpx|requests|aiohttp|urllib) behind a name that
    asserts the general property, so `import http.client` sailed through 463 tests (QUALITY
    MINOR-7). A closed set cannot be widened silently: any new import, HTTP or otherwise, fails
    here and forces a deliberate decision.
    """
    assert _imported_module_names(preflight.__file__) == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "json",
        "os",
        "pathlib",
        "tempfile",
        "typing",
        "veridex.signal_trials.challenge_spec",
        "veridex.signal_trials.okx_client",
        "veridex.signal_trials.spot_markout",
    }


def test_the_operator_script_imports_exactly_this_closed_set():
    """Same closure on the script. httpx IS expected here - this is the only file allowed a transport."""
    assert _imported_module_names(_run_preflight_path()) == {
        "__future__",
        "argparse",
        "asyncio",
        "collections.abc",
        "httpx",
        "os",
        "pathlib",
        "sys",
        "typing",
        "veridex.signal_trials.okx_client",
        "veridex.signal_trials.preflight",
    }


def test_the_operator_script_imports_without_credentials_or_network(monkeypatch):
    """Importing must not read credentials, construct a transport, or touch the network. If it did,
    this test would itself become the live request the packet prohibits."""
    for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    module = _load_run_preflight()
    assert hasattr(module, "main")
    # The docstring claims the import constructs no TRANSPORT, and the assertion above only pinned
    # "the import did not raise" - a module-scope httpx.AsyncClient(...) passed 463 tests
    # (QUALITY MINOR-7). Assert the claimed property directly.
    live_clients = [name for name, value in vars(module).items() if isinstance(value, module.httpx.AsyncClient)]
    assert live_clients == [], f"import-time transport constructed: {live_clients}"


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


# ==================================================================================================
# QUALITY MAJOR-1 - main() had NO test anywhere in the repository, and six mutants of it survived
# the full 463-test suite. It is 25 lines of orchestration on the operator path, and the suite
# covered parse_args, credentials_from_env, redact and render_summary IN ISOLATION with nothing
# composing them. A GUARD THAT IS TESTED BUT NOT WIRED IS NOT A GUARD.
#
# GATE B: none of these tests executes the script as a program and none can issue a live request.
# Verified independently before they were written, not taken on report: main() was called with the
# credential environment cleared while BOTH httpx.AsyncClient AND socket.socket were replaced by
# tripwires that raise on construction. Result: exit 2, no artifact, NEITHER TRIPWIRE FIRED -
# `credentials_from_env` raises before `_probe` is ever referenced, so no transport is constructed.
# For the failure and success paths `_probe` itself is monkeypatched, which intercepts before any
# transport exists because main resolves it as a module global.
# ==================================================================================================


class LiveClientConstructed(BaseException):
    """Deliberately a BaseException, NOT an Exception.

    main() wraps the probe in a broad `except Exception`. An AssertionError is an Exception, so a
    tripwire raising one is CAUGHT BY THE CODE IT GUARDS and converted into a tidy "failure artifact
    written" - the safety mechanism fires and the suite stays green. Measured, not theorised: with a
    misspelled monkeypatch target the real _probe ran, this tripwire fired, and
    test_main_writes_a_failure_artifact_when_the_probe_raises still PASSED. Deriving from
    BaseException puts it outside the reach of the handler under test.
    """


class _AsyncClientTripwire:
    """Raises if anything tries to construct a live client. Armed in every main() test."""

    def __init__(self, *args, **kwargs):
        raise LiveClientConstructed("TRIPWIRE: a real httpx.AsyncClient was constructed inside a test")


@pytest.fixture
def operator_script(monkeypatch):
    """The loaded script with a live-client tripwire armed and the credential env cleared."""
    module = _load_run_preflight()
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClientTripwire)
    for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "OKX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return module


def _set_sentinel_credentials(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "SENTINEL-KEY-DO-NOT-LEAK")
    monkeypatch.setenv("OKX_SECRET_KEY", "SENTINEL-SECRET-DO-NOT-LEAK")
    monkeypatch.setenv("OKX_PASSPHRASE", "SENTINEL-PASS-DO-NOT-LEAK")


def test_main_aborts_without_credentials_and_RECORDS_the_abort(operator_script, tmp_path, capsys):
    """ABSENCE is decided HERE, and this is the only place in the codebase where it is decided.

    Both reviewers verified carry-forward 5 as SATISFIED and were right at the level they measured:
    write_preflight_result and write_preflight_failure distinguish the three states cleanly. But the
    ABSENCE decision is taken in main, and making this path write a failure artifact instead left
    463 tests green (QUALITY MAJOR-1). The carry-forward was satisfied where it was measured and
    unpinned where it is decided.
    """
    out = tmp_path / "preflight_result.json"
    assert operator_script.main(["--out", str(out)]) == 2
    # CONTRACT CHANGED at this head, and the earlier assertion (`not out.exists()`) encoded the
    # milestone MAJOR-1: writing nothing leaves whatever a PREVIOUS run left, and H2.4 reads the
    # artifact and never sees the terminal. The path must describe THIS invocation.
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["probe_status"] == "aborted"
    assert payload["season_status"] is None
    assert payload["counts"] == []
    assert "MissingCredentialError" in payload["failure_reason"]
    # STANDING LESSON 62: a guard firing is not the guard under test. Exit 2 alone cannot say WHICH
    # guard produced it, so identify the cause - the message must name every missing variable.
    stderr = capsys.readouterr().err
    assert "aborted before any request" in stderr
    for variable in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        assert variable in stderr, f"the abort did not name {variable}"
    assert "SENTINEL" not in stderr


def test_main_redacts_credentials_out_of_the_failure_artifact(operator_script, tmp_path, monkeypatch):
    """THE credential-surface pin. redact() was well tested in isolation and NOT WIRED.

    Deleting the redact() call at run_preflight.py:218 left 463 tests green, and an upstream
    exception message that echoed a key would then be written verbatim into an artifact the operator
    attaches to a review. The module docstring declares this call load-bearing in almost those words.
    """
    _set_sentinel_credentials(monkeypatch)

    async def exploding_probe(creds, args):
        raise RuntimeError("upstream auth failure for key SENTINEL-KEY-DO-NOT-LEAK and SENTINEL-PASS-DO-NOT-LEAK")

    monkeypatch.setattr(operator_script, "_probe", exploding_probe)
    out = tmp_path / "preflight_result.json"
    assert operator_script.main(["--out", str(out)]) == 1

    raw = out.read_text()
    assert "SENTINEL" not in raw, "a credential reached the artifact"
    payload = json.loads(raw)
    assert payload["probe_status"] == "failed"
    assert payload["season_status"] is None
    assert "RuntimeError" in payload["failure_reason"]
    assert "***" in payload["failure_reason"], "the reason must be redacted, not merely emptied"
    assert "upstream auth failure" in payload["failure_reason"], "redaction must not destroy the diagnostic"


def test_main_writes_a_failure_artifact_when_the_probe_raises(operator_script, tmp_path, monkeypatch):
    """The FAILURE state, decided in main. Deleting the write left 463 tests green."""
    _set_sentinel_credentials(monkeypatch)

    async def exploding_probe(creds, args):
        raise OKXAPIError("51000", "Invalid parameter")

    monkeypatch.setattr(operator_script, "_probe", exploding_probe)
    out = tmp_path / "preflight_result.json"
    assert operator_script.main(["--out", str(out)]) == 1

    payload = json.loads(out.read_text())
    assert payload["probe_status"] == "failed"
    # STANDING LESSON 62. `except Exception` catches EVERYTHING, so exit 1 plus "failed" is reached
    # by any error at all - including a bug in this test's own setup. Measured: with a misspelled
    # monkeypatch target the real _probe ran, the live-client tripwire fired, and this test still
    # PASSED. Identify the exception that was actually injected.
    assert "OKXAPIError" in payload["failure_reason"], f"a different guard fired: {payload['failure_reason']}"
    assert "51000" in payload["failure_reason"]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit], ids=["KeyboardInterrupt", "SystemExit"])
def test_main_does_not_swallow_an_operator_interrupt(operator_script, tmp_path, monkeypatch, interrupt):
    """main()'s handler must stay `except Exception`, never widen to BaseException.

    Found by a surviving mutant while closing standing lesson 62, and it is a real operator property
    rather than a test-harness one. The live probe runs for ~30 minutes; widening the handler would
    convert a Ctrl-C into "failure artifact written" and exit 1 - an artifact claiming the PROBE
    FAILED when in fact the operator stopped it. That is a false record in the one document a later
    stage reads, and it is the same absence-versus-failure confusion carry-forward 5 exists to
    prevent, arriving through the exception handler instead of through the writer.

    It also keeps this suite's own live-client tripwire outside the reach of the code it guards.
    """
    _set_sentinel_credentials(monkeypatch)

    async def interrupted_probe(creds, args):
        raise interrupt()

    monkeypatch.setattr(operator_script, "_probe", interrupted_probe)
    out = tmp_path / "preflight_result.json"
    with pytest.raises(interrupt):
        operator_script.main(["--out", str(out)])
    assert not out.exists(), "an interrupted run must not claim the probe FAILED"


def _seed_qualified_artifact(path):
    """Put a genuinely consumable, qualified artifact at `path` - what a previous good run leaves."""
    matrix = _full(41, 0, 0, 0)
    write_preflight_result(select_combo(matrix), matrix, path)
    seeded = json.loads(path.read_text())
    assert seeded["probe_status"] == "completed" and seeded["season_status"] == "qualified"
    return seeded


@pytest.mark.parametrize(
    ("argv_extra", "exit_code", "status"),
    [(["--min-trials", "1"], 3, "refused"), ([], 2, "aborted")],
    ids=["policy-refusal-exit-3", "credential-abort-exit-2"],
)
def test_a_pre_probe_stop_makes_a_PREVIOUS_artifact_unconsumable(
    operator_script, tmp_path, monkeypatch, argv_extra, exit_code, status
):
    """MILESTONE MAJOR-1. Re-running the fixed `--out preflight_result.json` command is NORMAL, so
    an artifact from the last run is normally already sitting there.

    Every earlier vector started from an ABSENT path, which is the condition that hides this: the
    fixture held "no prior artifact exists" constant, so `not out.exists()` passed for a reason that
    had nothing to do with the refusal. Here the path starts with a genuinely QUALIFIED artifact -
    exactly what H2.4 would consume - and the refusal must make it unconsumable.
    """
    if exit_code == 3:
        _set_sentinel_credentials(monkeypatch)
    out = tmp_path / "preflight_result.json"
    seeded = _seed_qualified_artifact(out)

    reached = []

    async def probe(creds, args):
        reached.append(args)
        return _full(41, 0, 0, 0)

    monkeypatch.setattr(operator_script, "_probe", probe)
    assert operator_script.main(["--out", str(out), *argv_extra]) == exit_code
    assert reached == [], "no probe may run on a pre-probe stop"

    payload = json.loads(out.read_text())
    assert payload["probe_status"] == status, "the authoritative path still describes the OLD run"
    assert payload["season_status"] is None, "a stopped run must make no season claim"
    assert payload["counts"] == []
    assert payload != seeded

    # The previous artifact is preserved but MOVED, so nothing consumable was destroyed and nothing
    # stale remains at the path H2.4 reads.
    superseded = out.with_name(out.name + ".superseded")
    assert superseded.exists()
    assert json.loads(superseded.read_text()) == seeded


@pytest.mark.parametrize(
    ("argv_extra", "status"),
    [(["--min-trials", "1"], "refused"), ([], "aborted")],
    ids=["refused", "aborted"],
)
def test_a_stopped_run_is_not_consumable_as_a_completed_one(operator_script, tmp_path, monkeypatch, argv_extra, status):
    """The property H2.4 actually depends on, asserted as H2.4 would test it."""
    if status == "refused":
        _set_sentinel_credentials(monkeypatch)
    out = tmp_path / "preflight_result.json"
    _seed_qualified_artifact(out)
    operator_script.main(["--out", str(out), *argv_extra])

    payload = json.loads(out.read_text())
    assert payload["probe_status"] not in ("completed",), "a stopped run must never read as completed"
    assert payload["probe_status"] in preflight.NOT_RUN_STATUSES
    assert list(payload) == RESULT_KEYS, "a stopped run uses the SAME schema - no KeyError for a reader"


def test_write_preflight_not_run_refuses_a_status_that_means_a_probe_RAN(tmp_path):
    """`completed` and `failed` describe a probe that ran; this writer is for one that did not."""
    out = tmp_path / "x.json"
    for bad in ("completed", "failed"):
        with pytest.raises(PreflightError, match="is for a run that never probed"):
            preflight.write_preflight_not_run(bad, "reason", out)
    assert not out.exists()


def test_superseding_is_a_no_op_when_nothing_was_there(tmp_path):
    """A first-ever run must not manufacture a superseded file out of nothing."""
    out = tmp_path / "preflight_result.json"
    assert preflight.supersede_existing_artifact(out) is None
    assert not out.with_name(out.name + ".superseded").exists()


def test_main_writes_a_completed_artifact_on_success(operator_script, tmp_path, monkeypatch):
    """The COMPLETED state and exit 0. Deleting the write, or returning 1, left 463 tests green."""
    _set_sentinel_credentials(monkeypatch)
    matrix = _full(41, 0, 0, 0)

    async def probe(creds, args):
        return matrix

    monkeypatch.setattr(operator_script, "_probe", probe)
    out = tmp_path / "preflight_result.json"
    assert operator_script.main(["--out", str(out)]) == 0

    payload = json.loads(out.read_text())
    assert payload["probe_status"] == "completed"
    assert payload["season_status"] == "qualified"
    assert (payload["chain_index"], payload["bar"]) == ("196", "1m")


@pytest.mark.parametrize(
    ("flag", "value", "frozen"),
    [
        ("--min-trials", "1", 40),
        ("--min-trials", "200", 40),
        ("--cooldown-ms", "1000", 14_400_000),
        ("--horizon-ms", "60000", 3_600_000),
    ],
    ids=["min-trials-lowered", "min-trials-raised", "cooldown", "horizon"],
)
def test_main_REFUSES_a_non_frozen_season_policy(operator_script, tmp_path, monkeypatch, capsys, flag, value, frozen):
    """The authoritative Gate B command must not be able to publish a non-frozen experiment.

    This REPLACES a test that asserted the opposite - that `--min-trials` changes the published
    `season_status` for the same matrix - which the milestone Codex correctly cited as proving the
    defect rather than a property. Spec 5.1 fixes these three values and forbids lowering thresholds
    after observing outcomes, and this artifact is the authority H2.4 consumes, so a deviating run
    must be refused rather than merely disclosed.
    """
    _set_sentinel_credentials(monkeypatch)
    reached = []

    async def probe(creds, args):
        reached.append(args)
        return _full(41, 0, 0, 0)

    monkeypatch.setattr(operator_script, "_probe", probe)
    out = tmp_path / "preflight_result.json"
    assert operator_script.main(["--out", str(out), flag, value]) == 3

    assert reached == [], "the probe must not run at all under a non-frozen policy"
    payload = json.loads(out.read_text())
    assert payload["probe_status"] == "refused"
    assert payload["season_status"] is None
    assert payload["counts"] == []
    stderr = capsys.readouterr().err
    assert "refused before any request" in stderr
    assert flag in stderr and str(frozen) in stderr and value in stderr


def test_main_accepts_the_frozen_policy_stated_explicitly(operator_script, tmp_path, monkeypatch):
    """Passing the frozen values by hand is legal - the refusal is on DEVIATION, not on the flags."""
    _set_sentinel_credentials(monkeypatch)

    async def probe(creds, args):
        return _full(41, 0, 0, 0)

    monkeypatch.setattr(operator_script, "_probe", probe)
    out = tmp_path / "preflight_result.json"
    code = operator_script.main(
        ["--out", str(out), "--min-trials", "40", "--cooldown-ms", "14400000", "--horizon-ms", "3600000"]
    )
    assert code == 0
    assert json.loads(out.read_text())["season_status"] == "qualified"


def test_the_artifact_DECLARES_the_frozen_policy(tmp_path):
    """PROVENANCE, and the name says DECLARES because that is what this asserts.

    The previous name - "..._the_policy_it_was_produced_under" - claimed a RECORD of the run while
    the assertion only ever checked a DECLARATION of the frozen constants (QUALITY MINOR-10, C26:
    a test whose name claims more than it pins). The block is sourced from FROZEN_* and is therefore
    true of anything the authoritative CLI can produce, because that path REFUSES a deviation before
    the probe runs.

    KNOWN LIMITATION, stated here rather than pinned by a test: on the LIBRARY path a caller can run
    `run_matrix_probe(..., cooldown_ms=1000)` and hand the result to this writer, and the artifact
    will declare the frozen 4h cooldown beside counts the frozen policy would never produce. The
    writer cannot detect it - cooldown and horizon change the COUNTS, and a matrix carries no record
    of how it was counted. Closing it needs a channel from the probe to the writer, which means a
    production change; it is NOT pinned by a test here because a test asserting the present
    behaviour would defend the defect against being fixed.
    """
    matrix = _full(41, 0, 0, 0)
    out = tmp_path / "preflight_result.json"
    write_preflight_result(select_combo(matrix), matrix, out)
    assert _read(out)["policy"] == {"min_trials": 40, "cooldown_ms": 14_400_000, "horizon_ms": 3_600_000}

    failed = tmp_path / "failed.json"
    write_preflight_failure("boom", failed)
    assert _read(failed)["policy"] == {"min_trials": 40, "cooldown_ms": 14_400_000, "horizon_ms": 3_600_000}


def test_the_frozen_policy_constants_are_the_spec_values():
    """One source of truth, asserted against LITERALS rather than against itself."""
    assert preflight.FROZEN_MIN_TRIALS == 40
    assert preflight.FROZEN_COOLDOWN_MS == 14_400_000
    assert preflight.FROZEN_HORIZON_MS == 3_600_000
    signature = inspect.signature(run_matrix_probe)
    assert signature.parameters["min_trials"].default == 40
    assert signature.parameters["cooldown_ms"].default == 14_400_000
    assert signature.parameters["horizon_ms"].default == 3_600_000
    assert inspect.signature(select_combo).parameters["min_trials"].default == 40


def test_the_writer_refuses_a_selection_the_frozen_policy_would_not_produce(tmp_path):
    """The second half of MAJOR-2, at the writer rather than the CLI.

    A caller can still run `select_combo(result, min_trials=1)` in process. The artifact declares the
    FROZEN policy, so it must not carry a selection that policy would not have reached.
    """
    matrix = _full(39, 0, 0, 0)
    lenient = select_combo(matrix, min_trials=1)
    assert lenient.season_status == "qualified", "precondition: a lowered threshold qualifies here"
    assert select_combo(matrix).season_status == "exploratory", "precondition: the frozen policy does not"

    out = tmp_path / "preflight_result.json"
    with pytest.raises(PreflightError, match="not what the frozen policy"):
        write_preflight_result(lenient, matrix, out)
    assert not out.exists()


@pytest.mark.parametrize(
    ("exit_code", "scenario"), [(2, "abort"), (1, "failure"), (0, "success")], ids=["abort", "failure", "success"]
)
def test_main_returns_a_distinct_exit_code_per_outcome(
    operator_script, tmp_path, monkeypatch, capsys, exit_code, scenario
):
    """Per C28 the exit code IS the surface. All three were unpinned."""
    if scenario != "abort":
        _set_sentinel_credentials(monkeypatch)

    async def probe(creds, args):
        if scenario == "failure":
            raise RuntimeError("boom")
        return _full(41, 0, 0, 0)

    monkeypatch.setattr(operator_script, "_probe", probe)
    out = tmp_path / f"{scenario}.json"
    assert operator_script.main(["--out", str(out)]) == exit_code

    # STANDING LESSON 62: an exit code alone names no cause. Each scenario asserts the state that
    # identifies WHICH path produced it, so a wrong-guard pass is impossible.
    stderr = capsys.readouterr().err
    if scenario == "abort":
        assert json.loads(out.read_text())["probe_status"] == "aborted"
        assert "OKX_API_KEY" in stderr
    elif scenario == "failure":
        assert json.loads(out.read_text())["failure_reason"].startswith("RuntimeError: boom")
    else:
        assert json.loads(out.read_text())["probe_status"] == "completed"


# --- QUALITY MINOR-2: HttpxTransport and filters_by_chain, both on the live operator path ----------


class RecordingResponse:
    def __init__(self, payload, *, status_error=None):
        self._payload, self._status_error, self.raise_for_status_calls = payload, status_error, 0

    def raise_for_status(self):
        self.raise_for_status_calls += 1
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        return self._payload


class RecordingHttp:
    """Any object with an async request(...) satisfies HttpxTransport. No network, no httpx."""

    def __init__(self, response):
        self.response, self.calls = response, []

    async def request(self, method, path, *, params=None, json=None, headers=None):
        self.calls.append({"method": method, "path": path, "params": params, "json": json, "headers": headers})
        return self.response


async def test_the_transport_raises_for_status_before_parsing(operator_script):
    """The boundary between a TRANSPORT failure and OKX's in-200 application errors.

    Dropping raise_for_status() left 463 tests green and would feed a 4xx body into the OKX envelope
    parser on the live Gate B run.
    """
    boom = RuntimeError("HTTP 401")
    http = RecordingHttp(RecordingResponse({"code": "0", "data": []}, status_error=boom))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await operator_script.HttpxTransport(http).request("GET", "/p", params=None, json_body=None, headers={})
    assert http.response.raise_for_status_calls == 1


async def test_the_transport_refuses_a_non_object_payload(operator_script):
    """A JSON array or scalar is not an OKX envelope; the client's parser assumes a dict."""
    http = RecordingHttp(RecordingResponse([{"code": "0"}]))
    with pytest.raises(TypeError, match="must be a JSON object"):
        await operator_script.HttpxTransport(http).request("GET", "/p", params=None, json_body=None, headers={})


async def test_the_transport_preserves_param_iteration_order(operator_script):
    """The OKX signature covers the query string, so a transport that reordered params would produce
    a URL the OK-ACCESS-SIGN no longer matches. The class docstring says so; nothing pinned it."""
    params = {"chainIndex": "196", "tokenContractAddress": "0xabc", "bar": "1m", "limit": "100"}
    http = RecordingHttp(RecordingResponse({"code": "0", "data": []}))
    await operator_script.HttpxTransport(http).request(
        "GET", "/p", params=params, json_body=None, headers={"OK-ACCESS-KEY": "k"}
    )
    assert list(http.calls[0]["params"]) == ["chainIndex", "tokenContractAddress", "bar", "limit"]
    assert http.calls[0]["headers"] == {"OK-ACCESS-KEY": "k"}


async def test_the_probe_hands_run_matrix_probe_the_ATTESTING_binding(operator_script, monkeypatch):
    """The wiring, not just the wrapper. Unwrapping the client would make the LIVE Gate B run fail
    closed - every conforming response unconfirmed, no season - and nothing in the suite noticed.

    No live request is possible here: httpx.AsyncClient is replaced by a stub that does nothing, and
    run_matrix_probe is intercepted before it can read anything.
    """
    captured = {}

    class StubAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def capture(client, filters_by_chain, **kwargs):
        captured["client"] = client
        return _full(41, 0, 0, 0)

    monkeypatch.setattr(operator_script.httpx, "AsyncClient", StubAsyncClient)
    monkeypatch.setattr(operator_script, "run_matrix_probe", capture)

    creds = operator_script.OKXCredentials("k", "s", "p")
    await operator_script._probe(creds, operator_script.parse_args(["--out", "x.json"]))

    assert "client" in captured, "run_matrix_probe was never reached - this vector proves nothing"
    assert isinstance(captured["client"], operator_script.FrozenSignalListSource)
    assert captured["client"].signal_source_direction == preflight.SIGNAL_SOURCE_DIRECTION


async def test_the_operator_binding_attests_the_frozen_endpoint_contract(operator_script):
    """The ONE place that can attest polarity: whoever bound the client to an endpoint.

    The attestation lives on the BINDING, not on OKXMarketClient - that class is a generic reader
    that will happily be pointed elsewhere, and it is outside this task's ownership. If the real
    run stopped going through this wrapper, the probe would fail closed rather than inherit a
    guarantee it no longer has.
    """
    assert operator_script.FrozenSignalListSource.signal_source_direction == "buy"
    assert operator_script.FrozenSignalListSource.signal_source_direction == preflight.SIGNAL_SOURCE_DIRECTION

    inner = FakeMarketClient({"501": [_page([_sig()])]}, _both_bars("501", "TOK", BASE_MS))
    bound = operator_script.FrozenSignalListSource(inner)
    page = await bound.list_signals(SignalFilters(chain_index="501"))
    assert len(page.signals) == 1
    series = await bound.get_candles("501", "TOK", "1H")
    assert series.bar == "1H"
    assert inner.candle_calls == [("501", "TOK", "1H", 100, None)]


def test_the_operator_filters_cover_every_chain_in_the_frozen_matrix(operator_script):
    """A hard-coded map would fail loudly via run_matrix_probe's guard, but this is the seam that
    decides WHICH markets the live probe reads."""
    filters = operator_script.filters_by_chain()
    assert sorted(filters) == ["196", "501"]
    for chain_index, entry in filters.items():
        assert entry.chain_index == chain_index
    assert {chain for chain, _ in COMBO_ORDER} == set(filters)
