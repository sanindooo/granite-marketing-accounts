"""Tests for execution.adapters.amex_csv."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from execution.adapters.amex_csv import (
    ACCOUNT,
    CANONICAL_COLUMNS,
    MAX_FILE_BYTES,
    SOURCE_ID,
    canonicalise_description,
    compute_txn_id,
    discover_csv_files,
    fetch_from_file,
)
from execution.shared.errors import (
    DataQualityError,
    PathViolationError,
    SchemaViolationError,
)

HEADER = ",".join(CANONICAL_COLUMNS) + "\n"


def _write_csv(path: Path, rows: list[str]) -> None:
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# canonicalise_description
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("STARBUCKS COFFEE #12345", "STARBUCKS COFFEE #12345"),
        ("  starbucks coffee   ", "STARBUCKS COFFEE"),
        ("STARBUCKS LONDON GB 12345", "STARBUCKS"),
        ("STARBUCKS LONDON", "STARBUCKS"),
        ("Netflix ABCD1234EF", "NETFLIX"),
        # Trailing country with no digits still matches the pattern
        ("Kings Cross GB", "KINGS CROSS"),
    ],
)
def test_canonicalise_description(raw, expected):
    assert canonicalise_description(raw) == expected


def test_canonicalise_strips_control_chars():
    assert canonicalise_description("Acme\x00 Ltd\x7f") == "ACME LTD"


# ---------------------------------------------------------------------------
# compute_txn_id
# ---------------------------------------------------------------------------


def test_reference_wins_over_synthesised_hash():
    ref = compute_txn_id(
        reference="REF-123",
        account=ACCOUNT,
        booking_date=date(2026, 4, 10),
        canonical_description="STARBUCKS",
        amount=Decimal("4.50"),
        row_ordinal=0,
    )
    no_ref = compute_txn_id(
        reference=None,
        account=ACCOUNT,
        booking_date=date(2026, 4, 10),
        canonical_description="STARBUCKS",
        amount=Decimal("4.50"),
        row_ordinal=0,
    )
    assert ref != no_ref


def test_synthesised_hash_depends_on_row_ordinal():
    a = compute_txn_id(
        reference=None, account=ACCOUNT,
        booking_date=date(2026, 4, 10),
        canonical_description="COFFEE", amount=Decimal("3.50"), row_ordinal=0,
    )
    b = compute_txn_id(
        reference=None, account=ACCOUNT,
        booking_date=date(2026, 4, 10),
        canonical_description="COFFEE", amount=Decimal("3.50"), row_ordinal=1,
    )
    assert a != b


def test_hash_stable_across_calls():
    kwargs = dict(
        reference=None, account=ACCOUNT,
        booking_date=date(2026, 4, 10),
        canonical_description="COFFEE", amount=Decimal("3.50"), row_ordinal=2,
    )
    assert compute_txn_id(**kwargs) == compute_txn_id(**kwargs)


# ---------------------------------------------------------------------------
# fetch_from_file — schema + parsing + error paths
# ---------------------------------------------------------------------------


def test_fetch_happy_path(tmp_path: Path):
    csv_path = tmp_path / "statement.csv"
    _write_csv(
        csv_path,
        [
            "10/04/2026,STARBUCKS LONDON,4.50,,,,London,EC2A,GB,REF-001,Dining",
            "11/04/2026,ATLASSIAN,480.00,,,,Sydney,NSW,AU,REF-002,Software",
        ],
    )
    batches = list(fetch_from_file(csv_path, drop_root=tmp_path))
    assert len(batches) == 1
    assert len(batches[0]) == 2
    first = batches[0][0]
    assert first.account == ACCOUNT
    assert first.booking_date == date(2026, 4, 10)
    assert first.description_raw == "STARBUCKS LONDON"
    assert first.description_canonical == "STARBUCKS"
    assert first.currency == "GBP"
    assert first.amount == Decimal("4.50")
    assert first.reference == "REF-001"
    assert first.category_hint == "Dining"


def test_fetch_rejects_unexpected_header(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(SchemaViolationError, match="canonical"):
        list(fetch_from_file(csv_path, drop_root=tmp_path))


def test_fetch_rejects_oversized_file(tmp_path: Path, monkeypatch):
    csv_path = tmp_path / "big.csv"
    _write_csv(csv_path, [])
    monkeypatch.setattr(
        "execution.adapters.amex_csv.MAX_FILE_BYTES", 10
    )
    with pytest.raises(SchemaViolationError, match="too large"):
        list(fetch_from_file(csv_path, drop_root=tmp_path))


def test_fetch_rejects_too_many_rows(tmp_path: Path, monkeypatch):
    csv_path = tmp_path / "many.csv"
    rows = [
        f"10/04/2026,Merchant {i},{i}.00,,,,,,,REF-{i},Cat" for i in range(5)
    ]
    _write_csv(csv_path, rows)
    monkeypatch.setattr(
        "execution.adapters.amex_csv.MAX_ROWS", 2
    )
    with pytest.raises(SchemaViolationError, match="row cap"):
        list(fetch_from_file(csv_path, drop_root=tmp_path))


def test_fetch_rejects_path_outside_drop_root(tmp_path: Path):
    drop = tmp_path / "drop"
    drop.mkdir()
    outside = tmp_path / "other.csv"
    _write_csv(outside, [])
    with pytest.raises(PathViolationError):
        list(fetch_from_file(outside, drop_root=drop))


def test_fetch_rejects_bad_date(tmp_path: Path):
    csv_path = tmp_path / "bad_date.csv"
    _write_csv(
        csv_path,
        ["not-a-date,Starbucks,4.50,,,,London,EC2A,GB,REF-1,Dining"],
    )
    with pytest.raises(DataQualityError, match="date"):
        list(fetch_from_file(csv_path, drop_root=tmp_path))


def test_fetch_rejects_bad_amount(tmp_path: Path):
    csv_path = tmp_path / "bad_amt.csv"
    _write_csv(
        csv_path,
        ["10/04/2026,Starbucks,not-a-number,,,,London,EC2A,GB,REF-1,Dining"],
    )
    with pytest.raises(DataQualityError, match="amount"):
        list(fetch_from_file(csv_path, drop_root=tmp_path))


def test_fetch_batches_at_configured_size(tmp_path: Path):
    csv_path = tmp_path / "statement.csv"
    rows = [
        f"10/04/2026,Merchant-{i},{i + 1}.00,,,,,,,REF-{i},Cat"
        for i in range(12)
    ]
    _write_csv(csv_path, rows)
    batches = list(fetch_from_file(csv_path, drop_root=tmp_path, batch_size=5))
    assert [len(b) for b in batches] == [5, 5, 2]


def test_fetch_handles_utf8_bom(tmp_path: Path):
    csv_path = tmp_path / "bom.csv"
    # Write with BOM prefix
    csv_path.write_bytes(
        b"\xef\xbb\xbf"
        + HEADER.encode("utf-8")
        + b"10/04/2026,Netflix,11.99,,,,,,,REF-N,Streaming\n"
    )
    batches = list(fetch_from_file(csv_path, drop_root=tmp_path))
    assert len(batches) == 1
    assert batches[0][0].description_raw == "Netflix"


def test_fetch_skips_rows_missing_mandatory_fields(tmp_path: Path):
    csv_path = tmp_path / "sparse.csv"
    _write_csv(
        csv_path,
        [
            ",,,,,,,,,REF-blank,Cat",  # entirely empty mandatory fields
            "10/04/2026,Starbucks,4.50,,,,,,,REF-001,Cat",
        ],
    )
    batches = list(fetch_from_file(csv_path, drop_root=tmp_path))
    assert len(batches) == 1
    assert len(batches[0]) == 1


# ---------------------------------------------------------------------------
# discover_csv_files
# ---------------------------------------------------------------------------


def test_discover_returns_sorted_csvs(tmp_path: Path):
    (tmp_path / "b.csv").write_text("x", encoding="utf-8")
    (tmp_path / "a.csv").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    out = discover_csv_files(tmp_path)
    assert [p.name for p in out] == ["a.csv", "b.csv"]


def test_discover_raises_when_root_missing(tmp_path: Path):
    with pytest.raises(PathViolationError):
        discover_csv_files(tmp_path / "nope")


def test_module_constants_published():
    assert SOURCE_ID == "amex_csv"
    assert ACCOUNT == "amex"
    assert MAX_FILE_BYTES == 10 * 1024 * 1024
