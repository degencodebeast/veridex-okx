"""H2.4 — the sealed signal-trials pack: a tamper-evident, self-describing season input.

A pack is the frozen record of what a season was scored over. It is written once by
``scripts/signal_trials/fetch_and_seal.py`` and read by the H3.5 scorer, which runs in a different
process at a different time — so, like every other artifact on this path, it has to carry its own
evidence rather than rely on the run that produced it still being observable.

Three properties, each answering a specific way a season can quietly become a false claim.

1. **One bar per season, checked against the REAL model.** §5.1 makes the season's bar a
   probe-gated decision, and a settlement series at a different width is a different settlement
   (``spot_markout.select_settlement_candle`` takes ``bar_ms`` from the series as request
   provenance). A pack mixing widths would score some trials at 1m and some at 1H while presenting
   one leaderboard, so :func:`seal_pack` refuses the whole seal rather than the offending series —
   a partial pack is the thing a reader cannot detect.

   ``PackMeta`` names the bar TWICE — once as ``bar``/``bar_ms``, once inside ``combo`` — and those
   two are checked against each other before anything is written. Two authoritative-looking surfaces
   that can disagree is the defect recorded at the end of PKT-DEC-C48; here they cannot.

2. **The metadata is INSIDE the digest, not beside it.** ``cost_bps`` prices every markout in the
   season and ``combo`` names the market it was settled against. A ``content_hash`` over the data
   files alone would leave both editable after sealing, and the pack would still load clean. The
   canonical meta block is therefore folded in behind a domain separator, exactly as
   ``veridex/ingest/replay_pack.py`` binds its v2 authority block for the same reason.

3. **One in-memory read, so what is verified is what is parsed.** :func:`load_pack` reads each file
   exactly once, hashes THOSE bytes, and parses THOSE bytes. Re-reading from disk to parse after
   hashing would leave a window in which the second read returns something the first did not — the
   load-vs-hash TOCTOU ``replay_pack._content_hash_from_file_bytes`` exists to close.

**Scope boundary, stated so its absence is not read as an oversight.** :func:`seal_pack` enforces
BAR provenance. It does not police whether ``combo`` names a chain, whether the probe that produced
that combo completed, or whether every trial has a settlement series. The first two are the read-path
obligation of ``run_fetch_and_seal`` (PKT-DEC-C48), which is where a preflight artifact is actually
consulted; the third is the scorer's, which reports an unsettled trial as UNSCORED rather than
dropping it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veridex.signal_trials.challenge_spec import CanonicalSignal
from veridex.signal_trials.okx_client import Candle, CandleSeries
from veridex.signal_trials.preflight import ComboSelection

#: Bumped when the on-disk layout or the digest construction changes. Carried as a ``PackMeta``
#: FIELD, so it rides the hash-bound meta region and is compared by :func:`load_pack` against the
#: version this module implements — a pack cannot be read under a format whose rules it was not
#: written by. It is declared in exactly ONE place on disk (``manifest.meta.pack_format_version``);
#: a second copy elsewhere in the manifest could disagree with it, which is the defect this module
#: refuses for the bar and must not reintroduce for its own format version.
PACK_FORMAT_VERSION = 1

TRIALS_FILENAME = "trials.json"
SETTLEMENT_FILENAME = "settlement.json"
MANIFEST_FILENAME = "manifest.json"

#: The files the content hash covers, as a FIXED set rather than a directory listing. The hash scope
#: is therefore a property of this module, not of whatever happens to be sitting in the pack
#: directory: a stale file from an earlier build can neither join the digest nor silently leave it.
DATA_FILENAMES: tuple[str, ...] = (SETTLEMENT_FILENAME, TRIALS_FILENAME)

#: Separates the data-file region of the digest from the meta region, so a meta block can never be
#: mistaken for file content and no meta-inclusive hash can collide with a data-only one.
_META_DOMAIN_SEP = b"\x00signaltrials.pack.meta.v1\x00"

#: ``season_id`` becomes a directory name, so it is constrained to characters that cannot escape
#: ``out_dir``. A season id is machine-generated; this rejects a hand-edited or injected one.
_SEASON_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class MixedBarError(ValueError):
    """The pack does not have exactly one, agreed-upon bar.

    ``ValueError`` so callers already treating bad input as ``ValueError`` keep working, but its own
    type so "these series disagree about the bar" is never conflated with a malformed pack on disk.
    """


class PackIntegrityError(ValueError):
    """A sealed pack does not match its own digest, or cannot be read as a pack at all.

    Raised for a tampered byte, an edited manifest, a missing leg, and unreadable JSON alike: from a
    reader's position these are one thing — the pack cannot be trusted to be what it says it is.
    Returning a partially-loaded pack for any of them would put an unverified season on the
    leaderboard, which is the single outcome this module exists to prevent.
    """


@dataclass(frozen=True)
class PackMeta:
    """Everything about a season that is not a trial or a candle.

    ``combo`` is the preflight selection the pack was authorized by, carried verbatim so the pack
    states which market it settled against rather than leaving it to be inferred from the trials.

    ``probe_counts`` IS NOT A CENSUS OF ``trials``, and the two are not expected to match. It is the
    PROBE's observation, made at an earlier moment against a signal list that has since moved on;
    ``trials`` is fetched fresh at seal time. Both apply the same frozen rules — that is what the
    shared eligibility code buys — but they describe different populations at different moments, so a
    reader comparing ``len(trials)`` to the selected combo's count should expect them to differ.

    ``pack_format_version`` defaults to this module's :data:`PACK_FORMAT_VERSION` so a caller cannot
    silently declare a format it did not write. It is a field rather than a manifest-level key
    precisely so it lands INSIDE the hashed meta region.
    """

    season_id: str
    combo: ComboSelection
    probe_counts: dict[str, Any]
    filters: dict[str, Any]
    cost_bps: int
    horizon_ms: int
    bar: str
    bar_ms: int
    versions: dict[str, str]
    pack_format_version: int = PACK_FORMAT_VERSION


@dataclass(frozen=True)
class PackRef:
    """Where a pack is, and what it hashed to when it was sealed.

    The hash travels WITH the reference rather than being looked up from the pack, so a caller that
    kept a ref can detect a pack whose data and manifest were rewritten together — a rewrite that is
    self-consistent on disk and undetectable from the directory alone.
    """

    dir: Path
    content_hash: str


@dataclass(frozen=True)
class SealedPack:
    """A verified pack, in memory."""

    ref: PackRef
    meta: PackMeta
    trials: tuple[CanonicalSignal, ...]
    settlement: Mapping[str, CandleSeries]


def _canonical_bytes(payload: Any) -> bytes:
    """Deterministic JSON encoding — the same construction ``replay_pack`` hashes over.

    ``sort_keys`` makes the encoding independent of insertion order, the tight separators remove
    every whitespace degree of freedom, and ``ensure_ascii`` pins the escaping of non-ASCII text so a
    symbol like ``TOKé`` hashes identically wherever it was encoded.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _meta_to_json(meta: PackMeta) -> dict[str, Any]:
    """The manifest's ``meta`` block. ``asdict`` flattens the nested ``ComboSelection``."""
    return dataclasses.asdict(meta)


