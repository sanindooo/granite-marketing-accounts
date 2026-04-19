"""Tests for execution.reconcile.state."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from execution.reconcile.state import (
    NULL_SENTINEL,
    SCRIPT_STATES,
    USER_STATES,
    IllegalTransitionError,
    RowState,
    TransitionRecord,
    Trigger,
    append_history,
    compute_row_id,
    preserve_user_state,
    transition,
    void_for_reversal,
)

NOW = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# compute_row_id — hash independence from state
# ---------------------------------------------------------------------------


class TestComputeRowId:
    def test_deterministic(self):
        a = compute_row_id(
            fiscal_year="FY-2026-27",
            invoice_id="inv-1",
            txn_id="t-1",
            link_kind="full",
        )
        b = compute_row_id(
            fiscal_year="FY-2026-27",
            invoice_id="inv-1",
            txn_id="t-1",
            link_kind="full",
        )
        assert a == b
        assert len(a) == 16

    def test_sensitive_to_inputs(self):
        base = compute_row_id(
            fiscal_year="FY-2026-27",
            invoice_id="inv-1",
            txn_id="t-1",
            link_kind="full",
        )
        fy_changed = compute_row_id(
            fiscal_year="FY-2027-28",
            invoice_id="inv-1",
            txn_id="t-1",
            link_kind="full",
        )
        link_changed = compute_row_id(
            fiscal_year="FY-2026-27",
            invoice_id="inv-1",
            txn_id="t-1",
            link_kind="split_txn",
        )
        assert base != fy_changed
        assert base != link_changed

    def test_null_sides_use_sentinel(self):
        unmatched_invoice = compute_row_id(
            fiscal_year="FY-2026-27",
            invoice_id=None,
            txn_id="t-1",
            link_kind="full",
        )
        manual = compute_row_id(
            fiscal_year="FY-2026-27",
            invoice_id=NULL_SENTINEL,
            txn_id="t-1",
            link_kind="full",
        )
        assert unmatched_invoice == manual


# ---------------------------------------------------------------------------
# transition — legal vs illegal moves
# ---------------------------------------------------------------------------


class TestTransition:
    def test_script_new_to_auto_matched(self):
        rec = transition(
            current=RowState.NEW,
            proposed=RowState.AUTO_MATCHED,
            trigger=Trigger.SCRIPT,
            at=NOW,
        )
        assert rec.from_state is RowState.NEW
        assert rec.to_state is RowState.AUTO_MATCHED
        assert rec.trigger is Trigger.SCRIPT

    def test_script_cannot_move_out_of_user_verified(self):
        with pytest.raises(IllegalTransitionError):
            transition(
                current=RowState.USER_VERIFIED,
                proposed=RowState.AUTO_MATCHED,
                trigger=Trigger.SCRIPT,
                at=NOW,
            )

    def test_user_can_clear_override_back_to_new(self):
        rec = transition(
            current=RowState.USER_PERSONAL,
            proposed=RowState.NEW,
            trigger=Trigger.USER,
            at=NOW,
        )
        assert rec.to_state is RowState.NEW

    def test_system_can_void_user_verified(self):
        rec = transition(
            current=RowState.USER_VERIFIED,
            proposed=RowState.VOIDED,
            trigger=Trigger.SYSTEM,
            at=NOW,
        )
        assert rec.to_state is RowState.VOIDED

    def test_user_cannot_system_void(self):
        with pytest.raises(IllegalTransitionError):
            transition(
                current=RowState.USER_VERIFIED,
                proposed=RowState.VOIDED,
                trigger=Trigger.USER,
                at=NOW,
            )

    def test_voided_is_terminal(self):
        with pytest.raises(IllegalTransitionError):
            transition(
                current=RowState.VOIDED,
                proposed=RowState.NEW,
                trigger=Trigger.USER,
                at=NOW,
            )

    def test_script_idempotent_on_same_state(self):
        rec = transition(
            current=RowState.AUTO_MATCHED,
            proposed=RowState.AUTO_MATCHED,
            trigger=Trigger.SCRIPT,
            at=NOW,
        )
        assert rec.from_state is RowState.AUTO_MATCHED
        assert rec.to_state is RowState.AUTO_MATCHED

    def test_user_cannot_directly_set_script_states(self):
        with pytest.raises(IllegalTransitionError):
            transition(
                current=RowState.NEW,
                proposed=RowState.SUGGESTED,
                trigger=Trigger.USER,
                at=NOW,
            )


# ---------------------------------------------------------------------------
# preserve_user_state
# ---------------------------------------------------------------------------


class TestPreserveUserState:
    @pytest.mark.parametrize("state", sorted(USER_STATES, key=lambda s: s.value))
    def test_user_states_are_sticky(self, state):
        out = preserve_user_state(
            current=state,
            script_proposed=RowState.AUTO_MATCHED,
        )
        assert out is state

    def test_voided_is_sticky(self):
        out = preserve_user_state(
            current=RowState.VOIDED,
            script_proposed=RowState.AUTO_MATCHED,
        )
        assert out is RowState.VOIDED

    @pytest.mark.parametrize("state", sorted(SCRIPT_STATES, key=lambda s: s.value))
    def test_script_states_accept_script_proposal(self, state):
        out = preserve_user_state(
            current=state,
            script_proposed=RowState.SUGGESTED,
        )
        assert out is RowState.SUGGESTED

    def test_new_accepts_script_proposal(self):
        out = preserve_user_state(
            current=RowState.NEW,
            script_proposed=RowState.AUTO_MATCHED,
        )
        assert out is RowState.AUTO_MATCHED


# ---------------------------------------------------------------------------
# void_for_reversal
# ---------------------------------------------------------------------------


class TestVoidForReversal:
    def test_from_user_verified_is_allowed(self):
        rec = void_for_reversal(
            RowState.USER_VERIFIED, at=NOW, reason="txn reversed"
        )
        assert rec.to_state is RowState.VOIDED
        assert rec.trigger is Trigger.SYSTEM
        assert "txn reversed" in rec.note

    def test_from_voided_rejected(self):
        with pytest.raises(IllegalTransitionError):
            void_for_reversal(
                RowState.VOIDED, at=NOW, reason="already voided"
            )


# ---------------------------------------------------------------------------
# Transition record + history append
# ---------------------------------------------------------------------------


class TestTransitionRecord:
    def test_to_jsonl_is_valid_json(self):
        rec = TransitionRecord(
            from_state=RowState.AUTO_MATCHED,
            to_state=RowState.USER_VERIFIED,
            trigger=Trigger.USER,
            at=NOW,
            note="user ticked Verified",
        )
        payload = json.loads(rec.to_jsonl())
        assert payload == {
            "from": "auto_matched",
            "to": "user_verified",
            "trigger": "user",
            "at": NOW.isoformat(),
            "note": "user ticked Verified",
        }

    def test_append_history_preserves_prior_lines(self):
        first = TransitionRecord(
            from_state=RowState.NEW,
            to_state=RowState.AUTO_MATCHED,
            trigger=Trigger.SCRIPT,
            at=NOW,
        )
        second = TransitionRecord(
            from_state=RowState.AUTO_MATCHED,
            to_state=RowState.USER_VERIFIED,
            trigger=Trigger.USER,
            at=NOW,
        )
        body = append_history("", first)
        body = append_history(body, second)
        lines = body.split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["to"] == "auto_matched"
        assert json.loads(lines[1])["to"] == "user_verified"
