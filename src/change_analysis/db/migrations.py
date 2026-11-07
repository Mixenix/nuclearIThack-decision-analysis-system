"""Numbered SQL migrations with an applied-migrations table.

No SQLAlchemy, no Alembic (PLAN.md §4).  Each file runs inside its own
transaction; a file that fails leaves the database unchanged and the migration
is not recorded.  Normal startup never drops user data.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .connection import get_pool

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE SCHEMA IF NOT EXISTS ca;"
            "CREATE TABLE IF NOT EXISTS ca.schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT version FROM ca.schema_migrations")
        return {row["version"] for row in cur.fetchall()}


def apply_migrations(directory: Path | None = None) -> list[str]:
    """Apply every not-yet-applied migration in filename order."""
    directory = directory or MIGRATIONS_DIR
    files = sorted(directory.glob("*.sql"))
    applied: list[str] = []
    with get_pool().connection() as conn:
        done = _applied(conn)
        conn.commit()
        for path in files:
            version = path.name
            if version in done:
                continue
            sql_text = path.read_text(encoding="utf-8")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql_text)
                    cur.execute(
                        "INSERT INTO ca.schema_migrations (version) VALUES (%s)", (version,)
                    )
            applied.append(version)
    return applied


def verify() -> dict[str, int]:
    """Report the tables that actually exist — used by the health check."""
    from .connection import query

    rows = query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'ca'"
    )
    return {"tables": len(rows), "names": sorted(r["table_name"] for r in rows)}  # type: ignore[return-value]


def main() -> int:
    applied = apply_migrations()
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("nothing to apply")
    info = verify()
    print(f"tables in schema ca: {info['tables']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
