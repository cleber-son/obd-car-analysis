"""Destino provável, previsão de chegada e autonomia (docs/GPS-ARQUITETURA.md, seção 9.18).

`EtaEngine` (hub.LIVE_ENGINES) reavalia a cada 15 s:
- **Destino:** candidatas = rotas (F4) que começam na origem desta viagem (lugar salvo ou célula). Pontuação =
  frequência (peso maior para o mesmo tipo de dia e saída a ±90 min) × fração das células percorridas até agora que
  pertencem à rota (precisa de 80 %). Mostra quando a melhor passa de 0,6 de confiança e tem o dobro da segunda.
- **Chegada:** pelo perfil da rota (tempo mediano a cada 250 m), corrigido pelo ritmo de hoje (0,7–1,6).
- **Autonomia:** litros no tanque = `vehicle.tank_l` − litros estimados desde o último tanque cheio (só depois de um
  tanque cheio registrado). km/L da rota para o que falta dela e a média real recente para o resto.

Destino e chegada vão só para o gps.json (privado: o nome do destino é dado de localização). A autonomia é um número
e vai também no live.json (`gps.range_km`). Abaixo de 60 km, aviso no celular uma vez por viagem só com
`gps.notify_routine = true` (o modo direção fala sozinho).
"""
from __future__ import annotations

import json
import logging
import statistics
import time

from . import routes as routesmod
from .geo import cell, haversine_m, neighbors

log = logging.getLogger("gps")

EVAL_MS = 15_000
MIN_FRAC = 0.80              # células percorridas que pertencem à rota
MIN_CONF = 0.60
DOMINANCE = 2.0              # a melhor tem o dobro da segunda
MIN_KM = 1.5                 # antes disso todo caminho parece com todos
MIN_CELLS = 4
SAME_TIME_MIN = 90
PACE = (0.7, 1.6)
ORIGIN_WAIT_MS = 10 * 60_000
FUEL_REFRESH_MS = 5 * 60_000
LOW_RANGE_KM = 60
LOW_RANGE_EXPIRE_MS = 2 * 3600_000
STATION_NEAR_M = 5000
MAX_STEP_S = 5.0


# ---------------------------------------------------------------- funções puras
def range_km(tank_l: float, used_l: float, kml_recent: float | None, left_km: float | None = None,
             kml_route: float | None = None) -> float | None:
    """Autonomia: litros no tanque × km/L. Com destino previsto, o que falta da rota é feito com o km/L da rota e o
    resto com a média recente. (50 − 30) × 11 = 220 km."""
    if kml_recent is None and kml_route is None:
        return None
    liters = max(0.0, float(tank_l) - float(used_l or 0))
    if left_km and kml_route and kml_recent:
        need = left_km / kml_route
        if liters <= need:
            return round(liters * kml_route, 1)
        return round(left_km + (liters - need) * kml_recent, 1)
    return round(liters * (kml_recent or kml_route), 1)


def profile_time(t_s: list[float], step_m: float, d_m: float) -> float:
    """Tempo mediano (s) para chegar a d_m metros pelo perfil da rota (interpolado; 0 m = 0 s)."""
    if not t_s or d_m <= 0:
        return 0.0
    k = d_m / step_m
    i = int(k)
    if i >= len(t_s):
        return float(t_s[-1])
    a = t_s[i - 1] if i >= 1 else 0.0
    return a + (t_s[i] - a) * (k - i)


def arrival(prof: dict, total_s: float, route_m: float, traveled_m: float, elapsed_s: float) -> dict:
    """{"left_s", "left_km", "pace"}: restante = (T_total − T(km atual)) × ritmo de hoje (decorrido ÷ mediano até aqui)."""
    t_now = profile_time(prof.get("t_s") or [], prof.get("step_m") or routesmod.PROFILE_STEP_M, traveled_m)
    pace = elapsed_s / t_now if t_now > 30 else 1.0
    pace = min(PACE[1], max(PACE[0], pace))
    left = max(0.0, total_s - t_now) * pace
    return {"left_s": round(left), "left_km": round(max(0.0, route_m - traveled_m) / 1000, 1), "pace": round(pace, 2)}


def day_kind(ts: int) -> int:
    wd = time.localtime(ts / 1000).tm_wday
    return 2 if wd == 6 else 1 if wd == 5 else 0


def mod(ts: int) -> int:
    lt = time.localtime(ts / 1000)
    return lt.tm_hour * 60 + lt.tm_min


def weight(member_ts: int, now_ts: int) -> float:
    w = 1.0
    if day_kind(member_ts) == day_kind(now_ts):
        w += 1.0
    d = abs(mod(member_ts) - mod(now_ts))
    if min(d, 1440 - d) <= SAME_TIME_MIN:
        w += 1.0
    return w


