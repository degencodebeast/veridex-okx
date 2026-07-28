"""H2.4 — the executable non-sealable branch (PKT-DEC-C48).

The frozen plan short-circuits ``run_fetch_and_seal`` on ONE condition, ``season_status ==
"no_season"``. H2.3 later widened ``ProbeStatus`` to four values whose failures expose
``season_status`` NULL, so a plan-faithful implementation falls through to "otherwise seals the
pack" and seals with a null chain/bar combo. C48 supersedes plan lines 416-418 on branch logic:
EVERY non-sealable artifact is non-sealable.

``preflight.py:625-655`` already refuses to WRITE such a selection, but that is a write-side guard
inside preflight. This module reads ``preflight_result.json`` back off disk, and nothing on that
path re-runs it — so the read path needs its own guard, and these are its vectors.

**Every fixture artifact below is written by preflight's OWN writers** rather than hand-assembled,
so the reader is proven against what preflight actually emits. The single exception is the
completed-with-null-combo artifact, which the write-side guard refuses to produce: it can only reach
disk by hand-editing or corruption, and the test constructs it exactly that way.

Two controls run alongside the refusals:

* an ACCEPTANCE control (``test_a_completed_qualified_artifact_reaches_the_seal``) — without it this
  suite passes identically against a ``run_fetch_and_seal`` that refuses EVERYTHING, including one
  whose transport fake is simply broken;
* a DISCRIMINATION control (``test_no_season_and_failed_are_not_written_as_the_same_state``) — the
  refusals must not merely all refuse, they must record WHICH answer they refused on.
"""

from __future__ import annotations

import contextlib
import functools
import json
import subprocess
import sys
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

import pytest

from veridex.signal_trials import published
from veridex.signal_trials.okx_client import Candle, CandleSeries, SignalFilters, SignalPage
from veridex.signal_trials.pack import (
    PACK_FORMAT_VERSION,
    MixedBarError,
    PackRef,
    load_pack,
    read_pack_ref,
)
from veridex.signal_trials.preflight import (
    COMBO_ORDER,
    FROZEN_COOLDOWN_MS,
    FROZEN_HORIZON_MS,
    ComboCount,
    ComboSelection,
    MatrixProbeResult,
    write_preflight_failure,
    write_preflight_not_run,
    write_preflight_result,
)


@functools.lru_cache(maxsize=1)
def _fetch_and_seal_module():
    """Load the operator script BY PATH, once.

    By path because importing ``scripts.signal_trials.fetch_and_seal`` would rely on a
    ``scripts/signal_trials/__init__.py`` this task does not own — the same reason and the same
    mechanism ``test_preflight.py`` uses for ``run_preflight.py``. Cached so the module executes once.

    Registered in ``sys.modules`` BEFORE execution, which ``run_preflight.py`` did not need: the
    ``@dataclass`` decorator resolves its annotations through ``sys.modules[cls.__module__]``, and a
    module that was never registered resolves to ``None`` there. Without this the module raises
    ``AttributeError: 'NoneType' object has no attribute '__dict__'`` at import.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "fetch_and_seal.py"
    assert script.exists(), f"operator script missing at {script}"
    name = "fetch_and_seal_under_test"
    spec = importlib_util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def run_fetch_and_seal(preflight_path: Path, client: Any, data_dir: Path) -> Path | None:
    """Call the script's entry point, loaded by path."""
    result: Path | None = await _fetch_and_seal_module().run_fetch_and_seal(preflight_path, client, data_dir)
    return result


BAR = "1m"
BAR_MS = 60_000
CHAIN = "196"
T0_MS = 1_753_400_000_000

#: What the fake transport answers each bar at. Independent of the module under test's own table on
#: purpose — a fake that imported ``FROZEN_BAR_MS`` could not disagree with it, and a wire that
#: disagrees with the request is exactly what ``MislabelledWidthClient`` exists to simulate.
_BAR_MS_BY_NAME = {"1m": 60_000, "1H": 3_600_000}


class RecordingClient:
    """A ``MarketClient`` that records every call it receives and issues no transport.

    ``calls == []`` is the whole assertion on a short-circuit, so this records BEFORE returning:
    a call that raised would still be recorded, and a guard that fetched then discarded the result
    could not hide behind an exception.
    """

    signal_source_direction = "buy"

    def __init__(self, signals: tuple[dict[str, Any], ...] = ()) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._signals = signals

    async def list_signals(self, f: SignalFilters, cursor: str | None = None) -> SignalPage:
        self.calls.append(("list_signals", f.chain_index, cursor))
        return SignalPage(signals=self._signals, next_cursor=None)

    async def get_candles(
        self,
        chain_index: str,
        token: str,
        bar: str,
        *,
        before_ms: int | None = None,
        limit: int = 100,
    ) -> CandleSeries:
        self.calls.append(("get_candles", chain_index, token, bar))
        # Answers at the width it was ASKED for. A fake that always returned 1m would make every
        # non-1m combo trip the mixed-bar guard for a reason the venue never caused, and would hide
        # whether the caller requests the right bar at all.
        #
        # An UNKNOWN bar gets a nominal width rather than a KeyError. A real venue does not raise
        # `KeyError` because this file has no row for a bar, and a fake that did would preempt the
        # subject's own behaviour: the non-frozen-combo vectors would fail inside the HARNESS before
        # the module under test ever decided anything.
        width = _BAR_MS_BY_NAME.get(bar, BAR_MS)
        settle_ms = T0_MS + FROZEN_HORIZON_MS
        candles = tuple(
            Candle(settle_ms - width * offset, 1.0, 1.5, 0.5, 1.0 + offset, 10.0, 100.0, True) for offset in (1, 0)
        )
        return CandleSeries(bar=bar, bar_ms=width, candles=candles)


