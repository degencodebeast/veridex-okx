"""Two-phase journaled commit store — the durable half of the settlement-atomic paid commit.

Four directories, and the split between them is the whole design:

``slots/``
    One file per ``(payer, trial_id)`` DECISION SLOT. This is the state machine, and it is
    durable and state-bearing rather than a transient lock — a process that dies holding one
    leaves the state behind on purpose, because the state is the only record of how far the
    payment got. States: ``in_flight``, ``settle_attempted``, ``quarantined``,
    ``finalized(receipt_id)``.
``staged/``
    One file per staging id, holding the payer's verbatim request. Never readable by any free
    read. A staged row is a commitment that has been *received*, not one that has been *paid*.
``journal/``
    One file per staging id, written only after a settlement RETURNED SUCCESS, carrying the
    transaction hash. The journal is the durable proof that money moved; finalization is
    bookkeeping over it, and that ordering is what makes a crash recoverable.
``finalized/``
    One file per receipt id. The only rows any free read may serve.

Three rules govern the slot's lifetime, and every one of them exists to make a specific double
charge impossible:

* **A slot is RELEASED only where no payment can exist** — a validation 4xx, or a settlement
  that returned a definitive failure. Those are the two cases with a *proof* that no money
  moved.
* **``finalized`` is RETAINED PERMANENTLY**, as the idempotency pointer. Deleting it on success
  would let a later retry bearing a fresh signature see "no slot", pass every check, and settle
  a second time for the same commitment. The pointer *is* the anti-double-charge mechanism; the
  receipt is just what it points at.
* **``quarantined`` is RETAINED** until an operator resolves it by hand. It means a settlement
  was attempted and its outcome is UNKNOWN — the facilitator may hold a real payment, and the
  only way to ask is by a transaction hash this state does not have. It is never auto-deleted,
  never served, and it blocks every future settle for that slot.

The asymmetry between the last two bullets and the first is the point of the module. An
exception from ``settle`` is **not evidence of failure**; it is absence of evidence. Deleting on
absence of evidence is how a paid commit becomes an unpaid one.

**Write order, and it is this module's obligation as a writer:** the payload is written before
the state that advertises it, everywhere the two are separate. Staging precedes the attempt
marker; the journal precedes finalization; the finalized payload precedes the slot transition
that publishes it. A crash in that order leaves a payload nothing points at, which the
reconciler completes or sweeps. The reverse order leaves a pointer to a payload that does not
exist, which nothing can repair.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel

from veridex.chain.anchor import run_manifest_hash

#: The slot states, as runtime values. ``SlotState`` is erased at runtime, so membership tests
#: need this alongside it.
SlotState = Literal["in_flight", "settle_attempted", "quarantined", "finalized"]
SLOT_STATES: Final[frozenset[str]] = frozenset({"in_flight", "settle_attempted", "quarantined", "finalized"})

#: States in which NO settle call may ever be made for the slot, because an earlier payment for
#: it MAY already have settled. This set is what makes a post-quarantine retry unable to
#: double-charge, and it is deliberately a named constant rather than two inline comparisons:
#: adding a fifth indeterminate state must extend the refusal by construction.
INDETERMINATE_STATES: Final[frozenset[str]] = frozenset({"settle_attempted", "quarantined"})

#: The trial mode a PAID commit is permitted to bind to (frozen spec section 11: paid external
#: commits are live-only). Spelled here rather than imported from ``live`` because ``live``
#: imports THIS module, so the dependency only runs one way. ``test_the_live_mode_constant_matches
#: _the_trial_module`` is what keeps the two copies equal.
LIVE_TRIAL_MODE: Final[str] = "live"

#: How each field of the payer's canonical commit body is recovered from a finalized record,
#: as ``body field -> stored record key``.
#:
#: All but one are the same name. ``trial_id`` is not: the body carries the PAYER'S spelling of
#: the id and the record is keyed on the RESOLVED one, and resolution is explicitly permitted to
#: canonicalize between them (see the slot-acquisition comment in ``payments.py``). The body hash
#: was taken over what the payer sent, so re-deriving it from the resolved id would report a
#: tamper on every honest receipt the first time resolution changed a character.
#:
#: **This mapping is coupled to ``CommitRequest``'s shape**, because the staged hash is over the
#: whole dumped request. A field added there and not here makes every receipt fail ``body_hash``
#: — fail-closed, and loud, but still wrong;
#: ``test_the_body_derivation_covers_every_field_a_commit_request_can_carry`` pins the two
#: together so the obligation cannot be missed while editing either one.
COMMIT_BODY_FIELDS: Final[dict[str, str]] = {
    "trial_id": "committed_trial_id",
    "p_follow_profitable": "p_follow_profitable",
    "methodology_version": "methodology_version",
}

#: The commit-time facts the receipt's manifest hash BINDS, sealed at finalization.
#:
#: The payer's probability and methodology are deliberately absent: the manifest carries
#: ``body_hash``, so it binds the commitment BY REFERENCE. That keeps the two checks separable — a
#: rewritten probability is ``body_hash``'s finding and a rewritten payment is the manifest's,
#: rather than one compound verdict reported under two names.
COMMIT_MANIFEST_FIELDS: Final[tuple[str, ...]] = (
    "receipt_id",
    "trial_id",
    "committed_trial_id",
    "payer",
    "body_hash",
    "payment_tx_hash",
    "committed_at_ms",
    "commit_deadline_ms",
    "trial_mode",
)

#: The checks :func:`verify_receipt` reports, in report order. COMMIT-TIME only: the four outcome
#: checks (``bar_version``, ``law_version``, ``evidence_equality``, ``outcome_source``) arrive at
#: H4.3 with the settlement path that can answer them. A placeholder for them here would
#: advertise a settlement verdict nothing has computed.
VERIFY_COMMIT_CHECKS: Final[tuple[str, ...]] = ("body_hash", "manifest", "deadline_respected", "live_mode")

#: A single check's verdict. Two values, and neither is "unknown": every commit-time check reads
#: facts the receipt itself carries, so there is no state in which one of them cannot be decided.
CheckState = Literal["pass", "fail"]

#: How long after staging a row with NO attempt marker becomes sweepable. Derived, not picked:
#: the commit window is 300_000 ms (frozen spec section 11), and 600_000 ms of grace covers
#: clock skew and a slow request, matching the plan's ``FETCH_GRACE_MS``. A row younger than
#: this may still belong to a request that is mid-flight, and sweeping it would delete a
#: commitment out from under a live caller.
STALE_AFTER_MS: Final[int] = 300_000 + 600_000

_SLOTS_DIRNAME = "slots"
_STAGED_DIRNAME = "staged"
_JOURNAL_DIRNAME = "journal"
_FINALIZED_DIRNAME = "finalized"


@dataclass(frozen=True)
class CommitRecord:
    """One FINALIZED paid commitment. The only shape a free read may serve.

    Frozen because it is the thing a receipt attests to: a record that could be mutated after
    its ``body_hash`` was computed would let the receipt and its subject drift apart silently.

    ``body_hash`` is over the payer's canonical request, so H4.2 can re-derive it and report
    tampering without trusting this row's own fields. ``commit_deadline_ms`` and ``trial_mode``
    are carried rather than re-looked-up for the same reason: the trial may be long closed by
    the time anyone verifies, and a check that had to consult a live trial store could not
    verify a historical receipt at all.
    """

    receipt_id: str
    trial_id: str
    payer: str
    p_follow_profitable: float
    methodology_version: str | None
    body_hash: str
    payment_tx_hash: str
    committed_at_ms: int
    commit_deadline_ms: int | None
    trial_mode: str | None


@dataclass(frozen=True)
class VerifyReport:
    """The verdict on one finalized receipt: every check, and what each one found.

    ``checks`` is a mapping rather than four named fields because the set of checks GROWS — H4.3
    adds the outcome checks to the same report — and because a caller's job is to display or
    audit them uniformly, not to branch per check. :data:`VERIFY_COMMIT_CHECKS` is the key set at
    this task.

    Every value is ``"pass"`` or ``"fail"``. There is no "error" state and no exception path for a
    receipt that fails: **a tampered receipt is a successfully computed report that says so.**
    Reporting a verification failure as an API failure would make tampering indistinguishable
    from an outage, which is the one confusion a trust surface cannot afford.
    """

    receipt_id: str
    checks: dict[str, CheckState]


def canonical_body_hash(body: BaseModel | dict[str, Any]) -> str:
    """Hash a commit body so that "identical canonical body" is a decidable question.

    Frozen spec section 11 makes idempotency turn on canonical-body identity: the same payer and
    trial with an IDENTICAL body returns the original receipt, and a DIFFERENT body is a 409
    that writes nothing. That comparison has to be insensitive to things the payer does not
    control the spelling of — key order, insignificant whitespace, ``0.60`` versus ``0.6`` —
    and sensitive to every value that is part of the commitment.

    Sorted-key JSON with no whitespace over the JSON-mode dump gives exactly that: field order
    and formatting vanish, while every value survives. ``mode="json"`` rather than the default
    so the digest is over the same representation that crossed the wire.

    Args:
        body: The commit request, as a pydantic model or an already-dumped mapping.

    Returns:
        The hex sha256 digest of the canonical serialization.
    """
    payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def committed_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the canonical commit body a finalized row attests to.

    Rebuilt from the fields the receipt SERVES — via :data:`COMMIT_BODY_FIELDS` — rather than from
    a second stored copy of the request, and that choice is the whole strength of the check. If the
    row carried its own verbatim body and the hash were taken over that, rewriting the
    ``p_follow_profitable`` a reader is actually shown would leave the hash intact and the
    verifier would report ``pass`` on a receipt that serves a probability nobody committed to.
    Deriving from the served fields means every value a consumer can see is a hashed input.

    Args:
        payload: The stored finalized row, verbatim.

    Returns:
        The body as it would have to have been for this row's ``body_hash`` to be correct.
    """
    return {field: payload.get(source) for field, source in COMMIT_BODY_FIELDS.items()}


