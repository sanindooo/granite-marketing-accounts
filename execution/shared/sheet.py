"""Google Sheets + Drive integration.

Phase 1B ships the OAuth bootstrap and per-fiscal-year workbook creator:

- :func:`load_credentials` runs :class:`InstalledAppFlow` once, caches the
  refresh-capable token to ``.state/token.json`` with 0o600 permissions,
  and silently refreshes on subsequent runs.
- :class:`GoogleClients` packages the ``gspread`` + ``google-api-python-client``
  handles required by later phases. Mock mode refuses to construct one so
  tests cannot accidentally touch the real Drive.
- :func:`create_fy_workbook` creates a Drive folder ``Accounts/FY-YYYY-YY/``,
  a Sheets workbook titled ``Granite Accounts - FY-YYYY-YY`` with the five
  canonical tabs, and records the result in the ``fiscal_year_sheets`` table.
- :func:`sanitize_cell` is the single formula-injection chokepoint mandated
  by the plan: every value that reaches a Sheets cell goes through it.

The per-tab column schemas + the upsert-by-row-key helper land in Phase 4
when the reconciliation output is wired.

OAuth scopes are the narrowest pair that lets us build and update our own
workbooks without seeing the rest of the user's Drive:

- ``https://www.googleapis.com/auth/spreadsheets``
- ``https://www.googleapis.com/auth/drive.file``
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from execution.shared import secrets
from execution.shared.clock import now_utc
from execution.shared.errors import AuthExpiredError, ConfigError

SOURCE_ID: Final[str] = "google"

if TYPE_CHECKING:  # pragma: no cover
    from google.oauth2.credentials import Credentials

SCOPES: Final[tuple[str, ...]] = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

DRIVE_ROOT_NAME: Final[str] = "Accounts"

SHEET_TAB_NAMES: Final[tuple[str, ...]] = (
    "Reconciliation",
    "Unmatched",
    "Exceptions",
    "Sales",
    "Run Status",
)

_FORMULA_PREFIX: Final[re.Pattern[str]] = re.compile(r"^[=+\-@]")
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)
_FY_RE: Final[re.Pattern[str]] = re.compile(r"^FY-\d{4}-\d{2}$")
_DRIVE_FOLDER_MIME: Final[str] = "application/vnd.google-apps.folder"


def sanitize_cell(value: object) -> str:
    """Make a value safe to write into a Google Sheet cell.

    - ``None`` becomes the empty string.
    - Control characters are stripped (they hide content and break CSV export).
    - Values starting with ``=``, ``+``, ``-``, or ``@`` are prefixed with an
      apostrophe so Sheets treats them as text, not a formula.
    """
    s = "" if value is None else str(value)
    s = _CONTROL_CHARS.sub("", s)
    if _FORMULA_PREFIX.search(s):
        return "'" + s
    return s


def validate_fy_label(fy: str) -> str:
    """Accept ``FY-YYYY-YY`` or raise ``ValueError``."""
    if not _FY_RE.match(fy):
        raise ValueError(f"bad fiscal-year label {fy!r}; expected 'FY-YYYY-YY'")
    return fy


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def credentials_path() -> Path:
    """Resolve the OAuth client-secrets file path (override via ``GRANITE_GOOGLE_CREDENTIALS``)."""
    override = os.environ.get("GRANITE_GOOGLE_CREDENTIALS")
    if override:
        return Path(override)
    return _project_root() / "credentials.json"


def token_path() -> Path:
    """Resolve the cached OAuth token path (override via ``GRANITE_GOOGLE_TOKEN``)."""
    override = os.environ.get("GRANITE_GOOGLE_TOKEN")
    if override:
        return Path(override)
    return _project_root() / ".state" / "token.json"


def load_credentials(*, allow_interactive: bool = True) -> Credentials:
    """Return authenticated Google OAuth ``Credentials``.

    First run: opens the browser-based :class:`InstalledAppFlow`, prompts for
    consent, then writes the refresh-capable token to ``token_path()``.
    Subsequent runs load the cached token and transparently refresh it.

    Args:
        allow_interactive: When ``False`` (e.g. running from the web UI's
            spawned CLI process where there's no terminal), a missing or
            revoked token surfaces as :class:`AuthExpiredError` with a
            user_message telling the operator to run
            ``granite ops reauth google`` from the terminal — instead of
            blocking on a browser window that no one can see.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    cred_p = credentials_path()
    token_p = token_path()

    creds: Credentials | None = None
    if token_p.exists():
        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(token_p), list(SCOPES)
        )

    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as err:
                # Only wipe the stored token when Google actually says the
                # refresh token is dead (``invalid_grant``). RefreshError
                # also fires for transient network blips, 5xx from
                # oauth2.googleapis.com, and side-channel rate-limit
                # responses — none of those should force the user back
                # through the browser flow. Keep the token, surface the
                # error, let the next attempt retry.
                error_code = _refresh_error_code(err)
                if error_code == "invalid_grant":
                    _delete_token(token_p)
                raise AuthExpiredError(
                    f"Google OAuth refresh failed: {error_code or 'transient error'}",
                    source=SOURCE_ID,
                    user_message=(
                        "Google access has expired. Run "
                        "`granite ops reauth google` from your terminal to "
                        "re-authorise. (The browser-based OAuth flow can't "
                        "run from the web UI — it needs a desktop browser.)"
                    ) if error_code == "invalid_grant" else (
                        "Google OAuth refresh hit a transient error. Retry "
                        "the run; if it persists, run "
                        "`granite ops reauth google` from your terminal."
                    ),
                    cause=err,
                    details={
                        "token_path": str(token_p),
                        "error_code": error_code,
                    },
                ) from err
        else:
            creds = None

    if not creds or not creds.valid:
        if not cred_p.exists():
            raise ConfigError(
                f"Google OAuth client not found at {cred_p}.",
                source=SOURCE_ID,
                user_message=(
                    "Download an OAuth 2.0 Desktop client from Google Cloud "
                    "Console, save it as credentials.json at the project root, "
                    "then rerun. See directives/setup.md."
                ),
            )
        if not allow_interactive:
            raise AuthExpiredError(
                "Google OAuth requires interactive consent",
                source=SOURCE_ID,
                user_message=(
                    "Google access has not been authorised yet. Run "
                    "`granite ops reauth google` from your terminal."
                ),
                details={"token_path": str(token_p)},
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_p), list(SCOPES))
        creds = flow.run_local_server(port=0)

    _write_token(token_p, creds.to_json())
    return creds


