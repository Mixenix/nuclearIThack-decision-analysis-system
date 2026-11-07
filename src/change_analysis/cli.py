"""Small CLI used by the README and by integration checks.

    python -m change_analysis.cli bootstrap          # schema + demo users + dataset
    python -m change_analysis.cli run <login>        # create and execute a full run
"""

from __future__ import annotations

import json
import sys
import time

from .config import load_settings
from .db.migrations import apply_migrations


def bootstrap() -> int:
    applied = apply_migrations()
    print(f"migrations applied: {applied or 'none (already up to date)'}")
    from .auth import service as auth
    from .services import datasets

    admin = auth.ensure_user("analyst", "analyst", "Аналитик")
    auth.ensure_user("analyst2", "analyst2", "Второй аналитик")
    dataset_id = datasets.ensure_delivered_dataset(admin)
    print(f"demo users: analyst/analyst, analyst2/analyst2")
    print(f"dataset registered: {dataset_id}")
    return 0


def run_once(login: str = "analyst", name: str = "Полный прогон") -> int:
    from .auth import service as auth
    from .db.connection import query
    from .services import configs, runs

    rows = query("SELECT id FROM ca.users WHERE login = %s", (login,))
    if not rows:
        print(f"user {login!r} does not exist — run bootstrap first")
        return 1
    owner = str(rows[0]["id"])
    datasets_list = [d for d in __import__(
        "change_analysis.services.datasets", fromlist=["list_datasets"]).list_datasets(owner)]
    if not datasets_list:
        print("no dataset registered — run bootstrap first")
        return 1
    selections = configs.default_selections(owner)
    selections["checks"] = [
        {"id": cid, "params": {}} for cid in (
            "attribute_new_loss", "stop_factor_disappeared", "target_model_changed",
            "external_score_drift", "ar_drop_by_product",
        )
    ]
    started = time.perf_counter()
    run_id = runs.create_run(owner, name, datasets_list[0]["id"], selections,
                             backend="cpu")
    print(f"run {run_id} queued; starting worker...")
    from .worker.main import main as worker_main

    worker_main(["--once"])
    elapsed = time.perf_counter() - started
    run = runs.get_run(owner, run_id)
    print(f"state={run['state']} elapsed={elapsed:.1f}s")
    print(json.dumps(run["summary"], indent=2, ensure_ascii=False))
    if run["error_message"]:
        print("error:", run["error_message"])
        for event in runs.run_events(owner, run_id, limit=50):
            print(f"  [{event['level']}] {event['message']}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    load_settings()
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "bootstrap":
        return bootstrap()
    if command == "run":
        return run_once(argv[2] if len(argv) > 2 else "analyst")
    print(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