class WrongWidthClient(RecordingClient):
    """A client that answers every candle request at 1H however it was asked.

    Not a hypothetical: ``get_candles`` takes the bar as a request parameter, and nothing in the
    response is obliged to honour it. Both halves of the series disagree with the request here — the
    NAME and the width — so this vector is caught by whichever check runs first.
    """

    async def get_candles(
        self,
        chain_index: str,
        token: str,
        bar: str,
        *,
        before_ms: int | None = None,
        limit: int = 100,
    ) -> CandleSeries:
        await super().get_candles(chain_index, token, bar, before_ms=before_ms, limit=limit)
        settle_ms = T0_MS + FROZEN_HORIZON_MS
        return CandleSeries(
            bar="1H",
            bar_ms=3_600_000,
            candles=(Candle(settle_ms - 3_600_000, 1.0, 1.5, 0.5, 1.0, 10.0, 100.0, True),),
        )


class MislabelledWidthClient(RecordingClient):
    """A client whose series carry the REQUESTED bar name at the WRONG width.

    The sharp vector, and the only one that isolates where ``bar_ms`` comes from. Because the name
    agrees, the bar-NAME check cannot fire; the seal's only remaining witness is the ``bar_ms`` the
    pack was sealed at. If that were read off this very response the two would agree by construction
    and the pack would seal at 1H under a ``1m`` heading.
    """

    async def get_candles(
        self,
        chain_index: str,
        token: str,
        bar: str,
        *,
        before_ms: int | None = None,
        limit: int = 100,
    ) -> CandleSeries:
        await super().get_candles(chain_index, token, bar, before_ms=before_ms, limit=limit)
        settle_ms = T0_MS + FROZEN_HORIZON_MS
        return CandleSeries(
            bar=bar,
            bar_ms=3_600_000,
            candles=(Candle(settle_ms - 3_600_000, 1.0, 1.5, 0.5, 1.0, 10.0, 100.0, True),),
        )


class DuplicateOpenTimeClient(RecordingClient):
    """Returns ONE token's series with two candles sharing a ``ts_open_ms``.

    ``identical`` selects which of ``_with_unique_open_times``' two outcomes the response lands in,
    and the pair is what makes this a discriminating vector rather than a single rejection:

    * ``identical=False`` — the duplicates carry DIFFERENT closes, so the series is irreducibly
      ambiguous and the token is dropped from settlement;
    * ``identical=True`` — the duplicates are equal, so the series is COLLAPSED and kept.

    ``spot_markout.select_settlement_candle`` is why the first case cannot be sealed: on a duplicate
    ``ts_open_ms`` the two rows tie on ``close_ts``, ``min`` keeps whichever the wire put first, and
    the settlement PRICE then follows wire order. Sealing it would make the season's own numbers
    depend on how a page happened to paginate.
    """

    def __init__(
        self,
        signals: tuple[dict[str, Any], ...] = (),
        *,
        duplicated_token: str,
        identical: bool,
    ) -> None:
        super().__init__(signals)
        self._duplicated_token = duplicated_token
        self._identical = identical

    async def get_candles(
        self,
        chain_index: str,
        token: str,
        bar: str,
        *,
        before_ms: int | None = None,
        limit: int = 100,
    ) -> CandleSeries:
        clean = await super().get_candles(chain_index, token, bar, before_ms=before_ms, limit=limit)
        if token != self._duplicated_token:
            return clean
        first = clean.candles[-1]
        twin = first if self._identical else Candle(first.ts_open_ms, 1.0, 1.5, 0.5, 99.0, 10.0, 100.0, True)
        return CandleSeries(bar=clean.bar, bar_ms=clean.bar_ms, candles=(first, twin))


def _wire_signal(
    index: int, *, token: str | None = None, offset_ms: int | None = None, chain: str = CHAIN
) -> dict[str, Any]:
    """One raw REST row that survives the frozen filters (verified against ``preflight._screen``).

    ``token`` and ``offset_ms`` default to being derived from ``index``, which gives every row a
    DISTINCT token — the shape every other vector in this file wants. The cooldown vector needs the
    opposite (one token at several times), so both are overridable rather than duplicated. ``chain``
    is overridable because ``_screen`` rejects a row whose ``chainIndex`` does not match the combo
    being probed, so a vector covering the whole frozen matrix has to speak each combo's chain.
    """
    address = token if token is not None else f"0xtoken{index}"
    t0_ms = T0_MS + (offset_ms if offset_ms is not None else index * BAR_MS)
    return {
        "timestamp": str(t0_ms),
        "chainIndex": chain,
        "price": "0.042",
        "walletType": "1",
        "triggerWalletCount": "7",
        "triggerWalletAddress": f"0xwallet{index}",
        "amountUsd": "25000",
        "token": {
            "tokenAddress": address,
            "symbol": f"TOK{index}",
            "name": f"Token {index}",
            "marketCapUsd": "5000000",
            "holders": "1200",
            "top10HolderPercent": "31.5",
        },
    }


def _matrix(counts: dict[tuple[str, str], int], *, direction_confirmed: bool = True) -> MatrixProbeResult:
    """A full frozen matrix — all four combos, as ``_index_counts`` requires."""
    return MatrixProbeResult(
        counts=tuple(ComboCount(chain, bar, counts.get((chain, bar), 0), {}) for chain, bar in COMBO_ORDER),
        direction_semantics_confirmed=direction_confirmed,
    )


def _write_no_season(path: Path) -> None:
    """A real ``completed`` + ``no_season`` artifact: the probe RAN and DECLINED."""
    write_preflight_result(ComboSelection(None, None, "no_season"), _matrix({}), path)


def _write_qualified(path: Path) -> None:
    """A real ``completed`` + ``qualified`` artifact naming a non-null combo."""
    write_preflight_result(ComboSelection(CHAIN, BAR, "qualified"), _matrix({(CHAIN, BAR): 41}), path)


def _state_of(data_dir: Path) -> str:
    state: str = published.read_state(data_dir)["state"]
    return state


def _reason_of(data_dir: Path) -> str:
    """The published detail's own explanation of the refusal.

    Asserted alongside the state because the state ALONE does not bind the branch that produced it:
    ``failed``/``refused``/``aborted`` all carry a null combo too, so the null-combo branch would
    record the same ``not_built`` if the probe-status branch were deleted. The reason is what
    distinguishes which guard actually fired — a guard checked in the units of its own claim (C46).
    """
    reason: str = published.read_state(data_dir)["detail"]["reason"]
    return reason


# --- The plan's mandated vector -------------------------------------------------------------------


