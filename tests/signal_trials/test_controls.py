import pytest
from veridex.signal_trials.controls import prior_only_climatology, assert_not_full_pack, FullPackClimatologyError

def test_cold_start_neutral_until_ten():
    assert prior_only_climatology([1] * 9) == 0.5 and prior_only_climatology([1] * 10) == 1.0
def test_expanding_mean():
    assert prior_only_climatology([1, 0] * 5) == 0.5
def test_full_pack_baseline_cannot_rank():
    with pytest.raises(FullPackClimatologyError):
        assert_not_full_pack(prior_len=50, pack_len=50, trial_index=10)


# ---------------------------------------------------------------------------
# REGION B — implementer pins. Everything ABOVE this banner is the plan's
# mandated RED block (plan lines 607-616), byte-identical and untouched.
#
# The mandated block is under-discriminated in two measured ways. Both were
# named in the task packet, both are re-verified here, and each pin below says
# which mutant it exists to kill.
#
#   1. CONSTANCY (C18). All three mandated vectors use the DEFAULT
#      min_prior_trials=10, so a body that hard-codes 10 and ignores the
#      parameter passes every one of them. The pins vary the threshold in BOTH
#      directions off the default (3 and 25) against hard-coded expectations,
#      so the constant cannot move undetected. A vector derived from the
#      constant under test cannot detect that constant moving.
#
#   2. THE FULL-PACK RULE. The single mandated vector
#      (prior_len=50, pack_len=50, trial_index=10) makes "prior_len >=
#      trial_index" and "prior_len == pack_len" true simultaneously, so it
#      cannot tell the plan's stated rule from an equality-on-pack rule. The
#      pins separate them in both directions, and the mandated block's only
#      assertion is that something raises — so the ACCEPTING case is pinned
#      here too.
#
# Imports are function-local throughout this region: a module-level import
# placed after executable code is E402, and the frozen block owns the top of
# the file.
# ---------------------------------------------------------------------------


def test_the_three_constant_controls_are_one_zero_and_a_half():
    """The frozen p-values of the three input-free controls (§8.5).

    The mandated block never imports these three, let alone calls them, so absent this pin every
    value they return is free to be anything. Float-ness is asserted as well as equality because
    the season feeds them straight into Brier arithmetic beside real probabilities, and `1 == 1.0`
    in Python — an int-returning mutant is invisible to the equality assertions alone.
    """
    from veridex.signal_trials.controls import always_fade, always_follow, neutral

    assert always_follow() == 1.0
    assert always_fade() == 0.0
    assert neutral() == 0.5
    assert isinstance(always_follow(), float)
    assert isinstance(always_fade(), float)
    assert isinstance(neutral(), float)


def test_threshold_follows_the_parameter_and_is_not_a_hard_coded_ten():
    """min_prior_trials is honoured, pinned in both directions off its default. This is the C18 pin.

    A body spelled `if len(prior_outcomes) < 10` passes all three mandated vectors, because every
    mandated vector uses the default. Below the default (3) a hard-coded ten returns 0.5 where the
    parameter demands the mean; above it (25) a hard-coded ten returns the mean where the parameter
    demands 0.5. Every expectation is a literal derived from §8.5's rule, never from the
    implementation's own threshold.
    """
    from veridex.signal_trials.controls import prior_only_climatology

    assert prior_only_climatology([1, 1], min_prior_trials=3) == 0.5
    assert prior_only_climatology([1, 1, 1], min_prior_trials=3) == 1.0  # hard-coded 10 would give 0.5
    assert prior_only_climatology([1] * 24, min_prior_trials=25) == 0.5  # hard-coded 10 would give 1.0
    assert prior_only_climatology([1] * 25, min_prior_trials=25) == 1.0
    assert prior_only_climatology([1, 0, 0, 0], min_prior_trials=4) == 0.25  # value and threshold both moved


def test_mean_divides_by_the_outcome_count_not_by_the_threshold():
    """The denominator, which every mandated vector leaves ambiguous.

    Both mandated mean vectors carry exactly ten outcomes at min_prior_trials=10, so
    `sum / min_prior_trials` and `sum / len` agree on both and the wrong denominator survives them.
    One vector of a different length separates the two: three ones among twelve outcomes is 0.25 as
    a mean and 0.3 as sum-over-threshold.
    """
    from veridex.signal_trials.controls import prior_only_climatology

    assert prior_only_climatology([1, 1, 1] + [0] * 9) == 0.25


