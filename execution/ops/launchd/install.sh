#!/usr/bin/env bash
# Install (or reinstall) the Granite Accounts scheduled-pipeline launchd agent.
#
# Usage:  ./install.sh [path/to/granite-repo]
#
# Steps:
# 1. Interpolate ${HOME} into the plist template and copy to
#    ~/Library/LaunchAgents/com.granite.accounts.plist.
# 2. Create ~/Library/Application Support/granite-accounts/run.sh — a tiny
#    wrapper that activates the repo's .venv and runs healthcheck +
#    reconcile run. Keeping this out of the plist means you can tweak the
#    pipeline invocation without reloading launchd.
# 3. Ensure ~/Library/Logs/granite-accounts/ exists.
# 4. Bootstrap the agent via launchctl.
#
# Re-running is idempotent — bootout is attempted before bootstrap.

set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "$0")/../../.." && pwd)}"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.granite.accounts.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.granite.accounts.plist"

APP_SUPPORT="$HOME/Library/Application Support/granite-accounts"
LOGS="$HOME/Library/Logs/granite-accounts"
WRAPPER="$APP_SUPPORT/run.sh"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$APP_SUPPORT"
mkdir -p "$LOGS"

# 1. Interpolate ${HOME} + write plist.
sed "s|\${HOME}|$HOME|g" "$PLIST_SRC" > "$PLIST_DEST"

# 2. Wrapper script — activate venv, run healthcheck, then pipeline.
cat > "$WRAPPER" <<WRAPPER_EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
# shellcheck source=/dev/null
source .venv/bin/activate
timestamp=\$(date "+%Y-%m-%dT%H:%M:%S%z")
echo "[\$timestamp] healthcheck starting"
if ! granite ops healthcheck; then
    echo "[\$timestamp] healthcheck failed — aborting pipeline"
    exit 1
fi
echo "[\$timestamp] healthcheck ok; running pipeline"
granite reconcile run
echo "[\$timestamp] pipeline finished"
WRAPPER_EOF
chmod +x "$WRAPPER"

# 3. Bootstrap the agent — bootout first to allow reinstall.
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM" "$PLIST_DEST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DEST"

echo "Installed launchd agent at $PLIST_DEST"
echo "Wrapper script at $WRAPPER"
echo "Logs stream to $LOGS/run.log"
echo "Trigger manually:  launchctl kickstart -k gui/$UID_NUM/com.granite.accounts"
