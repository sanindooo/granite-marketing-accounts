# Credential Renewal

Handle OAuth token expiry and re-authentication for all data sources.

## Overview

Each source has different token expiry characteristics:

| Source | Expiry Trigger | Window |
|--------|----------------|--------|
| MS365 | 90 days inactive | Warning at 60d, error at 90d |
| Google | 6 months inactive (Testing) or 7 days (Testing mode) | Check consent screen status |
| Monzo | 90 days hard cliff | **Data loss** after 90d — reauth early |
| Wise | Never (personal token) | SCA key rotation optional |

## Checking Reauth Status

```bash
granite ops healthcheck
```

Look for `reauth_required` entries:
```json
{
  "reauth_required": [
    {"source": "monzo", "days_until_expiry": 12, "message": "Re-auth before 2026-05-15 to avoid data loss"}
  ]
}
```

## MS365 Re-Authentication

### When Needed

- 401 error during email fetch
- 90 days since last token refresh
- Changed password or revoked app consent

### Process

```bash
granite ops reauth ms365
```

Output:
```
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code ABCD-EFGH to authenticate.
```

1. Open the URL in a browser
2. Enter the displayed code
3. Sign in with your Microsoft account
4. Return to terminal — reauth completes automatically

### Verification

```bash
granite ingest email ms365 --initial
```

## Google Re-Authentication

### When Needed

- OAuth consent screen in Testing mode (7-day token expiry)
- 6 months of inactivity
- Revoked access in Google Account settings
- Pipeline emits `needs_reauth` and the dashboard's red "Re-authenticate
  Google" banner appears on Needs Attention

### Process

```bash
granite ops reauth google
```

A browser window opens. Sign in and re-grant permissions. The dashboard's
"Re-authenticate Google" button calls the same command.

`load_credentials` only deletes `.state/token.json` when Google explicitly
returns `invalid_grant` (token revoked / expired). Transient `RefreshError`s
(network blips, 5xx from `oauth2.googleapis.com`) preserve the token —
retry the run before reauthing.

`granite ops setup-sheets` is the legacy bootstrap command for the initial
OAuth flow; once a token exists, prefer `reauth google` for re-auth.

### Production Mode (Recommended)

To avoid 7-day expiry:
1. Google Cloud Console → OAuth consent screen
2. Publishing status → **Production**
3. Complete verification if prompted

## Monzo Re-Authentication

### Critical: 90-Day Data Loss Cliff

Monzo's Strong Customer Authentication (SCA) expires after 90 days. Once expired:
- Access to transactions older than 90 days is **permanently lost** via API
- You cannot retroactively fetch historical data

### Warning System

The healthcheck warns at 60 days:
```json
{
  "warnings": ["Monzo: reauth in 30 days to preserve historical access"]
}
```

At 90+ days:
```json
{
  "errors": ["Monzo: historical data before 2026-01-15 is now unreachable via API"]
}
```

### Re-Authentication Process

```bash
granite ops reauth monzo
```

1. Browser opens to Monzo OAuth consent
2. Approve access in the Monzo app
3. Return to terminal within 5 minutes (SCA window)

During the 5-minute SCA window, the adapter pulls **all available history** into the database.

### Manual CSV Fallback

If you missed the 90-day window:
1. Open Monzo app → Account → Statements
2. Export CSV for each month in the gap period
3. Place files in `.tmp/monzo_csv_import/`
4. Run: `granite ingest bank monzo-csv` (when implemented)

## Wise Re-Authentication

### SCA Signing Key Setup

Wise uses a personal API token with RSA key signing for Strong Customer Authentication.

1. Generate RSA keypair:
   ```bash
   openssl genrsa -out wise_private.pem 2048
   openssl rsa -in wise_private.pem -pubout -out wise_public.pem
   ```

2. Upload public key to Wise:
   - Wise Developer Portal → Your profile → SCA
   - Add the public key content

3. Store private key in Keychain:
   ```bash
   security add-generic-password -a "granite-accounts" \
     -s "granite-accounts/wise/sca_private_key" \
     -w "$(cat wise_private.pem)"
   ```

4. Store API token:
   ```bash
   security add-generic-password -a "granite-accounts" \
     -s "granite-accounts/wise/api_token" \
     -w "YOUR_PERSONAL_TOKEN"
   ```

### Key Rotation (Optional)

To rotate the SCA key:
1. Generate new keypair
2. Add new public key in Wise portal
3. Update Keychain with new private key
4. Remove old public key from Wise portal

Wise tokens themselves don't expire — only rotate if compromised.

## Automated Notifications

The pipeline sends email notifications for reauth events:

| Event | When | Action |
|-------|------|--------|
| First failure | Immediately | Email with reauth command |
| Ongoing failure | Every 7 days | Reminder email |
| Monzo 60-day warning | 60 days since reauth | Pre-emptive warning |
| Monzo 90-day cliff | 90 days | Urgent: data loss imminent |

Configure notifications in `.state/account_config.json`:
```json
{
  "notifications": {
    "email": "you@example.com",
    "enabled": true
  }
}
```

## Circuit Breaker

To avoid spamming failed auth attempts:

| Consecutive Failures | Retry Interval |
|---------------------|----------------|
| 1-3 | Every run |
| 4-10 | Every 4 hours |
| 11+ | Daily |

Check circuit breaker status:
```bash
sqlite3 .state/pipeline.db "SELECT * FROM reauth_required"
```

Clear after successful reauth:
```bash
granite ops reauth <source>
# Automatically clears reauth_required row on success
```

## Troubleshooting

### MS365: "AADSTS50173"

Password changed since last auth. Run `granite ops reauth ms365`.

### Google: "Token has been expired or revoked"

Run `granite ops setup-sheets`. If recurring, check that the OAuth consent screen is in Production mode.

### Monzo: "forbidden" after reauth

The SCA window (5 minutes) may have closed. Re-run:
```bash
granite ops reauth monzo
# Approve quickly in the app when prompted
```

### Wise: "403 SCA required"

The private key doesn't match the public key registered with Wise. Verify:
```bash
# Extract public key from private
openssl rsa -in wise_private.pem -pubout

# Compare with what's in Wise portal
```
