"""Rotas: viagens pelo mesmo caminho agrupadas, com km/L, L/100 km e R$/km por rota (docs/GPS-ARQUITETURA.md, seção 9.3).

`assign_trip(con, tid, ctx)` é um finalizador (hub.FINALIZERS, depois de trip.finalize): lê o trajeto guardado
(trip_gps.track_json), monta a assinatura (células de ~220 m visitadas) e procura uma rota com as mesmas pontas
(lugar salvo ou célula de ~330 m) e caminho parecido (Jaccard ≥ 0,6, distância a ±20 %). Sem rota: cria
"Origem → Destino" (e "· via 2", "· via 3" para os caminhos alternativos). Ida e volta são rotas diferentes.
Reanalisar a viagem (obdctl gps reanalyze) reagrupa: ela sai da rota antiga, que tem a estatística refeita.
"""
from __future__ import annotations

import json
import math
import statistics
import time

from . import trip as tripmod
from .geo import cell, haversine_m, neighbors

CELL_DEG = 0.002             # assinatura: células de ~220 m…
SAMPLE_M = 100               # …amostrando o trajeto a cada 100 m
END_DEG = 0.003              # pontas sem lugar salvo: célula de ~330 m (e as 8 vizinhas, contra o ruído na borda)
MIN_GPS_KM = 1.0
MIN_COVERAGE = 0.7
SAME_JACCARD = 0.6
SAME_KM = 0.20
LONG_STOP_S = 600            # parada de mais de 10 min com o motor ligado: viagem atípica (fora das medianas)
PROFILE_STEP_M = 250
# faixas de horário de saída (hora local), usadas também na análise do trânsito (9.19)
HOURS = ((0, 6, "madrugada"), (6, 9, "manhã cedo (pico)"), (9, 12, "manhã"), (12, 17, "tarde"),
         (17, 20, "fim de tarde (pico)"), (20, 24, "noite"))
MIX_GAS, MIX_ETH = 0.25, 0.75