async def test_no_season_short_circuits(tmp_path: Path) -> None:
    """``completed`` + ``no_season``: returns None, records ``no_season``, fetches nothing.

    The probe ran and declined. That is an OBSERVATION, and ``no_season`` is the state that says so
    (``published.py:55-56`` — ``not_built`` means the scorer never ran, ``no_season`` means it ran
    and declined).
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_no_season(preflight_path)
    client = RecordingClient()

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "no_season"


async def test_no_season_detail_carries_the_probe_counts(tmp_path: Path) -> None:
    """The recorded verdict keeps the counts it was reached from, per plan line 417."""
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_no_season(preflight_path)

    await run_fetch_and_seal(preflight_path, RecordingClient(), data_dir)

    detail = published.read_state(data_dir)["detail"]
    assert detail["probe_status"] == "completed"
    assert detail["season_status"] == "no_season"
    assert len(detail["counts"]) == len(COMBO_ORDER)


# --- C48: the three probe statuses the frozen plan does not branch on ------------------------------


@pytest.mark.parametrize("probe_status", ["refused", "aborted"])
async def test_a_run_that_never_probed_is_not_sealable(tmp_path: Path, probe_status: str) -> None:
    """``refused`` and ``aborted`` expose a null combo, and neither is an observation.

    Recording these as ``no_season`` would claim the probe RAN AND DECLINED about a run that never
    happened — the observation-versus-failure conflation ``preflight.py:36-40`` exists to prevent.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    write_preflight_not_run(probe_status, "sentinel reason: no credentials supplied", preflight_path)
    client = RecordingClient()

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "not_built"
    assert probe_status in _reason_of(data_dir)


async def test_a_failed_probe_is_not_sealable(tmp_path: Path) -> None:
    """``failed`` exposes ``season_status`` null and no counts at all — not a season, not an emptiness."""
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    write_preflight_failure("sentinel reason: upstream returned a malformed page", preflight_path)
    client = RecordingClient()

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "not_built"
    assert "failed" in _reason_of(data_dir)


async def test_a_completed_probe_with_a_null_combo_is_not_sealable(tmp_path: Path) -> None:
    """The exact artifact the frozen plan would have sealed on.

    ``preflight.write_preflight_result`` REFUSES to emit this, so it is produced here the only way it
    can reach disk: by editing a legitimate qualified artifact. That is the hand-edit / partial-restore
    case the read path is responsible for, and the plan's single ``no_season`` comparison misses it —
    ``season_status`` is ``"qualified"``, so control would fall straight through to the seal.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
    artifact["chain_index"] = None
    artifact["bar"] = None
    preflight_path.write_text(json.dumps(artifact), encoding="utf-8")
    client = RecordingClient()

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "not_built"
    assert "must name the market it ran on" in _reason_of(data_dir)


async def test_an_unrecognized_probe_status_is_not_sealable(tmp_path: Path) -> None:
    """Fail-closed on a status outside the frozen four, rather than open.

    DISCRIMINATION for the guard's SHAPE: a guard written as a membership test against the three
    known-bad statuses would accept this; ``!= "completed"`` refuses it.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
    artifact["probe_status"] = "sentinel-unknown-status"
    preflight_path.write_text(json.dumps(artifact), encoding="utf-8")
    client = RecordingClient()

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "not_built"


async def test_an_unrecognized_season_status_is_not_sealable(tmp_path: Path) -> None:
    """A combo-naming artifact whose season_status is outside the frozen set fails closed."""
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
    artifact["season_status"] = "sentinel-unknown-season-status"
    preflight_path.write_text(json.dumps(artifact), encoding="utf-8")
    client = RecordingClient()

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "not_built"


async def test_an_absent_artifact_authorizes_nothing(tmp_path: Path) -> None:
    """No artifact at all means no probe has authorized a season.

    Absence is a reading preflight's contract names explicitly (``preflight.py:32-40``), so it is
    answered rather than crashed on — and answered as ``not_built``, because nothing observed.
    """
    data_dir = tmp_path / "data"
    client = RecordingClient()

    result = await run_fetch_and_seal(tmp_path / "absent.json", client, data_dir)

    assert result is None
    assert client.calls == []
    assert _state_of(data_dir) == "not_built"


async def test_a_corrupt_artifact_is_refused_rather_than_interpreted(tmp_path: Path) -> None:
    """Corruption is not a verdict.

    The mirror of the test above, and the reason the two are separate: absence says something true
    about the season, while an unparseable artifact says nothing at all. Publishing a state from it
    would be inventing an observation.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    preflight_path.write_text("{not json at all", encoding="utf-8")
    client = RecordingClient()

    with pytest.raises(ValueError):
        await run_fetch_and_seal(preflight_path, client, data_dir)
    assert client.calls == []


def test_frozen_bar_ms_covers_the_frozen_matrix() -> None:
    """Every bar the frozen matrix can select has a width this module can price.

    Subset, not equality: ``FROZEN_BAR_MS`` is a PRICING table and an extra row in it is harmless.
    What must not happen is an extra row WIDENING a guard, which is what the next test pins.
    """
    module = _fetch_and_seal_module()
    assert {bar for _chain, bar in COMBO_ORDER} <= set(module.FROZEN_BAR_MS)


def test_the_membership_guards_derive_from_the_frozen_matrix() -> None:
    """The combo guards' authority is ``COMBO_ORDER`` EXACTLY — not a table that happens to agree.

    Asserted as equality against the matrix rather than by naming rejected strings. A guard pinned
    only by the vectors that exercise it (``5m``, ``1D``, ``999``) is pinned for those strings and
    not structurally: adding ``"4H"`` to a table the guard consulted would silently admit a bar the
    frozen probe can never select, and no vector in this file names ``4H``. This is what makes the
    pricing table unable to widen the guard, and it is why the two are separate objects.
    """
    module = _fetch_and_seal_module()
    assert {bar for _chain, bar in COMBO_ORDER} == module.FROZEN_BARS
    assert {chain for chain, _bar in COMBO_ORDER} == module.FROZEN_CHAINS


async def test_a_series_at_the_wrong_width_is_refused(tmp_path: Path) -> None:
    """A client returning series at a different bar entirely is refused, not accommodated."""
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = WrongWidthClient(signals=tuple(_wire_signal(index) for index in range(3)))

    with pytest.raises(MixedBarError):
        await run_fetch_and_seal(preflight_path, client, data_dir)


async def test_the_season_bar_ms_is_request_provenance_not_the_wire(tmp_path: Path) -> None:
    """DISCRIMINATION CONTROL for where ``bar_ms`` comes from.

    ``test_a_series_at_the_wrong_width_is_refused`` does NOT establish this, and a mutation drill is
    how that was found: mutating ``bar_ms`` to be read off the first fetched series left that test
    passing, because its client also mislabels the bar NAME and the name check fires first. The test
    was true and its stated reason was not.

    This client returns the REQUESTED name at the wrong width, so the name check cannot fire. If
    ``bar_ms`` came from the response, the pack would seal at 3_600_000 under a ``1m`` heading and
    every settlement in the season would be read at the wrong horizon.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = MislabelledWidthClient(signals=tuple(_wire_signal(index) for index in range(3)))

    with pytest.raises(MixedBarError):
        await run_fetch_and_seal(preflight_path, client, data_dir)


