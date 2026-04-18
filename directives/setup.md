# Initial Setup

Configure credentials and initialize the accounting pipeline.

## Overview

The pipeline requires credentials for:
1. **MS365** — Email inbox access (delegated auth via device flow)
2. **Google** — Drive storage + Sheets output (OAuth consent flow)
3. **Claude** — AI classification and extraction (API key)
4. **Banks** — Wise (API token + SCA), Monzo (OAuth), Amex (CSV drop)

All secrets are stored in macOS Keychain under the `granite-accounts` namespace.

## Step 1: Database Initialization

```bash
granite db migrate
```

Creates `.state/pipeline.db` with all required tables.

## Step 2: Claude API Key

Store your Anthropic API key:
```bash
security add-generic-password -a "granite-accounts" -s "granite-accounts/anthropic/api_key" -w "sk-ant-..."
```

Verify:
```bash
granite ops smoke-claude
```

Expected output:
```json
{"model": "claude-haiku-4-5", "input_tokens": 42, "output_tokens": 8, "cost_gbp": "0.0001"}
```

## Step 3: MS365 Email Access

### Register an Entra (Azure AD) App

1. Go to [Entra Portal](https://entra.microsoft.com) → App registrations → New registration
2. Name: `Granite Accounts`
3. Supported account types: Single tenant (your organization only)
4. Redirect URI: `https://login.microsoftonline.com/common/oauth2/nativeclient` (Public client/native)
5. Note the **Application (client) ID** and **Directory (tenant) ID**

### Configure API Permissions

In the app registration:
1. API permissions → Add permission → Microsoft Graph → Delegated
2. Add: `Mail.Read`, `offline_access`
3. Grant admin consent (if required by your org)

### Store Credentials

```bash
# Client ID
security add-generic-password -a "granite-accounts" -s "granite-accounts/ms365/client_id" -w "YOUR_CLIENT_ID"

# Tenant ID (for single-tenant authority)
security add-generic-password -a "granite-accounts" -s "granite-accounts/ms365/tenant_id" -w "YOUR_TENANT_ID"
```

### Authenticate

```bash
granite ops reauth ms365
```

Follow the device flow prompt — open the URL in a browser, enter the code, and sign in with your Microsoft account.

## Step 4: Google Drive + Sheets

### Create OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project (or select existing)
3. Enable APIs: Google Drive API, Google Sheets API
4. Credentials → Create credentials → OAuth client ID
5. Application type: Desktop app
6. Download the JSON file

### Store Credentials

Save the downloaded JSON as `credentials.json` in the project root:
```bash
mv ~/Downloads/client_secret_*.json ./credentials.json
```

### Authenticate

```bash
granite ops setup-sheets
```

A browser window opens. Sign in with your Google account and grant access. The refresh token is saved to `token.json`.

### Production Mode (Recommended)

For refresh tokens that don't expire after 7 days:
1. In Google Cloud Console → OAuth consent screen
2. Set publishing status to **Production**
3. May require verification for sensitive scopes

## Step 5: Bank Account Configuration

Create `.state/account_config.json`:

```json
{
  "accounts": {
    "amex": {
      "designation": "business",
      "drop_folder": "~/Downloads/Amex"
    },
    "wise": {
      "designation": "business",
      "profile_id": "YOUR_WISE_PROFILE_ID"
    },
    "monzo": {
      "designation": "personal",
      "default_business": false
    }
  }
}
```

### Account Designations

- **business**: Transactions auto-tagged as business expenses
- **personal**: Transactions require manual business flag in the sheet

### Wise Setup

See `directives/reauth.md` for Wise SCA signing key setup.

### Monzo Setup

```bash
granite ops reauth monzo
```

Opens browser for OAuth consent. **Important:** Re-auth every 90 days or lose access to historical data.

### Amex Setup

No API access. Download CSVs monthly and place in the drop folder.

## Step 6: Create Fiscal Year Workbook

```bash
granite output create-fy FY-2026-27
```

Creates:
- Google Drive folder: `Accounts/FY-2026-27/`
- Google Sheets workbook with tabs: Run Status, Expenses, Invoices, Transactions, Exceptions, Sales

## Verification

Run the full healthcheck:
```bash
granite ops healthcheck
```

Expected output (all green):
```json
{
  "healthy": true,
  "checks": {
    "keychain_backend": "ok",
    "db_openable": "ok",
    "fiscal_year": "FY-2026-27",
    "ms365_token": "ok",
    "google_token": "ok",
    "claude_api": "ok"
  }
}
```

## Directory Structure After Setup

```
granite-marketing-accounts/
├── .state/
│   ├── pipeline.db          # SQLite database
│   └── account_config.json  # Bank account designations
├── .tmp/                    # Intermediate files (gitignored)
├── credentials.json         # Google OAuth client (gitignored)
├── token.json              # Google refresh token (gitignored)
└── config/
    └── vendor_categories.json  # Category overrides (optional)
```

## Changing Account Designations Mid-Year

If an account changes from personal to business (or vice versa):

```bash
granite ops retag-account amex --from personal --to business --effective-from 2026-09-01
```

This:
1. Appends to `account_designation_history` in the config
2. Re-derives `is_business` for affected reconciliation rows
3. Updates the sheet on next run

## Troubleshooting

### Keychain Access Denied

Grant Terminal (or your IDE) access in System Preferences → Security & Privacy → Privacy → Keychain Access.

### MS365 "AADSTS700016"

The app registration is misconfigured. Verify:
- Single tenant matches your tenant ID
- Redirect URI is the native client URL

### Google "Access blocked"

The OAuth consent screen is in Testing mode with a user limit. Add your email to the test users list, or publish to Production.
