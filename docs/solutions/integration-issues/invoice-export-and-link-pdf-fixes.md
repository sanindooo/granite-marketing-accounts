---
title: Invoice export validator, downloaded indicator, vendor search hang, link-only PDF extraction
category: integration-issues
date: 2026-05-01
tags: [zod, validation, sha256, server-actions, better-sqlite3, html-extraction, stripe, archiver, layer-separation]
components: [web/src/app/api/download/route.ts, web/src/lib/queries/invoices.ts, web/src/app/invoices/invoice-list.tsx, execution/invoice/processor.py]
symptoms: [download-400-on-every-id, no-exported-indicator, vendor-search-hangs-then-fails, webflow-link-only-classified-no-attachment]
---

# Invoice export, downloaded indicator, vendor search, and link-to-PDF extraction

## Problem Summary

Four user-reported bugs blocking the invoices page after Phases 1–5 shipped:

1. **`/api/download` returned 400 for every selection.** Zod validator was `z.string().uuid()` but `invoice_id` is `sha256(msg_id||idx)[:16]` — a 16-char hex, not a UUID.
2. **No way to see which invoices were already exported.** Schema had no "downloaded" column; UI had no badge or filter.
3. **Searching vendor "Webflow" hung and failed silently minutes later.** Diagnosed in v1 plan as RSC payload size — wrong. Real cause: `SELECT i.*` shipped the `confidence_json` blob over the wire on every keystroke, and better-sqlite3 (sync) blocked the Node event loop while un-debounced fetches stacked up.
4. **Webflow-style "click here for PDF" emails classified as `no_attachment`.** `fetch_message_body` defaulted to plaintext, so HTML anchor `href`s never reached the URL-extraction regex. Webflow bills via Stripe; the `invoice.stripe.com/.../pdf` pattern was already in the regex allowlist — we just never fed HTML to the regex.

## Reusable Learnings

### 1. Validator shape must match producer shape

`zod` validators look right until they don't. The download endpoint had used `z.string().uuid()` since Phase 1 because invoice IDs *looked* UUID-shaped to the writer. They aren't — they're `hashlib.sha256(...).hexdigest()[:16]` per `execution/invoice/filer.py:_invoice_id`.

**Rule:** any `zod` validator that constrains an internal ID's format must cite the file/function that produces it in a comment. If the producer changes shape, the validator regresses immediately rather than silently allowing through (or rejecting) the new shape.

```ts
// invoice_id is sha256(msg_id||idx)[:16] (see execution/invoice/filer.py:_invoice_id),
// not a UUID. Validate the actual hex shape so a typo regresses loudly.
const INVOICE_ID = z.string().regex(/^[a-f0-9]{16}$/, "...");
```

Also: when `safeParse` fails, **always log the flattened zod error** and return `issues` in the 400 body. The original handler returned a generic "Invalid invoice IDs" — that's exactly the message that hides this class of bug.

### 2. Sync better-sqlite3 + un-debounced Server Actions = queue backup that surfaces minutes later

The user's "search hangs and fails minutes later" symptom looked like an RSC payload limit. It wasn't (Next.js `bodySizeLimit` only applies to Server Action *request* bodies, not responses — verified against `web/node_modules/next/dist/docs/01-app/.../serverActions.md`). The actual chain:

- `useEffect` depended on the entire `filters` object → fired on every render where the object identity changed (which is every nuqs URL update)
- `useDebouncedCallback` debounced the URL update (300 ms), but each URL update still triggered the effect that called `fetchInvoices`
- `fetchInvoices` is a Server Action that hits a singleton better-sqlite3 connection. better-sqlite3 is a **sync C++ binding** — every query blocks the Node main thread for its full duration.
- 7 keystrokes in "Webflow" = 7 serialised queries on the same blocked thread. With `SELECT i.*` shipping the `confidence_json` blob (potentially 100s of KB per row × 500 rows), each query took long enough that the queue never drained before the browser's idle-timeout (~5 min) fired and surfaced a generic toast.