async def test_no_season_and_failed_are_not_written_as_the_same_state(tmp_path: Path) -> None:
    """DISCRIMINATION CONTROL: the refusals must not collapse onto one answer.

    Every refusal test above asserts a state, but each alone still passes against an implementation
    that writes ONE state for all of them. This is the vector that fails when they are collapsed.
    """
    declined_dir = tmp_path / "declined"
    failed_dir = tmp_path / "failed"
    declined_path = tmp_path / "declined.json"
    failed_path = tmp_path / "failed.json"
    _write_no_season(declined_path)
    write_preflight_failure("sentinel reason: probe did not complete", failed_path)

    await run_fetch_and_seal(declined_path, RecordingClient(), declined_dir)
    await run_fetch_and_seal(failed_path, RecordingClient(), failed_dir)

    assert _state_of(declined_dir) == "no_season"
    assert _state_of(failed_dir) == "not_built"
    assert _state_of(declined_dir) != _state_of(failed_dir)


# --- The acceptance control -----------------------------------------------------------------------


async def test_a_completed_qualified_artifact_reaches_the_seal(tmp_path: Path) -> None:
    """ACCEPTANCE CONTROL: the one artifact that IS sealable gets fetched and sealed.

    Without this, every assertion above is satisfied by a ``run_fetch_and_seal`` that refuses
    unconditionally — including one whose fake transport never worked in the first place.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    result = await run_fetch_and_seal(preflight_path, client, data_dir)

    assert result is not None
    assert result.is_dir()
    assert [call for call in client.calls if call[0] == "list_signals"] != []
    assert [call for call in client.calls if call[0] == "get_candles"] != []
    # Every candle fetch is at the SELECTED bar — a pack settled at another width is a different season.
    assert {call[3] for call in client.calls if call[0] == "get_candles"} == {BAR}


async def test_the_sealed_pack_loads_and_carries_the_selected_combo(tmp_path: Path) -> None:
    """The seal path produces a pack that verifies, and its meta names the probe's own combo."""
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    result = await run_fetch_and_seal(preflight_path, client, data_dir)
    assert result is not None

    ref: PackRef = read_pack_ref(result)
    pack = load_pack(ref)
    assert pack.meta.combo == ComboSelection(CHAIN, BAR, "qualified")
    assert pack.meta.bar == BAR
    assert pack.meta.bar_ms == BAR_MS
    assert len(pack.trials) == 3


async def test_the_sealed_pack_declares_its_format_version_only_once(tmp_path: Path) -> None:
    """``versions`` may not restate the pack format — ``PackMeta`` owns that field.

    Removing the duplicate was the fix; this is what keeps it removed. Nothing else in the suite
    notices a second surface being reintroduced: the copy is inert, so every roundtrip, digest and
    refusal vector stays green while the pack once again declares its own format in two places that
    can drift. That is the defect this module refuses for the bar, one level up, and it is the
    reason the duplicate existed in the first place.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    result = await run_fetch_and_seal(preflight_path, client, data_dir)
    assert result is not None
    pack = load_pack(read_pack_ref(result))

    assert pack.meta.pack_format_version == PACK_FORMAT_VERSION
    assert "pack_format" not in pack.meta.versions
    # ACCEPTANCE: `versions` still carries what it IS for — provenance pack.py does not own.
    assert "preflight_policy" in pack.meta.versions


async def test_the_seal_path_publishes_no_season_state(tmp_path: Path) -> None:
    """Sealing a pack is not scoring a season, so it may not claim one.

    ``qualified``/``exploratory`` are the SCORER's verdicts (H3.5). Writing one here would assert a
    season over a pack nothing has scored yet, and ``published.read_season`` would then refuse to
    serve a payload the state insists exists.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    await run_fetch_and_seal(preflight_path, client, data_dir)

    assert _state_of(data_dir) == "not_built"


# --- Settlement normalization: the property the private import is justified BY --------------------


async def _seal_with_duplicates(tmp_path: Path, *, identical: bool) -> tuple[Any, str]:
    """Seal a pack where one token's series carries duplicate ``ts_open_ms``. Returns (pack, token)."""
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    signals = tuple(_wire_signal(index) for index in range(3))
    duplicated_token = str(signals[0]["token"]["tokenAddress"])
    client = DuplicateOpenTimeClient(signals=signals, duplicated_token=duplicated_token, identical=identical)

    result = await run_fetch_and_seal(preflight_path, client, data_dir)
    assert result is not None
    return load_pack(read_pack_ref(result)), duplicated_token


async def test_an_ambiguous_series_is_omitted_while_its_trial_is_retained(tmp_path: Path) -> None:
    """A series that cannot be settled deterministically is dropped; its TRIAL stays in the pack.

    Both halves are asserted because both are the documented behaviour, and they pull in opposite
    directions. Omitting the series is what keeps the season's prices independent of pagination
    order; retaining the trial is what keeps the scored POPULATION unchanged — dropping it would
    quietly shrink the season instead of reporting the trial UNSCORED.

    The ACCEPTANCE half rides in the same assertion: the two unambiguous tokens ARE present, so this
    cannot pass against an implementation that seals an empty settlement.
    """
    pack, duplicated_token = await _seal_with_duplicates(tmp_path, identical=False)

    assert duplicated_token not in pack.settlement
    assert [trial.token_address for trial in pack.trials].count(duplicated_token) == 1
    assert len(pack.trials) == 3
    assert sorted(pack.settlement) == sorted(
        trial.token_address for trial in pack.trials if trial.token_address != duplicated_token
    )


