"""H2.4 — read the preflight verdict, fetch the season's data, seal the pack.

This is the READ side of ``preflight_result.json``. ``run_preflight.py`` decides which
(chain x bar) combo the season runs on and writes that decision down; this module consults the
decision and either seals a pack from it or records why it could not.

**The branch is the point of this file** (PKT-DEC-C48, superseding plan lines 416-418 on branch
logic only; the frozen plan is not edited). The plan short-circuits on ONE condition,
``season_status == "no_season"``. H2.3 later widened ``ProbeStatus`` to four values, and the three
non-``completed`` ones expose ``season_status`` NULL with ``chain_index`` and ``bar`` both null —
which does not equal ``"no_season"``, so a plan-faithful implementation falls through to "otherwise
seals the pack" AND SEALS WITH A NULL COMBO. The plan is not wrong; it is older than the status
domain it branches on.

So: **every non-sealable artifact is treated as non-sealable.** Non-sealable is
``probe_status != "completed"``, or a null ``chain_index``/``bar`` pair, or
``season_status == "no_season"``. Each writes the appropriate published state, fetches NOTHING, and
returns ``None``. Only a ``completed`` probe naming a real combo reaches the seal.

``preflight.py:625-655`` validates the same property, but that is a WRITE-side guard inside
preflight. Nothing on this path re-runs it: the artifact arrives as bytes on disk, and a hand-edit
or a partial restore is exactly the case a reader has to survive. This is that guard's read-side
twin, and :func:`non_sealable_reason` is where it lives.

**Which state gets written is a truth claim, not a formality.** ``published.py:55-56`` fixes the
distinction: ``not_built`` means the scorer never ran, ``no_season`` means it ran and declined — and
``preflight.py:36-40`` draws the same line on its own side between an OBSERVATION and a FAILURE. A
``completed`` probe reporting ``no_season`` observed a barren market, so it records ``no_season``. A
``failed``, ``refused`` or ``aborted`` probe observed nothing at all, so recording ``no_season`` for
it would assert "we looked and there was nothing" about a look that never happened. Those record
``not_built``.

**No operator entry point here, deliberately.** ``run_fetch_and_seal`` takes its client by injection
— the same seam ``preflight.run_matrix_probe`` uses, and what makes it impossible for a test to
reach the network without constructing a transport on purpose. The credentialed CLI is NOT rebuilt
in this file: it would fork ``run_preflight.py``'s Gate-A credential reading and redaction into a
second place, and the polarity-attesting endpoint binding (``FrozenSignalListSource``) that makes a
client usable at all lives in that file, which this task does not own.

**The frozen eligibility rules are IMPORTED, not restated.** The trials sealed into a pack have to
be the same population the matrix counted; two implementations of §5.1's filters or §8.6's cooldown
would drift, and the pack's ``probe_counts`` would then describe a different set than its
``trials``. Those rules currently live under private names in ``preflight``, so they are imported
under them — see the note at the import site.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from veridex.signal_trials import published
from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.okx_client import CandleSeries, SignalFilters
from veridex.signal_trials.pack import PackMeta, seal_pack

# `_collect_signals`, `_dedup_by_cooldown`, `_screen` and `_with_unique_open_times` are PRIVATE names
# in `preflight`, imported deliberately. They are the frozen counting rules (§5.1's filters, §8.6's
# cooldown, the pagination bound), and the trials sealed into a pack have to be the same population
# the matrix counted — a second implementation would drift, and the pack's `probe_counts` would then
# describe a different set than its `trials` while both looked correct. Promoting them to public
# names in `preflight` is the natural follow-up and needs ownership of that file, which this task
# does not have.
#
# `_with_unique_open_times` in particular is not optional politeness:
# `spot_markout.select_settlement_candle` documents that duplicate `ts_open_ms` makes the settlement
# PRICE follow wire order and requires the caller to guarantee uniqueness upstream. Sealing an
# un-normalized series would make the season's own numbers depend on how a page happened to paginate.
from veridex.signal_trials.preflight import (
    CANDLE_LIMIT,
    COMBO_ORDER,
    FROZEN_COOLDOWN_MS,
    FROZEN_HORIZON_MS,
    PROBE_COMPLETED,
    ComboSelection,
    MarketClient,
    SeasonStatus,
    _collect_signals,
    _dedup_by_cooldown,
    _screen,
    _with_unique_open_times,
)

#: §5.1: the official rank uses ``declared_cost_bps = 25``. The ``[0, 10, 25, 50]`` sweep is a
#: diagnostic display and never changes the official ranking, so 25 is what a pack is sealed with.
FROZEN_DECLARED_COST_BPS = 25

#: Where packs live under the signal-trials data directory.
PACKS_DIRNAME = "packs"

#: What each frozen bar name is WORTH in milliseconds. A PRICING table, and nothing else: it is not
#: the authority on which bars exist. Adding an entry here must not widen any guard, because a
#: hand-added ``"4H"`` would otherwise admit a bar the frozen matrix can never select.
#: ``test_frozen_bar_ms_covers_the_frozen_matrix`` fails if the matrix names a bar this cannot price.
FROZEN_BAR_MS: dict[str, int] = {"1m": 60_000, "1H": 3_600_000}

#: The chains and bars the frozen matrix can select. BOTH are DERIVED from ``COMBO_ORDER`` — the one
#: authority — rather than restated, so widening the matrix cannot leave either guard behind, and
#: adding a row to the pricing table above cannot widen this one.
FROZEN_CHAINS: frozenset[str] = frozenset(chain for chain, _bar in COMBO_ORDER)
FROZEN_BARS: frozenset[str] = frozenset(bar for _chain, bar in COMBO_ORDER)

#: The season statuses that authorize a seal. ``no_season`` is handled before this is consulted, so
#: these are the whole remainder of the ``SeasonStatus`` domain — a value outside it is a corrupted
#: or hand-edited artifact and fails closed rather than reaching the seal.
SEALABLE_SEASON_STATUSES: frozenset[str] = frozenset({"qualified", "exploratory"})


@dataclass(frozen=True)
class NonSealable:
    """Why an artifact may not be sealed, and what the published record should say about it."""

    state: published.PublishedState
    reason: str


def non_sealable_reason(artifact: Mapping[str, Any]) -> NonSealable | None:
    """Return why ``artifact`` must not be sealed, or ``None`` when it may be.

    SIX conditions, in this order: probe status, ``no_season``, a null combo, season-status
    membership, chain membership, bar membership. C48 named the first three; the last three were
    added later and the order still matters for the same reason. The artifact's own key order
    guarantees a reader reaches ``probe_status`` first (``preflight.py:703-706``), and a probe that
    never ran carries a null ``season_status`` AND a null combo — reporting it under any of the
    later, narrower reasons would describe the artifact rather than the run.

    Returns:
        A :class:`NonSealable` naming the state to publish and the reason, or ``None`` for a
        ``completed`` probe naming a real combo.
    """
    probe_status = artifact.get("probe_status")
    if probe_status != PROBE_COMPLETED:
        # Covers `failed`, `refused`, `aborted` AND anything outside the frozen four. Written as a
        # negative test against the ONE sealable value rather than as membership in the known-bad
        # set, so a status this module has never heard of fails closed instead of open.
        return NonSealable(
            published.NOT_BUILT,
            f"probe_status is {probe_status!r}, not {PROBE_COMPLETED!r}: no probe result authorizes a season",
        )

    season_status = artifact.get("season_status")
    if season_status == "no_season":
        # The probe RAN AND DECLINED. That is an observation, and `no_season` is the state that says
        # so — the plan's original short-circuit, unchanged.
        return NonSealable(
            "no_season",
            "the probe completed and reported no_season: the market was observed and yielded no season",
        )

    chain_index = artifact.get("chain_index")
    bar = artifact.get("bar")
    if chain_index is None or bar is None:
        # A completed, non-`no_season` artifact with a null combo contradicts itself; preflight's
        # write-side guard refuses to emit one, so reaching here means the bytes were edited or
        # partially restored. `not_built` rather than `no_season`: this artifact claims a season, so
        # reporting it as a declined one would replace a contradiction with a false observation.
        return NonSealable(
            published.NOT_BUILT,
            f"season_status is {season_status!r} but the combo is chain_index={chain_index!r} "
            f"bar={bar!r}: an artifact claiming a season must name the market it ran on",
        )

    if season_status not in SEALABLE_SEASON_STATUSES:
        return NonSealable(
            published.NOT_BUILT,
            f"season_status {season_status!r} is outside the frozen set {sorted(SEALABLE_SEASON_STATUSES)}",
        )

    # The combo must name a market the FROZEN MATRIX could actually have selected, not merely a
    # non-null one. Membership is checked HERE, before the caller's first `await`, because that is
    # what makes the zero-transport claim true for every non-sealable artifact rather than only for
    # the ones caught above: a bar of "5m" would otherwise pass this guard and be fetched at, one
    # request per token, before `frozen_bar_ms` refused it at seal time. The chain is checked for
    # the same reason and in the same breath — an unfrozen chain is fetched just as eagerly, and
    # guarding one axis while leaving the other open would make the invariant half true.
    if chain_index not in FROZEN_CHAINS:
        return NonSealable(
            published.NOT_BUILT,
            f"chain_index {chain_index!r} is not in the frozen matrix {sorted(FROZEN_CHAINS)}: "
            f"a season may only be sealed against a market the frozen probe could have selected",
        )
    if bar not in FROZEN_BARS:
        return NonSealable(
            published.NOT_BUILT,
            f"bar {bar!r} is not a frozen width {sorted(FROZEN_BARS)}: "
            f"a season may only be sealed at a bar the frozen probe could have selected",
        )

    # LATENT, stated so it is a decision rather than an oversight: the two axes are checked
    # INDEPENDENTLY, while "a market the frozen probe could have selected" is a property of the
    # PAIR. `COMBO_ORDER` is currently the full cross product {196,501} x {1m,1H}, so the two are
    # equivalent. If it ever becomes non-rectangular, `(chain_index, bar) not in COMBO_ORDER`
    # replaces both checks and is strictly stronger. It is not written that way today because the
    # third branch it would need — a valid chain and a valid bar that are not a valid PAIR — cannot
    # be reached under the current matrix, and an unreachable rejection reason is one that can
    # silently become wrong (the hazard `preflight._screen`'s own docstring names).
    return None


def _read_artifact(preflight_path: Path) -> dict[str, Any] | None:
    """Read ``preflight_result.json``, distinguishing ABSENCE from CORRUPTION.

    Absence is a meaningful reading — no probe has written here, so nothing authorizes a season, and
    the caller records that. Corruption is not: an artifact that exists but cannot be parsed says
    nothing about the season, and guessing a verdict from it is how a false one gets published.

    Returns:
        The artifact, or ``None`` when the path does not exist.

    Raises:
        ValueError: The file exists but is not a readable JSON object.
    """
    if not preflight_path.is_file():
        return None
    try:
        loaded = json.loads(preflight_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"preflight artifact {preflight_path} is not readable JSON") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"preflight artifact {preflight_path} must hold a JSON object")
    return loaded


def _season_id(chain_index: str, bar: str, now: datetime | None = None) -> str:
    """A directory-safe season id naming the combo and the moment it was sealed.

    Time-stamped rather than derived from the combo alone so a second seal of the same combo does
    not collide with the first — :func:`pack.seal_pack` refuses to write a second generation into an
    existing pack directory, and silently overwriting would be the worse of the two behaviours.
    """
    moment = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"season-{chain_index}-{bar}-{moment}"


async def _fetch_trials(
    client: MarketClient,
    filters: SignalFilters,
    *,
    cooldown_ms: int,
) -> list[CanonicalSignal]:
    """Collect the chain's signals and reduce them to the season's trials under the frozen rules."""
    raw_signals = await _collect_signals(client, filters)
    eligible: list[CanonicalSignal] = []
    for raw in raw_signals:
        screened = _screen(raw, filters.chain_index, filters)
        if isinstance(screened, str):
            continue
        eligible.append(screened)
    kept, _dropped = _dedup_by_cooldown(eligible, cooldown_ms)
    return kept


async def _fetch_settlement(
    client: MarketClient,
    trials: list[CanonicalSignal],
    *,
    chain_index: str,
    bar: str,
) -> dict[str, CandleSeries]:
    """Fetch one candle series per distinct token, at the SELECTED bar only.

    A token whose series is irreducibly ambiguous (duplicate ``ts_open_ms`` carrying different
    candles) is OMITTED rather than sealed or repaired. The trial itself stays in the pack: dropping
    it would change the population the season is scored over, whereas an absent series is reported
    by the scorer as UNSCORED, which is what actually happened.
    """
    settlement: dict[str, CandleSeries] = {}
    for token in dict.fromkeys(trial.token_address for trial in trials):
        fetched = await client.get_candles(chain_index, token, bar, limit=CANDLE_LIMIT)
        series = _with_unique_open_times(fetched)
        if series is not None:
            settlement[token] = series
    return settlement


async def run_fetch_and_seal(
    preflight_path: Path,
    client: MarketClient,
    data_dir: Path,
    *,
    cost_bps: int = FROZEN_DECLARED_COST_BPS,
    cooldown_ms: int = FROZEN_COOLDOWN_MS,
    horizon_ms: int = FROZEN_HORIZON_MS,
    season_id: str | None = None,
) -> Path | None:
    """Seal the season's pack, or record why no pack could be sealed.

    ``async`` because the market client's read surface is async (``preflight.MarketClient``). The
    plan's ``Produces`` sketch writes it ``def``; per PKT-DEC-C16 a Produces signature is a sketch
    and may be adjusted where reality requires, and the transport contract requires it here.

    **EVERY non-sealable artifact returns before the first ``await``, so it issues no transport call
    at all** rather than merely discarding the result of one. That is a claim about which artifacts
    reach the fetch, not only about where the returns sit, and it is why ``non_sealable_reason``
    tests the combo for MEMBERSHIP in the frozen matrix rather than merely for non-nullness — an
    earlier revision checked only ``is None``, and an artifact naming a bar of ``"5m"`` therefore
    passed the guard and was fetched once per token before the seal refused it.

    **Nothing here publishes a season.** ``qualified`` and ``exploratory`` are the SCORER's verdicts
    (H3.5). Writing one at seal time would assert a season over a pack nothing has scored yet, and
    ``published.read_season`` would then be obliged to refuse a payload the state insists exists.

    Args:
        preflight_path: The authoritative ``preflight_result.json``.
        client: Structural market read surface. No transport is constructed here.
        data_dir: The signal-trials data directory. Packs land in ``<data_dir>/packs/``.
        cost_bps: Declared round-trip cost sealed into the pack. Frozen default 25 (§5.1).
        cooldown_ms: Same-token dedup window. Frozen default 4h.
        horizon_ms: Ranked settlement horizon, recorded in the meta. Frozen default 1h.
        season_id: Override for the generated season id, for a caller that needs a stable name.

    Returns:
        The sealed pack's directory, or ``None`` when the artifact was not sealable.

    Raises:
        ValueError: The preflight artifact exists but cannot be read as a JSON object, or the
            season id derived from it cannot safely be a directory name.
        MixedBarError: A fetched series disagrees with the season's bar. A ``ValueError`` subclass.
        FileExistsError: A pack directory for this season already exists and is not empty. Named
            explicitly because it is an ``OSError``, NOT a ``ValueError`` — a caller catching only
            ``ValueError`` to mean "the seal was refused" would miss exactly this one.
    """
    data_dir = Path(data_dir)
    artifact = _read_artifact(Path(preflight_path))
    if artifact is None:
        published.write_state(
            data_dir,
            published.NOT_BUILT,
            {"reason": f"no preflight artifact at {preflight_path}: no probe has authorized a season"},
        )
        return None

    verdict = non_sealable_reason(artifact)
    if verdict is not None:
        # Carries the artifact through as supporting evidence so an operator can explain the state
        # afterwards without needing the original file — including the counts on the `no_season`
        # route, which plan line 417 names specifically. `reason` is spread LAST so this module's
        # explanation of its own decision cannot be displaced by a same-named key in the artifact.
        published.write_state(data_dir, verdict.state, {**artifact, "reason": verdict.reason})
        return None

    # Narrowed by `non_sealable_reason`, which returns non-None for either being absent.
    chain_index = str(artifact["chain_index"])
    bar = str(artifact["bar"])
    season_status = str(artifact["season_status"])

    filters = SignalFilters(chain_index=chain_index)
    trials = await _fetch_trials(client, filters, cooldown_ms=cooldown_ms)
    settlement = await _fetch_settlement(client, trials, chain_index=chain_index, bar=bar)

    # REQUEST provenance, never the wire. Reading `bar_ms` off a fetched series would let the
    # response define the width it is then checked against, and `seal_pack`'s mixed-bar guard would
    # be comparing the series to themselves — it would pass a client that returned every series at
    # the wrong width. The selected bar is what the fetch was ISSUED under, so it is what the pack is
    # sealed at, and a series that disagrees is refused.
    bar_ms = frozen_bar_ms(bar)
    meta = PackMeta(
        season_id=season_id or _season_id(chain_index, bar),
        # Only `season_status` needs widening — it is a `SeasonStatus` Literal and `:325` has already
        # narrowed it to `str`. A blanket `type: ignore[arg-type]` here would also suppress checking
        # of `chain_index` and `bar`, so a later change to either type would go unnoticed under
        # `--strict`; the cast puts the suppression on the one argument that needs it.
        combo=ComboSelection(chain_index, bar, cast(SeasonStatus, season_status)),
        probe_counts={"counts": artifact.get("counts", []), "rejection_reasons": artifact.get("rejection_reasons", {})},
        # `asdict` rather than `vars()`: `SignalFilters` is a frozen dataclass today, but `vars()`
        # raises `TypeError` the moment anyone adds `slots=True`, and this is not the file that would
        # find out.
        filters=dataclasses.asdict(filters),
        cost_bps=cost_bps,
        horizon_ms=horizon_ms,
        bar=bar,
        bar_ms=bar_ms,
        # The pack format version is NOT restated here. `PackMeta.pack_format_version` defaults to
        # `pack.PACK_FORMAT_VERSION` and rides the hashed meta region, so there is exactly one
        # surface declaring it; a copy in this dict could disagree with the module that owns it.
        versions={"preflight_policy": json.dumps(artifact.get("policy", {}), sort_keys=True)},
    )
    ref = seal_pack(trials, settlement, meta, out_dir=data_dir / PACKS_DIRNAME)
    return ref.dir


def frozen_bar_ms(bar: str) -> int:
    """The width of a frozen bar, in milliseconds.

    The season's ``bar_ms`` is REQUEST provenance — the width the fetch was issued under — which is
    the same rule ``spot_markout.select_settlement_candle`` states for settlement. It is never read
    back off a response, so a series arriving at another width is a disagreement the seal can detect
    rather than a value that quietly redefines the season.

    Raises:
        ValueError: ``bar`` is not one of the widths in the frozen matrix (``COMBO_ORDER``).
    """
    if bar not in FROZEN_BAR_MS:
        raise ValueError(f"bar {bar!r} is not a frozen width; expected one of {sorted(FROZEN_BAR_MS)}")
    return FROZEN_BAR_MS[bar]