**Two-part fix:**
- **Server-side:** `LIST_COLUMNS` constant. `getInvoices`/`getInvoicesByIds`/`getExceptionInvoices` project only display fields. `confidence_json` is detail-page only. Split `InvoiceRow` into `InvoiceListRow` (trimmed) + `InvoiceRow extends InvoiceListRow` (full) so TypeScript catches future regressions.
- **Client-side:** destructure individual primitive fields out of the `filters` object as effect dependencies, and use a `cancelled` flag in the cleanup function to drop stale results. **Important:** `AbortController.abort()` does NOT cancel server-side Server Action execution (vercel/next.js#81418, discussion #54516) — the effect still completes server-side. The cancelled flag only stops the client from updating state with stale results.

If the queue-backup symptom returns after this fix, the next escalation is to convert the search path to a Route Handler (which DOES expose `request.signal` to the server) or move better-sqlite3 onto a worker thread.

### 3. Plaintext-body default hides HTML anchor URLs

The MS365 adapter's `fetch_message_body(prefer_html=False)` returns the short `bodyPreview` for HTML-only emails. The processor used the default. Webflow's Stripe-generated receipt is HTML-only — its "PDF" anchor `href="https://invoice.stripe.com/.../pdf"` never reached the URL-extraction regex despite the Stripe URL pattern being in the allowlist.

**Fix:** call `fetch_message_body_both` once per email; pass HTML to the URL extractor (which already has the right regex) and pass *stripped* HTML to the classifier. Do **not** pass raw HTML to the classifier — burns tokens on `<table>` markup, inline styles, and tracking pixels, and degrades classification accuracy on the very vendor we're trying to support.

```python
html_body, text_body = adapter.fetch_message_body_both(msg_id)
classifier_body = text_body or _html_to_text(html_body)  # strip, never raw HTML

if not pdf_attachments:
    pdf_bytes, _ = _try_fetch_pdf_from_body(
        text_body=text_body, html_body=html_body, http_client=http_client
    )
```

`_html_to_text` uses stdlib `html.parser` (non-validating, no DTD/entity expansion → XXE-safe by construction). Do not swap to `lxml` without `no_network=True, resolve_entities=False, load_dtd=False`.

`_try_fetch_pdf_from_body` was rewritten to use `finditer` (not `search`) across both bodies — HTML first — so a vendor-specific URL deeper in the document still wins over the first-matched generic `.pdf` URL.

### 4. Stream lifecycle: mark per-entry on `end`, not on `archive.finalize()`

The original download route called `markInvoicesExported(allIds)` from `Promise.all(downloadPromises).then(() => archive.finalize())`. That's wrong:
- `archive.append(stream, ...)` is **non-blocking** — it queues the stream
- `archive.finalize()` resolves when archiver has written into its buffer, not when the client received bytes
- A client disconnect mid-zip would over-credit invoices

**Fix:** mark per-entry on the *inner stream's* `end` event, then call `markInvoicesExported(exportedIds)` from `archive.on("end", ...)` gated on `!request.signal.aborted`. Wire `request.signal.addEventListener("abort", () => archive.abort())` to actually stop work on disconnect.

Also replaced `PassThrough → ReadableStream` with `Readable.toWeb(archive) as ReadableStream` — preserves backpressure end-to-end (archiver issues #613, #571, #321 documented hangs and swallowed read errors with the PassThrough bridge).

### 5. `lib/queries/` is read-only — writes belong in `lib/actions/`

Per `docs/solutions/architecture-issues/layer-separation-enforcement.md`, `lib/queries/` is read-only by convention and grep-gated in CI. The new `markInvoicesExported` write lives in `web/src/lib/actions/exports.ts`, not in `lib/queries/invoices.ts`. Future writes follow the same rule.

## Things explicitly NOT done (deferred)

These were in the v1 plan and intentionally cut to keep the slice tight:

- **`export_count` column.** `last_exported_at IS NOT NULL` answers both "is exported?" and "when?". `count` was speculative for a future "stale export" warning.
- **`idx_invoices_exported` partial index.** Premature without profiling on a few-thousand-row table.
- **Anchor-text scoring + `INVOICE_LIKELY_HOSTS` allowlist** for non-Stripe link-only vendors. Speculative defense; anchor text is fully attacker-controllable, so taking on that risk surface is deferred until a second real vendor justifies it.
- **Persisting NEEDS_MANUAL_DOWNLOAD URLs into `invoices.manual_download_url`.** The existing dashboard exceptions UI reads from `emails`, not `invoices` — wiring this end-to-end requires a deeper change. The user's reported case (Webflow → Stripe) does not hit this path because the Stripe URL fetches successfully via the existing pattern allowlist.

## Pre-existing risks flagged but not addressed in this PR

- `/api/download` has no auth gate. `invoice_id` has only ~64 bits of entropy and is not secret. Not introduced by this PR but flagged for follow-up.
- DNS-rebinding TOCTOU between `validate_url` and httpx connect (pre-existing). The HTML-body fetch widens the volume of attacker-supplied URLs being fetched, so the latent risk weight rises slightly.

## References

- Plan: `docs/plans/2026-05-01-001-fix-invoice-export-and-link-pdf-bugs-plan.md`
- Layer separation: `docs/solutions/architecture-issues/layer-separation-enforcement.md`
- archiver issues: [#613](https://github.com/archiverjs/node-archiver/issues/613), [#571](https://github.com/archiverjs/node-archiver/issues/571), [#321](https://github.com/archiverjs/node-archiver/issues/321)
- Server Actions abort behavior: [vercel/next.js#81418](https://github.com/vercel/next.js/issues/81418), [discussion #54516](https://github.com/vercel/next.js/discussions/54516)