async def test_duplicate_but_identical_candles_are_collapsed_and_kept(tmp_path: Path) -> None:
    """DISCRIMINATION CONTROL: only an IRREDUCIBLE ambiguity is dropped.

    ``test_an_ambiguous_series_is_omitted_while_its_trial_is_retained`` on its own is satisfied by an
    implementation that discards any series carrying a repeated ``ts_open_ms`` at all — and equally
    by one that never normalizes and simply mislays a token. This separates those: equal duplicates
    are collapsed to one candle and the token is KEPT, which is a different outcome from both.

    The collapse assertion is also what makes this vector fail against a pack sealed from the raw
    response, where the duplicate would still be sitting in the series.
    """
    pack, duplicated_token = await _seal_with_duplicates(tmp_path, identical=True)

    assert duplicated_token in pack.settlement
    assert len(pack.settlement) == 3
    series = pack.settlement[duplicated_token]
    assert len(series.candles) == 1
    assert len({candle.ts_open_ms for candle in series.candles}) == 1


# --- The published reason is this module's, not the artifact's ------------------------------------


async def test_a_hand_edited_reason_cannot_displace_the_modules_own(tmp_path: Path) -> None:
    """The artifact is spread into ``detail`` as evidence; it may not overwrite the verdict.

    ``reason`` is spread LAST so a same-named key in the artifact cannot displace this module's
    explanation of its own decision. No preflight writer emits a bare ``reason`` (they emit
    ``failure_reason``), so this defends against the hand-edited artifact the read path already takes
    seriously — and it is the field the whole probe-status binding is asserted through, so a
    displaced reason would silently weaken four other tests.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    write_preflight_failure("sentinel reason: probe did not complete", preflight_path)
    artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
    artifact["reason"] = "SENTINEL-INJECTED-REASON-the season was fine"
    preflight_path.write_text(json.dumps(artifact), encoding="utf-8")

    result = await run_fetch_and_seal(preflight_path, RecordingClient(), data_dir)

    assert result is None
    published_reason = _reason_of(data_dir)
    assert "SENTINEL-INJECTED-REASON" not in published_reason
    assert "failed" in published_reason
    # ACCEPTANCE: the artifact is still carried as evidence — the guard narrows one key, not the detail.
    assert published.read_state(data_dir)["detail"]["probe_status"] == "failed"


# --- a combo outside the frozen matrix is refused BEFORE any transport ----------------------------


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("bar", "5m", "not a frozen width"),
        ("bar", "1D", "not a frozen width"),
        ("chain_index", "999", "not in the frozen matrix"),
        ("chain_index", "../../etc", "not in the frozen matrix"),
    ],
)
async def test_a_combo_outside_the_frozen_matrix_fetches_nothing(
    tmp_path: Path, field: str, value: str, needle: str
) -> None:
    """A non-frozen chain or bar is non-sealable, and is refused before the first ``await``.

    THE CALL COUNT IS THE DISCRIMINATOR, AND THE TEST IS ARRANGED SO THAT IT ACTUALLY IS ONE.
    Without the ``try`` below it would not be: with the guard removed, these vectors fail by a
    propagating exception — ``frozen_bar_ms`` at SEAL time, or the ``season_id`` traversal guard —
    which reaches the test before any assertion runs. The mutants would still die, but by the same
    exception mechanism the guard is supposed to make unnecessary, and the count would be decoration.
    Catching first makes ``client.calls == []`` the assertion that fails, which is the claim: the
    old behaviour ran the ENTIRE fetch at the bogus combo — one ``list_signals`` plus one
    ``get_candles`` per token — and only then objected. On a credentialed run those were real
    requests against a venue at a combo the frozen matrix never selects.

    A refusal is also NOT a raise, and the second assertion pins that: a non-sealable artifact is
    recorded and returns ``None`` like every other one, rather than propagating.

    ``chain_index`` is covered by the same vector because it is fetched just as eagerly. Under the
    unguarded chain it was strictly worse than the bar: the bar failed closed at ``frozen_bar_ms``,
    while a non-frozen chain SEALED A PACK unless its id happened to be path-unsafe.
    """
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
    artifact[field] = value
    preflight_path.write_text(json.dumps(artifact), encoding="utf-8")
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    raised: Exception | None = None
    result: Path | None = None
    try:
        result = await run_fetch_and_seal(preflight_path, client, data_dir)
    except (ValueError, KeyError) as error:
        raised = error

    # FIRST, so it is the assertion that fails when the guard is removed.
    assert client.calls == [], f"a non-frozen combo must fetch nothing (raised={raised!r})"
    assert raised is None, f"a non-sealable combo must be RECORDED and refused, not raise: {raised!r}"
    assert result is None
    assert _state_of(data_dir) == "not_built"
    assert needle in _reason_of(data_dir)


async def test_every_frozen_combo_still_reaches_the_fetch(tmp_path: Path) -> None:
    """ACCEPTANCE CONTROL for the membership guard: it must SEPARATE, not merely refuse.

    Without this, every vector above passes against a guard that rejects every combo — including one
    whose frozen table is simply wrong. Run over the whole frozen matrix rather than one combo, so a
    table that happened to omit a real entry is caught here rather than at Gate B.
    """
    for index, (chain, bar) in enumerate(COMBO_ORDER):
        preflight_path = tmp_path / f"preflight_{index}.json"
        data_dir = tmp_path / f"data_{index}"
        write_preflight_result(ComboSelection(chain, bar, "qualified"), _matrix({(chain, bar): 41}), preflight_path)
        client = RecordingClient(signals=tuple(_wire_signal(i, chain=chain) for i in range(3)))

        result = await run_fetch_and_seal(preflight_path, client, data_dir)

        assert result is not None, f"frozen combo {(chain, bar)} was refused"
        assert client.calls != []
        assert {call[3] for call in client.calls if call[0] == "get_candles"} == {bar}
        assert load_pack(read_pack_ref(result)).meta.combo == ComboSelection(chain, bar, "qualified")


# --- §8.6's same-token cooldown decides the season's scored population ----------------------------


async def test_the_same_token_cooldown_decides_which_trials_are_sealed(tmp_path: Path) -> None:
    """The 4h cooldown is applied to the trials that reach the pack, and it is a WINDOW.

    Three properties, because §8.6 is a time-windowed rule that keeps a SPECIFIC member and a test
    binding only the first would pass against two different wrong rules:

    1. **The refusal** — a second signal on the same token INSIDE the window does not become a
       second trial. Bound on the SEALED PACK rather than on ``_dedup_by_cooldown``'s return,
       because the claim is about what gets sealed: a test calling the helper directly would still
       pass if this module stopped calling it, which is the exact defect being closed.
    2. **The window is a window** — a third signal on the same token OUTSIDE the window SURVIVES.
       Without this the test passes against "one trial per token, forever", a strictly wrong rule.
    3. **WHICH member survives** — the EARLIEST of each cluster, which ``_dedup_by_cooldown``
       argues is the no-look-ahead choice a chronological replay reaches first. Bound by token C,
       and ONLY by token C. See below: this assertion was unbound in the first version of this test.

    Plus acceptance: a different token is untouched, so it cannot pass against a cooldown that eats
    everything. The window is half-open (exactly ``cooldown_ms`` apart survives), so the outside
    vector sits a full bar clear of the boundary rather than on it.

    **WHY TOKEN C EXISTS, AND WHY TOKEN A CANNOT DO ITS JOB.** Token A's cluster is 0, +1h, +4h1m
    against a 4h window, and it is SYMMETRIC under the transformation property 3 claims to detect::

        |mid - first|  =  3_600_000 < 14_400_000   -> inside
        |last - mid|   = 10_860_000 < 14_400_000   -> inside

    The middle signal is inside the cooldown of BOTH neighbours, so it drops under an
    earliest-first sweep and under a latest-first one alike, and the survivors are the two extremes
    either way. Asserting ``a_offsets == [0, outside_ms]`` therefore holds under the correct rule
    AND under its look-ahead inverse — it was passing property 3 for no reason at all. A two-member
    cluster has no middle to be symmetric about, which is what makes the direction observable:
    earliest-first keeps ``[0]``, latest-first keeps ``[inside_ms]``.

    THE FIXTURE IS PART OF THE PREDICATE. A cluster symmetric under the transformation you are
    trying to detect is exactly as blind as a control that cannot fire.
    """
    inside_ms = 3_600_000
    outside_ms = FROZEN_COOLDOWN_MS + BAR_MS
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = RecordingClient(
        signals=(
            _wire_signal(0, token="0xtokenA", offset_ms=0),
            _wire_signal(1, token="0xtokenA", offset_ms=inside_ms),
            _wire_signal(2, token="0xtokenA", offset_ms=outside_ms),
            _wire_signal(3, token="0xtokenB", offset_ms=inside_ms),
            # An ASYMMETRIC cluster: two members, one window, no middle. This is the only vector
            # here that can tell an earliest-first sweep from a latest-first one.
            _wire_signal(4, token="0xtokenC", offset_ms=0),
            _wire_signal(5, token="0xtokenC", offset_ms=inside_ms),
        )
    )

    result = await run_fetch_and_seal(preflight_path, client, data_dir)
    assert result is not None
    pack = load_pack(read_pack_ref(result))

    # 1 — six eligible signals in, four trials sealed.
    assert len(pack.trials) == 4
    # 2 — the window is a window: token A's signal beyond it survives, the one inside it does not.
    a_offsets = sorted(trial.t0_ms - T0_MS for trial in pack.trials if trial.token_address == "0xtokenA")
    assert a_offsets == [0, outside_ms]
    assert inside_ms not in a_offsets
    # 3 — NO LOOK-AHEAD: of two signals inside one window, the EARLIER survives. Keeping the later
    # would be a rule that consults what happens after the decision point.
    c_offsets = [trial.t0_ms - T0_MS for trial in pack.trials if trial.token_address == "0xtokenC"]
    assert c_offsets == [0], f"expected the EARLIEST of the cluster to survive, got {c_offsets}"
    # ACCEPTANCE — a different token inside the same window is untouched.
    assert [trial.t0_ms - T0_MS for trial in pack.trials if trial.token_address == "0xtokenB"] == [inside_ms]


# --- the Gate B operator command ------------------------------------------------------------------
#
# The frozen command is
#     fetch_and_seal.py --from-preflight preflight_result.json --out $SIGNAL_TRIALS_DATA_DIR/packs/
# and before these vectors existed it PARSED NOTHING, fetched nothing, wrote nothing and exited 0.
# An operator at Gate B would have read success from a command that had done nothing at all.
#
# NO REAL OR REALISTIC CREDENTIAL APPEARS HERE. The three below are sentinels, and the redaction
# vector asserts the sentinel's ABSENCE from rendered output rather than asserting that some real
# value was hidden.

_SENTINEL_ENV = {
    "OKX_API_KEY": "SENTINEL-APIKEY-ZZZZ0001",
    "OKX_SECRET_KEY": "SENTINEL-SECRET-ZZZZ0002",
    "OKX_PASSPHRASE": "SENTINEL-PASSPHRASE-ZZZZ0003",
}


def _factory_yielding(client: Any) -> Any:
    """A ``SourceFactory`` that hands ``main`` an already-built recording fake."""

    @contextlib.asynccontextmanager
    async def factory(_credentials: Any) -> Any:
        yield client

    return factory


def _set_sentinel_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SENTINEL_ENV.items():
        monkeypatch.setenv(name, value)


def test_the_frozen_operator_command_parses() -> None:
    """``--from-preflight`` and ``--out`` are the frozen surface, and both are required.

    The bare parse is worth pinning on its own: the defect this closes was not a wrong argument
    surface, it was NO argument surface — the script accepted anything and exited 0.
    """
    module = _fetch_and_seal_module()
    args = module.build_arg_parser().parse_args(["--from-preflight", "preflight_result.json", "--out", "/data/packs"])
    assert args.from_preflight == Path("preflight_result.json")
    assert args.out == Path("/data/packs")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="neither"),
        pytest.param(["--out", "/data/packs"], id="missing-from-preflight"),
        pytest.param(["--from-preflight", "p.json"], id="missing-out"),
    ],
)
def test_each_frozen_argument_is_required_individually(argv: list[str]) -> None:
    """EACH argument is required, not merely one of them.

    Asserted per-argument because the obvious form is weaker than it reads: with only ``[]`` as the
    vector, making ``--from-preflight`` optional still errors — ``--out`` is missing too — so the
    test passes while half the frozen surface has quietly become optional. An operator would then
    get an exit-0 run against a default they never named. A mutation drill found this in exactly
    that state; the parametrization is what closes it.
    """
    module = _fetch_and_seal_module()
    with pytest.raises(SystemExit) as exit_info:
        module.build_arg_parser().parse_args(argv)
    assert exit_info.value.code != 0


def test_out_must_name_the_packs_directory(tmp_path: Path) -> None:
    """``--out`` names ``packs/``; the data dir is its PARENT, which is where state is published."""
    module = _fetch_and_seal_module()
    assert module.data_dir_from_out(tmp_path / "packs") == tmp_path
    with pytest.raises(ValueError, match="must name the 'packs' directory"):
        module.data_dir_from_out(tmp_path / "somewhere-else")


def test_the_operator_command_refuses_a_non_frozen_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A differently-shaped ``--out`` exits NON-ZERO and fetches nothing.

    Refused before credentials are even read, so this vector needs none.
    """
    module = _fetch_and_seal_module()
    client = RecordingClient()
    code = module.main(
        ["--from-preflight", str(tmp_path / "p.json"), "--out", str(tmp_path / "not-packs")],
        source_factory=_factory_yielding(client),
    )
    assert code == 3
    assert client.calls == []


