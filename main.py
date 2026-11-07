"""Web application entry point.

    python main.py                # 127.0.0.1:8080 by default (APP_HOST / APP_PORT)

The worker is a *separate* process (``scripts/worker.sh``): the web process must
stay responsive while a run is computing (PLAN.md §5, §14).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from nicegui import ui  # noqa: E402

from change_analysis.config import load_settings  # noqa: E402
from change_analysis.db.migrations import apply_migrations  # noqa: E402
from change_analysis.ui import register  # noqa: E402

settings = load_settings()
apply_migrations()
register()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host=settings.host,
        port=settings.port,
        title="Graeae · Система анализа изменений СПР",
        storage_secret=settings.storage_secret,
        show=False,
        reload=False,
        favicon="🔍",
    )