def score(cands: list[dict], visited: set[int]) -> list[tuple[float, dict, float]]:
    """[(confiança, rota, fração)] em ordem, só as com fração ≥ 80 %. cands: {freq, cells (com as vizinhas)}."""
    if not visited:
        return []
    scored = []
    for c in cands:
        frac = len(visited & c["cells"]) / len(visited)
        if frac >= MIN_FRAC:
            scored.append((c["freq"] * frac, c, frac))
    tot = sum(s for s, _, _ in scored)
    out = [(s / tot if tot else 0.0, c, fr) for s, c, fr in scored]
    out.sort(key=lambda x: -x[0])
    return out


def pick(scored: list) -> tuple | None:
    if not scored:
        return None
    best = scored[0]
    if best[0] <= MIN_CONF:
        return None
    if len(scored) > 1 and best[0] < DOMINANCE * scored[1][0]:
        return None
    return best


# ---------------------------------------------------------------- banco
def candidates(con, origin_keys: list[str], now_ts: int, exclude_tid: int | None = None) -> list[dict]:
    if not origin_keys:
        return []
    rows = con.execute(f"SELECT id, name, end_key, cells_json, km, profile_json, stats_json FROM route WHERE n > 0 AND start_key IN"
                       f" ({','.join('?' * len(origin_keys))})", origin_keys).fetchall()
    out = []
    for r in rows:
        try:
            cells = json.loads(r["cells_json"] or "[]")
            prof = json.loads(r["profile_json"] or "null") or {}
            st = json.loads(r["stats_json"] or "{}") or {}
        except ValueError:
            continue
        if not cells or not prof.get("t_s"):
            continue
        ms = con.execute("SELECT t.start_ts FROM trip_gps g JOIN trip t ON t.id = g.trip_id WHERE g.route_id = ? AND t.id != ?",
                         (r["id"], exclude_tid or -1)).fetchall()
        if not ms:
            continue
        near = set()
        for c in cells:
            near.update(neighbors(c, routesmod.CELL_DEG))
        end = con.execute("SELECT end_name FROM trip_gps WHERE route_id=? AND end_name IS NOT NULL ORDER BY trip_id DESC LIMIT 1",
                          (r["id"],)).fetchone()
        name = end[0] if end else None
        if r["end_key"] and r["end_key"].startswith("p"):
            p = con.execute("SELECT name FROM place WHERE id=?", (int(r["end_key"][1:]),)).fetchone()
            name = p[0] if p else name
        total = st.get("dur_s") or prof["t_s"][-1]
        out.append({"id": r["id"], "route": r["name"], "name": name or r["name"], "cells": near, "km": r["km"] or 0.0,
                    "prof": prof, "total_s": float(total), "km_l": st.get("km_l"),
                    "freq": sum(weight(m[0], now_ts) for m in ms)})
    return out


def fuel_state(con, exclude_tid: int | None = None) -> dict | None:
    """Litros desde o último tanque cheio (viagens fechadas) e o km/L real recente. None sem tanque cheio registrado."""
    f = con.execute("SELECT ts FROM fuel_fill WHERE full=1 ORDER BY ts DESC LIMIT 1").fetchone()
    if not f:
        return None
    r = con.execute("SELECT COALESCE(SUM(fuel_l),0), COALESCE(SUM(distance_km),0) FROM trip WHERE finalized=1 AND start_ts >= ? AND id != ?",
                    (f[0], exclude_tid or -1)).fetchone()
    real = [x[0] for x in con.execute("SELECT real_kml FROM fuel_fill WHERE real_kml IS NOT NULL ORDER BY ts DESC LIMIT 5")]
    kml = statistics.fmean(real) if real else (r[1] / r[0] if r[0] > 1 and r[1] >= 30 else None)
    return {"fill_ts": f[0], "used_l": float(r[0]), "kml": kml}


