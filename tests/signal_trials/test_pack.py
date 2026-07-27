"""H2.4 — the sealed pack: roundtrip, tamper-evidence, and bar provenance.

The three plan-mandated vectors are ``test_seal_then_load_roundtrip``,
``test_tampered_byte_fails_closed`` and ``test_mixed_bar_series_rejected_at_seal``. The rest defend
properties the seal claims but those three do not exercise: that the METADATA is hash-bound (a pack
whose ``cost_bps`` can be edited after sealing prices every markout off an unsealed number), and that
the two places ``PackMeta`` names the bar cannot disagree.

``canonical_fixtures`` lives here rather than in a ``conftest.py``: this task owns four files and a
shared conftest is not one of them. A module-level fixture is visible to exactly the tests that use
it, which is all the plan's snippet requires.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.okx_client import Candle, CandleSeries
from veridex.signal_trials.pack import (
    MANIFEST_FILENAME,
    PACK_FORMAT_VERSION,
    SETTLEMENT_FILENAME,
    TRIALS_FILENAME,
    MixedBarError,
    PackIntegrityError,
    PackMeta,
    load_pack,
    seal_pack,
)
from veridex.signal_trials.preflight import ComboSelection

BAR = "1m"
BAR_MS = 60_000
HORIZON_MS = 3_600_000
T0_MS = 1_753_400_000_000

# Every fixture trial carries a symbol beginning "TOK", which is what the plan's tamper vector flips.
_TRIAL_COUNT = 3

#: What the ``canonical_fixtures`` fixture yields. Named so the twelve consumers can annotate it
#: without restating the triple, and so ``tmp_path: Path`` beside it is not the only annotated
#: parameter in the file.
CanonicalFixtures = tuple[list[CanonicalSignal], dict[str, CandleSeries], PackMeta]


def _signal(index: int) -> CanonicalSignal:
    """One canonical trial.

    ``CanonicalSignal`` is ``BaseModel, frozen=True`` (``challenge_spec.py:41``) with thirteen
    fields — it cannot be constructed positionally, so every argument is named.
    """
    return CanonicalSignal(
        t0_ms=T0_MS + index * BAR_MS,
        chain_index="196",
        token_address=f"0xtoken{index}",
        symbol=f"TOK{index}",
        name=f"Token {index}",
        market_cap_usd=5_000_000.0 + index,
        holders=1_200 + index,
        top10_holder_percent=31.5,
        trigger_price=0.042 + index,
        wallet_type="1",
        trigger_wallet_count=7,
        trigger_wallet_address=f"0xwallet{index}",
        amount_usd=25_000.0 + index,
    )


def _series(signal: CanonicalSignal) -> CandleSeries:
    """A settlement series spanning ``signal``'s horizon.

    ``Candle`` is arity 8 in a FIXED order and its public shape must not change (C17-R2); it is
    constructed positionally here exactly as the law constructs it.
    """
    settle_ms = signal.t0_ms + HORIZON_MS
    candles = tuple(
        Candle(settle_ms - BAR_MS * offset, 1.0, 1.5, 0.5, 1.0 + offset, 10.0, 100.0, True) for offset in (1, 0)
    )
    return CandleSeries(bar=BAR, bar_ms=BAR_MS, candles=candles)


@pytest.fixture
def canonical_fixtures() -> CanonicalFixtures:
    """Three canonical trials, one settlement series per token, and a matching 1m/25bps meta."""
    trials = [_signal(index) for index in range(_TRIAL_COUNT)]
    settlement = {signal.token_address: _series(signal) for signal in trials}
    meta = PackMeta(
        season_id="season-h24-fixture",
        combo=ComboSelection("196", BAR, "qualified"),
        probe_counts={"196:1m": 41},
        filters={"wallet_type": "1", "min_address_count": 2},
        cost_bps=25,
        horizon_ms=HORIZON_MS,
        bar=BAR,
        bar_ms=BAR_MS,
        versions={"pack": "1", "spec": "5.1"},
    )
    return trials, settlement, meta


# --- The three plan-mandated vectors -------------------------------------------------------------


def test_seal_then_load_roundtrip(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """A sealed pack loads back with its meta and every trial intact.

    This is also the ACCEPTANCE CONTROL for every refusal below: a suite that only asserts refusal
    passes identically when the subject refuses everything.
    """
    trials, settlement, meta = canonical_fixtures
    pack = load_pack(seal_pack(trials, settlement, meta, out_dir=tmp_path))
    assert pack.meta.cost_bps == 25
    assert pack.meta.bar == BAR
    assert len(pack.trials) == _TRIAL_COUNT
    assert [trial.token_address for trial in pack.trials] == [trial.token_address for trial in trials]
    assert pack.settlement[trials[0].token_address].bar_ms == BAR_MS


def test_tampered_byte_fails_closed(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """One flipped byte in ``trials.json`` makes the pack refuse to load."""
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    path = ref.dir / TRIALS_FILENAME
    original = path.read_bytes()
    tampered = original.replace(b"TOK", b"HAK", 1)
    assert tampered != original, "the tamper vector must actually change the bytes it claims to change"
    path.write_bytes(tampered)
    with pytest.raises(PackIntegrityError):
        load_pack(ref)


def test_mixed_bar_series_rejected_at_seal(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """One series at a different bar width refuses the whole seal."""
    trials, settlement, meta = canonical_fixtures
    key = next(iter(settlement))
    settlement = dict(settlement)
    settlement[key] = CandleSeries(bar="1H", bar_ms=3_600_000, candles=settlement[key].candles)
    with pytest.raises(MixedBarError):
        seal_pack(trials, settlement, meta, out_dir=tmp_path)


# --- Properties the seal claims that the three above do not reach ---------------------------------


def test_a_refused_seal_writes_nothing(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """A mixed-bar refusal leaves no partial pack behind.

    Validation before any write is the difference between "refused" and "refused, but the next reader
    finds half a pack": ``out_dir`` must be untouched.
    """
    trials, settlement, meta = canonical_fixtures
    key = next(iter(settlement))
    settlement = dict(settlement)
    settlement[key] = CandleSeries(bar="1H", bar_ms=3_600_000, candles=settlement[key].candles)
    with pytest.raises(MixedBarError):
        seal_pack(trials, settlement, meta, out_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_bar_name_disagreeing_with_bar_ms_is_rejected(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """A series whose ``bar`` name contradicts its own ``bar_ms`` is not a settled provenance.

    DISCRIMINATION CONTROL for the mixed-bar guard: ``test_mixed_bar_series_rejected_at_seal`` moves
    BOTH fields together, so a guard reading only ``bar_ms`` would pass it. This moves only the name.
    """
    trials, settlement, meta = canonical_fixtures
    key = next(iter(settlement))
    settlement = dict(settlement)
    settlement[key] = CandleSeries(bar="1H", bar_ms=BAR_MS, candles=settlement[key].candles)
    with pytest.raises(MixedBarError):
        seal_pack(trials, settlement, meta, out_dir=tmp_path)


def test_meta_bar_must_agree_with_its_own_combo(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """``PackMeta`` names the bar twice, and a pack may not be sealed while the two disagree."""
    trials, settlement, base = canonical_fixtures
    meta = PackMeta(
        season_id=base.season_id,
        combo=ComboSelection("196", "1H", "qualified"),
        probe_counts=base.probe_counts,
        filters=base.filters,
        cost_bps=base.cost_bps,
        horizon_ms=base.horizon_ms,
        bar=BAR,
        bar_ms=BAR_MS,
        versions=base.versions,
    )
    with pytest.raises(MixedBarError):
        seal_pack(trials, settlement, meta, out_dir=tmp_path)


def test_edited_meta_fails_closed(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """Rewriting ``cost_bps`` in the manifest invalidates the pack.

    ``cost_bps`` prices every markout in the season. A content hash over the DATA FILES ALONE would
    let it be edited after sealing and still load clean, so the meta is folded into the digest —
    the same reason ``replay_pack`` v2 binds its authority block.
    """
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    path = ref.dir / MANIFEST_FILENAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["meta"]["cost_bps"] == 25
    manifest["meta"]["cost_bps"] = 0
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PackIntegrityError):
        load_pack(ref)


def test_tampered_settlement_fails_closed(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """The settlement leg is in the hash scope too, not just ``trials.json``."""
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    path = ref.dir / SETTLEMENT_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    series = payload[trials[0].token_address]
    series["candles"][0]["close"] = 999.0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PackIntegrityError):
        load_pack(ref)


def test_load_rejects_a_ref_whose_hash_disagrees(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """A caller holding an independent record of the hash is honoured over the manifest.

    Tampering with the data AND the manifest together is self-consistent on disk. The ref the sealer
    handed back is the second witness, so ``load_pack`` checks against it as well.
    """
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    forged = type(ref)(dir=ref.dir, content_hash="0" * 64)
    with pytest.raises(PackIntegrityError):
        load_pack(forged)


def test_seal_writes_exactly_the_three_pack_files(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """The pack directory holds the three named files and nothing else."""
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    written = sorted(entry.name for entry in ref.dir.iterdir())
    assert written == sorted([MANIFEST_FILENAME, SETTLEMENT_FILENAME, TRIALS_FILENAME])


# --- season_id is untrusted input to a path join --------------------------------------------------

# The shape ``fetch_and_seal._season_id`` emits. That generator's output is proven ACCEPTABLE on the
# integration side — every seal-path test in ``test_no_season_branch.py`` runs through it, so a guard
# that rejected the real format would fail there. This literal is the acceptance control for the
# guard read in isolation.
_GENERATED_SHAPE_SEASON_ID = "season-196-1m-20260727T010553Z"

_ESCAPING_SEASON_IDS = [
    "../escaped",
    "..",
    "nested/child",
    "/absolute",
    ".",
]


@pytest.mark.parametrize("season_id", _ESCAPING_SEASON_IDS)
def test_a_season_id_that_could_escape_out_dir_is_refused(
    tmp_path: Path, canonical_fixtures: CanonicalFixtures, season_id: str
) -> None:
    """``season_id`` becomes a directory name, and it arrives from a hand-editable artifact.

    ``run_fetch_and_seal`` builds it as ``season-{chain_index}-{bar}-{moment}`` with ``chain_index``
    read straight off ``preflight_result.json``, and the non-sealable check tests that field only for
    ``is None`` — never for CONTENT. So a restored or hand-edited artifact carrying a traversal in
    ``chain_index`` reaches this guard, and nothing else stands between it and the path join.

    Asserted in the units of the claim: not merely that it raises, but that NOTHING was written
    outside ``out_dir``. A guard that raised after creating the directory would pass a raises-only
    test while having already escaped.
    """
    trials, settlement, base = canonical_fixtures
    out_dir = tmp_path / "packs"
    out_dir.mkdir()
    before = sorted(entry.name for entry in tmp_path.iterdir())

    with pytest.raises(ValueError, match="not a usable directory name"):
        seal_pack(trials, settlement, replace(base, season_id=season_id), out_dir=out_dir)

    assert list(out_dir.iterdir()) == []
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before


def test_a_generated_season_id_is_accepted(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """ACCEPTANCE CONTROL for the ``season_id`` guard: it must SEPARATE, not merely refuse.

    Without this, every vector above passes against a guard that rejects every id — including one
    whose pattern is simply broken.
    """
    trials, settlement, base = canonical_fixtures
    ref = seal_pack(trials, settlement, replace(base, season_id=_GENERATED_SHAPE_SEASON_ID), out_dir=tmp_path)

    assert ref.dir.name == _GENERATED_SHAPE_SEASON_ID
    assert ref.dir.parent == tmp_path
    assert load_pack(ref).meta.season_id == _GENERATED_SHAPE_SEASON_ID


# --- one pack directory holds one generation ------------------------------------------------------


def test_sealing_twice_into_one_season_dir_is_refused_and_the_first_survives(
    tmp_path: Path, canonical_fixtures
) -> None:
    """A second seal over an existing pack is refused, and the first pack is left intact.

    Both halves matter. The hash scope is a FIXED set of three filenames, so a file left by an
    earlier build would not even be VISIBLE in the digest — two generations side by side is a pack
    that verifies while not being what it appears to be. And a refusal that had already clobbered the
    first pack would trade one failure for a worse one.
    """
    trials, settlement, base = canonical_fixtures
    meta = replace(base, season_id="season-fixed-id")
    first = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    original_hash = first.content_hash

    with pytest.raises(FileExistsError):
        seal_pack(trials, settlement, meta, out_dir=tmp_path)

    reloaded = load_pack(first)
    assert reloaded.ref.content_hash == original_hash
    assert len(reloaded.trials) == _TRIAL_COUNT
    assert sorted(entry.name for entry in first.dir.iterdir()) == sorted(
        [MANIFEST_FILENAME, SETTLEMENT_FILENAME, TRIALS_FILENAME]
    )


def test_two_seasons_may_share_one_out_dir(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """DISCRIMINATION CONTROL: the refusal is about the SEASON directory, not about ``out_dir``.

    ``out_dir`` is ``<data_dir>/packs/`` and is expected to accumulate seasons. A guard that refused
    any non-empty ``out_dir`` would pass the test above and break the directory's actual purpose.
    """
    trials, settlement, base = canonical_fixtures
    first = seal_pack(trials, settlement, replace(base, season_id="season-one"), out_dir=tmp_path)
    second = seal_pack(trials, settlement, replace(base, season_id="season-two"), out_dir=tmp_path)

    assert first.dir != second.dir
    assert first.dir.parent == second.dir.parent == tmp_path
    assert load_pack(first).meta.season_id == "season-one"
    assert load_pack(second).meta.season_id == "season-two"


# --- the format version is declared once, hash-bound, and enforced on read ------------------------


def test_a_pack_from_another_format_version_is_refused(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """A pack sealed under a different format version will not load, even though it is INTACT.

    This is the case the digest alone cannot catch, and it is the whole reason the check exists. A
    hand-edited version breaks the hash and would be refused anyway; a pack sealed legitimately by a
    FUTURE build carries a version this module does not implement together with a digest that is
    perfectly valid under that build's rules. Nothing but comparing the version can refuse it.

    The assertions separate the two failure modes deliberately: the pack's own digest is verified to
    MATCH first, so the refusal below is provably about the version and not about tampering.
    """
    trials, settlement, base = canonical_fixtures
    future = replace(base, pack_format_version=PACK_FORMAT_VERSION + 1)
    ref = seal_pack(trials, settlement, future, out_dir=tmp_path)

    manifest = json.loads((ref.dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["meta"]["pack_format_version"] == PACK_FORMAT_VERSION + 1
    # The pack is INTACT: the sealer's own hash is what the manifest records.
    assert manifest["content_hash"] == ref.content_hash

    with pytest.raises(PackIntegrityError, match="format version"):
        load_pack(ref)


def test_the_format_version_is_hash_bound(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """Editing the version in the manifest changes the digest, so it cannot be swapped silently.

    DISCRIMINATION against the previous test: that one proves a *validly sealed* other-version pack
    is refused. This one proves the version is inside the hashed region rather than beside it — the
    property the constant's own comment claims. If it sat outside ``meta``, this edit would produce a
    pack that loads clean under the wrong rules.
    """
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    path = ref.dir / MANIFEST_FILENAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["meta"]["pack_format_version"] == PACK_FORMAT_VERSION
    manifest["meta"]["pack_format_version"] = PACK_FORMAT_VERSION + 1
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackIntegrityError):
        load_pack(ref)


def test_the_version_is_declared_in_exactly_one_place(tmp_path: Path, canonical_fixtures: CanonicalFixtures) -> None:
    """ACCEPTANCE plus single-surface: the sealed pack declares its format once, inside ``meta``.

    Two authoritative-looking surfaces that can disagree is the defect this module refuses for the
    bar; a manifest-level copy of the version would reintroduce it one level up.
    """
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    manifest = json.loads((ref.dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    assert "pack_format_version" not in manifest
    assert manifest["meta"]["pack_format_version"] == PACK_FORMAT_VERSION
    assert load_pack(ref).meta.pack_format_version == PACK_FORMAT_VERSION


def test_a_manifest_declaring_another_hash_scope_is_refused(
    tmp_path: Path, canonical_fixtures: CanonicalFixtures
) -> None:
    """``manifest["files"]`` records the hash scope, and a reader that ignored it would be lying.

    The field is not hash-bound — it is a description of what this reader will hash, not evidence —
    so this is a consistency refusal rather than a tamper check, and it is asserted as such: the
    manifest can be edited, and the point is that the edit is NOTICED rather than silently ignored.
    """
    trials, settlement, meta = canonical_fixtures
    ref = seal_pack(trials, settlement, meta, out_dir=tmp_path)
    path = ref.dir / MANIFEST_FILENAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["files"] == sorted([SETTLEMENT_FILENAME, TRIALS_FILENAME])
    manifest["files"] = ["not-even-a-real-file.json"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackIntegrityError, match="hash scope"):
        load_pack(ref)


def test_the_content_hash_is_independent_of_mapping_order(
    tmp_path: Path, canonical_fixtures: CanonicalFixtures
) -> None:
    """Identical content in a different insertion order seals to the SAME digest (probe G3).

    ``_canonical_bytes`` uses ``sort_keys=True`` and argues that this is what makes the encoding
    independent of insertion order. Nothing bound that claim: seal and load share the function, so
    a non-canonical encoding keeps every roundtrip and every tamper vector working and only breaks
    reproducibility ACROSS builds — which no test reached. This is that test.
    """
    trials, settlement, meta = canonical_fixtures
    reversed_settlement = dict(reversed(list(settlement.items())))
    assert list(reversed_settlement) != list(settlement), "the vector must actually reverse the order"
    assert reversed_settlement == settlement, "and must not change the content while doing so"

    # The two arms go to DIFFERENT out_dirs under the SAME meta. Varying `season_id` to avoid the
    # second-generation refusal would confound the measurement: `season_id` is itself hash-bound, so
    # both arms would differ for a reason that has nothing to do with insertion order.
    forward = seal_pack(trials, settlement, meta, out_dir=tmp_path / "a")
    backward = seal_pack(trials, reversed_settlement, meta, out_dir=tmp_path / "b")

    assert forward.content_hash == backward.content_hash
