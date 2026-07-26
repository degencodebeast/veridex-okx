"""Published-season repository — the artifact-to-API contract for signal trials.

The season is *built* by an offline scorer and *served* by the API, and those two
run in different processes at different times. This module is the only thing
between them: the scorer calls the writers, the router calls the readers, and the
on-disk layout is ``SIGNAL_TRIALS_DATA_DIR/published/{state.json, season.json}``.

Two honesty rules shape the whole module:

* **Absence reads as ``not_built``, and only absence does.** A missing file means
  no season has been built yet, which is a true statement the API may serve. An
  *unreadable* file means something is wrong, and reporting that as ``not_built``
  would be a lie — so corruption raises instead.
* **The state is a closed set of four values**, checked on the way in *and* on the
  way out. The writers stop a bad value at its source, and the readers stop one
  that reached the disk some other way (a hand-edited artifact, a partial restore).

``state`` and ``season`` are deliberately separate files. The state records the
scorer's verdict — including an intentional ``no_season``, which must stay
distinguishable from ``not_built`` — while the season document is the payload
``GET /signal-trials/season`` serves. A ``no_season`` verdict has a state and no
document, so a single combined file could not represent it honestly.

Separate files mean the two can disagree, and the third rule is what keeps that from
becoming a lie:

* **The state is authoritative on every read.** Writes stay atomic per file, which is
  what a crash between two file operations requires — but that same property means a
  ``no_season`` verdict recorded over a directory that once held a season leaves the
  old payload on disk. So ``read_season`` consults the state first: under ``not_built``
  or ``no_season`` it serves nothing, and under ``qualified``/``exploratory`` it refuses
  a payload whose own ``season_status`` disagrees. Deleting a superseded payload is an
  optimisation; refusing to serve it is the correctness, and it holds even if the
  process died before any deletion could run.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

PublishedState = Literal["not_built", "qualified", "exploratory", "no_season"]

#: The closed set of published states, as runtime values. ``PublishedState`` is
#: erased at runtime, so membership tests need this alongside it.
PUBLISHED_STATES: frozenset[str] = frozenset({"not_built", "qualified", "exploratory", "no_season"})

#: The state of a data dir that has never been published to.
NOT_BUILT: PublishedState = "not_built"

#: States under which NO season document may be served, whatever is on disk.
#: ``not_built`` means the scorer never ran; ``no_season`` means it ran and declined.
#: Both are answers, and neither of them is a season.
UNPUBLISHED_STATES: frozenset[str] = frozenset({"not_built", "no_season"})

PUBLISHED_DIRNAME = "published"
STATE_FILENAME = "state.json"
SEASON_FILENAME = "season.json"


def _published_dir(data_dir: Path | str) -> Path:
    """Return the ``published/`` subdirectory of ``data_dir``."""
    return Path(data_dir) / PUBLISHED_DIRNAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` as JSON, atomically.

    A reader may run at any moment — the API serves the same directory the scorer
    writes to — so the file must never be observable half-written. The payload is
    serialized to a temporary file in the *same* directory (``os.replace`` is only
    atomic within a filesystem), flushed to disk, and then renamed over the target.

    Serializing before opening the target also means a payload that cannot be
    encoded leaves the previous published artifact untouched rather than truncated.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Leave no half-written temp behind for the next writer or a directory listing.
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_json_object(path: Path) -> dict[str, Any]:
    """Load ``path`` as a JSON object, failing loudly if it is not one.

    Raises:
        ValueError: ``path`` is not readable as JSON, or does not hold an object.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"published artifact {path.name} is not readable JSON") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"published artifact {path.name} must hold a JSON object")
    return loaded


def _require_known_state(state: object, source: str) -> PublishedState:
    """Return ``state`` when it is one of the four published states, else raise.

    Raises:
        ValueError: ``state`` is outside the frozen set.
    """
    if not isinstance(state, str) or state not in PUBLISHED_STATES:
        raise ValueError(f"{source} carries an unknown published state; expected one of {sorted(PUBLISHED_STATES)}")
    # Narrowed by the membership test above; ``Literal`` is not inferable from it.
    return state  # type: ignore[return-value]


def write_state(data_dir: Path | str, state: PublishedState, detail: dict[str, Any]) -> None:
    """Publish the season-build ``state`` and its supporting ``detail``.

    Args:
        data_dir: The signal-trials data directory (created on demand).
        state: One of ``not_built``, ``qualified``, ``exploratory``, ``no_season``.
        detail: Free-form supporting evidence for the verdict (counts, filters,
            the reason a season was declined). Served nowhere verbatim; kept so an
            operator can explain a state after the fact.

    Raises:
        ValueError: ``state`` is outside the frozen set. Checked before anything is
            created, so a rejected write leaves no directory and no artifact.
    """
    _require_known_state(state, "write_state")
    _atomic_write_json(_published_dir(data_dir) / STATE_FILENAME, {"state": state, "detail": detail})


