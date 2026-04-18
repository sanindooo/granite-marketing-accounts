"""Structural sanity tests for the launchd plist + installer template.

We don't run launchctl in CI — these just assert the plist is
syntactically valid XML + carries the keys the README claims, and
that install.sh is a valid bash script with no obvious expansion
surprises.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

PLIST = Path("execution/ops/launchd/com.granite.accounts.plist")
INSTALL = Path("execution/ops/launchd/install.sh")


def test_plist_parses_as_valid_apple_plist():
    data = PLIST.read_bytes()
    doc = plistlib.loads(data)
    assert doc["Label"] == "com.granite.accounts"
    assert doc["WakeSystem"] is True
    assert doc["RunAtLoad"] is False
    assert doc["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    # ProgramArguments points at the wrapper script the installer writes.
    assert len(doc["ProgramArguments"]) == 1
    assert doc["ProgramArguments"][0].endswith("run.sh")


def test_plist_sets_log_paths_under_library_logs():
    doc = plistlib.loads(PLIST.read_bytes())
    assert "Library/Logs/granite-accounts" in doc["StandardOutPath"]
    assert "Library/Logs/granite-accounts" in doc["StandardErrorPath"]


def test_install_script_is_executable_bash():
    assert INSTALL.exists()
    assert (INSTALL.stat().st_mode & 0o111) != 0  # owner executable


def test_install_script_has_set_euo_pipefail():
    text = INSTALL.read_text()
    assert "set -euo pipefail" in text
    assert "launchctl bootstrap" in text
    assert "launchctl bootout" in text
    assert "com.granite.accounts" in text


@pytest.mark.skipif(
    not Path("/bin/bash").exists(), reason="bash not available on this platform"
)
def test_install_script_passes_bash_n_syntax_check():
    result = subprocess.run(
        ["/bin/bash", "-n", str(INSTALL)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
