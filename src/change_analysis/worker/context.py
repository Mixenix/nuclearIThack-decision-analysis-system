"""The ``context`` object handed to user procedures.

It is deliberately small: no database, no shell, no secrets, no environment
variables.  Everything a procedure can touch is either its inputs or its own
artifact directory (PLAN.md §11.1, §11.6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from .artifacts import JsonlReader, JsonlWriter


class ProcedureContext:
    def __init__(self, run_id: str, stage: str, backend: str, out_dir: Path) -> None:
        self.run_id = run_id
        self.stage = stage
        self.backend = backend
        self.artifact_dir = out_dir
        self.out_dir = out_dir
        self._writers: dict[str, JsonlWriter] = {}
        self.meta: dict[str, Any] = {}
        self._progress_fh = (out_dir / "progress.jsonl").open("w", encoding="utf-8")
        self._last_done = -1

    # -- logging / progress --------------------------------------------------
    def log(self, message: str) -> None:
        print(f"[{self.stage}] {message}", flush=True)

    def progress(self, done: int, total: int = 0, message: str = "") -> None:
        """Throttled on the producer side; the parent also throttles writes."""
        if done == self._last_done:
            return
        self._last_done = done
        self._progress_fh.write(
            json.dumps({"done": int(done), "total": int(total), "message": message},
                       ensure_ascii=False) + "\n"
        )
        self._progress_fh.flush()

    # -- tables --------------------------------------------------------------
    def write_rows(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        writer = self._writers.get(table)
        if writer is None:
            writer = JsonlWriter(self.out_dir / f"{table}.jsonl")
            self._writers[table] = writer
        return writer.write_many(rows)

    def open_writer(self, table: str) -> JsonlWriter:
        return self._writers[table] if table in self._writers else JsonlWriter(
            self.out_dir / f"{table}.jsonl"
        )

    def read_rows(self, path: str | Path) -> Iterator[dict[str, Any]]:
        return iter(JsonlReader(Path(path)))

    def table_path(self, table: str) -> Path:
        return self.out_dir / f"{table}.jsonl"

    # -- bookkeeping ---------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value

    def close(self) -> None:
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()
        try:
            self._progress_fh.close()
        except Exception:  # pragma: no cover - already closed
            pass
