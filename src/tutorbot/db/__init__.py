"""SQLite connection + migration runner.

schema.sql is migration version 1. Later migrations live in migrations/NNN_*.sql
(NNN >= 002) and are applied in order; schema_version is stored in `meta`.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
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


def _feedback_schema(script: str) -> dict[str, tuple[str, str]]:
    """Expected definitions, including constraints, for interrupted 003 recovery."""
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(script)
        return {
            name: (kind, " ".join(sql.split()))
            for kind, name, sql in expected.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"
            )
        }


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    ver = _current_version(conn)
    if ver < 1:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + (HERE / "schema.sql").read_text(encoding="utf-8"))
            conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
            conn.execute("INSERT INTO meta(key,value) VALUES('hash_version',?)", (HASH_VERSION,))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        ver = 1
    for n, path in _migrations():
        if n <= ver:
            continue
        script = path.read_text(encoding="utf-8")
        expected = _feedback_schema(script) if n == 3 else {}
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + script)
            for name, definition in expected.items():
                row = conn.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                if not row or row[1] is None or (row[0], " ".join(row[1].split())) != definition:
                    raise sqlite3.DatabaseError(f"incompatible existing migration 003 object: {name}")
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(n),))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        ver = n
    return ver
