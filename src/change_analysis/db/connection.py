"""Database connections.

One small pool for the web process, short transactions everywhere, and a COPY
path for the bulk loads (a full run inserts ~600k normalized rows and ~290k
comparison rows).  See docs/CONTRACTS.md §2.
"""

from __future__ import annotations

import atexit
import contextlib
from typing import Any, Iterable, Iterator, Sequence

import psycopg
import psycopg.rows
from psycopg import sql
from psycopg_pool import ConnectionPool

from ..config import load_settings

_POOL: ConnectionPool | None = None


def dsn() -> str:
    return load_settings().database_url


def get_pool() -> ConnectionPool:
    """Lazy process-wide pool.  NiceGUI is single-threaded-async; a small pool
    is enough and keeps the connection count predictable."""
    global _POOL
    if _POOL is None:
        # Close the pool before the interpreter starts tearing threads down:
        # otherwise every CLI invocation ends with a noisy
        # ``PythonFinalizationError: cannot join thread`` from the pool's
        # destructor, which looks like a crash in logs and demos.
        atexit.register(close_pool)
        _POOL = ConnectionPool(
            conninfo=dsn(),
            min_size=1,
            max_size=8,
            timeout=30.0,
            kwargs={"autocommit": False, "row_factory": psycopg.rows.dict_row},
            open=True,
        )
    return _POOL


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


@contextlib.contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """One short transaction.  Never hold it across a long computation."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def query(sql_text: str, params: Sequence[Any] = ()) -> list[dict]:
    with tx() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            if cur.description is None:
                return []
            return cur.fetchall()


def execute(sql_text: str, params: Sequence[Any] = ()) -> int:
    with tx() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            return cur.rowcount


def copy_rows(table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """Bulk insert through COPY.  ``table`` is a trusted internal identifier.

    ``copy.write_row`` is used rather than hand-rendered text: it is already
    efficient (measured 76k-105k rows/s here) and it renders arrays, booleans and
    JSON correctly, which a hand-written formatter silently gets wrong.  A
    block-text variant was measured against it and was not faster.
    """
    schema, _, name = table.partition(".")
    table_sql = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name)) if name \
        else sql.Identifier(schema)
    column_list = ", ".join(columns)
    count = 0
    with tx() as conn:
        with conn.cursor() as cur:
            # A bulk load of ~290k rows is a single transaction; waiting for a WAL
            # flush at its end buys nothing here and, under a concurrent run or an
            # autovacuum, turns the load into a WALWrite stall (measured: 7+ min
            # instead of seconds).  ``synchronous_commit`` is local to this
            # transaction, so a crash can only lose this load — the job is still
            # RUNNING, the watchdog marks it INTERRUPTED, and no half-published
            # report can appear (PLAN.md §13.2).
            cur.execute("SET LOCAL synchronous_commit = off")
            with cur.copy(
                sql.SQL("COPY {} ({}) FROM STDIN").format(table_sql, sql.SQL(column_list))
            ) as copy:
                for row in rows:
                    copy.write_row(tuple(row))
                    count += 1
    return count