# ---------------------------------------------------------------- funções puras
def signature(track: dict) -> list[int]:
    """Células de 0,002° visitadas, amostrando o trajeto a cada 100 m (pontas incluídas), sem repetir, em ordem."""
    lat, lon = (track or {}).get("lat") or [], (track or {}).get("lon") or []
    out: list[int] = []
    seen: set[int] = set()

    def add(la, lo):
        c = cell(la, lo, CELL_DEG)
        if c not in seen:
            seen.add(c)
            out.append(c)
    if not lat:
        return out
    add(lat[0], lon[0])
    carry = 0.0                                      # metros desde a última amostra
    for i in range(1, len(lat)):
        a, b = (lat[i - 1], lon[i - 1]), (lat[i], lon[i])
        d = haversine_m(a[0], a[1], b[0], b[1])
        need, t = SAMPLE_M - carry, 0.0
        while d > 0 and d - t >= need:
            t += need
            f = t / d
            add(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            need = SAMPLE_M
        carry = SAMPLE_M - need + (d - t)
    add(lat[-1], lon[-1])
    return out


def similarity(a: list[int], b: list[int]) -> float:
    """Jaccard das células."""
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa or sb else 0.0


def end_key(place_id, lat, lon) -> str | None:
    if place_id:
        return f"p{place_id}"
    if lat is None or lon is None:
        return None
    return f"c{cell(lat, lon, END_DEG)}"


def _key_variants(key: str) -> list[str]:
    if key.startswith("c"):
        return [f"c{n}" for n in neighbors(int(key[1:]), END_DEG)]
    return [key]


def hour_band(ts: int) -> str:
    h = time.localtime(ts / 1000).tm_hour
    return next(name for a, b, name in HOURS if a <= h < b)


def fuel_kind(mix) -> str | None:
    if mix is None:
        return None
    return "gasolina" if mix <= MIX_GAS else "etanol" if mix >= MIX_ETH else "mistura"


def time_profile(track: dict, step_m: float = PROFILE_STEP_M) -> list[float]:
    """Tempo (s desde a saída) ao passar por cada múltiplo de step_m ao longo do trajeto."""
    lat, lon, t = track.get("lat") or [], track.get("lon") or [], track.get("t") or []
    out, cum, mark = [], 0.0, step_m
    for i in range(1, min(len(lat), len(t))):
        d = haversine_m(lat[i - 1], lon[i - 1], lat[i], lon[i])
        while d > 0 and cum + d >= mark:
            f = (mark - cum) / d
            out.append(round(t[i - 1] + (t[i] - t[i - 1]) * f, 1))
            mark += step_m
        cum += d
    return out


def _med(xs, d: int | None = None):
    v = [x for x in xs if x is not None]
    if not v:
        return None
    m = statistics.median(v)
    return round(m, d) if d is not None else m


# ---------------------------------------------------------------- banco
def _mix_at(con, ts: int):
    r = con.execute("SELECT mix_after FROM fuel_fill WHERE ts <= ? AND mix_after IS NOT NULL ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    return r[0] if r else None


def _members(con, route_id: int) -> list[dict]:
    rows = con.execute("""SELECT t.id, t.start_ts, t.duration_s, t.distance_km, t.fuel_l, t.km_l, g.cost, g.cost_km, g.flags,
                                 g.stops_json, g.track_json
                          FROM trip_gps g JOIN trip t ON t.id = g.trip_id WHERE g.route_id = ? ORDER BY t.start_ts""", (route_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        km, fl = d["distance_km"], d["fuel_l"]
        d["km_l"] = round(km / fl, 2) if km and fl and fl > 0.05 else None
        d["l100"] = round(fl / km * 100, 2) if km and fl and km > 0.5 else None
        d["atipica"] = "atipica" in (d["flags"] or "").split(",")
        d["mix"] = _mix_at(con, d["start_ts"])
        out.append(d)
    return out


def _summ(ms: list[dict]) -> dict:
    return {"n": len(ms), "km": _med((m["distance_km"] for m in ms), 2), "dur_s": _med((m["duration_s"] for m in ms), 0),
            "km_l": _med((m["km_l"] for m in ms), 2), "l100": _med((m["l100"] for m in ms), 2),
            "cost_km": _med((m["cost_km"] for m in ms), 3), "cost": _med((m["cost"] for m in ms), 2)}


def stats(con, route_id: int) -> dict:
    """Medianas da rota (sem as viagens atípicas), melhor e pior viagem, por combustível e por faixa de horário."""
    ms = _members(con, route_id)
    typ = [m for m in ms if not m["atipica"]]
    out = _summ(typ)
    out.update(n=len(ms), n_tipicas=len(typ), n_atipicas=len(ms) - len(typ), last_ts=ms[-1]["start_ts"] if ms else None)
    ranked = sorted((m for m in typ if m["km_l"] is not None), key=lambda m: m["km_l"])
    pick = lambda m: {"trip_id": m["id"], "start_ts": m["start_ts"], "km_l": m["km_l"], "l100": m["l100"], "dur_s": m["duration_s"],
                      "cost_km": m["cost_km"]} if m else None
    out["best"], out["worst"] = (pick(ranked[-1]), pick(ranked[0])) if len(ranked) >= 2 else (None, None)
    by_fuel: dict[str, list] = {}
    for m in typ:
        k = fuel_kind(m["mix"])
        if k:
            by_fuel.setdefault(k, []).append(m)
    out["by_fuel"] = {k: _summ(v) for k, v in by_fuel.items()}
    by_hour: dict[str, list] = {}
    for m in typ:
        by_hour.setdefault(hour_band(m["start_ts"]), []).append(m)
    out["by_hour"] = [dict(_summ(by_hour[name]), faixa=name) for _, _, name in HOURS if name in by_hour]
    return out


def _profile(ms: list[dict]) -> dict | None:
    profs = []
    for m in ms:
        if m["atipica"] or not m["track_json"]:
            continue
        try:
            profs.append(time_profile(json.loads(m["track_json"])))
        except ValueError:
            continue
    profs = [p for p in profs if p]
    if not profs:
        return None
    need = math.ceil(len(profs) / 2)
    t = []
    for i in range(max(len(p) for p in profs)):
        col = [p[i] for p in profs if i < len(p)]
        if len(col) < need:
            break
        t.append(round(statistics.median(col), 1))
    return {"step_m": PROFILE_STEP_M, "t_s": t, "n": len(profs)}


def refresh(con, route_id: int) -> dict | None:
    """Refaz km, n, estatística e perfil da rota (ou apaga a rota que ficou sem viagens)."""
    ms = _members(con, route_id)
    if not ms:
        con.execute("DELETE FROM route WHERE id=?", (route_id,))
        return None
    st = stats(con, route_id)
    km = st["km"] if st["km"] is not None else _med(m["distance_km"] for m in ms)
    con.execute("UPDATE route SET km=?, n=?, stats_json=?, profile_json=?, updated_ts=? WHERE id=?",
                (km, len(ms), json.dumps(st, ensure_ascii=False), json.dumps(_profile(ms)), int(time.time() * 1000), route_id))
    return st


def _name(con, g, start_key: str, end_key_: str) -> str:
    base = tripmod.route_label(g["start_name"], g["end_name"], g["start_place_id"], g["end_place_id"]) or "Rota"
    n = con.execute("SELECT COUNT(*) FROM route WHERE start_key=? AND end_key=?", (start_key, end_key_)).fetchone()[0]
    return base if n == 0 else f"{base} · via {n + 1}"


def _flag(flags: str | None, name: str, on: bool) -> str | None:
    fl = [f for f in (flags or "").split(",") if f and f != name]
    if on:
        fl.append(name)
    return ",".join(fl) or None


def assign_trip(con, tid: int, ctx=None) -> int | None:
    """Finalizador: põe a viagem numa rota (ou cria uma). Só viagens com ≥ 1 km pelo GPS e cobertura ≥ 70 %."""
    g = con.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    if g is None:
        return None
    old = g["route_id"]
    t = con.execute("SELECT distance_km FROM trip WHERE id=?", (tid,)).fetchone()
    ok = (g["track_json"] and (g["gps_km"] or 0) >= MIN_GPS_KM and (g["coverage"] or 0) >= MIN_COVERAGE and t and t["distance_km"])
    sk = end_key(g["start_place_id"], g["start_lat"], g["start_lon"]) if ok else None
    ek = end_key(g["end_place_id"], g["end_lat"], g["end_lon"]) if ok else None
    if not (ok and sk and ek):
        if old is not None:
            con.execute("UPDATE trip_gps SET route_id=NULL WHERE trip_id=?", (tid,))
            refresh(con, old)
        return None
    try:
        sig = signature(json.loads(g["track_json"]))
    except ValueError:
        return None
    km = t["distance_km"]
    sv, ev = _key_variants(sk), _key_variants(ek)
    best, bs = None, 0.0
    for r in con.execute(f"SELECT id, cells_json, km FROM route WHERE start_key IN ({','.join('?' * len(sv))})"
                         f" AND end_key IN ({','.join('?' * len(ev))}) ORDER BY id", (*sv, *ev)).fetchall():
        try:
            cells = json.loads(r["cells_json"] or "[]")
        except ValueError:
            continue
        s = similarity(sig, cells)
        if s >= SAME_JACCARD and r["km"] and abs(km - r["km"]) / r["km"] < SAME_KM and s > bs:
            best, bs = r["id"], s
    now = int(time.time() * 1000)
    if best is None:
        best = con.execute("INSERT INTO route(name, start_key, end_key, cells_json, km, n, created_ts, updated_ts) VALUES (?,?,?,?,?,0,?,?)",
                           (_name(con, g, sk, ek), sk, ek, json.dumps(sig), round(km, 2), now, now)).lastrowid
    try:
        stops = json.loads(g["stops_json"] or "[]")
    except ValueError:
        stops = []
    atyp = any((s.get("s") or 0) > LONG_STOP_S for s in stops)
    con.execute("UPDATE trip_gps SET route_id=?, flags=? WHERE trip_id=?", (best, _flag(g["flags"], "atipica", atyp), tid))
    refresh(con, best)
    if old is not None and old != best:
        refresh(con, old)
    return best


def list_routes(con) -> list[dict]:
    out = []
    for r in con.execute("SELECT id, name, start_key, end_key, km, n, stats_json, updated_ts FROM route WHERE n > 0 ORDER BY n DESC, id").fetchall():
        d = dict(r)
        try:
            st = json.loads(d.pop("stats_json") or "{}")
        except ValueError:
            st = {}
        d.update({k: st.get(k) for k in ("dur_s", "km_l", "l100", "cost_km", "cost", "n_tipicas", "last_ts")})
        out.append(d)
    return out


def detail(con, route_id: int) -> dict | None:
    r = con.execute("SELECT * FROM route WHERE id=?", (route_id,)).fetchone()
    if not r:
        return None
    d = {k: r[k] for k in r.keys() if k not in ("cells_json", "stats_json", "profile_json")}
    for k in ("stats", "profile"):
        try:
            d[k] = json.loads(r[f"{k}_json"] or "null")
        except ValueError:
            d[k] = None
    ms = _members(con, route_id)
    d["trips"] = [{"id": m["id"], "start_ts": m["start_ts"], "km": m["distance_km"], "dur_s": m["duration_s"], "km_l": m["km_l"],
                   "l100": m["l100"], "cost": m["cost"], "cost_km": m["cost_km"], "atipica": m["atipica"], "combustivel": fuel_kind(m["mix"])}
                  for m in reversed(ms)]
    d["ref_trip_id"] = next((m["id"] for m in reversed(ms) if m["track_json"] and not m["atipica"]), ms[-1]["id"] if ms else None)
    return d