def test_empty_prior_is_neutral_even_when_the_threshold_is_zero():
    """The degenerate input the frozen spec does not speak to, decided deliberately and pinned.

    With min_prior_trials <= 0 the length comparison alone falls through to a mean over an empty
    list and raises ZeroDivisionError. 0.5 is returned instead: zero prior outcomes is precisely
    the "no information" state that the cold start already answers with 0.5. Recorded as a pin so
    the decision is reviewable rather than discovered by a caller.
    """
    from veridex.signal_trials.controls import prior_only_climatology

    assert prior_only_climatology([], min_prior_trials=0) == 0.5
    assert prior_only_climatology([]) == 0.5


def test_climatology_does_not_mutate_the_callers_outcome_list():
    """The scorer feeds a GROWING prior list trial by trial; a mutating control would corrupt it.

    Invisible in the return value, destructive across a whole season, and cheap to pin.
    """
    from veridex.signal_trials.controls import prior_only_climatology

    outcomes = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    prior_only_climatology(outcomes)
    assert outcomes == [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]


def test_strictly_prior_feed_of_exactly_the_right_length_is_accepted():
    """The accepting case, which the mandated block never exercises at all.

    trial_index is the 1-based chronological position, so the 11th trial's strictly-prior set has
    exactly ten members and the first trial's has none. A guard that raised unconditionally, or
    that inverted its comparison, passes the mandated vector — that vector only ever asserts that
    something raises.
    """
    from veridex.signal_trials.controls import assert_not_full_pack

    assert assert_not_full_pack(prior_len=10, pack_len=44, trial_index=11) is None
    assert assert_not_full_pack(prior_len=0, pack_len=44, trial_index=1) is None


def test_feeding_the_trials_own_outcome_is_lookahead_at_the_boundary():
    """prior_len == trial_index raises: the `>=` boundary itself.

    Under the 1-based convention the k-th trial has k-1 strictly-prior outcomes, so a prior_len of
    exactly k already includes the trial's own settled outcome. A `>` implementation accepts this
    and still passes the mandated vector (50 > 10). match= is carried because a bare pytest.raises
    cannot tell THIS refusal from any other FullPackClimatologyError the module might grow.
    """
    from veridex.signal_trials.controls import FullPackClimatologyError, assert_not_full_pack

    with pytest.raises(FullPackClimatologyError, match="strictly prior"):
        assert_not_full_pack(prior_len=11, pack_len=44, trial_index=11)


def test_partial_over_feed_raises_even_though_the_pack_is_not_full():
    """HAZARD 2, the decisive direction: twenty outcomes fed for trial 10 of a 44-trial pack.

    prior_len >= trial_index (a genuine leak) while prior_len != pack_len, so an equality-on-pack
    rule accepts it. This is a well-formed, reachable season input, which is why the plan's prose
    rule and not the equality rule is the one implemented. The message is asserted to name the
    offending value, so the refusal identifies WHICH input was wrong and not merely that one was.
    """
    from veridex.signal_trials.controls import FullPackClimatologyError, assert_not_full_pack

    with pytest.raises(FullPackClimatologyError, match="prior_len=20"):
        assert_not_full_pack(prior_len=20, pack_len=44, trial_index=10)


def test_pack_length_is_context_and_never_the_trigger():
    """HAZARD 2, the reverse direction: prior_len == pack_len, yet strictly prior to the trial.

    DISCLOSED LIMIT OF THIS VECTOR. It cannot arise in a well-formed season, because a 1-based
    trial_index never exceeds pack_len. That is not an accident of the numbers chosen — the two
    candidate rules AGREE on every well-formed input where prior_len == pack_len, so no reachable
    vector can separate them in this direction and this shape is the only one that can. The pin is
    therefore behaviourally invisible in production; it exists solely to stop the rule silently
    widening to "raise on >= trial_index OR on == pack_len", which no line of the plan asks for.
    """
    from veridex.signal_trials.controls import assert_not_full_pack

    assert assert_not_full_pack(prior_len=9, pack_len=9, trial_index=10) is None


