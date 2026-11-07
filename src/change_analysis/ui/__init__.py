"""Web UI package: registers every page."""

from __future__ import annotations

from . import (  # noqa: F401 - imported for the @ui.page side effects
    pages_config,
    pages_explore,
    pages_report,
    pages_run,
    pages_runs,
    pages_workspace,
)


def register() -> None:
    """Importing the modules above registers their routes."""
    return None