def _meta_from_json(raw: Any) -> PackMeta:
    """Rebuild a :class:`PackMeta`, refusing anything that is not exactly one.

    Raises:
        PackIntegrityError: ``raw`` is not a meta block. A meta that cannot be rebuilt cannot be
            hashed, so there is no way to tell a corrupt pack from a tampered one — both fail here.
    """
    if not isinstance(raw, dict):
        raise PackIntegrityError(f"manifest meta must be an object, got {type(raw).__name__}")
    combo = raw.get("combo")
    if not isinstance(combo, dict):
        raise PackIntegrityError(f"manifest meta.combo must be an object, got {type(combo).__name__}")
    try:
        return PackMeta(
            season_id=raw["season_id"],
            combo=ComboSelection(
                chain_index=combo["chain_index"],
                bar=combo["bar"],
                season_status=combo["season_status"],
            ),
            probe_counts=raw["probe_counts"],
            filters=raw["filters"],
            cost_bps=raw["cost_bps"],
            horizon_ms=raw["horizon_ms"],
            bar=raw["bar"],
            bar_ms=raw["bar_ms"],
            versions=raw["versions"],
            # Read STRICTLY, with no default: a pack that declares no format version cannot be
            # checked against one, and silently treating it as the current format is the exact
            # "read under rules it was not written by" outcome the version exists to prevent.
            pack_format_version=raw["pack_format_version"],
        )
    except (KeyError, TypeError) as error:
        raise PackIntegrityError(f"manifest meta is missing or malformed: {error}") from error


