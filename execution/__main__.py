"""Allow ``python -m execution`` to run the Typer CLI."""

from __future__ import annotations

from execution.cli import app

if __name__ == "__main__":
    app()
