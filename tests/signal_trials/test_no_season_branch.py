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

import functools
import json
import sys
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

import pytest

from veridex.signal_trials import published
from veridex.signal_trials.okx_client import Candle, CandleSeries, SignalFilters, SignalPage
from veridex.signal_trials.pack import MixedBarError, PackRef, load_pack, read_pack_ref
from veridex.signal_trials.preflight import (
    COMBO_ORDER,
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
        settle_ms = T0_MS + FROZEN_HORIZON_MS
        candles = tuple(
            Candle(settle_ms - BAR_MS * offset, 1.0, 1.5, 0.5, 1.0 + offset, 10.0, 100.0, True) for offset in (1, 0)
        )
        return CandleSeries(bar=bar, bar_ms=BAR_MS, candles=candles)


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


def _wire_signal(index: int) -> dict[str, Any]:
    """One raw REST row that survives the frozen filters (verified against ``preflight._screen``)."""
    return {
        "timestamp": str(T0_MS + index * BAR_MS),
        "chainIndex": CHAIN,
        "price": "0.042",
        "walletType": "1",
        "triggerWalletCount": "7",
        "triggerWalletAddress": f"0xwallet{index}",
        "amountUsd": "25000",
        "token": {
            "tokenAddress": f"0xtoken{index}",
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
    """Every bar the frozen matrix can select has a width this module can price."""
    module = _fetch_and_seal_module()
    assert {bar for _chain, bar in COMBO_ORDER} <= set(module.FROZEN_BAR_MS)


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