def test_the_operator_command_aborts_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing credentials abort NON-ZERO, naming the VARIABLES and never a value."""
    module = _fetch_and_seal_module()
    for name in _SENTINEL_ENV:
        monkeypatch.delenv(name, raising=False)
    preflight_path = tmp_path / "preflight_result.json"
    _write_no_season(preflight_path)
    client = RecordingClient()

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(tmp_path / "data" / "packs")],
        source_factory=_factory_yielding(client),
    )

    assert code == 2
    assert client.calls == []


def test_the_operator_command_seals_a_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sealable artifact reaches the injected client and WRITES A PACK, exit 0.

    The acceptance half of the whole entry point: without it every refusal vector below is satisfied
    by a command that refuses unconditionally.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(client),
    )

    assert code == 0
    assert [call for call in client.calls if call[0] == "list_signals"] != []
    packs = sorted((data_dir / "packs").iterdir())
    assert len(packs) == 1
    pack = load_pack(read_pack_ref(packs[0]))
    assert len(pack.trials) == 3
    assert pack.meta.combo == ComboSelection(CHAIN, BAR, "qualified")


@pytest.mark.parametrize("writer", ["no_season", "failed", "non_frozen_bar"])
def test_the_operator_command_fetches_nothing_on_a_non_sealable_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    """Every non-sealable verdict still fetches NOTHING through the operator command.

    ASSERTED AS THE CALL COUNT. A command that fetched and then failed would satisfy a bare
    exit-code check, which is the same reason the guard's own vectors assert the count.

    This also answers whether adding an entry point made any existing refusal unreachable: it did
    not. ``main`` delegates to ``run_fetch_and_seal``, so all three verdict shapes — an observation
    (``no_season``), a failure (``failed``), and a corrupted combo (a non-frozen bar) — still reach
    their branches and still publish their state, now with an exit code an operator can read.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    if writer == "no_season":
        _write_no_season(preflight_path)
        expected_state = "no_season"
    elif writer == "failed":
        write_preflight_failure("sentinel reason: probe did not complete", preflight_path)
        expected_state = "not_built"
    else:
        _write_qualified(preflight_path)
        artifact = json.loads(preflight_path.read_text(encoding="utf-8"))
        artifact["bar"] = "5m"
        preflight_path.write_text(json.dumps(artifact), encoding="utf-8")
        expected_state = "not_built"
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(client),
    )

    assert client.calls == []
    assert code == 0, "an honest non-sealable verdict is a completed run, not a failure"
    assert _state_of(data_dir) == expected_state
    assert not (data_dir / "packs").exists()


