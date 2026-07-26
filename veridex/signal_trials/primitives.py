"""Signal Trials scoring primitives — outcome-referenced, venue-neutral metric building blocks.

These are pure functions over caller-supplied vectors: NO I/O, no config, no market/venue coupling,
and no dependency on the sports metric stack. That neutrality is the point. The sports Brier
(``veridex.scoring._brier``) is CLV-coupled — it derives its own outcome indicator from each row's
``clv_bps`` sign and returns ``None`` when an agent emitted no usable confidence. This module takes
the outcome vector as an EXPLICIT argument instead, so the same arithmetic is reusable wherever the
truth signal comes from, and an unusable input is an error rather than a silent ``None``.

The two are equivalence-tested against each other (``tests/signal_trials/test_primitives.py``): for
the same probabilities and the same outcomes, this function reproduces ``_brier`` exactly. That test
pins the shared arithmetic; ``veridex.scoring`` remains the single source of truth for how a sports
outcome indicator is DERIVED, and is never modified from this lane.
"""

from __future__ import annotations

from collections.abc import Sequence


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probabilities and realized outcomes.

    ``brier = mean((p - o) ** 2)`` over the paired inputs — 0.0 for a perfectly confident correct
    forecast, 0.25 for a maximally uncertain one (``p = 0.5``), 1.0 for a confidently wrong one.
    Lower is better. Pairing is positional: ``probabilities[i]`` is scored against ``outcomes[i]``.

    Inputs are NOT range-checked. Callers own the semantics of their vectors; supplying a
    probability outside [0, 1] or an outcome outside {0, 1} yields a defined but meaningless number,
    exactly as the arithmetic implies. Only the two structural preconditions below are enforced,
    because each would otherwise corrupt the result silently: mismatched lengths would score
    unrelated pairs (or drop the tail), and an empty input has no defensible mean to return.

    Args:
        probabilities: Predicted probabilities, conventionally in [0, 1], in outcome order.
        outcomes: Realized outcome indicators, conventionally ``1`` (occurred) or ``0`` (did not),
            positionally aligned with ``probabilities``.

    Returns:
        The mean squared error as a ``float``.

    Raises:
        ValueError: If the two sequences differ in length, or if they are empty.
    """
    if len(probabilities) != len(outcomes):
        raise ValueError(
            f"probabilities and outcomes must be the same length, got {len(probabilities)} and {len(outcomes)}"
        )
    if not probabilities:
        raise ValueError("brier_score requires at least one (probability, outcome) pair, got empty input")
    errors = [(probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes, strict=True)]
    return sum(errors) / len(errors)
