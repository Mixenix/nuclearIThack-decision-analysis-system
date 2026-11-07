"""Streaming artifact I/O between the computing subprocess and the worker.

Large tables never travel through stdout or through a pipe as one blob: the
subprocess writes JSONL files, the worker bulk-COPYs them into PostgreSQL
(PLAN.md §11.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator


def writer_path(root: Path, table: str) -> Path:
    return root / f"{table}.jsonl"


class JsonlWriter:
    def __init__(self, path: Path, buffer_rows: int = 5000) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._buffer: list[str] = []
        self._buffer_rows = buffer_rows
        self.count = 0

    def write(self, row: dict[str, Any]) -> None:
        self._buffer.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        self.count += 1
        if len(self._buffer) >= self._buffer_rows:
            self.flush()

    def write_many(self, rows: Iterable[dict[str, Any]]) -> int:
        written = 0
        for row in rows:
            self.write(row)
            written += 1
        return written

    def flush(self) -> None:
        if self._buffer:
            self._fh.write("\n".join(self._buffer))
            self._fh.write("\n")
            self._buffer.clear()
            self._fh.flush()

    def close(self) -> None:
        self.flush()
        self._fh.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


class JsonlReader:
    """Lazy reader with the same shape as the procedure context API."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return read_jsonl(self.path)

    def count(self) -> int:
        total = 0
        with self.path.open("rb") as fh:
            for _ in fh:
                total += 1
        return total
