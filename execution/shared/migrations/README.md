# Migrations

Each `NNN_name.sql` file in this directory is applied exactly once per
database. The runner (`execution/shared/db.py::apply_migrations`) records a
SHA-256 of every file it applies into `schema_migrations`; on the next run
it re-hashes each file and **refuses to proceed if any byte has changed**.
That's an intentional security boundary, not a quirk — a tampered migration
is exactly the class of supply-chain bug we want to fail loudly.

## Hard rules

1. **Never edit a migration after it has been applied to any database.** Not
   the SQL, not the comments, not the trailing whitespace. Any byte change
   shifts the SHA-256 and breaks every existing database (production,
   developer laptops, CI fixtures).

2. **Add new behaviour as a new migration.** If migration 009 turned out to
   need a `CHECK` constraint, write `0NN_add_check_to_invoices.sql`. Same
   for renames, drops, index additions.

3. **Document rollback elsewhere.** This is the chicken-and-egg trap I
   walked into: adding a "ROLLBACK:" comment block to a deployed migration
   *is itself a checksum-changing edit*. Operator-facing rollback notes go
   in this README (below) or in a sibling `.md` file, never in the `.sql`.

4. **`tests/test_migrations_immutable.py` enforces rule 1.** It pins the
   SHA-256 of every committed migration. CI fails the moment anyone — human
   or agent — edits an applied file. The fix when that test fails is
   *always* to revert the file to the pinned hash, then write a new
   migration for whatever change was actually wanted.

## Rollback procedure (operator reference)

When a migration needs to be undone in production:

1. Stop the pipeline (`granite runs cancel <op>` or kill the dev server).
2. Run the inverse SQL **manually**, in `sqlite3 .state/pipeline.db`. SQLite
   ≥ 3.35 supports `ALTER TABLE … DROP COLUMN`; older versions need a
   table-rebuild via `CREATE … AS SELECT`, swap, recreate indexes.
3. `DELETE FROM schema_migrations WHERE version = '<version>';` — the runner
   skips files whose row exists, so this delete is mandatory before
   re-applying.

Per-migration notes:

- `005_add_needs_manual_download.sql`: adds `invoices.needs_manual_download`,
  `invoices.manual_download_url`. Reverse: drop both columns.
- `009_add_invoice_export_tracking.sql`: adds `invoices.last_exported_at`.
  Reverse: drop the column.
- `010_add_email_error_message.sql`: adds `emails.error_message`. Reverse:
  drop the column. Writes go through
  `execution.shared.error_message.prepare_error_message` which redacts
  Bearer tokens, signed URLs, emails and caps to 2000 chars; the
  application-layer cap is the only enforcement (SQLite ALTER TABLE can't
  add CHECK constraints).
- `011_add_email_manual_download_url.sql`: adds `emails.manual_download_url`.
  Migration 012 renames it to `source_invoice_url`; rolling back 011 means
  rolling back 012 first.
- `012_rename_email_manual_download_url.sql`: rename
  `emails.manual_download_url` → `emails.source_invoice_url`. Reverse:
  `ALTER TABLE emails RENAME COLUMN source_invoice_url TO manual_download_url;`
- `013_add_vendor_search_index.sql`: adds `idx_vendors_canonical_name_nocase`.
  Reverse: `DROP INDEX IF EXISTS idx_vendors_canonical_name_nocase;`
