"""Wire schemas for the signal-trials API surface.

These four models are frozen by the implementation plan at H1.2. The response models
for trials, agent records, receipt verification and commit receipts are deliberately
NOT frozen here — they are settled at H4.3, once the settlement path exists and there
is something truthful for them to carry.

The constraints below are not decoration; each one is a claim boundary:

* ``p_follow_profitable`` is bounded to ``[0, 1]`` because it is scored as a
  probability. The bound also rejects ``NaN`` and the infinities, since every
  comparison against ``NaN`` is false — which matters because Python's JSON decoder
  accepts the ``NaN`` literal, and an unbounded float would be Brier-scored as if it
  were a real commitment.
* ``trial_mode`` admits only ``"live"``. Frozen spec §11 restricts paid external
  commits to live trials: replay outcomes are publicly knowable, so advertising a
  replay trial for discovery would invite a paid "prediction" of a known result.
* ``season_status`` admits only the three PUBLISHED statuses. ``not_built`` is a
  health state describing a directory with no season in it, and can never be the
  status of a season document that exists.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CommitRequest(BaseModel):
    """A paid benchmark commitment: one probability, bound to one trial."""

    trial_id: str
    p_follow_profitable: float = Field(ge=0, le=1)
    methodology_version: str | None = None


class SignalTrialsRowModel(BaseModel):
    """One agent's standing in a season.

    ``avg_brier`` and ``capped_avg_markout_bps`` are nullable on purpose: an agent
    with no settled trials has no score, and a zero there would read as a real result.
    """

    agent_id: str
    qualified: bool
    avg_brier: float | None
    capped_avg_markout_bps: int | None
    active_decisions: int
    active_coverage: float
    unscored: int
    is_control: bool


class SignalTrialsSeasonResponse(BaseModel):
    """The published season document served by ``GET /signal-trials/season``.

    ``combo`` is the chain x bar selection the season was built under. It is typed
    ``dict[str, Any]`` rather than the plan's bare ``dict``: ``mypy --strict`` requires
    type arguments for generics, and this is the minimum parameterization that
    satisfies it without narrowing what a caller may pass (``PKT-DEC-C16``).
    """

    season_id: str
    season_status: Literal["qualified", "exploratory", "no_season"]
    combo: dict[str, Any]
    sample_size: int
    rows: list[SignalTrialsRowModel]


class OpenTrialResponse(BaseModel):
    """The currently open live trial, as free discovery (frozen spec §11).

    Carries only ``visible_at_decision`` evidence plus the hash that binds it. The
    ``evidence`` mapping is typed ``dict[str, Any]`` for the same ``--strict`` reason
    as ``combo`` above (``PKT-DEC-C16``); its leakage tiers are enforced upstream by
    the ChallengeSpec canonicalizer, not by this wire model.
    """

    trial_id: str
    trial_mode: Literal["live"]
    t0_ms: int
    commit_deadline_ms: int
    evidence: dict[str, Any]
    evidence_hash: str