class EtaEngine:
    def __init__(self, hub):
        self.con = hub.con
        self.hub = hub
        cfg = getattr(hub, "cfg", None) or {}
        self.notify = bool(cfg.get("notify_routine"))
        self.tank_l = float((getattr(hub, "vehicle", None) or {}).get("tank_l") or 50)
        self._reset(None, None)

    def _reset(self, tid, ts) -> None:
        self.tid, self.start_ts = tid, ts
        self.t0: int | None = None               # primeira posição boa da viagem
        self.origin: list[str] | None = None
        self.cands: list[dict] = []
        self.visited: set[int] = set()
        self.traveled = 0.0
        self.last = None
        self.last_eval: int | None = None
        self.dest: dict | None = None
        self.trip_l = 0.0
        self.fuel: dict | None = None
        self.fuel_ts: int | None = None
        self.range: float | None = None
        self.low_sent = False

    def on_trip_start(self, tid, ts, merged) -> None:
        self._reset(tid, ts)

    def shift(self, off: int) -> None:
        for k in ("start_ts", "t0", "last_eval", "fuel_ts"):
            if getattr(self, k) is not None:
                setattr(self, k, getattr(self, k) + off)
        if self.last:
            self.last = (self.last[0] + off, *self.last[1:])
        if self.dest and self.dest.get("eta_ts"):
            self.dest["eta_ts"] += off

    def _origin(self, f) -> list[str]:
        from . import places as placesmod
        p = placesmod.start_place(self.con, placesmod.load_places(self.con), self.start_ts or f.ts, f.lat, f.lon)
        key = routesmod.end_key(p["id"] if p else None, f.lat, f.lon)
        return routesmod._key_variants(key) if key else []

    def feed(self, ctx) -> list:
        f = ctx.fix
        if ctx.trip_id is None or ctx.trip_id != self.tid:
            if ctx.trip_id is not None and self.tid is None:
                self._reset(ctx.trip_id, f.ts)          # viagem já aberta quando o coletor subiu
            else:
                return []
        if self.t0 is None:
            if self.start_ts is not None and f.ts - self.start_ts > ORIGIN_WAIT_MS:
                self.t0 = f.ts                          # partida muito antes da primeira posição: sem destino
                self.origin = []
            else:
                self.t0 = f.ts
                self.origin = self._origin(f)
                self.cands = candidates(self.con, self.origin, f.ts, self.tid)
        if self.last is not None:
            dt = (f.ts - self.last[0]) / 1000
            if 0 < dt <= MAX_STEP_S:
                if (f.speed or 0) >= 2:
                    self.traveled += haversine_m(self.last[1], self.last[2], f.lat, f.lon)
                lh = (ctx.values or {}).get("fuel_lh")
                if lh is not None:
                    self.trip_l += float(lh) * dt / 3600
        self.last = (f.ts, f.lat, f.lon)
        if (f.speed or 0) >= 2:
            self.visited.add(cell(f.lat, f.lon, routesmod.CELL_DEG))
        if self.last_eval is not None and f.ts - self.last_eval < EVAL_MS:
            return []
        self.last_eval = f.ts
        self._evaluate(f)
        return []

    def _evaluate(self, f) -> None:
        now = f.ts
        best = None
        if self.cands and self.traveled >= MIN_KM * 1000 and len(self.visited) >= MIN_CELLS:
            best = pick(score(self.cands, self.visited))
        if best:
            conf, c, _ = best
            a = arrival(c["prof"], c["total_s"], c["km"] * 1000, self.traveled, (now - self.t0) / 1000)
            self.dest = {"name": c["name"], "route": c["route"], "route_id": c["id"], "conf": round(conf, 2),
                         "eta_ts": int(now + a["left_s"] * 1000), "left_km": a["left_km"], "left_s": a["left_s"], "pace": a["pace"]}
        else:
            self.dest = None
        if self.fuel_ts is None or now - self.fuel_ts >= FUEL_REFRESH_MS:
            self.fuel_ts = now
            try:
                self.fuel = fuel_state(self.con, self.tid)
            except Exception as e:
                log.debug("autonomia: %r", e)
                self.fuel = None
        rk = None
        if self.fuel:
            c = next((x for x in self.cands if self.dest and x["id"] == self.dest["route_id"]), None)
            rk = range_km(self.tank_l, self.fuel["used_l"] + self.trip_l, self.fuel["kml"],
                          self.dest["left_km"] if self.dest else None, c["km_l"] if c else None)
        self.range = rk
        if self.dest is not None:
            self.dest["range_km"] = round(rk) if rk is not None else None
        if rk is not None and rk < LOW_RANGE_KM and not self.low_sent:
            self.low_sent = True
            if self.notify:
                self._low_push(f, rk)

    def _low_push(self, f, rk: float) -> None:
        from . import fuelstop
        from .places import push
        body = f"Cerca de {round(rk)} km no tanque (estimado pelo consumo desde o último tanque cheio)."
        try:
            s = fuelstop.best_near(self.con, f.lat, f.lon, STATION_NEAR_M)
            if s:
                body += f" Posto mais bem avaliado por perto: {s['name']}."
        except Exception:
            pass
        push(self.con, "Autonomia baixa", body, "warn", ts=f.ts, expire_ms=LOW_RANGE_EXPIRE_MS)

    def live(self) -> dict:
        return {"range_km": round(self.range)} if self.range is not None and self.tid is not None else {}

    def private(self) -> dict:
        d = self.dest
        if not d:
            return {"dest": None}
        return {"dest": {k: d.get(k) for k in ("name", "route", "conf", "eta_ts", "left_km", "range_km")}}
