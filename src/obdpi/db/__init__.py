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


SHORT_TRIP_S = 180            # motor ligado por menos que isso não entra no histórico de viagens
RESTORED: list[str] = []      # nomes dos backups restaurados nesta execução (o coletor registra e avisa)


def _check(path) -> str:
    try:
        c = sqlite3.connect(str(path))
        ok = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        return ok
    except sqlite3.DatabaseError as e:
        return str(e)


def init(path: str) -> sqlite3.Connection:
    """Abre (ou cria) o banco. O Pi perde a energia junto com o carro: se o arquivo amanhecer corrompido, ele é posto
    de lado (com o -wal e o -shm, que não podem ser aplicados a outro banco) e o backup mais recente que estiver íntegro
    assume o lugar. Sem backup válido, começa um banco novo. O arquivo corrompido fica guardado para recuperação manual."""
    p = Path(path)
    if p.exists() and _check(p) != "ok":
        import shutil
        import time
        stamp = int(time.time())
        for suf in ("", "-wal", "-shm"):
            f = p.with_name(p.name + suf)
            if f.exists():
                f.rename(p.with_name(f"{p.name}.corrompido-{stamp}{suf}"))
        for b in sorted((p.parent / "backup").glob("obd-*.db"), reverse=True):
            if _check(b) == "ok":
                shutil.copy2(b, p)
                RESTORED.append(b.name)
                break
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
    con.execute("""CREATE TABLE IF NOT EXISTS repair_log (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, target TEXT,
                   signature TEXT, action TEXT, result TEXT, detail TEXT)""")
    for col in ("load", "gear", "maf"):
        _add_col(con, "fast_sample", col, "REAL")
    for col, decl in (("eco_score", "INTEGER"), ("eco_json", "TEXT"), ("warmup_s", "INTEGER"), ("start_ect", "REAL"),
                      ("crank_v", "REAL"), ("rest_v", "REAL"), ("air_json", "TEXT"), ("air_verdict", "TEXT")):
        _add_col(con, "trip", col, decl)
        _add_col(con, "fuel_fill", "total", "REAL")          # valor pago (R$): basta isso para registrar um tanque
    for col, decl in (("title", "TEXT"), ("note", "TEXT"), ("hidden", "INTEGER DEFAULT 0"), ("last_run_time", "REAL"),
                      ("start_snapshot", "TEXT"), ("end_snapshot", "TEXT")):
        _add_col(con, "trip", col, decl)
    # uma vez só: as viagens curtas que já estavam gravadas também saem do histórico (ficam em "Mostrar ocultas")
    if not con.execute("SELECT 1 FROM meta WHERE key='short_trips_hidden'").fetchone():
        con.execute("UPDATE trip SET hidden=2 WHERE finalized=1 AND COALESCE(hidden,0)=0 AND duration_s < ?", (SHORT_TRIP_S,))
        con.execute("INSERT INTO meta VALUES ('short_trips_hidden','1')")