def _series_to_json(series: CandleSeries) -> dict[str, Any]:
    """Encode one settlement series BY FIELD NAME.

    Name-keyed rather than positional on purpose. ``Candle``'s field ORDER is load-bearing elsewhere
    and must not change (C17-R2), and a positional encoding here would silently re-map every price if
    it ever did. Encoding and decoding both go by name, so a reorder is invisible to this module and
    a rename fails loudly on both sides at once.
    """
    return {
        "bar": series.bar,
        "bar_ms": series.bar_ms,
        "candles": [dataclasses.asdict(candle) for candle in series.candles],
    }


def _series_from_json(raw: Any) -> CandleSeries:
    """Rebuild one settlement series, refusing a malformed one."""
    if not isinstance(raw, dict):
        raise PackIntegrityError(f"settlement entry must be an object, got {type(raw).__name__}")
    try:
        return CandleSeries(
            bar=raw["bar"],
            bar_ms=raw["bar_ms"],
            candles=tuple(Candle(**candle) for candle in raw["candles"]),
        )
    except (KeyError, TypeError) as error:
        raise PackIntegrityError(f"settlement entry is missing or malformed: {error}") from error


def _content_hash(file_bytes: Mapping[str, bytes], meta: PackMeta) -> str:
    """sha256 over length-prefixed ``(name, bytes)`` pairs, then the canonical meta block.

    Length-prefixing rather than a separator byte makes the ``(name, bytes)`` decomposition provably
    injective: no pair of different file sets can mix to the same digest by straddling a delimiter.
    The meta region follows :data:`_META_DOMAIN_SEP` so it cannot be confused with file content.
    """
    digest = hashlib.sha256()
    for name in sorted(file_bytes):
        name_bytes = name.encode("utf-8")
        data = file_bytes[name]
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    meta_bytes = _canonical_bytes(_meta_to_json(meta))
    digest.update(_META_DOMAIN_SEP)
    digest.update(len(meta_bytes).to_bytes(8, "big"))
    digest.update(meta_bytes)
    return digest.hexdigest()


def _require_one_bar(settlement: Mapping[str, CandleSeries], meta: PackMeta) -> None:
    """Refuse a pack whose bar provenance is not single and self-consistent.

    Three ways it can fail, and all three are the same lie downstream — a leaderboard presented as
    one season that was settled at more than one width:

    * ``meta.combo`` names a different bar than ``meta`` does (or names none at all, which is what a
      ``no_season`` or not-run selection carries);
    * a series' ``bar_ms`` differs from the meta's;
    * a series' ``bar`` NAME differs, even where ``bar_ms`` agrees. Checked separately because the
      name is what every operator-facing surface displays, and a series labelled ``1H`` while
      measured at 60s would render a truthful number under a false heading.

    Raises:
        MixedBarError: On the first disagreement, naming the token so it can be looked up.
    """
    if meta.combo.bar != meta.bar:
        raise MixedBarError(
            f"pack meta names bar {meta.bar!r} while its combo names {meta.combo.bar!r}; a pack whose "
            f"two bar declarations disagree cannot state which width it was settled at"
        )
    for token in sorted(settlement):
        series = settlement[token]
        if series.bar_ms != meta.bar_ms or series.bar != meta.bar:
            raise MixedBarError(
                f"settlement series for {token!r} is bar={series.bar!r} bar_ms={series.bar_ms} but the "
                f"pack is sealed at bar={meta.bar!r} bar_ms={meta.bar_ms}; one bar per season (§5.1)"
            )