def write_season(data_dir: Path | str, season_json: dict[str, Any]) -> None:
    """Publish the season document served by ``GET /signal-trials/season``.

    Args:
        data_dir: The signal-trials data directory (created on demand).
        season_json: The season document, already in its served shape.
    """
    _atomic_write_json(_published_dir(data_dir) / SEASON_FILENAME, season_json)


def read_state(data_dir: Path | str | None) -> dict[str, Any]:
    """Read the published state, reporting an unpublished directory as ``not_built``.

    Args:
        data_dir: The signal-trials data directory, or ``None`` when none is
            configured — both mean nothing has been published.

    Returns:
        ``{"state": <one of the four>, "detail": {...}}``.

    Raises:
        ValueError: The artifact exists but is unreadable, or carries a state
            outside the frozen set. Absence is honest; corruption is not.
    """
    if data_dir is None:
        return {"state": NOT_BUILT, "detail": {}}
    path = _published_dir(data_dir) / STATE_FILENAME
    if not path.is_file():
        return {"state": NOT_BUILT, "detail": {}}
    record = _read_json_object(path)
    state = _require_known_state(record.get("state"), f"published artifact {STATE_FILENAME}")
    detail = record.get("detail", {})
    if not isinstance(detail, dict):
        raise ValueError(f"published artifact {STATE_FILENAME} must carry an object detail")
    return {"state": state, "detail": detail}


def read_season(data_dir: Path | str | None) -> dict[str, Any] | None:
    """Read the published season, with the published STATE as the authority.

    The two artifacts are written separately and can disagree — deliberately, because
    atomic writes are per-file and a crash can land between them. The scorer's
    ``no_season`` branch is specified to record its verdict without deleting anything,
    so a declined preflight over a directory that once held a season leaves a stale
    ``season.json`` sitting next to a ``no_season`` state. Reading the two independently
    let the API answer ``no_season`` on ``/health`` and ``200 qualified`` on ``/season``
    in the same breath.

    So the state decides, on every read:

    * ``not_built`` and ``no_season`` mean nothing is published. Any payload on disk is
      a leftover from an earlier generation and is not served, whether or not the
      scorer got around to deleting it. Deleting it is an optimisation; this is the
      correctness.
    * ``qualified`` and ``exploratory`` ASSERT that a season exists, so the payload has
      to be there and has to agree. A missing payload, a payload whose ``season_status``
      contradicts the state, and a corrupt payload are all the same kind of thing: an
      INCOMPLETE OR INCONSISTENT published set, not an absence. Each is refused. The
      module's rule is that absence reads as ``not_built`` and ONLY absence does, so
      answering "nothing is published" to a state that says otherwise would be exactly
      the lie this function exists to prevent — the mirror of serving a stale season
      under ``no_season``, and reachable through the same crash window.

    Refusing is deliberately noisier than returning ``None``. The alternative leaves
    ``/health`` asserting a season while ``/season`` reports none, which is the
    contradiction, merely pointing the other way.

    **Write order matters, and it is the writer's half of this contract.** Publish the
    payload BEFORE the state that advertises it. A crash in that order leaves a payload
    with a stale state, which the rules above already absorb silently. The reverse order
    leaves a state asserting a season that does not exist, which is the case this
    function has to refuse.

    Args:
        data_dir: The signal-trials data directory, or ``None`` when none is
            configured.

    Returns:
        The season document, or ``None`` when no data directory is configured or the
        state says nothing is published.

    Raises:
        ValueError: The state artifact is unreadable or carries an unknown state; or the
            state asserts a season whose payload is missing, unreadable, or carries a
            ``season_status`` that disagrees with the authoritative state.
    """
    if data_dir is None:
        return None
    state = read_state(data_dir)["state"]
    if state in UNPUBLISHED_STATES:
        return None
    path = _published_dir(data_dir) / SEASON_FILENAME
    if not path.is_file():
        raise ValueError(
            f"published state is {state!r}, which asserts a season, but {SEASON_FILENAME} is "
            "missing; refusing to report an asserted season as merely absent"
        )
    season = _read_json_object(path)
    payload_status = season.get("season_status")
    if payload_status != state:
        raise ValueError(
            f"published artifact {SEASON_FILENAME} carries season_status {payload_status!r} "
            f"while the authoritative state is {state!r}; refusing to serve a "
            "cross-generation season payload"
        )
    return season
