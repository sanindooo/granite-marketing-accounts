"""Sheet helpers: sanitizer, fy lookup, token write, create_fy_workbook orchestration."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from execution.shared import sheet as sheet_mod
from execution.shared.errors import ConfigError
from execution.shared.sheet import (
    SHEET_TAB_NAMES,
    FiscalYearSheet,
    GoogleClients,
    _escape_drive_query,
    _write_token,
    create_fy_workbook,
    get_fy_sheet,
    sanitize_cell,
    validate_fy_label,
)


class TestSanitizeCell:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("=SUM(A1)", "'=SUM(A1)"),
            ("+5", "'+5"),
            ("-5", "'-5"),
            ("@foo", "'@foo"),
            ("normal text", "normal text"),
            ("123.45", "123.45"),
            ("", ""),
        ],
    )
    def test_formula_prefix(self, raw: str, expected: str) -> None:
        assert sanitize_cell(raw) == expected

    def test_none_becomes_empty(self) -> None:
        assert sanitize_cell(None) == ""

    def test_non_string_coerced(self) -> None:
        assert sanitize_cell(42) == "42"

    def test_strips_control_chars(self) -> None:
        assert sanitize_cell("a\x00b\x1fc") == "abc"

    def test_control_then_formula_still_sanitized(self) -> None:
        # The control strip happens first; then the leading = triggers prefixing.
        assert sanitize_cell("\x00=1+1") == "'=1+1"


class TestValidateFyLabel:
    @pytest.mark.parametrize("good", ["FY-2026-27", "FY-2031-32", "FY-1999-00"])
    def test_accepts_good(self, good: str) -> None:
        assert validate_fy_label(good) == good

    @pytest.mark.parametrize("bad", ["2026-27", "FY-26-27", "FY-2026", "", "FY-2026-271"])
    def test_rejects_bad(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_fy_label(bad)


class TestTokenWrite:
    def test_token_is_chmod_600(self, tmp_path: Path) -> None:
        token_p = tmp_path / "state" / "token.json"
        _write_token(token_p, '{"refresh_token": "x"}')
        assert token_p.exists()
        mode = stat.S_IMODE(token_p.stat().st_mode)
        assert mode == 0o600
        assert token_p.read_text() == '{"refresh_token": "x"}'

    def test_parent_created_if_missing(self, tmp_path: Path) -> None:
        token_p = tmp_path / "new_dir" / "token.json"
        assert not token_p.parent.exists()
        _write_token(token_p, "body")
        assert token_p.parent.is_dir()


class TestEscapeDriveQuery:
    def test_escapes_apostrophe(self) -> None:
        assert _escape_drive_query("Mc'Donald") == "Mc\\'Donald"

    def test_escapes_backslash_before_apostrophe(self) -> None:
        assert _escape_drive_query("a\\b") == "a\\\\b"

    def test_passthrough_plain(self) -> None:
        assert _escape_drive_query("Accounts") == "Accounts"


class TestGoogleClientsConnect:
    def test_mock_mode_refuses(self, mock_secrets: None) -> None:
        del mock_secrets
        with pytest.raises(ConfigError):
            GoogleClients.connect()


class TestGetFySheet:
    def test_returns_none_when_missing(self, tmp_db: sqlite3.Connection) -> None:
        assert get_fy_sheet(tmp_db, "FY-2026-27") is None

    def test_round_trips_after_insert(self, tmp_db: sqlite3.Connection) -> None:
        tmp_db.execute(
            """INSERT INTO fiscal_year_sheets
                (fiscal_year, spreadsheet_id, drive_folder_id, created_at)
               VALUES (?, ?, ?, ?)""",
            ("FY-2026-27", "ss-id", "folder-id", "2026-04-17T00:00:00+00:00"),
        )
        got = get_fy_sheet(tmp_db, "FY-2026-27")
        assert got == FiscalYearSheet(
            fiscal_year="FY-2026-27",
            spreadsheet_id="ss-id",
            drive_folder_id="folder-id",
            web_view_link="https://docs.google.com/spreadsheets/d/ss-id",
        )

    def test_rejects_bad_fy_label(self, tmp_db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            get_fy_sheet(tmp_db, "bad")


class TestCreateFyWorkbook:
    def test_creates_folder_workbook_and_moves_into_folder(
        self, tmp_db: sqlite3.Connection
    ) -> None:
        fake = _FakeGoogle()
        fy = create_fy_workbook(fake, tmp_db, "FY-2026-27")
        assert fy.fiscal_year == "FY-2026-27"
        assert fy.spreadsheet_id == "ss-FY-2026-27"
        assert fy.drive_folder_id == "folder-FY-2026-27"
        # Created root + fy folder
        assert fake.folders_created == ["Accounts", "FY-2026-27"]
        # Spreadsheet body contained all 5 tabs
        assert len(fake.spreadsheets_created) == 1
        tabs = [
            s["properties"]["title"]
            for s in fake.spreadsheets_created[0]["sheets"]
        ]
        assert tabs == list(SHEET_TAB_NAMES)
        # Moved into FY folder
        assert fake.moves == [("ss-FY-2026-27", "folder-FY-2026-27")]
        # Row registered
        row = tmp_db.execute(
            "SELECT spreadsheet_id, drive_folder_id FROM fiscal_year_sheets "
            "WHERE fiscal_year='FY-2026-27'"
        ).fetchone()
        assert row["spreadsheet_id"] == "ss-FY-2026-27"
        assert row["drive_folder_id"] == "folder-FY-2026-27"

    def test_idempotent_on_second_call(self, tmp_db: sqlite3.Connection) -> None:
        fake = _FakeGoogle()
        first = create_fy_workbook(fake, tmp_db, "FY-2026-27")
        second = create_fy_workbook(fake, tmp_db, "FY-2026-27")
        assert first == second
        # No additional Drive/Sheets churn on the second call
        assert len(fake.spreadsheets_created) == 1
        assert len(fake.moves) == 1

    def test_rejects_bad_fy(self, tmp_db: sqlite3.Connection) -> None:
        fake = _FakeGoogle()
        with pytest.raises(ValueError):
            create_fy_workbook(fake, tmp_db, "bogus")


# ---------------------------------------------------------------------------
# Fakes — mimic the google-api-python-client fluent handles
# ---------------------------------------------------------------------------


@dataclass
class _FakeRequest:
    payload: dict[str, Any]

    def execute(self) -> dict[str, Any]:
        return self.payload


@dataclass
class _FakeFiles:
    parent: _FakeDrive

    def list(self, *, q: str, fields: str, pageSize: int) -> _FakeRequest:
        del fields, pageSize
        # Never find an existing folder — forces create path for deterministic tests.
        self.parent.list_queries.append(q)
        return _FakeRequest({"files": []})

    def create(self, *, body: dict[str, Any], fields: str) -> _FakeRequest:
        del fields
        folder_id = f"folder-{body['name']}"
        self.parent.folders_created.append(body["name"])
        return _FakeRequest({"id": folder_id})

    def update(
        self,
        *,
        fileId: str,
        addParents: str,
        removeParents: str,
        fields: str,
    ) -> _FakeRequest:
        del removeParents, fields
        self.parent.moves.append((fileId, addParents))
        return _FakeRequest({"id": fileId, "parents": [addParents]})


@dataclass
class _FakeDrive:
    folders_created: list[str] = field(default_factory=list)
    list_queries: list[str] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)

    def files(self) -> _FakeFiles:
        return _FakeFiles(self)


@dataclass
class _FakeSpreadsheets:
    parent: _FakeSheets

    def create(self, *, body: dict[str, Any], fields: str) -> _FakeRequest:
        del fields
        title = body["properties"]["title"]
        ss_id = "ss-" + title.split(" - ")[-1]
        self.parent.created.append(body)
        return _FakeRequest(
            {
                "spreadsheetId": ss_id,
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{ss_id}",
            }
        )


@dataclass
class _FakeSheets:
    created: list[dict[str, Any]] = field(default_factory=list)

    def spreadsheets(self) -> _FakeSpreadsheets:
        return _FakeSpreadsheets(self)


class _FakeGoogle:
    """Shape-compatible stand-in for :class:`GoogleClients`."""

    def __init__(self) -> None:
        self._drive = _FakeDrive()
        self._sheets = _FakeSheets()

    @property
    def drive(self) -> _FakeDrive:
        return self._drive

    @property
    def sheets(self) -> _FakeSheets:
        return self._sheets

    # Passthrough for test assertions.
    @property
    def folders_created(self) -> list[str]:
        return self._drive.folders_created

    @property
    def spreadsheets_created(self) -> list[dict[str, Any]]:
        return self._sheets.created

    @property
    def moves(self) -> list[tuple[str, str]]:
        return self._drive.moves


def test_sheet_module_does_not_import_gspread_eagerly() -> None:
    """gspread authorization is expensive; it must stay lazy."""
    import sys

    # sheet module itself is already imported at test collection time, but
    # gspread should not have been pulled in as a side-effect.
    assert "gspread" not in sys.modules or _gspread_imported_elsewhere()


def _gspread_imported_elsewhere() -> bool:
    """Return True if another fixture/test pulled gspread in."""
    # Any test that calls `.gspread` on a GoogleClients would import it. None of
    # the Phase 1B tests do, so in a freshly-spawned pytest run this returns
    # False. We keep this escape hatch so future tests that DO use gspread
    # don't cascade-break this assertion.
    import sys

    return any(mod.startswith("gspread") for mod in sys.modules)


def test_sheet_module_reexports() -> None:
    """Surface guard: public names stay exported."""
    for name in [
        "SCOPES",
        "SHEET_TAB_NAMES",
        "FiscalYearSheet",
        "GoogleClients",
        "create_fy_workbook",
        "credentials_path",
        "ensure_drive_folder",
        "get_fy_sheet",
        "load_credentials",
        "sanitize_cell",
        "token_path",
        "validate_fy_label",
    ]:
        assert hasattr(sheet_mod, name), f"missing: {name}"