def test_full_pack_climatology_error_is_a_value_error():
    """Surface pin (C22): the exception's place in the hierarchy is part of the public contract.

    Callers that already funnel malformed input to ValueError keep working, while a leakage refusal
    stays separately catchable — the two are different kinds of problem and a scorer may treat them
    differently.
    """
    from veridex.signal_trials.controls import FullPackClimatologyError

    assert issubclass(FullPackClimatologyError, ValueError)


def test_controls_module_imports_nothing_that_could_reach_the_pack():
    """The module docstring's no-leakage claim, made falsifiable.

    A control that could open a file, construct a client, or import the season repository could
    read outcomes it was never handed — and the probability it returned would look exactly the same
    as an honest one. Parsing this module's own import statements is the cheapest check that would
    NOTICE that reversal. The allowlist is deliberately a single entry; widening it is the
    reviewable event.
    """
    import ast
    import pathlib

    from veridex.signal_trials import controls

    source = pathlib.Path(controls.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__"}, f"controls.py must stay pure; unexpected imports: {sorted(imported)}"


# --- Added after mutation batch 2, which found these two properties genuinely unpinned. ---
# Batch 1 (28 decision-surface mutants) killed 28/28, which is the signature of an enumeration
# drawn to fit the suite. Batch 2 was chosen the opposite way — 15 mutants with no pin designed
# for them — and produced 13 survivors. Nine were non-equivalent; the two families below are the
# ones that matter, so they are pinned rather than merely reported. The rest are argued in the
# task report: four are true equivalents, three differ only on malformed input unreachable in a
# well-formed season, and one adds a guard no vector can reach.


def test_frozen_signatures_match_the_plan_exactly():
    """The plan's signatures are a frozen surface, and NOTHING above pins them (C22).

    Mutant N12 reordered assert_not_full_pack's parameters to (pack_len, prior_len, trial_index)
    and SURVIVED the entire suite, because the mandated vector and every pin above call it with
    keyword arguments. For a trust guard whose whole job is to be called correctly by a scorer that
    has not been written yet, an unpinned positional order is a live hazard: a positional caller
    would hand pack_len to prior_len and the guard would answer confidently about the wrong number.
    N11 (min_prior_trials made keyword-only) and N13 (always_fade given an extra parameter) also
    survived, for the same reason.

    Both halves are needed. inspect pins the declared surface; the positional calls beneath it pin
    that the order is actually USABLE, which is the property a caller depends on.
    """
    import inspect

    from veridex.signal_trials import controls

    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    empty = inspect.Parameter.empty

    def params(fn):
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]

    assert params(controls.always_follow) == []
    assert params(controls.always_fade) == []
    assert params(controls.neutral) == []
    assert params(controls.prior_only_climatology) == [
        ("prior_outcomes", kind, empty),
        ("min_prior_trials", kind, 10),
    ]
    assert params(controls.assert_not_full_pack) == [
        ("prior_len", kind, empty),
        ("pack_len", kind, empty),
        ("trial_index", kind, empty),
    ]

    # The same calls again, positionally, in the plan's order.
    assert controls.prior_only_climatology([1] * 9, 10) == 0.5
    assert controls.prior_only_climatology([1] * 3, 3) == 1.0
    assert controls.assert_not_full_pack(10, 44, 11) is None
    with pytest.raises(controls.FullPackClimatologyError):
        controls.assert_not_full_pack(50, 50, 10)


def test_refusal_message_reports_the_actual_values_and_the_indexing_convention():
    """The refusal must name the RIGHT numbers, and must carry the 1-based convention.

    Mutant N01 swapped the reported trial_index and pack_len and survived; N02 deleted the
    "(1-based)" note and survived. Neither changes the rule, and both are worth pinning anyway.
    A guard that refuses while misreporting which value offended sends whoever is debugging the
    leak to the wrong input — and the 1-based note is the ONLY in-band place a caller learns the
    indexing convention that H3.4's `>=` boundary depends on. That convention is this task's
    disclosed cross-task hazard for H3.5, so the sentence carrying it is load-bearing text, not
    decoration.
    """
    from veridex.signal_trials.controls import FullPackClimatologyError, assert_not_full_pack

    with pytest.raises(FullPackClimatologyError) as excinfo:
        assert_not_full_pack(prior_len=20, pack_len=44, trial_index=10)

    message = str(excinfo.value)
    assert "prior_len=20" in message
    assert "trial_index=10" in message
    assert "pack_len=44" in message
    assert "1-based" in message
