"""The event log: one SQLite file, one writer, events never edited.

Each event holds the hash of the event before it, so a changed event is found.
Removed last events are found by comparing with the end (number, hash) saved in a receipt.
Someone who can rewrite the log and every receipt can still forge history.
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, NamedTuple

from agent import doctor
from agent.mask import mask

VERSION = 1
START = "0" * 64


class End(NamedTuple):
    seq: int
    hash: str


def digest(prev: str, seq: int, at: float, task: str, kind: str, body: str) -> str:
    """Hash a JSON list, so that text moved from one field to the next gives a different hash."""
    return hashlib.sha256(json.dumps([prev, seq, at, task, kind, body]).encode()).hexdigest()


class Log:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = secrets
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        path.chmod(0o600)  # SQLite gives its side files the same permissions
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        # WAL is faster, but older SQLite versions have a bug there. The plain journal is safe everywhere.
        self.db.execute(f"pragma journal_mode={'wal' if doctor.sqlite().state == 'OK' else 'delete'}")
        self.db.execute("pragma synchronous=full")
        self.db.execute("pragma fullfsync=1")  # only macOS uses this
        found = self.db.execute("pragma user_version").fetchone()[0]
        if found not in (0, VERSION):
            raise ValueError(f"the log has version {found}; this program knows {VERSION}. Next: update the program.")
        self.db.execute(
            "create table if not exists event"
            "(seq integer primary key, at real, task text, kind text, body text, prev text, hash text)"
        )
        self.db.execute(f"pragma user_version={VERSION}")

    def add(self, task: str, kind: str, **body: Any) -> End:
        """Store one event and return only when it is safely on disk."""
        task, kind = mask(task, self.secrets), mask(kind, self.secrets)
        text = json.dumps(_masked(body, self.secrets), sort_keys=True)
        self.db.execute("begin immediate")  # one writer at a time, also across processes
        try:
            last = self.end()
            seq, at = last.seq + 1, time.time()
            new = digest(last.hash, seq, at, task, kind, text)
            self.db.execute("insert into event values(?,?,?,?,?,?,?)", (seq, at, task, kind, text, last.hash, new))
            self.db.execute("commit")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("rollback")
            raise
        return End(seq, new)

    def end(self) -> End:
        row = self.db.execute("select seq, hash from event order by seq desc limit 1").fetchone()
        return End(*row) if row else End(0, START)

    def events(self, task: str) -> list[tuple[int, str, dict[str, Any]]]:
        rows = self.db.execute("select seq, kind, body from event where task=? order by seq", (task,))
        return [(seq, kind, json.loads(body)) for seq, kind, body in rows]

    def verify(self, saved: End | None = None) -> str:
        """Return "" if the log is intact, else what is wrong. `saved` is the end from a receipt."""
        prev, count, seen = START, 0, saved is None or saved == End(0, START)
        for seq, at, task, kind, body, before, found in self.db.execute("select * from event order by seq"):
            count += 1
            if seq != count or before != prev or found != digest(prev, seq, at, task, kind, body):
                return f"event {count} is missing or was changed"
            prev = found
            seen = seen or saved == End(seq, found)
        return "" if seen else "the end saved in the receipt is not in the log"

    def close(self) -> None:
        self.db.close()


def _masked(value: Any, secrets: tuple[str, ...]) -> Any:
    """Mask inside the structure, before JSON changes quotes and line ends."""
    if isinstance(value, str):
        return mask(value, secrets)
    if isinstance(value, dict):
        return {mask(str(k), secrets): _masked(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_masked(v, secrets) for v in value]
    return value
