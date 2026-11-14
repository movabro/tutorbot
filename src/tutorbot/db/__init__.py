"""SQLite connection + migration runner.

schema.sql is migration version 1. Later migrations live in migrations/NNN_*.sql
(NNN >= 002) and are applied in order; schema_version is stored in `meta`.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
HASH_VERSION = "v1"


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if not has:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def _migrations() -> list[tuple[int, Path]]:
    out = []
    for p in (HERE / "migrations").glob("*.sql"):
        m = re.match(r"(\d+)_", p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    ver = _current_version(conn)
    if ver < 1:
        conn.executescript("BEGIN;\n" + (HERE / "schema.sql").read_text(encoding="utf-8") + "\nCOMMIT;")
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
        conn.execute("INSERT INTO meta(key,value) VALUES('hash_version',?)", (HASH_VERSION,))
        ver = 1
    for n, path in _migrations():
        if n <= ver:
            continue
        conn.executescript("BEGIN;\n" + path.read_text(encoding="utf-8") + "\nCOMMIT;")
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(n),))
        ver = n
    return ver
