"""Acesso ao SQLite (WAL). O coletor é o único escritor de telemetria."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def init(path: str) -> sqlite3.Connection:
    p = Path(path)
    if p.exists():
        try:
            c = sqlite3.connect(path)
            ok = c.execute("PRAGMA quick_check").fetchone()[0]
            c.close()
        except sqlite3.DatabaseError as e:
            ok = str(e)
        if ok != "ok":
            import time
            p.rename(p.with_name(f"{p.name}.corrompido-{int(time.time())}"))
    con = connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
    con.commit()
    return con
