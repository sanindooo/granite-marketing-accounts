"""Pin every migration's SHA-256 so a stray edit fails CI, not the operator.

The migration runner re-hashes each file on every startup and refuses to
proceed if the hash doesn't match what's stored in ``schema_migrations``.
That's the right safety boundary — but it fails *at runtime*, on the first
machine that tries to apply migrations after the edit. By that point the
pipeline is already wedged.

This test moves the failure forward to CI: any byte change to a committed
migration file (SQL, comments, whitespace, anything) trips the pin and the
PR can't merge. The fix when this test fails is *always* one of:

1. Revert the file. Whatever change you wanted, write a NEW migration for it.
2. If you genuinely intended to edit a not-yet-applied migration (rare —
   only legal before the file has shipped to any developer or production
   database), update the corresponding entry in ``EXPECTED_HASHES`` in the
   same commit and explain why in the message.

NEVER do option 2 for a migration that already has a row in
``schema_migrations`` anywhere — that breaks every existing database.

See ``execution/shared/migrations/README.md`` for the operator-facing
rollback procedure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent
    / "execution"
    / "shared"
    / "migrations"
)

# Pinned SHA-256 of each committed migration. To regenerate after a
# legitimate edit (see docstring for when that's allowed):
#   for f in execution/shared/migrations/*.sql; do
#       printf '    "%s": "%s",\n' \
#           "$(basename "$f" .sql)" "$(sha256sum "$f" | awk '{print $1}')"
#   done
EXPECTED_HASHES: dict[str, str] = {
    "001_init": "c4d18a6d57052471fbde76f322605b7ac57ad634e48839626912e35d98791634",
    "002_add_unprocessed_index": "f0ff6efa77b7a6b16ffc8c6425bad4c6524102b0ea2db9638e5866f040aafb30",
    "002_invoice_fx_columns": "1bebeb91d3db1fa5f9261e5e903debf7922b8255d883b6fa6bca9bd0a4d0eb90",
    "003_add_web_indexes": "edf4a2491a802ee4cfa2a331cb306f1accae98398de08685622ca6c0a127e35b",
    "004_add_run_operation": "161702f85b9f2b8fdf28ff9bab08dcc0e6b0355e4b62cf435a86c86d643e4878",
    "005_add_needs_manual_download": "3a42a5ada2e39d19df20792a6728c743c861f03958eb2f91f7115bc4bb28fe94",
    "006_add_email_dismissal": "d965e3314239ad0dab65639574abeec10588192600e4b81993b8d7f23d339e09",
    "007_add_performance_indexes": "dd43f55571c48b864ebb698f737d2185c440f1c122e04800225c660a92e0afbb",
    "008_add_blocked_domains": "42edc29cc32e14da33763d5d0ae729be58c71b24dc04c9a945b051509cecbac3",
    "009_add_invoice_export_tracking": "a3c66988d16d0bd4c8a154af06da50cc43d4d587b76d18dad1d88d5cc302bd69",
    "010_add_email_error_message": "2a628cc9fc5169f5bea074bcf0894df1832245a718f7cd83a30d7c6874548de7",
    "011_add_email_manual_download_url": "36cf2cfa4d3fd4404b8ad945d726420e9fffd00c10a87a7544ff32688fb6d902",
    "012_rename_email_manual_download_url": "148250982e59719bb9e1ffdb85cbaa19fc96162bb478aaaad311d21df1f4f017",
    "013_add_vendor_search_index": "62b9ff82f1ae6b78ad9d16e20568653d4c62d05fc8ebaeb5be5a74bd529a3ea0",
}


def _files_on_disk() -> dict[str, Path]:
    return {p.stem: p for p in sorted(MIGRATIONS_DIR.glob("*.sql"))}


def test_no_committed_migration_has_been_edited() -> None:
    """Any byte change to a committed migration breaks live databases."""
    on_disk = _files_on_disk()
    drift: list[str] = []
    for version, expected in EXPECTED_HASHES.items():
        path = on_disk.get(version)
        if path is None:
            drift.append(f"{version}: file is pinned but missing on disk")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            drift.append(
                f"{version}: SHA-256 changed\n"
                f"    expected {expected}\n"
                f"    got      {actual}\n"
                f"    path     {path}"
            )
    if drift:
        msg = (
            "Committed migrations must be byte-stable. See "
            "execution/shared/migrations/README.md and the docstring of this "
            "test for what to do.\n\n"
            + "\n\n".join(drift)
        )
        pytest.fail(msg)


def test_no_new_migration_landed_unpinned() -> None:
    """Adding a migration without pinning it is a silent regression risk."""
    on_disk = _files_on_disk()
    unpinned = sorted(set(on_disk) - set(EXPECTED_HASHES))
    if unpinned:
        pytest.fail(
            "New migration(s) committed without a pinned hash:\n  "
            + "\n  ".join(unpinned)
            + "\n\nAdd them to EXPECTED_HASHES in this file. The regen "
            "command is in the module docstring."
        )
