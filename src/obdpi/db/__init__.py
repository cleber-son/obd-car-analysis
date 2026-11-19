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
    migrate(con)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
    con.commit()
    return con


def _add_col(con, table, col, decl):
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def migrate(con) -> None:
    """Migrações aditivas (colunas novas em tabelas antigas)."""
    _add_col(con, "fast_sample", "fuel_lh", "REAL")      # consumo instantâneo estimado (L/h)
    _add_col(con, "trip", "fuel_l", "REAL")
    _add_col(con, "trip", "km_l", "REAL")
    _add_col(con, "alert", "count", "INTEGER DEFAULT 1")
    for col in ("load", "gear", "maf"):
        _add_col(con, "fast_sample", col, "REAL")
    for col, decl in (("eco_score", "INTEGER"), ("eco_json", "TEXT"), ("warmup_s", "INTEGER"), ("start_ect", "REAL"),
                      ("crank_v", "REAL"), ("rest_v", "REAL"), ("air_json", "TEXT"), ("air_verdict", "TEXT")):
        _add_col(con, "trip", col, decl)
    for col, decl in (("title", "TEXT"), ("note", "TEXT"), ("hidden", "INTEGER DEFAULT 0"), ("last_run_time", "REAL")):
        _add_col(con, "trip", col, decl)