def test_a_failure_message_is_redacted_before_it_is_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A credential appearing in an exception NEVER reaches the operator's terminal.

    The sentinel is placed where a real credential would be — inside the raised message — and its
    ABSENCE from rendered output is what is asserted. The DISCRIMINATION half is the second sentinel:
    a non-credential marker in the same message must survive, so this cannot pass against a redactor
    that blanks everything, or against a test whose capture is simply empty.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_qualified(preflight_path)

    class _Exploding(RecordingClient):
        async def list_signals(self, f: SignalFilters, cursor: str | None = None) -> SignalPage:
            raise RuntimeError(
                f"upstream rejected key={_SENTINEL_ENV['OKX_API_KEY']} "
                f"secret={_SENTINEL_ENV['OKX_SECRET_KEY']} "
                f"pass={_SENTINEL_ENV['OKX_PASSPHRASE']} marker=SENTINEL-NONCREDENTIAL-ZZZZ0009"
            )

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(_Exploding()),
    )
    rendered = capsys.readouterr()
    combined = rendered.out + rendered.err

    assert code == 1
    for secret in _SENTINEL_ENV.values():
        assert secret not in combined, f"a credential reached the operator's terminal: {secret}"
    # DISCRIMINATION — the message was rendered, and only the credentials were removed.
    assert "SENTINEL-NONCREDENTIAL-ZZZZ0009" in combined
    assert "RuntimeError" in combined


