"""Helpers for sanitising values written to ``emails.error_message``.

Two concerns, one chokepoint:

1. Length cap. SQLite TEXT has no native limit; a 5MB stack trace landing in
   ``error_message`` would bloat the DB and slow ``getPendingActions``.
2. Sensitive substrings. Error messages from upstream services routinely
   carry Bearer tokens, signed URLs (Stripe's hosted-invoice URLs ARE the
   access secret), email addresses, and OAuth client IDs. Persisted
   indefinitely + screenshotted into support tickets — keep them out of the
   database, even though the file already has 0o600 permissions.

Every write to ``emails.error_message`` should go through
:func:`prepare_error_message`. The current writers are:

- ``execution/invoice/processor.py::_update_email_outcome``

Add to that list if a new write site appears, or invoke the helper directly.
A SQL-layer CHECK constraint would be the strongest guarantee, but SQLite
doesn't support adding CHECK to an existing column without a table rebuild —
keep this enforced at the application layer with the helper as the single
source of truth.
"""

from __future__ import annotations

import re
from typing import Final

ERROR_MESSAGE_CAP: Final[int] = 2000

_REDACTORS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Bearer tokens in any header / log dump.
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]+"), "Bearer <redacted>"),
    # Stripe hosted-invoice URLs whose path token IS the access secret.
    (
        re.compile(r"(invoice|pay)\.stripe\.com/i/[A-Za-z0-9_]+"),
        r"\1.stripe.com/i/<token>",
    ),
    # Common signed-URL query params; cover the names attackers and SDKs
    # use most often. The replacement preserves the leading ``?`` or ``&``
    # so the URL still parses if a future reader runs it through urlparse.
    (
        re.compile(
            r"([?&])(token|signature|sig|access_token|refresh_token|key|auth|api_key)=[^&\s]+",
            re.IGNORECASE,
        ),
        r"\1\2=<redacted>",
    ),
    # Email addresses anywhere in the message — the From: / To: headers
    # should never leak into a persisted error.
    (
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        "<email>",
    ),
)


def redact_error_message(text: str) -> str:
    """Strip secrets from ``text`` before persistence.

    Pure function. Order matters — the Stripe-URL pattern runs before the
    generic signed-URL pattern so it produces a more legible replacement.
    Adds a known-good substitution per pattern; the redaction is lossy on
    purpose so a future reader can't reverse-engineer the secret.
    """
    out = text
    for pat, repl in _REDACTORS:
        out = pat.sub(repl, out)
    return out


def prepare_error_message(text: str | None) -> str | None:
    """Apply redaction + length cap. ``None`` passes through unchanged.

    Centralises both transforms so the call site only has to remember one
    function. Use this everywhere ``emails.error_message`` is written.
    """
    if text is None:
        return None
    redacted = redact_error_message(text)
    return redacted[:ERROR_MESSAGE_CAP]


__all__ = [
    "ERROR_MESSAGE_CAP",
    "prepare_error_message",
    "redact_error_message",
]
