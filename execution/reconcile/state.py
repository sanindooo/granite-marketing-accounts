"""Row state machine for ``reconciliation_rows``.

The plan's § Phase 4 commits to a 9-state machine (plus ``voided`` =
total 9 — see plan for naming). This module is the one chokepoint where
every transition happens. Callers go through :func:`transition` with
the current DB state + the proposed next state + the trigger (script
or user). The function enforces the authoritative matrix and raises
:class:`IllegalTransitionError` on any violation.

Row identity is stable across state transitions: ``row_id =
sha256(fiscal_year + canonical_invoice_id + canonical_txn_id +
link_kind)[:16]``. **State is NOT in the hash.** That is the
reviewer-caught invariant that makes upserts idempotent — the same
invoice↔transaction pair hashes to the same row_id whether it's
``new``, ``auto_matched``, or ``user_personal``.

User overrides (``user_verified``, ``user_overridden``,
``user_personal``, ``user_ignore``) are respected: the script may
**not** overwrite them. The only exception is the ``Sys → voided``
path, triggered automatically when the backing transaction flips to
``status='reversed'`` or an invoice is soft-deleted. That override
records the prior state in ``override_history`` so a later user undo
can restore intent.

The user's way to "change their mind" is to blank the override
columns in the sheet — that returns the row to ``new`` (a U-transition
that this machine allows from any ``user_*`` state).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from execution.shared.errors import PipelineError


class IllegalTransitionError(PipelineError):
    """Tried to move a row between states the matrix disallows."""

    category = "state_machine"
    retryable = False
    error_code = "illegal_transition"


class RowState(StrEnum):
    NEW = "new"
    AUTO_MATCHED = "auto_matched"
    SUGGESTED = "suggested"
    UNMATCHED = "unmatched"
    USER_VERIFIED = "user_verified"
    USER_OVERRIDDEN = "user_overridden"
    USER_PERSONAL = "user_personal"
    USER_IGNORE = "user_ignore"
    VOIDED = "voided"


class Trigger(StrEnum):
    SCRIPT = "script"  # reconciler decision
    USER = "user"  # user edited the sheet
    SYSTEM = "system"  # txn reversed / invoice soft-deleted


NULL_SENTINEL: Final[str] = "-"

# Transition matrix (see plan § Phase 4, Row state machine). Each entry
# maps ``(from_state, to_state) → allowed triggers``. A transition is
# allowed if the caller's trigger is in the set; missing entries raise.
_MATRIX: Final[dict[tuple[RowState, RowState], frozenset[Trigger]]] = {
    # From NEW → any script state or any user state or voided
    (RowState.NEW, RowState.AUTO_MATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.NEW, RowState.SUGGESTED): frozenset({Trigger.SCRIPT}),
    (RowState.NEW, RowState.UNMATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.NEW, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.NEW, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.NEW, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.NEW, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.NEW, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    # From any script state: can re-score to another script state, or
    # user can promote to any user_ state, or system voids.
    (RowState.AUTO_MATCHED, RowState.AUTO_MATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.AUTO_MATCHED, RowState.SUGGESTED): frozenset({Trigger.SCRIPT}),
    (RowState.AUTO_MATCHED, RowState.UNMATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.AUTO_MATCHED, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.AUTO_MATCHED, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.AUTO_MATCHED, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.AUTO_MATCHED, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.AUTO_MATCHED, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    (RowState.SUGGESTED, RowState.AUTO_MATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.SUGGESTED, RowState.SUGGESTED): frozenset({Trigger.SCRIPT}),
    (RowState.SUGGESTED, RowState.UNMATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.SUGGESTED, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.SUGGESTED, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.SUGGESTED, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.SUGGESTED, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.SUGGESTED, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    (RowState.UNMATCHED, RowState.AUTO_MATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.UNMATCHED, RowState.SUGGESTED): frozenset({Trigger.SCRIPT}),
    (RowState.UNMATCHED, RowState.UNMATCHED): frozenset({Trigger.SCRIPT}),
    (RowState.UNMATCHED, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.UNMATCHED, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.UNMATCHED, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.UNMATCHED, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.UNMATCHED, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    # From user_* states: only users move between them (blanking an
    # override column returns the row to `new`). System can still void.
    (RowState.USER_VERIFIED, RowState.NEW): frozenset({Trigger.USER}),
    (RowState.USER_VERIFIED, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.USER_VERIFIED, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.USER_VERIFIED, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.USER_VERIFIED, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    (RowState.USER_OVERRIDDEN, RowState.NEW): frozenset({Trigger.USER}),
    (RowState.USER_OVERRIDDEN, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.USER_OVERRIDDEN, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.USER_OVERRIDDEN, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.USER_OVERRIDDEN, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    (RowState.USER_PERSONAL, RowState.NEW): frozenset({Trigger.USER}),
    (RowState.USER_PERSONAL, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.USER_PERSONAL, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.USER_PERSONAL, RowState.USER_IGNORE): frozenset({Trigger.USER}),
    (RowState.USER_PERSONAL, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    (RowState.USER_IGNORE, RowState.NEW): frozenset({Trigger.USER}),
    (RowState.USER_IGNORE, RowState.USER_VERIFIED): frozenset({Trigger.USER}),
    (RowState.USER_IGNORE, RowState.USER_OVERRIDDEN): frozenset({Trigger.USER}),
    (RowState.USER_IGNORE, RowState.USER_PERSONAL): frozenset({Trigger.USER}),
    (RowState.USER_IGNORE, RowState.VOIDED): frozenset({Trigger.SYSTEM}),
    # Voided is terminal in this machine; the user must create a new
    # row (via sheet override) to re-evaluate, which becomes a
    # separate row_id anyway.
}

USER_STATES: Final[frozenset[RowState]] = frozenset(
    {
        RowState.USER_VERIFIED,
        RowState.USER_OVERRIDDEN,
        RowState.USER_PERSONAL,
        RowState.USER_IGNORE,
    }
)
SCRIPT_STATES: Final[frozenset[RowState]] = frozenset(
    {
        RowState.AUTO_MATCHED,
        RowState.SUGGESTED,
        RowState.UNMATCHED,
    }
)


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """Emitted by :func:`transition` — ready to append to override_history."""

    from_state: RowState
    to_state: RowState
    trigger: Trigger
    at: datetime
    note: str = ""

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "from": self.from_state.value,
                "to": self.to_state.value,
                "trigger": self.trigger.value,
                "at": self.at.isoformat(),
                "note": self.note,
            }
        )


def transition(
    *,
    current: RowState,
    proposed: RowState,
    trigger: Trigger,
    at: datetime,
    note: str = "",
) -> TransitionRecord:
    """Validate one state change. Raises on anything outside the matrix."""
    if current == proposed and trigger in _MATRIX.get(
        (current, proposed), frozenset()
    ):
        return TransitionRecord(
            from_state=current, to_state=proposed, trigger=trigger, at=at, note=note
        )
    allowed = _MATRIX.get((current, proposed))
    if allowed is None or trigger not in allowed:
        raise IllegalTransitionError(
            f"{current.value} -> {proposed.value} not allowed under {trigger.value}",
            source="state_machine",
            details={
                "from": current.value,
                "to": proposed.value,
                "trigger": trigger.value,
            },
        )
    return TransitionRecord(
        from_state=current, to_state=proposed, trigger=trigger, at=at, note=note
    )


def preserve_user_state(
    *,
    current: RowState,
    script_proposed: RowState,
) -> RowState:
    """Guard that blocks the reconciler from overwriting a user override.

    Called by the matcher before it writes. Returns the state that
    should actually be persisted — if the row is in a ``user_*`` state
    we keep it there; otherwise we accept the script's proposal.
    """
    if current in USER_STATES:
        return current
    if current == RowState.VOIDED:
        return current
    return script_proposed


def void_for_reversal(
    current: RowState, at: datetime, *, reason: str
) -> TransitionRecord:
    """System-driven transition to ``voided`` (txn reversed or invoice deleted).

    Bypasses the user-override preservation guard — the only legal way
    to clobber a ``user_*`` state.
    """
    return transition(
        current=current,
        proposed=RowState.VOIDED,
        trigger=Trigger.SYSTEM,
        at=at,
        note=f"voided: {reason}",
    )


def compute_row_id(
    *,
    fiscal_year: str,
    invoice_id: str | None,
    txn_id: str | None,
    link_kind: str,
) -> str:
    """Stable row_id hash. State is NOT in the hash."""
    invoice_component = invoice_id or NULL_SENTINEL
    txn_component = txn_id or NULL_SENTINEL
    payload = "\x00".join(
        [fiscal_year, invoice_component, txn_component, link_kind]
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def append_history(existing_jsonl: str, record: TransitionRecord) -> str:
    """Append a transition to an existing JSONL override history string."""
    prefix = existing_jsonl
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + record.to_jsonl()


__all__ = [
    "NULL_SENTINEL",
    "SCRIPT_STATES",
    "USER_STATES",
    "IllegalTransitionError",
    "RowState",
    "TransitionRecord",
    "Trigger",
    "append_history",
    "compute_row_id",
    "preserve_user_state",
    "transition",
    "void_for_reversal",
]
