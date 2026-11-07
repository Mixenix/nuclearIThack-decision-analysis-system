"""Small CLI used by the README and by integration checks.

    python -m change_analysis.cli bootstrap          # schema + demo users + dataset
    python -m change_analysis.cli run <login>        # create and execute a full run
    python -m change_analysis.cli useradd <login> [password] [--name ФИО]
    python -m change_analysis.cli users              # список пользователей
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


def useradd(login: str, password: str = "", display_name: str = "") -> int:
    """Add one user. Without a password a readable random one is generated.

    Нужно, чтобы дать доступ коллеге: у каждого пользователя свои прогоны,
    конфигурации и рабочая область, чужие данные ему не видны.
    """
    import secrets

    from .auth import service as auth

    if not login or not login.strip():
        print("логин не может быть пустым")
        return 1
    login = login.strip()
    password = password or secrets.token_urlsafe(9)
    user_id = auth.create_user(login, password, display_name or login)
    print(f"пользователь создан: {login} / {password}")
    print(f"id: {user_id}")
    return 0


def users() -> int:
    from .db.connection import query

    rows = query("SELECT login, display_name, created_at FROM ca.users ORDER BY created_at")
    if not rows:
        print("пользователей нет — запустите bootstrap")
        return 0
    for row in rows:
        print(f"  {row['login']:20s} {row['display_name'][:30]:30s} {row['created_at']:%Y-%m-%d %H:%M}")
    print(f"всего: {len(rows)}")
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
    if command == "useradd":
        if len(argv) < 3:
            print("usage: useradd <login> [password] [--name ФИО]")
            return 2
        login = argv[2]
        rest = argv[3:]
        name = ""
        if "--name" in rest:
            index = rest.index("--name")
            name = rest[index + 1] if index + 1 < len(rest) else ""
            rest = rest[:index] + rest[index + 2:]
        password = rest[0] if rest else ""
        return useradd(login, password, name)
    if command == "users":
        return users()
    print(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