def commit_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the commit-time manifest for a finalized row.

    Total by construction — every field is read with ``.get`` and nothing is coerced — because
    this runs on the money path inside :meth:`ReceiptStore.finalize_from_journal`, AFTER a
    settlement has succeeded. A manifest builder that could raise on an odd field would turn a
    paid commit into an unfinalized one.

    The same function serves the writer and the verifier, over the same stored shape, so the two
    cannot normalize differently. Two separate field lists would drift, and the drift would
    surface as ``manifest: fail`` on receipts nobody touched.

    Args:
        payload: The finalized row. ``manifest_hash`` itself is never an input — it is the OUTPUT
            of hashing this — so it is absent from :data:`COMMIT_MANIFEST_FIELDS`.

    Returns:
        The manifest, ready for :func:`~veridex.chain.anchor.run_manifest_hash`.
    """
    return {field: payload.get(field) for field in COMMIT_MANIFEST_FIELDS}


def _epoch_ms(value: Any) -> int | None:
    """Return ``value`` when it is usable as an epoch-millisecond stamp, else ``None``.

    ``type(value) is int`` rather than ``isinstance``, which would admit ``bool``: ``True`` would
    otherwise compare as the millisecond ``1`` and a receipt stamped ``true`` would be reported as
    a commitment made in 1970. A string, a float or an absent field is likewise not a timestamp,
    and the caller turns ``None`` into ``fail`` — a receipt whose timing nothing can establish has
    not been shown to be timely.
    """
    return value if type(value) is int else None


def _slot_key(payer: str, trial_id: str) -> str:
    """Return the filesystem-safe digest naming the ``(payer, trial_id)`` slot.

    Hashed rather than concatenated because both halves are caller-supplied strings: a payer or
    trial id containing a path separator would otherwise escape the slots directory, and one
    containing the separator used to join them could collide with a different pair. The digest
    is over a length-prefixed join so ``("ab", "c")`` and ``("a", "bc")`` cannot map together.
    """
    return hashlib.sha256(f"{len(payer)}:{payer}|{len(trial_id)}:{trial_id}".encode()).hexdigest()


def receipt_id_for(staging_id: str) -> str:
    """Derive the receipt id a staging id finalizes to.

    Deterministic on purpose. Finalization happens on the request path OR later inside
    :meth:`ReceiptStore.reconcile`, and both must produce the SAME receipt id — a random id
    would make a crash-recovered receipt a different receipt, so the payer's original 200 would
    reference an id that never materialized.
    """
    return "rcpt_" + hashlib.sha256(f"receipt:{staging_id}".encode()).hexdigest()[:32]


class ReceiptStore:
    """Durable two-phase commit store with a journal and a reconciler.

    One store owns one directory tree. Every write is atomic per file — serialize, write to a
    temporary file in the same directory, ``fsync``, then ``os.replace`` — so a reader (the API
    serves the same tree a settler writes to) can never observe a half-written row.
    """

    def __init__(self, root: Path | str) -> None:
        """Create a store rooted at ``root``, creating the four subdirectories on demand.

        Args:
            root: The directory this store owns.
        """
        self.root = Path(root)
        self._slots = self.root / _SLOTS_DIRNAME
        self._staged = self.root / _STAGED_DIRNAME
        self._journal = self.root / _JOURNAL_DIRNAME
        self._finalized = self.root / _FINALIZED_DIRNAME
        for directory in (self._slots, self._staged, self._journal, self._finalized):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ atomic primitives

    @staticmethod
    def _write_atomic(path: Path, payload: dict[str, Any], *, fsync_dir: bool = False) -> None:
        """Write ``payload`` to ``path`` atomically, optionally fsyncing the directory too.

        ``fsync_dir`` is not decoration. ``os.replace`` makes the CONTENT visible atomically,
        but the directory entry itself can still be lost to a power failure until the directory
        is synced. The one transition where that distinction matters is the attempt marker: if
        it were lost, a crash past the settle call would be misread as "never settled" and a
        retry could charge again. Every other write is recoverable from the reconciler, so they
        do not pay the cost.
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
        if fsync_dir:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        """Load ``path`` as a JSON object, failing loudly if it is not one.

        Raises:
            ValueError: ``path`` is unreadable as JSON or does not hold an object. Corruption
                is never reported as absence — a slot that reads as "missing" would be a slot
                that permits a fresh settle.
        """
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"commit-store artifact {path.name} is not readable JSON") from error
        if not isinstance(loaded, dict):
            raise ValueError(f"commit-store artifact {path.name} must hold a JSON object")
        return loaded

    @staticmethod
    def _require_known_state(state: object, source: str) -> str:
        """Return ``state`` when it is a known slot state, else raise.

        Raises:
            ValueError: ``state`` is outside :data:`SLOT_STATES`. An unrecognized state must
                never be treated as releasable or as settleable; refusing to interpret it at
                all is the only fail-closed reading.
        """
        if not isinstance(state, str) or state not in SLOT_STATES:
            raise ValueError(f"{source} carries an unknown slot state; expected one of {sorted(SLOT_STATES)}")
        return state

    def _slot_path(self, payer: str, trial_id: str) -> Path:
        """Return the path of the ``(payer, trial_id)`` slot file."""
        return self._slots / f"{_slot_key(payer, trial_id)}.json"

    def _iter_slots(self) -> list[dict[str, Any]]:
        """Read every slot record, validating each one's state on the way out."""
        records = []
        for path in sorted(self._slots.glob("*.json")):
            record = self._read_json(path)
            self._require_known_state(record.get("state"), f"slot artifact {path.name}")
            records.append(record)
        return records

    # ------------------------------------------------------------------ slot lifecycle

    def acquire_slot(self, payer: str, trial_id: str, *, now_ms: int) -> tuple[bool, str]:
        """Take the decision slot for ``(payer, trial_id)``, or report who already holds it.

        The create is ``O_CREAT | O_EXCL``, which is a single atomic filesystem operation: two
        concurrent requests for the same slot cannot both succeed, and the loser learns the
        winner's state rather than a lock-acquisition failure. That distinction is what lets a
        loser answer honestly — ``commit_in_flight`` versus ``payment_indeterminate`` versus the
        original receipt — instead of retrying into a double charge.

        Args:
            payer: The VERIFIED payer address. Never a caller-supplied one.
            trial_id: The resolved trial's id.
            now_ms: Creation timestamp for a newly created slot.

        Returns:
            ``(True, "in_flight")`` when this call created the slot, else ``(False, state)``
            with the existing state.
        """
        path = self._slot_path(payer, trial_id)
        record = {
            "payer": payer,
            "trial_id": trial_id,
            "state": "in_flight",
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "receipt_id": None,
            "staging_id": None,
        }
        encoded = json.dumps(record, indent=2, sort_keys=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = self._read_json(path)
            return False, self._require_known_state(existing.get("state"), f"slot artifact {path.name}")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return True, "in_flight"

    def create_slot(
        self,
        payer: str,
        trial_id: str,
        *,
        state: str = "in_flight",
        created_at_ms: int | None = None,
        receipt_id: str | None = None,
    ) -> None:
        """Write a slot in an arbitrary state, overwriting any existing one.

        The recovery-drill and operator entry point: it is how a crash-orphaned slot is
        reproduced for a test and how an operator reconstructs one. Distinct from
        :meth:`acquire_slot`, which is the request path and must never overwrite.
        """
        self._require_known_state(state, "create_slot")
        stamp = 0 if created_at_ms is None else created_at_ms
        self._write_atomic(
            self._slot_path(payer, trial_id),
            {
                "payer": payer,
                "trial_id": trial_id,
                "state": state,
                "created_at_ms": stamp,
                "updated_at_ms": stamp,
                "receipt_id": receipt_id,
                "staging_id": None,
            },
        )

    def _transition_slot(self, payer: str, trial_id: str, state: str, **extra: Any) -> None:
        """Move an existing slot to ``state``, preserving its creation stamp."""
        self._require_known_state(state, "_transition_slot")
        path = self._slot_path(payer, trial_id)
        record: dict[str, Any] = self._read_json(path) if path.is_file() else {"payer": payer, "trial_id": trial_id}
        record.update(state=state, **extra)
        record.setdefault("created_at_ms", 0)
        record.setdefault("updated_at_ms", 0)
        record.setdefault("receipt_id", None)
        record.setdefault("staging_id", None)
        # The attempt marker is the one transition whose loss would be misread as
        # "never settled", so it alone syncs the directory entry as well as the content.
        self._write_atomic(path, record, fsync_dir=state == "settle_attempted")

    def release_slot(self, payer: str, trial_id: str) -> None:
        """Delete the slot. Legitimate ONLY where no payment can possibly exist.

        The two lawful callers are a validation 4xx (settlement was provably never reached) and
        a settlement that RETURNED a definitive failure. Releasing in any other circumstance —
        in particular on a raised settle, or after a successful one — reopens the slot to a
        fresh-signature retry and is a double charge.
        """
        self._slot_path(payer, trial_id).unlink(missing_ok=True)

    def slot_state(self, payer: str, trial_id: str) -> str | None:
        """Return the slot's state, or ``None`` when no slot exists."""
        path = self._slot_path(payer, trial_id)
        if not path.is_file():
            return None
        return self._require_known_state(self._read_json(path).get("state"), f"slot artifact {path.name}")

    def slot_receipt_id(self, payer: str, trial_id: str) -> str | None:
        """Return the receipt id a ``finalized`` slot points at, else ``None``."""
        path = self._slot_path(payer, trial_id)
        if not path.is_file():
            return None
        value = self._read_json(path).get("receipt_id")
        return value if isinstance(value, str) else None

    def slot_count(self) -> int:
        """Return the number of slots of any state."""
        return len(list(self._slots.glob("*.json")))

    # ------------------------------------------------------------------ staging

    def stage(
        self,
        *,
        staging_id: str,
        trial_id: str,
        payer: str,
        body: BaseModel | dict[str, Any],
        staged_at_ms: int | None = None,
        commit_deadline_ms: int | None = None,
        trial_mode: str | None = None,
    ) -> None:
        """Record a received commitment that has NOT been paid for. Never readable.

        Written before the attempt marker and before any settle call, so the payload always
        exists by the time anything can point at it.

        ``trial_id`` is the RESOLVED trial, which is what the record binds to; ``body`` is the
        payer's verbatim request, which is what the body hash is taken over. In production the
        two agree by construction, because the trial was resolved FROM the body — they are
        separate parameters because the record must bind to what was resolved, not to what was
        claimed.

        Args:
            staging_id: ``sha256`` of the payment payload; unique per payment.
            trial_id: The resolved trial's id.
            payer: The verified payer address.
            body: The payer's commit request.
            staged_at_ms: Staging timestamp; drives sweep eligibility. Defaults to 0, which
                makes a row immediately sweepable and is safe only because a real caller always
                supplies the clock.
            commit_deadline_ms: The trial's deadline, carried so a receipt stays verifiable
                after the trial is gone.
            trial_mode: The trial's mode, carried for the same reason.
        """
        self._write_atomic(
            self._staged / f"{staging_id}.json",
            {
                "staging_id": staging_id,
                "trial_id": trial_id,
                "payer": payer,
                "body": body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body),
                "body_hash": canonical_body_hash(body),
                "staged_at_ms": 0 if staged_at_ms is None else staged_at_ms,
                "commit_deadline_ms": commit_deadline_ms,
                "trial_mode": trial_mode,
            },
        )

    def staged_row(self, staging_id: str) -> dict[str, Any] | None:
        """Return the staged row, or ``None`` when it is absent."""
        path = self._staged / f"{staging_id}.json"
        return self._read_json(path) if path.is_file() else None

    def staged_payers(self) -> list[str]:
        """Return the payers holding staged rows, in stable order. Diagnostics only."""
        return [str(self._read_json(p)["payer"]) for p in sorted(self._staged.glob("*.json"))]

    def delete_staged(self, staging_id: str) -> None:
        """Delete a staged row. Lawful only alongside :meth:`release_slot`."""
        (self._staged / f"{staging_id}.json").unlink(missing_ok=True)

    def mark_settle_attempted(self, staging_id: str) -> None:
        """Move the row's slot to ``settle_attempted``, durably, BEFORE the settle call.

        This is the single most important write in the module, and its ORDER is the reason. Once
        it is on disk, a process that dies anywhere past it — including inside the settle call,
        including after the facilitator moved real money — comes back to a slot that says "a
        settlement was attempted and I do not know how it ended". Without it, the same crash
        comes back to a slot that says "nothing happened", and the next retry charges again.

        Derives the slot key from the staged row rather than taking it as an argument, so the
        marker cannot be attached to a different slot than the one the row belongs to.

        Raises:
            KeyError: No staged row with that id. An attempt marker with no payload behind it
                would be a state nothing can reconcile.
        """
        row = self.staged_row(staging_id)
        if row is None:
            raise KeyError(f"cannot mark settle_attempted: no staged row {staging_id!r}")
        self._transition_slot(str(row["payer"]), str(row["trial_id"]), "settle_attempted", staging_id=staging_id)

    def count_pending(self) -> int:
        """Return the number of staged rows. Pending is never public."""
        return len(list(self._staged.glob("*.json")))

    # ------------------------------------------------------------------ journal

    def journal(self, staging_id: str, *, payer: str, tx_hash: str) -> None:
        """Record that settlement SUCCEEDED, with the transaction that proves it.

        Written after settle returns success and before finalization, so the durable proof
        exists before anything depends on it. If the process dies here, the reconciler finds a
        journal entry with no finalized record and completes it — the settlement is never
        re-attempted, because it is known to have happened.
        """
        self._write_atomic(
            self._journal / f"{staging_id}.json",
            {"staging_id": staging_id, "payer": payer, "tx_hash": tx_hash},
        )

    def journal_len(self) -> int:
        """Return the number of unconsumed journal entries."""
        return len(list(self._journal.glob("*.json")))

    def finalize_from_journal(self, staging_id: str) -> str:
        """Materialize the finalized record from the staged row and its journal entry.

        Order within this method is load-bearing: the finalized PAYLOAD is written first, then
        the slot is transitioned to point at it, and only then are the staged row and journal
        entry deleted. A crash at any point leaves either a completable journal entry or a
        finalized record that is already correct — never a pointer to a payload that does not
        exist.

        Returns:
            The receipt id, derived deterministically so a reconciler-completed finalization
            produces the same id the request path would have.

        Raises:
            KeyError: The staged row or the journal entry is missing.
        """
        row = self.staged_row(staging_id)
        if row is None:
            raise KeyError(f"cannot finalize: no staged row {staging_id!r}")
        journal_path = self._journal / f"{staging_id}.json"
        if not journal_path.is_file():
            raise KeyError(f"cannot finalize: no journal entry {staging_id!r}")
        entry = self._read_json(journal_path)
        body = row["body"] if isinstance(row.get("body"), dict) else {}
        receipt_id = receipt_id_for(staging_id)
        record = {
            "receipt_id": receipt_id,
            "trial_id": row["trial_id"],
            # The payer's own spelling of the trial id, carried ALONGSIDE the resolved one because
            # the body hash was taken over it. Resolution may canonicalize, so the two are not
            # interchangeable, and dropping this would leave ``body_hash`` unverifiable.
            "committed_trial_id": body.get("trial_id"),
            "payer": row["payer"],
            "p_follow_profitable": body.get("p_follow_profitable"),
            "methodology_version": body.get("methodology_version"),
            "body_hash": row.get("body_hash"),
            "payment_tx_hash": entry.get("tx_hash"),
            "committed_at_ms": row.get("staged_at_ms"),
            "commit_deadline_ms": row.get("commit_deadline_ms"),
            "trial_mode": row.get("trial_mode"),
        }
        # Sealed here, in the one place a finalized row is created, and over the row itself rather
        # than over the arguments that built it — so the hash commits to exactly what gets
        # written. Reconciler-completed finalizations run this same line, so a crash-recovered
        # receipt is sealed identically to one finalized on the request path.
        record["manifest_hash"] = run_manifest_hash(commit_manifest(record))
        self._write_atomic(self._finalized / f"{receipt_id}.json", record)
        self._transition_slot(
            str(row["payer"]),
            str(row["trial_id"]),
            "finalized",
            receipt_id=receipt_id,
            staging_id=staging_id,
        )
        journal_path.unlink(missing_ok=True)
        self.delete_staged(staging_id)
        return receipt_id

    # ------------------------------------------------------------------ finalized reads

    def _record_from(self, payload: dict[str, Any]) -> CommitRecord:
        """Build a :class:`CommitRecord` from a stored finalized payload."""
        return CommitRecord(
            receipt_id=str(payload["receipt_id"]),
            trial_id=str(payload["trial_id"]),
            payer=str(payload["payer"]),
            p_follow_profitable=float(payload["p_follow_profitable"]),
            methodology_version=payload.get("methodology_version"),
            body_hash=str(payload.get("body_hash")),
            payment_tx_hash=str(payload.get("payment_tx_hash")),
            committed_at_ms=int(payload.get("committed_at_ms") or 0),
            commit_deadline_ms=payload.get("commit_deadline_ms"),
            trial_mode=payload.get("trial_mode"),
        )

    def _public_iter(self) -> list[CommitRecord]:
        """Every row a free read may serve — finalized only, and by CONSTRUCTION.

        The single gate for public visibility. Both public accessors go through it, so widening
        what is served means editing one function whose name says what it does, rather than
        adding a directory to one of several call sites and missing the others.
        """
        return [self._record_from(self._read_json(p)) for p in sorted(self._finalized.glob("*.json"))]

    def finalized(self) -> tuple[CommitRecord, ...]:
        """Return every finalized record."""
        return tuple(self._public_iter())

    def public_records(self, payer: str) -> list[CommitRecord]:
        """Return the finalized records for ``payer``. Staged and quarantined rows are absent."""
        return [record for record in self._public_iter() if record.payer == payer]

    def _finalized_path(self, receipt_id: str) -> Path | None:
        """Return the file ``receipt_id`` names, or ``None`` when the id could not name one.

        The verify route takes this id straight from a URL path segment, so this is the boundary
        where a traversal attempt has to stop: without the guard, ``../../secrets`` would resolve
        outside the finalized directory and any readable JSON file on the host would be served as
        a receipt.

        Refused with ``None`` rather than by raising — unlike
        :meth:`LiveTrialRepository._trial_path`, which raises. The difference is what the caller
        can honestly say: an id that cannot name a receipt simply has no receipt behind it, and
        "no such receipt" is both the truth and a 404. Raising would answer a probe with a 500,
        which distinguishes a rejected id from an unknown one for whoever is probing.
        """
        if not receipt_id or "/" in receipt_id or "\\" in receipt_id or "\0" in receipt_id:
            return None
        if receipt_id in {".", ".."}:
            return None
        return self._finalized / f"{receipt_id}.json"

    def finalized_payload(self, receipt_id: str) -> dict[str, Any] | None:
        """Return the stored finalized row VERBATIM, or ``None`` when there is none.

        The verifier's read path. Verbatim rather than through :class:`CommitRecord` because
        verification has to see what is ON DISK: :meth:`_record_from` coerces as it loads, and a
        check fed coerced values could not tell a stamp stored as ``"1700000000000"`` from one
        stored as an integer. It is also what keeps the writer and the verifier hashing the same
        bytes — both sides read this shape.
        """
        path = self._finalized_path(receipt_id)
        if path is None or not path.is_file():
            return None
        return self._read_json(path)

    def record(self, receipt_id: str) -> CommitRecord | None:
        """Return one finalized record by receipt id, or ``None``."""
        payload = self.finalized_payload(receipt_id)
        return None if payload is None else self._record_from(payload)

    def finalized_for(self, payer: str, trial_id: str) -> CommitRecord | None:
        """Return the finalized record for ``(payer, trial_id)``, or ``None``.

        Resolved THROUGH the slot pointer rather than by scanning the finalized directory for a
        matching payer and trial. The slot is the single source of truth for "has this payer
        already committed to this trial", and idempotency has to be decided by the same artifact
        that blocks a second settle — otherwise the two could disagree, and the disagreement
        would surface as either a double charge or a lost receipt.
        """
        if self.slot_state(payer, trial_id) != "finalized":
            return None
        receipt_id = self.slot_receipt_id(payer, trial_id)
        return None if receipt_id is None else self.record(receipt_id)

    def count_finalized(self) -> int:
        """Return the number of finalized records."""
        return len(list(self._finalized.glob("*.json")))

    def count_all_public(self) -> int:
        """Return how many rows a free read would actually serve.

        Deliberately NOT ``count_finalized``, which counts a directory. This walks the public
        read path, so a read path widened to include staged, attempted or quarantined rows
        changes this number while the directory count stays put.
        """
        return len(self._public_iter())

    def count_all(self) -> int:
        """Return every commit ROW of any lifecycle state: staged plus finalized.

        Slots and journal entries are pointers, not rows, and are counted separately. This is
        the number the settlement-failure assertions use, because "zero records of any kind"
        has to mean the staged row was deleted too — not merely that nothing was published.
        """
        return self.count_pending() + self.count_finalized()

    def count_settle_attempted(self) -> int:
        """Return the number of slots that reached the settle call with an unknown outcome."""
        return sum(1 for record in self._iter_slots() if record["state"] == "settle_attempted")

    def count_quarantined(self) -> int:
        """Return the number of quarantined slots awaiting operator resolution."""
        return sum(1 for record in self._iter_slots() if record["state"] == "quarantined")

    # ------------------------------------------------------------------ reconciler

    def reconcile(self, now_ms: int | None = None) -> None:
        """Bring the tree to a consistent state. Safe to run at startup and repeatedly.

        Startup is the important caller: a crash means the request path's ``finally`` never ran,
        so orphaned rows and slots exist precisely when nobody is around to notice them.

        The four branches, in the order they must run:

        1. **Journal entry with no finalized record -> COMPLETE it.** Settlement is known to
           have happened; finishing the bookkeeping is the only correct action, and it must run
           before quarantine so a completable row is never quarantined instead.
        2. **``settle_attempted`` with no journal entry -> QUARANTINE.** The facilitator may
           hold a real payment and the outcome is unknowable without a transaction hash. Never
           deleted, never served, and it blocks all future settles for the slot. Time-independent
           on purpose: waiting longer produces no new information, so there is nothing to wait
           for.
        3. **``in_flight``, no attempt marker, and stale -> RELEASE.** Provably never reached
           settle, so no payment can exist and the slot is safe to reopen.
        4. **Staged row with no attempt marker and stale -> SWEEP.** Same proof, applied to the
           payload.

        Branches 3 and 4 need a clock and are skipped for rows that are not yet stale; branches
        1 and 2 do not. ``now_ms=None`` means "use the real clock", which is what a startup
        caller wants.

        Args:
            now_ms: The current time. ``None`` reads the wall clock.
        """
        clock = int(time.time() * 1000) if now_ms is None else now_ms

        # 1. Complete every journalled settlement first.
        for path in sorted(self._journal.glob("*.json")):
            staging_id = path.stem
            if self.record(receipt_id_for(staging_id)) is None:
                self.finalize_from_journal(staging_id)

        # 2. Quarantine every attempted slot that has no journal entry behind it.
        for record in self._iter_slots():
            if record["state"] != "settle_attempted":
                continue
            marker_id = record.get("staging_id")
            if isinstance(marker_id, str) and (self._journal / f"{marker_id}.json").is_file():
                continue
            self._transition_slot(str(record["payer"]), str(record["trial_id"]), "quarantined")

        attempted_or_worse = {
            str(record.get("staging_id"))
            for record in self._iter_slots()
            if record["state"] in INDETERMINATE_STATES or record["state"] == "finalized"
        }

        # 3. Release stale in_flight slots that never reached the settle call.
        for record in self._iter_slots():
            if record["state"] != "in_flight":
                continue
            if clock - int(record.get("created_at_ms") or 0) > STALE_AFTER_MS:
                self.release_slot(str(record["payer"]), str(record["trial_id"]))

        # 4. Sweep stale staged rows that never reached the settle call.
        for path in sorted(self._staged.glob("*.json")):
            row = self._read_json(path)
            if path.stem in attempted_or_worse:
                continue
            state = self.slot_state(str(row["payer"]), str(row["trial_id"]))
            if state in INDETERMINATE_STATES or state == "finalized":
                continue
            if clock - int(row.get("staged_at_ms") or 0) > STALE_AFTER_MS:
                path.unlink(missing_ok=True)


