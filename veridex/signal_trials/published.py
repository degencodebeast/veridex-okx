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
    """Read the published season document, or ``None`` when none is published.

    Args:
        data_dir: The signal-trials data directory, or ``None`` when none is
            configured.

    Returns:
        The season document, or ``None`` if nothing has been published.

    Raises:
        ValueError: The artifact exists but is unreadable.
    """
    if data_dir is None:
        return None
    path = _published_dir(data_dir) / SEASON_FILENAME
    if not path.is_file():
        return None
    return _read_json_object(path)