def _delete_token(token_p: Path) -> None:
    """Remove a stale token file. Best-effort; missing file is not an error."""
    with suppress(FileNotFoundError):
        token_p.unlink()


def _refresh_error_code(err: Exception) -> str:
    """Extract Google's OAuth ``error`` code from a RefreshError.

    google-auth raises ``RefreshError(message, response_dict)`` where
    ``response_dict`` is the JSON body Google returned (e.g.
    ``{"error": "invalid_grant", "error_description": "..."}``). We use the
    code to decide whether to delete the token: only ``invalid_grant`` means
    the token is genuinely dead. Everything else is transient.
    """
    args = getattr(err, "args", ())
    for arg in args:
        if isinstance(arg, dict):
            code = arg.get("error")
            if isinstance(code, str):
                return code
    return ""


def _write_token(token_p: Path, body: str) -> None:
    token_p.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        token_p.parent.chmod(0o700)
    fd = os.open(str(token_p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)


class GoogleClients:
    """gspread + Drive + Sheets API handles, instantiated once per run."""

    def __init__(self, creds: Credentials) -> None:
        from googleapiclient.discovery import build

        self.creds = creds
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._gspread: Any | None = None  # lazy: gspread auth is slow

    @property
    def gspread(self) -> Any:
        """Lazy gspread client; used by Phase 4 upsert helpers."""
        if self._gspread is None:
            import gspread

            self._gspread = gspread.authorize(self.creds)
        return self._gspread

    @classmethod
    def connect(cls, *, allow_interactive: bool = True) -> GoogleClients:
        """Construct clients via the OAuth flow. Fails fast under mock mode."""
        if secrets.is_mock():
            raise ConfigError(
                "GoogleClients.connect() called under MOCK_MODE; tests must "
                "inject a fake.",
                source=SOURCE_ID,
            )
        return cls(load_credentials(allow_interactive=allow_interactive))


class LazyGoogleClients:
    """Defers Google OAuth until something actually needs Drive/Sheets.

    Why: classify-only runs (where every email turns out to be ``neither`` /
    ``no_attachment`` / ``needs_manual_download``) never reach
    :func:`file_invoice` and therefore never need Google. Constructing
    :class:`GoogleClients` upfront caused the entire run to fail when the
    Google refresh token had expired — even though no Drive upload was
    actually attempted. With this proxy, the refresh-token-expired error
    only surfaces when an invoice/receipt is found AND we try to upload it.

    On first :class:`AuthExpiredError`, the proxy is poisoned: subsequent
    accesses re-raise the same exception without re-trying. This avoids
    spamming the OAuth endpoint with a doomed refresh per email; the user
    sees one ``needs_reauth`` error per emails-that-needed-Google rather
    than one per all-emails-in-the-run.

    Test/fake usage: pass an existing :class:`GoogleClients` instance via
    ``preconnected=`` and the proxy is a thin pass-through.
    """

    def __init__(
        self,
        *,
        allow_interactive: bool = False,
        preconnected: GoogleClients | None = None,
    ) -> None:
        self._allow_interactive = allow_interactive
        self._impl: GoogleClients | None = preconnected
        self._failed_with: AuthExpiredError | None = None
        # Double-checked locking around connect() so a 20-thread pipeline
        # only triggers one OAuth refresh — and on failure, every thread
        # raises the same AuthExpiredError instance instead of each making
        # its own doomed refresh request.
        self._connect_lock = threading.Lock()

    def _ensure(self) -> GoogleClients:
        impl = self._impl
        if impl is not None:
            return impl
        if self._failed_with is not None:
            raise self._failed_with
        with self._connect_lock:
            if self._impl is not None:
                return self._impl
            if self._failed_with is not None:
                raise self._failed_with
            try:
                self._impl = GoogleClients.connect(
                    allow_interactive=self._allow_interactive
                )
            except AuthExpiredError as err:
                self._failed_with = err
                raise
            return self._impl

    @property
    def is_connected(self) -> bool:
        """True if the OAuth flow has completed at least once for this proxy."""
        return self._impl is not None

    @property
    def creds(self) -> Credentials:
        return self._ensure().creds

    @property
    def drive(self) -> Any:
        return self._ensure().drive

    @property
    def sheets(self) -> Any:
        return self._ensure().sheets

    @property
    def gspread(self) -> Any:
        return self._ensure().gspread


@dataclass(frozen=True, slots=True)
class FiscalYearSheet:
    """Identifiers + link for a per-FY workbook."""

    fiscal_year: str
    spreadsheet_id: str
    drive_folder_id: str
    web_view_link: str


def create_fy_workbook(
    clients: GoogleClients,
    conn: sqlite3.Connection,
    fiscal_year: str,
) -> FiscalYearSheet:
    """Create (or return) the Drive folder + Sheets workbook for ``fiscal_year``.

    Idempotent: a second call for the same FY returns the existing record
    without touching Drive.
    """
    validate_fy_label(fiscal_year)
    existing = get_fy_sheet(conn, fiscal_year)
    if existing is not None:
        return existing

    root_id = ensure_drive_folder(clients, DRIVE_ROOT_NAME)
    fy_folder_id = ensure_drive_folder(clients, fiscal_year, parent_id=root_id)

    body = {
        "properties": {"title": f"Granite Accounts - {fiscal_year}"},
        "sheets": [{"properties": {"title": name}} for name in SHEET_TAB_NAMES],
    }
    ss = (
        clients.sheets.spreadsheets()
        .create(body=body, fields="spreadsheetId,spreadsheetUrl")
        .execute()
    )
    spreadsheet_id = str(ss["spreadsheetId"])
    web_view_link = str(
        ss.get(
            "spreadsheetUrl",
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
        )
    )

    # Move workbook into the FY folder. drive.file scope permits this since
    # we created the file itself.
    clients.drive.files().update(
        fileId=spreadsheet_id,
        addParents=fy_folder_id,
        removeParents="root",
        fields="id,parents",
    ).execute()

    _upsert_fy_row(conn, fiscal_year, spreadsheet_id, fy_folder_id)

    return FiscalYearSheet(
        fiscal_year=fiscal_year,
        spreadsheet_id=spreadsheet_id,
        drive_folder_id=fy_folder_id,
        web_view_link=web_view_link,
    )


def ensure_drive_folder(
    clients: GoogleClients,
    name: str,
    *,
    parent_id: str | None = None,
) -> str:
    """Return the Drive folder-id for ``name`` under ``parent_id``; create if missing.

    drive.file scope already restricts listing to files this app created, so
    this lookup can't leak unrelated folder names.
    """
    safe_name = _escape_drive_query(name)
    q = (
        f"mimeType='{_DRIVE_FOLDER_MIME}' "
        f"and name='{safe_name}' and trashed=false"
    )
    if parent_id:
        q += f" and '{_escape_drive_query(parent_id)}' in parents"
    listing = (
        clients.drive.files()
        .list(q=q, fields="files(id,name,parents)", pageSize=10)
        .execute()
    )
    files = listing.get("files", [])
    if files:
        return str(files[0]["id"])
    metadata: dict[str, Any] = {"name": name, "mimeType": _DRIVE_FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    created = clients.drive.files().create(body=metadata, fields="id").execute()
    return str(created["id"])


def get_fy_sheet(conn: sqlite3.Connection, fy: str) -> FiscalYearSheet | None:
    """Look up the workbook record for ``fy`` or return ``None``."""
    validate_fy_label(fy)
    row = conn.execute(
        "SELECT fiscal_year, spreadsheet_id, drive_folder_id "
        "FROM fiscal_year_sheets WHERE fiscal_year = ?",
        (fy,),
    ).fetchone()
    if row is None:
        return None
    return FiscalYearSheet(
        fiscal_year=row["fiscal_year"],
        spreadsheet_id=row["spreadsheet_id"],
        drive_folder_id=row["drive_folder_id"],
        web_view_link=(
            f"https://docs.google.com/spreadsheets/d/{row['spreadsheet_id']}"
        ),
    )


def _upsert_fy_row(
    conn: sqlite3.Connection,
    fiscal_year: str,
    spreadsheet_id: str,
    drive_folder_id: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fiscal_year_sheets
          (fiscal_year, spreadsheet_id, drive_folder_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (fiscal_year, spreadsheet_id, drive_folder_id, now_utc().isoformat()),
    )


def _escape_drive_query(s: str) -> str:
    """Escape a value for use inside a Drive ``q=`` single-quoted literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


__all__ = [
    "DRIVE_ROOT_NAME",
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
]
