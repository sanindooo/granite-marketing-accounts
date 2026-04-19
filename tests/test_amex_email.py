"""Tests for execution.adapters.amex_email."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from execution.adapters.amex_email import (
    EmailKind,
    classify_email_kind,
    parse_statement_closing,
    parse_transaction_notification,
    require_dmarc_pass,
)
from execution.shared.errors import DataQualityError, SchemaViolationError

# ---------------------------------------------------------------------------
# DMARC gate
# ---------------------------------------------------------------------------


class TestDmarc:
    def test_rejects_when_header_missing(self):
        with pytest.raises(SchemaViolationError, match="Authentication-Results"):
            require_dmarc_pass(authentication_results_header=None)

    def test_rejects_when_dmarc_fail(self):
        with pytest.raises(SchemaViolationError, match="DMARC"):
            require_dmarc_pass(
                authentication_results_header=(
                    "spf=pass smtp.mailfrom=amex.com; dkim=pass; dmarc=fail"
                )
            )

    def test_rejects_when_dmarc_none(self):
        with pytest.raises(SchemaViolationError, match="DMARC"):
            require_dmarc_pass(
                authentication_results_header="dmarc=none"
            )

    def test_accepts_dmarc_pass(self):
        # No exception = accepted
        require_dmarc_pass(
            authentication_results_header=(
                "dkim=pass header.d=amex.com; dmarc=pass action=none;"
            )
        )

    def test_dmarc_case_insensitive(self):
        require_dmarc_pass(
            authentication_results_header="DMARC=PASS"
        )


# ---------------------------------------------------------------------------
# classify_email_kind
# ---------------------------------------------------------------------------


class TestClassifyEmailKind:
    def test_statement_subject(self):
        kind = classify_email_kind(
            subject="Your statement is ready",
            body="Your closing balance is £247.43",
        )
        assert kind is EmailKind.STATEMENT_CLOSING

    def test_statement_closing_in_body(self):
        kind = classify_email_kind(
            subject="Amex update",
            body="This is a statement closing notification. Balance: £100.00",
        )
        assert kind is EmailKind.STATEMENT_CLOSING

    def test_transaction_notification(self):
        kind = classify_email_kind(
            subject="A charge on your account",
            body="You used your card at STARBUCKS for Amount: £4.50",
        )
        assert kind is EmailKind.TRANSACTION_NOTIFICATION

    def test_unrecognised(self):
        kind = classify_email_kind(
            subject="Marketing",
            body="Please do not reply",
        )
        assert kind is EmailKind.UNRECOGNISED


# ---------------------------------------------------------------------------
# parse_transaction_notification
# ---------------------------------------------------------------------------


class TestParseTransactionNotification:
    def test_happy_path(self):
        body = (
            "You used your card at STARBUCKS LONDON on 2026-04-10 "
            "for Amount: £4.50. Approval code: ABC123"
        )
        parsed = parse_transaction_notification(
            source_msg_id="msg-1",
            subject="A charge on your account",
            body=body,
            received_date=date(2026, 4, 10),
        )
        assert parsed.amount == Decimal("4.50")
        assert parsed.merchant == "STARBUCKS LONDON"
        assert parsed.posted_date == date(2026, 4, 10)
        assert parsed.approval_code == "ABC123"

    def test_no_amount_raises(self):
        body = "You used your card at ATLASSIAN for services."
        with pytest.raises(DataQualityError, match="amount"):
            parse_transaction_notification(
                source_msg_id="msg-2",
                subject="Charge",
                body=body,
                received_date=date(2026, 4, 10),
            )

    def test_no_merchant_defaults_to_unknown(self):
        body = "Amount: £9.99 charged to your account"
        parsed = parse_transaction_notification(
            source_msg_id="msg-3",
            subject="Charge",
            body=body,
            received_date=date(2026, 4, 10),
        )
        assert parsed.amount == Decimal("9.99")
        assert parsed.merchant == "UNKNOWN MERCHANT"

    def test_no_approval_code_ok(self):
        body = (
            "You used your card at NETFLIX on 11 Apr 2026 for Amount: £11.99"
        )
        parsed = parse_transaction_notification(
            source_msg_id="msg-4",
            subject="Charge",
            body=body,
            received_date=date(2026, 4, 10),
        )
        assert parsed.approval_code is None
        assert parsed.posted_date == date(2026, 4, 11)

    def test_comma_in_amount(self):
        body = "You used your card at APPLE for Amount: £1,299.00 on 2026-04-10."
        parsed = parse_transaction_notification(
            source_msg_id="msg-5",
            subject="Charge",
            body=body,
            received_date=date(2026, 4, 10),
        )
        assert parsed.amount == Decimal("1299.00")

    def test_fallback_date_to_received(self):
        body = "You used your card at NETFLIX for Amount: £11.99."
        parsed = parse_transaction_notification(
            source_msg_id="msg-6",
            subject="Charge",
            body=body,
            received_date=date(2026, 5, 1),
        )
        assert parsed.posted_date == date(2026, 5, 1)


# ---------------------------------------------------------------------------
# parse_statement_closing
# ---------------------------------------------------------------------------


class TestParseStatementClosing:
    def test_happy_path(self):
        body = (
            "Your Business Gold statement is ready. New balance: £2,447.63. "
            "Statement closing on 25 March 2026. Due date 22 April 2026."
        )
        parsed = parse_statement_closing(
            source_msg_id="stmt-1",
            subject="Your statement is ready",
            body=body,
            received_date=date(2026, 3, 26),
        )
        assert parsed.statement_billed_amount == Decimal("2447.63")
        assert parsed.statement_close_date == date(2026, 3, 25)

    def test_iso_close_date(self):
        body = "Closing balance: £1,000.00. Statement closing on 2026-04-05."
        parsed = parse_statement_closing(
            source_msg_id="stmt-2",
            subject="Your statement is ready",
            body=body,
            received_date=date(2026, 4, 6),
        )
        assert parsed.statement_close_date == date(2026, 4, 5)

    def test_missing_close_falls_back_to_received(self):
        body = "Your new balance: £500.00"
        parsed = parse_statement_closing(
            source_msg_id="stmt-3",
            subject="Your statement is ready",
            body=body,
            received_date=date(2026, 4, 1),
        )
        assert parsed.statement_billed_amount == Decimal("500.00")
        assert parsed.statement_close_date == date(2026, 4, 1)

    def test_no_balance_raises(self):
        with pytest.raises(DataQualityError, match="balance"):
            parse_statement_closing(
                source_msg_id="stmt-4",
                subject="Your statement is ready",
                body="No numbers here at all",
                received_date=date(2026, 4, 1),
            )

    def test_comma_in_amount(self):
        body = "Statement balance: £12,345.67. Closing on 2026-04-10."
        parsed = parse_statement_closing(
            source_msg_id="stmt-5",
            subject="Your statement is ready",
            body=body,
            received_date=date(2026, 4, 11),
        )
        assert parsed.statement_billed_amount == Decimal("12345.67")
