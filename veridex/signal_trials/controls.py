"""Negative controls for Signal Trials — the four baselines every contestant is ranked against (H3.4).

A Brier score in isolation says nothing. An agent that emits 0.5 on every trial scores 0.25 every
time and looks respectable next to no reference at all. The frozen spec (§8.5) therefore ranks four
controls ALONGSIDE the contestants — ``always-follow`` (p=1), ``always-fade`` (p=0), ``neutral``
(p=0.5), and prior-only ``climatology`` — so a leaderboard row is read against what a rule holding
no information could have achieved. Climatology is the sharpest of the four precisely because Brier
rewards knowing the base rate: an agent must beat the RUNNING base rate before its score is evidence
of per-signal discrimination rather than evidence that it learned how often signals work.

**The no-lookahead law.** Climatology is the one control that could cheat, because it is the only
one that looks at outcomes at all. §8.5 defines it as ``p_t = mean(follow_profitable of trials
settled strictly BEFORE t)``, and the emphasis carries the whole rule. A FULL-PACK base rate — the
mean over the entire season, including trial ``t`` itself and every trial after it — is not a
control. It scores the season using the season's own answers, which is the exact leak that
chronological evaluation exists to prevent, and it would outscore honest agents for a reason that
has nothing to do with skill. The spec permits the full-pack rate only as a clearly-labelled
retrospective diagnostic, never as a ranked control.

``assert_not_full_pack`` exists because that distinction is otherwise unenforceable. A leaked
climatology returns a perfectly ordinary probability: nothing about the number, the row, or the
resulting Brier score reveals that the mean was taken over outcomes the control was not entitled to
see. The only place the leak is visible is at the call site, in the SHAPE of what was handed over,
so that is where it is checked.

**Cold start.** Below ``min_prior_trials`` settled outcomes the mean is noise, so climatology
returns 0.5 — the same "no information" answer ``neutral`` gives — rather than a confident number
derived from three coin flips.

Everything here is a pure function of its arguments: no I/O, no config, no clock, no handle on the
pack. That is a leakage control and not a style preference. A control that could reach the pack
could read outcomes it was never handed, and no reviewer could tell from the return value that it
had. ``tests/signal_trials/test_controls.py`` pins the claim by parsing this module's imports, so
the guarantee fails loudly if it is ever reversed.
"""

from __future__ import annotations


def always_follow() -> float:
    """The p=1 control: follow every signal with total confidence.

    Ranked beside the contestants so a reader can see what unconditional trust in the smart-money
    label would have scored. The arena's thesis is that the label is a stimulus and not an oracle,
    which makes this the row an agent claiming to read wallet quality most needs to beat.

    Returns:
        ``1.0``, for every trial, with no input to vary on.
    """
    return 1.0


def always_fade() -> float:
    """The p=0 control: fade every signal with total confidence.

    The mirror of ``always_follow``, and present for the same reason in the opposite direction: if
    the labelled wallets are systematically exit liquidity over the season, blanket fading scores
    well without discriminating anything. An agent has to beat BOTH constant stances, because
    beating only one is consistent with having merely picked the season's prevailing direction.

    Returns:
        ``0.0``, for every trial, with no input to vary on.
    """
    return 0.0


def neutral() -> float:
    """The p=0.5 control: decline to discriminate, on every trial.

    Scores a flat 0.25 Brier regardless of outcome. That fixed number is the floor a proper score
    has to clear before any confidence is worth paying attention to — a confidently wrong agent
    scores worse than this, which is the property that makes Brier proper and worth reporting.

    Returns:
        ``0.5``, for every trial, with no input to vary on.
    """
    return 0.5


