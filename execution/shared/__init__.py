"""Shared helpers used by every adapter and pipeline stage.

Modules in this package are the only place the rest of the codebase is
allowed to call the outside world: time, money, secrets, the database,
the HTTP client factory, and the Claude/Sheets API wrappers. Mypy is
strict here.
"""