def test_the_frozen_command_is_a_real_command_in_a_subprocess() -> None:
    """Run the operator command as an OPERATOR runs it, in its own interpreter.

    The `main(argv)` vectors above all import the module first, which is exactly what the broken
    revision did NOT do wrong — it imported fine. What it got wrong was being invoked as a command:
    ``python scripts/signal_trials/fetch_and_seal.py --help`` exited 0 having printed nothing. Only
    a subprocess reproduces that, because it is the only thing that exercises ``__main__``, the
    ``sys.path`` a script actually gets, and the exit code a shell actually sees.

    SCOPED DELIBERATELY TO PATHS THAT RETURN BEFORE ANY CREDENTIAL OR TRANSPORT WORK. Gate A is
    operator-only and this command is precisely the thing that would touch it, so no vector here
    supplies credentials or reaches the client factory.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "signal_trials" / "fetch_and_seal.py"
    root = script.parents[2]

    helped = subprocess.run(  # noqa: S603
        [sys.executable, str(script), "--help"], capture_output=True, text=True, cwd=root, timeout=60
    )
    assert helped.returncode == 0
    assert "--from-preflight" in helped.stdout
    assert "--out" in helped.stdout

    # No arguments is an ERROR. This is the assertion the broken revision would have failed: it
    # exited 0 on every invocation, including this one.
    bare = subprocess.run(  # noqa: S603
        [sys.executable, str(script)], capture_output=True, text=True, cwd=root, timeout=60
    )
    assert bare.returncode != 0
    assert "--from-preflight" in bare.stderr

    # A non-frozen --out is refused before credentials are read, so this needs none.
    refused = subprocess.run(  # noqa: S603
        [sys.executable, str(script), "--from-preflight", "x.json", "--out", "/tmp/not-packs"],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=60,
    )
    assert refused.returncode == 3
    assert "packs" in refused.stderr

    # And from a DIFFERENT working directory, because a script's sys.path[0] is its own directory
    # and the run_preflight seam is imported by anchoring on __file__ rather than on the cwd.
    elsewhere = subprocess.run(  # noqa: S603
        [sys.executable, str(script), "--from-preflight", "x.json", "--out", "/tmp/not-packs"],
        capture_output=True,
        text=True,
        cwd=script.parent,
        timeout=60,
    )
    assert elsewhere.returncode == 3, f"the command must work from any cwd: {elsewhere.stderr}"


# ---------------------------------------------------------------------------
# CF-6 — the credential-redaction boundary on the SUCCESS paths.
#
# `main` builds `secrets` as soon as credentials are read, and the FAILURE path at
# exit 1 redacts against it. The three exit-0 outputs did not, while the function's
# own docstring promised "every message is redacted before it is printed". The
# redactor was in scope and simply not applied — a stated contract the success path
# did not honour.
#
# Each test below drives a sentinel credential value into one success output through
# a CONTROLLED SEAM and asserts the value does not survive to stdout. The failure-path
# and diagnostic-preservation controls sit alongside them, because a redactor that
# blanks everything would satisfy the leak tests while destroying the output.
# ---------------------------------------------------------------------------


def test_the_sealed_pack_path_does_not_echo_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit-0 output 3 — ``pack sealed at {sealed}``.

    Seam: the operator-supplied ``--out``. A path carrying a credential value is
    printed verbatim on the success path while the identical value in a failure
    message is redacted.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    leak = _SENTINEL_ENV["OKX_API_KEY"]
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / f"data-{leak}"
    _write_qualified(preflight_path)
    client = RecordingClient(signals=tuple(_wire_signal(index) for index in range(3)))

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(client),
    )

    assert code == 0
    assert leak not in capsys.readouterr().out, "the sealed-pack line published a credential value"


def test_the_no_season_state_line_does_not_echo_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit-0 output 1 — ``published state is {state!r}``.

    Seam: the published-state read. ``!r`` does not redact; it only adds quotes.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    leak = _SENTINEL_ENV["OKX_SECRET_KEY"]
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_no_season(preflight_path)
    monkeypatch.setattr(
        module.published, "read_state", lambda _dir: {"state": f"no_season::{leak}"}
    )
    client = RecordingClient()

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(client),
    )

    assert code == 0
    assert leak not in capsys.readouterr().out, "the state line published a credential value"


def test_the_state_location_line_does_not_echo_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit-0 output 2 — ``state written under {data_dir / PUBLISHED_DIRNAME}``."""
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    leak = _SENTINEL_ENV["OKX_PASSPHRASE"]
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / f"data-{leak}"
    _write_no_season(preflight_path)
    client = RecordingClient()

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(client),
    )

    assert code == 0
    assert leak not in capsys.readouterr().out, "the state-location line published a credential value"


def test_the_success_output_keeps_its_non_secret_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DISCRIMINATION — redaction must not be achieved by printing nothing useful.

    A redactor that blanked the whole line would pass every leak test above. The
    operator still needs to know WHICH state was published and WHERE it went, so the
    non-secret substance of both lines is pinned here.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    preflight_path = tmp_path / "preflight_result.json"
    data_dir = tmp_path / "data"
    _write_no_season(preflight_path)
    client = RecordingClient()

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(data_dir / "packs")],
        source_factory=_factory_yielding(client),
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "no pack sealed" in out, "the operator must still learn no pack was sealed"
    assert "no_season" in out, "the published state must still be named"
    assert "state written under" in out, "the operator must still learn where state went"
    assert str(data_dir) in out, "the non-secret path must survive redaction"


def test_the_failure_path_remains_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CONTROL — exit 1 was already redacted and must stay that way.

    Green before and after, by design: it exists so a change to the success boundary
    cannot silently regress the failure boundary that already worked.
    """
    module = _fetch_and_seal_module()
    _set_sentinel_credentials(monkeypatch)
    leak = _SENTINEL_ENV["OKX_API_KEY"]
    preflight_path = tmp_path / "preflight_result.json"
    _write_qualified(preflight_path)

    @contextlib.asynccontextmanager
    async def _exploding_factory(_credentials: Any) -> Any:
        raise RuntimeError(f"upstream rejected {leak}")
        yield  # pragma: no cover - unreachable, present so this is an async generator

    code = module.main(
        ["--from-preflight", str(preflight_path), "--out", str(tmp_path / "data" / "packs")],
        source_factory=_exploding_factory,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert leak not in captured.err, "the failure path leaked a credential value"
    assert "upstream rejected" in captured.err, "the diagnostic itself must survive"