def prior_only_climatology(prior_outcomes: list[int], min_prior_trials: int = 10) -> float:
    """The running base rate over STRICTLY PRIOR outcomes, or 0.5 while there are too few.

    Returns ``mean(prior_outcomes)`` once at least ``min_prior_trials`` outcomes are available, and
    0.5 before that. The threshold is a genuine parameter and is read on every call: it is NOT
    re-spelled as a literal ``10`` anywhere in the body, because a hard-coded threshold is invisible
    to any suite whose vectors all use the default, and that is the defect this program has found in
    every lane that permitted it.

    The strictly-prior property is the CALLER'S to uphold and cannot be checked from here — this
    function sees a list of outcomes and has no idea which trial they belong to or when they
    settled. ``assert_not_full_pack`` is the enforcement point, and the season scorer is expected to
    call it per trial before calling this.

    Degenerate threshold, decided deliberately rather than left to the arithmetic: with
    ``min_prior_trials <= 0`` the length comparison alone would fall through to a mean over an empty
    list and raise ``ZeroDivisionError``. An empty prior is answered 0.5 instead, because zero prior
    outcomes is precisely the "no information" state the cold start already answers that way. The
    frozen spec does not speak to this input; the choice is stated here so it is reviewable rather
    than discovered.

    Outcome VALUES are not range-checked, matching ``veridex.signal_trials.primitives.brier_score``.
    Callers own the semantics of their vectors; feeding values outside ``{0, 1}`` yields a defined
    but meaningless number, exactly as the arithmetic implies.

    Args:
        prior_outcomes: ``follow_profitable`` indicators (``1``/``0``) for trials settled strictly
            before the trial being scored, in chronological order. Order does not affect the result,
            only membership does. The list is read and never modified.
        min_prior_trials: Outcomes required before the mean is trusted. Frozen at 10 by §8.5; the
            default is the frozen value and the parameter exists so the threshold is testable.

    Returns:
        The mean of ``prior_outcomes`` as a ``float`` once there are at least ``min_prior_trials`` of
        them, otherwise ``0.5``.
    """
    if not prior_outcomes or len(prior_outcomes) < min_prior_trials:
        return 0.5
    return sum(prior_outcomes) / len(prior_outcomes)


class FullPackClimatologyError(ValueError):
    """A climatology feed included outcomes the control was not entitled to see.

    Subclasses ``ValueError`` so a caller that already funnels malformed input to ``ValueError``
    keeps working, while a leakage refusal stays separately catchable — the two are not the same
    kind of problem and a scorer may well want to treat them differently.
    """


def assert_not_full_pack(prior_len: int, pack_len: int, trial_index: int) -> None:
    """Refuse a climatology feed that reaches the trial being scored, or its future.

    The rule, verbatim from the plan: raise when a caller feeds outcomes ``>=`` the trial's
    chronological index — that is, when ``prior_len >= trial_index``.

    **``trial_index`` is the trial's 1-BASED chronological position** in the pack: equivalently, the
    number of trials up to and INCLUDING it. Under that convention the strictly-prior set for the
    k-th trial has exactly ``k - 1`` members, so a correctly-fed caller satisfies
    ``prior_len == trial_index - 1``, and ``prior_len >= trial_index`` means at least one outcome
    from the trial itself or from its future was included. The 1-based reading is the only one under
    which the plan's ``>=`` is coherent: with a 0-based index the strictly-prior count IS the index,
    so ``>=`` would reject the correct call and the guard would be unusable. **Callers must pass the
    1-based ordinal**; passing a 0-based index makes every legitimate feed raise, loudly and
    immediately, which is the failure direction chosen on purpose.

    ``pack_len`` is CONTEXT, never a trigger. The rule is a function of ``prior_len`` and
    ``trial_index`` alone, and ``pack_len`` only tells whoever reads the error how large the season
    was. The distinction is invisible on a well-formed season — there ``prior_len == pack_len``
    implies ``prior_len >= trial_index``, so an equality-on-pack rule would fire on exactly the same
    inputs — and it is decisive on the case that actually matters: a PARTIAL over-feed, say 20
    outcomes supplied for trial 10 of a 44-trial pack, is a real leak that an equality rule waves
    straight through.

    KNOWN GAP, stated rather than implied: this checks the SIZE of the feed, not its membership. A
    caller that supplies five outcomes drawn from the trial's future passes, because five is fewer
    than the index and nothing here can see which trials those five came from. The guard catches the
    full-pack and over-feed mistakes — the ones a scorer makes by wiring the wrong list — not a
    deliberately shuffled feed. Nor are the arguments checked for negativity; a caller passing
    nonsense lengths gets a defined answer over nonsense.

    Args:
        prior_len: Number of outcomes the caller is about to hand to ``prior_only_climatology``.
        pack_len: Total trials in the sealed pack. Reported in the error; never tested against.
        trial_index: The 1-based chronological position of the trial being scored.

    Returns:
        ``None`` when the feed is strictly prior. The function is a guard, not a predicate — it
        answers by not raising.

    Raises:
        FullPackClimatologyError: If ``prior_len >= trial_index``. The message names all three
            values, so the refusal identifies which input was wrong instead of only that one was.
    """
    if prior_len >= trial_index:
        raise FullPackClimatologyError(
            f"climatology must see only strictly prior outcomes: prior_len={prior_len} is not strictly "
            f"prior to trial_index={trial_index} (1-based) of pack_len={pack_len}"
        )