def _require_usable_season_id(season_id: str) -> None:
    """Refuse a ``season_id`` that cannot safely become a directory name.

    The id reaches here from an on-disk artifact, so it is untrusted input to a path join. A value
    containing a separator or a parent reference would write the pack outside ``out_dir`` entirely.
    """
    if not _SEASON_ID_PATTERN.match(season_id):
        raise ValueError(
            f"season_id {season_id!r} is not a usable directory name; expected 1-128 characters from "
            f"[A-Za-z0-9._-] beginning with an alphanumeric"
        )


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` via a same-directory temp file and a rename.

    ``os.replace`` is only atomic within a filesystem, hence the same-directory temp. A reader can
    arrive at any moment, and a half-written leg would fail verification in a way indistinguishable
    from tampering.
    """
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def seal_pack(
    trials: Sequence[CanonicalSignal],
    settlement: Mapping[str, CandleSeries],
    meta: PackMeta,
    out_dir: Path,
) -> PackRef:
    """Write a sealed pack under ``out_dir/<season_id>/`` and return its reference.

    Everything is validated and the whole digest is computed BEFORE the first byte is written, so a
    refused seal leaves ``out_dir`` exactly as it found it. The alternative — writing as we go and
    failing partway — produces a directory a later reader has no way to recognize as abandoned.

    The digest is taken over the in-memory bytes that are then written, so ``content_hash`` describes
    what this call produced rather than what a subsequent read happens to return.

    Args:
        trials: The season's canonical trials, in the order they will be scored.
        settlement: One candle series per token address, all at the meta's bar.
        meta: The season's metadata, including the preflight combo it was authorized by.
        out_dir: The directory packs live under. Created on demand.

    Returns:
        A :class:`PackRef` naming the pack directory and its content hash.

    Raises:
        MixedBarError: The bar provenance is not single and self-consistent.
        ValueError: ``season_id`` cannot safely be a directory name.
        FileExistsError: A pack directory for this season already exists and is not empty. Sealing
            into it would leave files from two generations side by side, and the fixed hash scope
            means the stale ones would not even be visible in the digest.
    """
    _require_usable_season_id(meta.season_id)
    _require_one_bar(settlement, meta)

    file_bytes = {
        TRIALS_FILENAME: _canonical_bytes([trial.model_dump() for trial in trials]),
        SETTLEMENT_FILENAME: _canonical_bytes({token: _series_to_json(series) for token, series in settlement.items()}),
    }
    content_hash = _content_hash(file_bytes, meta)
    # `pack_format_version` is NOT repeated here: it lives in `meta`, inside the digest, and a
    # manifest-level copy would be a second authoritative-looking surface that could disagree with
    # it. `files` records the hash SCOPE for a human reading the manifest, and `load_pack` compares
    # it against `DATA_FILENAMES` so it cannot quietly become decorative.
    manifest_bytes = _canonical_bytes(
        {
            "files": sorted(DATA_FILENAMES),
            "meta": _meta_to_json(meta),
            "content_hash": content_hash,
        }
    )

    pack_dir = Path(out_dir) / meta.season_id
    if pack_dir.exists() and any(pack_dir.iterdir()):
        raise FileExistsError(
            f"pack directory {pack_dir} already exists and is not empty; refusing to seal a second "
            f"generation over the first"
        )
    pack_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in file_bytes.items():
        _write_atomic(pack_dir / name, payload)
    # The manifest lands LAST: it is what names the pack complete, so a crash before this point
    # leaves a directory with no manifest — unreadable as a pack, which is the honest outcome —
    # rather than a manifest advertising legs that are not all there yet.
    _write_atomic(pack_dir / MANIFEST_FILENAME, manifest_bytes)
    return PackRef(dir=pack_dir, content_hash=content_hash)


def read_pack_ref(pack_dir: Path) -> PackRef:
    """Reconstitute a reference from a pack's own manifest.

    For a caller that has a directory and no ref — the H3.5 scorer, reading a pack the fetch script
    sealed in an earlier process. **Stated exactly:** a ref built this way cannot witness a manifest
    that was rewritten wholesale, because it is derived from that manifest. It still carries the
    ref-versus-manifest check through :func:`load_pack` unchanged for every caller that kept the ref
    :func:`seal_pack` returned, and tampering with any DATA leg is caught either way.

    Raises:
        PackIntegrityError: The manifest is missing, unreadable, or carries no content hash.
    """
    pack_dir = Path(pack_dir)
    manifest = _load_manifest(pack_dir)
    content_hash = manifest.get("content_hash")
    if not isinstance(content_hash, str):
        raise PackIntegrityError(f"pack manifest at {pack_dir} carries no content_hash")
    return PackRef(dir=pack_dir, content_hash=content_hash)


def _load_manifest(pack_dir: Path) -> dict[str, Any]:
    """Read and parse ``manifest.json``, failing closed on every unreadable shape."""
    try:
        raw = (pack_dir / MANIFEST_FILENAME).read_bytes()
    except OSError as error:
        raise PackIntegrityError(f"pack manifest at {pack_dir} is unreadable: {error}") from error
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PackIntegrityError(f"pack manifest at {pack_dir} is not readable JSON") from error
    if not isinstance(manifest, dict):
        raise PackIntegrityError(f"pack manifest at {pack_dir} must hold a JSON object")
    return manifest


def load_pack(ref: PackRef) -> SealedPack:
    """Read a sealed pack, verifying it against its digest before returning anything.

    Each file is read EXACTLY ONCE into memory; the hash and the parse both consume those same
    bytes. A second read to parse after hashing would leave a window where the bytes verified are
    not the bytes returned.

    Both witnesses are checked. The recomputed digest must equal the manifest's — which catches a
    tampered data file — and it must equal ``ref.content_hash``, which catches a data file and a
    manifest that were rewritten together to agree with each other.

    Args:
        ref: The pack directory and the hash it is expected to carry.

    Returns:
        The verified pack.

    The FORMAT VERSION is checked before anything else is trusted. That check is not redundant with
    the digest: a hand-edited version breaks the hash and is caught either way, but a pack sealed
    legitimately by a FUTURE build carries a version this module does not implement together with a
    digest that is perfectly valid under that build's rules. Only comparing the version can refuse
    it, and accepting it would be reading a pack under rules it was not written by.

    Raises:
        PackIntegrityError: Any leg is missing or unreadable, the pack does not parse, the manifest
            declares a hash scope this module does not implement, the format version is not this
            module's, or either digest comparison fails. Nothing partially-loaded is ever returned.
    """
    manifest = _load_manifest(ref.dir)
    meta = _meta_from_json(manifest.get("meta"))
    if meta.pack_format_version != PACK_FORMAT_VERSION:
        raise PackIntegrityError(
            f"pack at {ref.dir} declares pack format version {meta.pack_format_version!r} but this "
            f"module implements {PACK_FORMAT_VERSION!r}; refusing to read a pack under rules it was "
            f"not written by"
        )
    declared_files = manifest.get("files")
    if declared_files != sorted(DATA_FILENAMES):
        raise PackIntegrityError(
            f"pack at {ref.dir} declares hash scope {declared_files!r} but this module hashes "
            f"{sorted(DATA_FILENAMES)!r}; the manifest describes a pack this reader cannot verify"
        )
    try:
        file_bytes = {name: (ref.dir / name).read_bytes() for name in DATA_FILENAMES}
    except OSError as error:
        raise PackIntegrityError(f"pack at {ref.dir} is missing a leg: {error}") from error

    recomputed = _content_hash(file_bytes, meta)
    if recomputed != manifest.get("content_hash"):
        raise PackIntegrityError(
            f"pack at {ref.dir} hashes to {recomputed} but its manifest records "
            f"{manifest.get('content_hash')!r}; the pack has been modified since it was sealed"
        )
    if recomputed != ref.content_hash:
        raise PackIntegrityError(
            f"pack at {ref.dir} hashes to {recomputed} but the reference expects {ref.content_hash!r}; "
            f"the pack and its manifest disagree with the record kept by whoever sealed it"
        )

    try:
        raw_trials = json.loads(file_bytes[TRIALS_FILENAME])
        raw_settlement = json.loads(file_bytes[SETTLEMENT_FILENAME])
    except json.JSONDecodeError as error:
        raise PackIntegrityError(f"pack at {ref.dir} carries a leg that is not readable JSON") from error
    if not isinstance(raw_trials, list) or not isinstance(raw_settlement, dict):
        raise PackIntegrityError(f"pack at {ref.dir} carries legs of the wrong shape")

    try:
        trials = tuple(CanonicalSignal(**row) for row in raw_trials)
    except (TypeError, ValueError) as error:
        raise PackIntegrityError(
            f"pack at {ref.dir} carries a trial that is not a canonical signal: {error}"
        ) from error
    settlement = {token: _series_from_json(series) for token, series in raw_settlement.items()}
    return SealedPack(ref=ref, meta=meta, trials=trials, settlement=settlement)
