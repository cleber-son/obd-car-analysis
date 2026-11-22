"""SQL do GPS: gravar posições, ler por viagem, modelos aprendidos (gps_model), acontecimentos com posição (geo_event).

Tudo roda na conexão e na transação do coletor (o commit continua sendo o de maybe_commit)."""
from __future__ import annotations

import json
import sqlite3

from . import Fix

HEADING_MIN_KMH = 3.0        # abaixo disso o rumo do receptor é ruído


def _row(f: Fix, trip_id, flags):
    heading = f.heading if f.heading is not None and (f.speed or 0) >= HEADING_MIN_KMH else None
    r = lambda v, d: round(v, d) if v is not None else None
    return (f.ts, trip_id, round(f.lat, 7), round(f.lon, 7), r(f.alt, 1), r(f.speed, 2), r(heading, 1), r(f.climb, 2),
            r(f.hdop, 2), f.sats, f.mode, flags)


def insert_fixes(con, fixes: list[Fix], trip_id: int | None, flags: int = 0) -> None:
    """Colisão de ts (chave primária) substitui a anterior."""
    if fixes:
        con.executemany("INSERT OR REPLACE INTO gps_fix(ts, trip_id, lat, lon, alt, speed, heading, climb, hdop, sats, mode, flags)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [_row(f, trip_id, flags) for f in fixes])


def trip_fixes(con, a: int, b: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM gps_fix WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()


def nearest_fix(con, ts: int, tol_ms: int = 10_000) -> sqlite3.Row | None:
    a = con.execute("SELECT * FROM gps_fix WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1", (ts - tol_ms, ts)).fetchone()
    b = con.execute("SELECT * FROM gps_fix WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 1", (ts, ts + tol_ms)).fetchone()
    if a and b:
        return a if ts - a["ts"] <= b["ts"] - ts else b
    return a or b


def model_get(con, key: str, default=None):
    r = con.execute("SELECT json FROM gps_model WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def model_set(con, key: str, obj, ts: int) -> None:
    con.execute("INSERT OR REPLACE INTO gps_model(key, json, updated_ts) VALUES (?,?,?)",
                (key, json.dumps(obj, ensure_ascii=False), ts))


def add_event(con, ts: int, kind: str, *, trip_id=None, place_id=None, lat=None, lon=None, value=None, detail=None) -> int:
    cur = con.execute("INSERT INTO geo_event(ts, trip_id, kind, place_id, lat, lon, value, detail) VALUES (?,?,?,?,?,?,?,?)",
                      (ts, trip_id, kind, place_id, lat, lon, value, detail))
    return cur.lastrowid


def shift(con, off: int, start: int) -> None:
    """Correção do relógio para trás (mesma transação de apply_clock_offset). gps_fix em dois passos, como sample e
    fast_sample (a chave primária é ts); as demais por soma simples. Tabelas calculadas no fechamento não entram."""
    con.execute("UPDATE gps_fix SET ts = -(ts + ?) WHERE ts >= ?", (off, start))
    con.execute("UPDATE gps_fix SET ts = -ts WHERE ts < 0")
    for t in ("geo_event", "dyno_run", "accel_run"):
        con.execute(f"UPDATE {t} SET ts = ts + ? WHERE ts >= ?", (off, start))
