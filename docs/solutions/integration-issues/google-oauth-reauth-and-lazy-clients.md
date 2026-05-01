---
title: Google OAuth invalid_grant — clear reauth path + lazy Google clients
category: integration-issues
date: 2026-05-01
tags: [google-oauth, refresh-token, drive, sheets, reauth, lazy-init, invoice-processing]
components: [execution/shared/sheet.py, execution/cli.py, web/src/app/api/auth/google/reauth, web/src/app/dashboard/needs-attention-card.tsx]
symptoms: [invalid-grant-toast, processing-fails-on-token-revoke, classify-only-runs-blocked-by-google]
---

# Google OAuth `invalid_grant` and "why does processing need Google?"

## Problem Summary

After the MS Graph fix, Sync Emails started succeeding — but **Process invoices** then surfaced a cryptic toast:

```
('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant',
 'error_description': 'Token has been expired or revoked.'})
```

Two underlying issues:

1. **The error itself was a Google OAuth refresh-token failure.** `creds.refresh(Request())` raised `RefreshError`, which the code didn't catch — the raw exception text reached the toast with no actionable next step.
2. **`Process invoices` failed entirely** even though only the *Drive upload* step needs Google. Emails that classify as `neither`/`no_attachment`/`needs_manual_download` never touch Google. Failing the whole run on Google auth blocked even the work that didn't depend on Google.

## Fixes

### 1. `RefreshError` → `AuthExpiredError` with clear next step + delete stale token

`execution/shared/sheet.py::load_credentials`:

```python
try:
    creds.refresh(Request())
except RefreshError as err:
    _delete_token(token_p)
    raise AuthExpiredError(
        "Google OAuth refresh failed: token has been expired or revoked",
        source="google",
        user_message=(
            "Google access has expired. Run `granite ops reauth google` "
            "from your terminal to re-authorise. (The browser-based OAuth "
            "flow can't run from the web UI — it needs a desktop browser.)"
        ),
        cause=err,
    ) from err
```

Why delete the token: a revoked refresh token will never refresh again. Leaving it on disk just means the next run repeats the same failure. Wiping it forces a clean InstalledAppFlow on the next interactive launch.

### 2. `granite ops reauth google` CLI command

Added to `execution/cli.py::ops_reauth`. Wipes the cached token, calls `load_credentials(allow_interactive=True)` which spins up `flow.run_local_server(port=0)` + opens the browser. Returns when the user completes consent (including 2FA if their account requires it). Mirrors the existing `ms365` and `monzo` reauth flows for consistency.

### 3. `LazyGoogleClients` proxy — defer auth until something actually needs Google

```python
class LazyGoogleClients:
    """Defers Google OAuth until something actually needs Drive/Sheets.

    Why: classify-only runs never reach file_invoice and therefore never
    need Google. Constructing GoogleClients upfront caused the entire run
    to fail when the refresh token had expired — even when no Drive upload
    was attempted. With this proxy, that error only surfaces when an
    invoice/receipt is found AND we try to upload it. Then the per-email
    error handler records `needs_reauth` and continues with the next email.

    On first AuthExpiredError, the proxy is poisoned: subsequent accesses
    re-raise the same exception without re-trying. Avoids spamming the
    OAuth endpoint with a doomed refresh per email — the user sees one
    `needs_reauth` per emails-that-needed-Google, not one per all emails.
    """
```

`ingest invoice process` now passes `LazyGoogleClients(allow_interactive=False)`. `allow_interactive=False` is critical when called from a spawned-subprocess context (the web UI's pipeline run) where there's no way for the OAuth `run_local_server` flow to interact with the user.

### 4. Web UI: re-auth banner + `Retry all` button on Needs Attention

- When **any** pending action carries `error_code = "needs_reauth"`, the Needs Attention card shows a red banner with a "Re-authenticate Google" button. Click triggers `POST /api/auth/google/reauth` which spawns `granite ops reauth google` — the user's default browser opens, they approve (with 2FA), and the spawned process exits. No need to drop into a terminal.
- A new "Retry all" button resets `processed_at`/`outcome`/`error_code` for everything currently in the list (via `POST /api/pipeline/retry-errors` → `granite ingest invoice retry-errors`), then immediately kicks off `processInvoices`. Useful after a fix lands (Google reauth, new URL extractor, etc.) to sweep the backlog without picking each email by hand.

### 5. Surface `error_code` in the dashboard

`getPendingActions` now SELECTs and returns `error_code`. The Needs Attention card renders a friendly label per known code (`needs_reauth → "Re-authentication required"`, `rate_limited → "Rate limited (will retry)"`, etc.) with an `aria-title`/`title` carrying the raw code for power users.

## Reusable Learnings

### a. Don't auth eagerly when you might not need it

Constructing API clients upfront feels tidy but blocks unrelated work when the auth fails. If a service is needed only on a subset of emails, **defer initialisation** behind a proxy. The poison-on-failure pattern prevents per-item re-tries from spamming the auth endpoint.

### b. Error toasts need next steps, not just status codes

The original `RefreshError` text was `('invalid_grant: Token has been expired or revoked.', {...})`. Accurate, useless. The replacement `AuthExpiredError` carries `user_message="Run \`granite ops reauth google\` from your terminal to re-authorise"` — same root cause, actionable next step.

### c. Conflict resolution for re-scanned items: rely on the outcome filter

User question: "If an email is in needs-attention and gets resolved on rescan, how do we handle it?"

Answer: the `getPendingActions` query is `WHERE outcome IN ('needs_manual_download', 'error', 'no_attachment') AND dismissed_at IS NULL`. When a rescan overwrites the outcome to `invoice` or `receipt`, the row drops out of the query results automatically — no explicit reconciliation step needed. Same in reverse: a previously-good email whose rescan errors will appear in the list. The dashboard is a derived view, not a state machine of its own.

### d. Browser OAuth from a spawned subprocess works (when both share a desktop)

The web dev server runs on the user's machine, and so does the spawned `granite` subprocess. The subprocess opens `flow.run_local_server` on a random port and `webbrowser.open(url)` — the user's default browser launches in the foreground. The user's session already controls the desktop, so this works fine. It would NOT work in a remote/headless deployment — for that we'd need a different flow (PKCE with manual code paste, or a custom redirect handler in the web app).

## Tests Added

- `test_load_credentials_refresh_error_raises_auth_expired_and_deletes_token`
- `test_load_credentials_no_token_non_interactive_raises_auth_expired`
- `TestLazyGoogleClients::test_does_not_connect_until_first_attribute_access`
- `TestLazyGoogleClients::test_first_access_calls_connect_then_caches`
- `TestLazyGoogleClients::test_failure_poisons_proxy_and_re_raises`
- `TestLazyGoogleClients::test_preconnected_bypasses_oauth`
- `test_ingest_invoice_retry_errors_clears_processed_state`
- `test_ingest_invoice_retry_errors_rejects_invalid_outcome`
- `test_ops_reauth_rejects_unknown_source` updated to assert `google` is now listed.

## Out of scope (intentional)

- **Headless reauth (no desktop browser).** Only meaningful if/when this app runs on a server. Today the dev server is local, so the InstalledAppFlow works.
- **Surfacing the OAuth-completion event back to the UI.** The current API is request/response: the user clicks the button, the browser opens, when they finish OAuth the request resolves. If the user cancels in the browser, the spawned process eventually times out — acceptable for a development surface.
- **Audit log of who reauth'd when.** Single-user app; not worth the table yet.