def verify_receipt(receipt_id: str, store: ReceiptStore) -> VerifyReport:
    """Verify a finalized receipt's COMMIT-TIME claims. Never raises on a bad receipt.

    Four checks, all four always evaluated, each reporting only what it covers:

    ``body_hash``
        The canonical body rebuilt from the fields the receipt SERVES re-hashes to the hash taken
        over the payer's request at staging time. Catches any rewrite of the probability, the
        methodology, the payer's trial-id spelling, or the stored hash itself.
    ``manifest``
        The receipt's binding facts re-hash to the manifest hash sealed at finalization. Catches a
        rewritten payer, payment transaction, receipt id or resolved trial — none of which is
        inside the signed body, which is why this is a check of its own.
    ``deadline_respected``
        The recorded commit instant is STRICTLY before the recorded deadline. Frozen spec section
        11 makes the boundary instant late (``received_at >= commit_deadline`` is rejected), so the
        comparison is ``<``, not ``<=``.
    ``live_mode``
        The recorded mode is exactly :data:`LIVE_TRIAL_MODE`. Paid commits are live-only: a replay
        outcome is publicly knowable, so a paid "prediction" of one is not a prediction.

    The last two read facts the receipt CARRIES rather than consulting the live trial, and that is
    what makes a historical receipt verifiable at all — the trial it belongs to may have closed
    long ago, and a check that needed the trial store could only verify recent commitments.

    **A failure is a returned verdict, not an exception.** The only ``raise`` here is for a
    receipt that does not exist, because "no such receipt" is a different statement from "this
    receipt does not verify" and the route answers them with different status codes.

    Args:
        receipt_id: The receipt to verify. Arrives from a URL path segment on the free verify
            route, and is resolved through :meth:`ReceiptStore.finalized_payload`, which refuses
            an id that could escape the finalized directory.
        store: The commit store, read-only throughout.

    Returns:
        The report. Its ``receipt_id`` is the id that RESOLVED to a stored row — not the row's
        self-reported one, which a tamper could have rewritten and which the ``manifest`` check
        covers.

    Raises:
        KeyError: No FINALIZED row answers to ``receipt_id``. A staged row is a commitment that
            was received and not yet paid for, and a quarantined slot is a settlement whose
            outcome is unknown; neither is a receipt, and reporting checks over one would let an
            unpaid commitment be quoted as a verified one.
    """
    payload = store.finalized_payload(receipt_id)
    if payload is None:
        raise KeyError(f"no finalized receipt {receipt_id!r}; pending and quarantined rows are not receipts")

    committed_at_ms = _epoch_ms(payload.get("committed_at_ms"))
    commit_deadline_ms = _epoch_ms(payload.get("commit_deadline_ms"))
    verdicts: dict[str, bool] = {
        "body_hash": canonical_body_hash(committed_body(payload)) == payload.get("body_hash"),
        "manifest": run_manifest_hash(commit_manifest(payload)) == payload.get("manifest_hash"),
        "deadline_respected": (
            committed_at_ms is not None and commit_deadline_ms is not None and committed_at_ms < commit_deadline_ms
        ),
        "live_mode": payload.get("trial_mode") == LIVE_TRIAL_MODE,
    }
    checks: dict[str, CheckState] = {check: "pass" if verdicts[check] else "fail" for check in VERIFY_COMMIT_CHECKS}
    return VerifyReport(receipt_id=receipt_id, checks=checks)
