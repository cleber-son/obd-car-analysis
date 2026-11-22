"""Fechamento da viagem com GPS (docs/GPS-ARQUITETURA.md, seções 9.1, 9.3 e 4.5): distância híbrida, origem e destino,
paradas, altitude, custo, trajeto reduzido e lugares automáticos.

`finalize(con, tid, ctx)` é o primeiro item de hub.FINALIZERS. As funções puras (`hybrid_distance`, `find_stops`,
`altitude`, `build_track`) recebem listas e devolvem dicionários; são elas que os testes exercitam.
"""
from __future__ import annotations

import json
import statistics
import time
from bisect import bisect_left

from . import places as placesmod
from . import store
from . import terrain as terrainmod
from .geo import haversine_m, simplify
from .geocode import name_of

VERSION = 1
MIN_FIXES = 10               # menos que isso: viagem sem trajeto
GPS_TOL_MS = 1500            # posição boa a até 1,5 s vale para aquele segundo
OBD_TOL_MS = 2500
HOLD_MS = 5000               # sem nada: repete a última velocidade conhecida por no máximo 5 s
STOP_KMH = 1.5               # abaixo disso (GPS) conta como parado
MOVING_KMH = 1.5
MIN_COVERAGE = 0.5           # abaixo disso a distância volta a ser a do OBD × k
ORIGIN_LATE_MS = 20_000
ORIGIN_PARKED_M = 150
EXPLAINED_SLACK = (1.15, 50)  # distância explicada pelo OBD × 1,15 + 50 m (velocidade inteira do PID 0D subestima)
STOP_SPEED = 3.0             # paradas: < 3 km/h…
STOP_MIN_S = 20              # …por 20 s ou mais (o plano dizia 60 s; os semáforos do caminho param 30–45 s)
ALT_WIN_MS = 4500            # mediana móvel de 9 s
ALT_HYST_M = 5.0             # o plano dizia 3 m; com o ruído de altitude do simulador (4 m) 3 m inventa ~70 m de subida em 6 km planos
TRACK_EPS_M = 6.0
TRACK_EVERY_S = 5.0
TRACK_MAX_PTS = 800
TRACK_MAX_BYTES = 100_000
GAP_S = 5.0
AUTO_PLACE_M = 80
AUTO_PLACE_DAYS = 3
FAST_COLS = ("rpm", "gear", "load", "map", "tps", "maf", "fuel_lh")
SLOW_COLS = (("stft1", "stft1"), ("lam", "o2c1_lambda"), ("timing", "timing"), ("ect", "ect"), ("iat", "iat"))
FAST_TOL_MS, SLOW_TOL_MS = 1500, 6000
SPEEDO_K_RANGE = (0.85, 1.15)


# ---------------------------------------------------------------- utilitários
def good_row(r, max_hdop: float = 5.0, min_sats: int = 5) -> bool:
    """gps.good() para uma linha de gps_fix."""
    if (r["mode"] or 0) < 2:
        return False
    if r["hdop"] is not None and r["hdop"] > max_hdop:
        return False
    if r["sats"] is not None and r["sats"] < min_sats:
        return False
    return True


def good_fixes(con, a: int, b: int, cfg: dict | None = None) -> list:
    cfg = cfg or {}
    mh, ms = float(cfg.get("max_hdop", 5.0)), int(cfg.get("min_sats", 5))
    return [r for r in store.trip_fixes(con, a, b) if good_row(r, mh, ms)]


def _nearest(ts_list: list[int], t: int, tol: int) -> int | None:
    i = bisect_left(ts_list, t)
    best, bd = None, None
    for j in (i - 1, i):
        if 0 <= j < len(ts_list):
            d = abs(ts_list[j] - t)
            if d <= tol and (bd is None or d < bd):
                best, bd = j, d
    return best


def speedo_k(con) -> float:
    """Fator do velocímetro (velocidade verdadeira ÷ a do OBD), aprendido na F5 (gps_model['speedo']['k']); 1,0 até lá."""
    try:
        m = store.model_get(con, "speedo") or {}
        k = float(m.get("k") or 1.0)
        return k if SPEEDO_K_RANGE[0] <= k <= SPEEDO_K_RANGE[1] else 1.0
    except Exception:
        return 1.0


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


# ---------------------------------------------------------------- distância híbrida (9.3)
def hybrid_distance(fixes, fast_rows, k: float = 1.0, a: int | None = None, b: int | None = None,
                    obd_km: float | None = None) -> dict:
    """Linha do tempo de 1 s de 'a' a 'b'. Em cada segundo a velocidade é a do GPS (posição boa a até 1,5 s; abaixo de
    1,5 km/h, ou com o OBD dizendo 0, é 0); senão a do OBD × k (amostra a até 2,5 s); senão a última conhecida por até
    5 s; senão 0. fixes e fast_rows: sequências de (ts, km/h) em ordem (ou linhas com 'ts' e 'speed').
    km = Σ v ÷ 3600 com coverage ≥ 0,5; senão obd_km × k (obd_km = a conta antiga, intacta)."""
    g = [(r[0], r[1]) if isinstance(r, tuple) else (r["ts"], r["speed"]) for r in fixes]
    o = [(r[0], r[1]) if isinstance(r, tuple) else (r["ts"], r["speed"]) for r in fast_rows]
    g = [x for x in g if x[1] is not None]
    if obd_km is None:
        obd_km = placesmod.obd_km(o)
    ts_all = [x[0] for x in g] + [x[0] for x in o]
    if a is None:
        a = min(ts_all) if ts_all else 0
    if b is None:
        b = max(ts_all) if ts_all else a
    gt, ot = [x[0] for x in g], [x[0] for x in o]
    km = gps_km = 0.0
    moving_s = gps_s = stopped_s = 0
    last_v, last_t = None, None
    n = max(0, int((b - a) // 1000))
    for i in range(n):
        t = a + i * 1000
        gi = _nearest(gt, t, GPS_TOL_MS)
        oi = _nearest(ot, t, OBD_TOL_MS)
        src = None
        if gi is not None:
            v = g[gi][1]
            if v < STOP_KMH or (oi is not None and o[oi][1] == 0):
                v = 0.0
            src = "gps"
        elif oi is not None and o[oi][1] is not None:
            v, src = o[oi][1] * k, "obd"
        elif last_v is not None and t - last_t <= HOLD_MS:
            v = last_v
        else:
            v = 0.0
        if src:
            last_v, last_t = v, t
        km += v / 3600
        if v > MOVING_KMH:
            moving_s += 1
            if src == "gps":
                gps_s += 1
                gps_km += v / 3600
        else:
            stopped_s += 1
    coverage = gps_s / moving_s if moving_s else 0.0
    final = km if coverage >= MIN_COVERAGE else obd_km * k
    return {"km": round(final, 2), "hybrid_km": round(km, 3), "gps_km": round(gps_km, 3), "obd_km": round(obd_km, 3),
            "coverage": round(coverage, 3), "moving_s": moving_s, "stopped_s": stopped_s, "k": k}


def trip_distance(con, a: int, b: int, obd_km: float, cfg: dict | None = None) -> dict | None:
    """Para GpsHub.trip_distance: None sem nenhuma posição boa (a distância continua sendo a do OBD, como antes)."""
    fixes = good_fixes(con, a, b, cfg)
    if not fixes:
        return None
    fast = con.execute("SELECT ts, speed FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
    return hybrid_distance([(r["ts"], r["speed"]) for r in fixes], [(r["ts"], r["speed"]) for r in fast], speedo_k(con), a, b, obd_km)


# ---------------------------------------------------------------- paradas e altitude
def find_stops(fixes: list, min_s: float = STOP_MIN_S) -> list[dict]:
    """Intervalos com velocidade < 3 km/h por ≥ min_s, fora as pontas da viagem (sair da vaga, chegar)."""
    out, i, n = [], 0, len(fixes)
    while i < n:
        if (fixes[i]["speed"] or 0) < STOP_SPEED:
            j = i
            while j + 1 < n and (fixes[j + 1]["speed"] or 0) < STOP_SPEED:
                j += 1
            if i > 0 and j < n - 1:
                t_a, t_b = fixes[i]["ts"], fixes[j + 1]["ts"]
                dur = (t_b - t_a) / 1000
                if dur >= min_s:
                    seg = fixes[i:j + 1]
                    out.append({"ts": t_a, "s": int(round(dur)), "lat": round(sum(f["lat"] for f in seg) / len(seg), 5),
                                "lon": round(sum(f["lon"] for f in seg) / len(seg), 5)})
            i = j + 1
        else:
            i += 1
    return out


def smooth_alt(fixes: list) -> list[float | None]:
    """Mediana móvel de 9 s (± 4,5 s) da altitude."""
    ts = [f["ts"] for f in fixes]
    alts = [f["alt"] for f in fixes]
    out: list[float | None] = []
    lo = hi = 0
    for i, t in enumerate(ts):
        while ts[lo] < t - ALT_WIN_MS:
            lo += 1
        while hi + 1 < len(ts) and ts[hi + 1] <= t + ALT_WIN_MS:
            hi += 1
        w = [x for x in alts[lo:hi + 1] if x is not None]
        out.append(statistics.median(w) if w and alts[i] is not None else None)
    return out


def altitude(fixes: list) -> dict:
    sm = [x for x in smooth_alt(fixes) if x is not None]
    if not sm:
        return {"alt_min": None, "alt_max": None, "climb_m": None, "descent_m": None}
    climb = desc = 0.0
    ref = sm[0]
    for x in sm[1:]:                                # histerese de 3 m contra o ruído
        if x - ref >= ALT_HYST_M:
            climb += x - ref
            ref = x
        elif ref - x >= ALT_HYST_M:
            desc += ref - x
            ref = x
    return {"alt_min": round(min(sm), 1), "alt_max": round(max(sm), 1), "climb_m": round(climb), "descent_m": round(desc)}


# ---------------------------------------------------------------- trajeto reduzido (4.5)
def _num(v, d):
    if v is None:
        return None
    x = round(float(v), d)
    return int(x) if d == 0 or x == int(x) else x


def build_track(fixes: list, fast_rows: list | None = None, slow: dict | None = None, tid: int | None = None,
                eps_m: float = TRACK_EPS_M, max_pts: int = TRACK_MAX_PTS, every_s: float = TRACK_EVERY_S,
                limits: list | None = None) -> dict | None:
    """Douglas–Peucker (6 m), mantendo ao menos um ponto a cada 5 s em movimento e as pontas dos buracos; até 800
    pontos (a tolerância dobra até caber) e 100 kB. Colunas com o valor mais próximo no tempo (fast_sample ±1,5 s;
    sample ±6 s; null sem dado). 't' e 'gaps' em segundos desde t0. limits (F7): limite da via em cada posição
    (geo.db), vira a coluna "limit"."""
    if not fixes:
        return None
    fast_rows = fast_rows or []
    slow = slow or {}
    t0 = fixes[0]["ts"]
    pts = [(f["lat"], f["lon"]) for f in fixes]
    gaps, forced_gap = [], set()
    for i in range(1, len(fixes)):
        p, q = fixes[i - 1], fixes[i]
        if (q["ts"] - p["ts"]) / 1000 > GAP_S and min(p["speed"] or 0, q["speed"] or 0) >= STOP_SPEED:
            gaps.append([_num((p["ts"] - t0) / 1000, 1), _num((q["ts"] - t0) / 1000, 1)])
            forced_gap |= {i - 1, i}
    try:
        slope = terrainmod.slope_series(fixes)          # F6: coluna "slope" (inclinação, %) no trajeto
    except Exception:
        slope = None
    eps, every = eps_m, every_s
    for _round in range(40):
        keep = set(simplify(pts, eps)) | forced_gap | {0, len(fixes) - 1}
        last = None
        for i, f in enumerate(fixes):                # no mínimo um ponto a cada 5 s em movimento
            if i in keep:
                last = f["ts"]
                continue
            if (f["speed"] or 0) >= STOP_SPEED and last is not None and (f["ts"] - last) / 1000 >= every:
                keep.add(i)
                last = f["ts"]
        idx = sorted(keep)
        if len(idx) > max_pts:
            eps *= 2
            if len(forced_gap) + (fixes[-1]["ts"] - t0) / 1000 / every > max_pts:
                every *= 2
            continue
        trk = _assemble(fixes, idx, fast_rows, slow, t0, tid, gaps, slope, limits)
        if len(json.dumps(trk, separators=(",", ":"))) <= TRACK_MAX_BYTES or len(idx) <= 50:
            trk["eps_m"] = eps
            return trk
        eps *= 2
        every *= 2
    step = max(1, len(fixes) // 50)                  # caso patológico (centenas de buracos): amostra uniforme
    idx = sorted(set(range(0, len(fixes), step)) | {len(fixes) - 1})
    trk = _assemble(fixes, idx, fast_rows, slow, t0, tid, gaps[:50], slope, limits)
    trk["eps_m"] = None
    return trk


def _assemble(fixes, idx, fast_rows, slow, t0, tid, gaps, slope=None, limits=None) -> dict:
    sel = [fixes[i] for i in idx]
    fts = [r["ts"] for r in fast_rows]
    cols: dict[str, list] = {"speed": [_num(f["speed"], 1) for f in sel]}
    fpick = [_nearest(fts, f["ts"], FAST_TOL_MS) for f in sel]
    for c in FAST_COLS:
        d = 0 if c in ("rpm", "gear", "map") else 1 if c in ("load", "tps") else 2
        cols[c] = [_num(fast_rows[j][c], d) if j is not None else None for j in fpick]
    cols["kml"] = [_num(s / l, 1) if s is not None and l and l > 0.05 and s >= STOP_SPEED else None
                   for s, l in zip(cols["speed"], cols["fuel_lh"])]
    for name, key in SLOW_COLS:
        ser = slow.get(key) or []
        sts = [x[0] for x in ser]
        d = 3 if name == "lam" else 1
        cols[name] = [(lambda j: _num(ser[j][1], d) if j is not None else None)(_nearest(sts, f["ts"], SLOW_TOL_MS)) for f in sel]
    if slope and any(x is not None for x in slope):
        cols["slope"] = [_num(slope[i], 1) for i in idx]
    if limits and any(x is not None for x in limits):
        cols["limit"] = [limits[i] for i in idx]
    lats, lons = [f["lat"] for f in sel], [f["lon"] for f in sel]
    return {"v": 1, "trip": tid, "t0": t0, "n": len(sel), "bbox": [round(min(lats), 5), round(min(lons), 5), round(max(lats), 5), round(max(lons), 5)],
            "t": [_num((f["ts"] - t0) / 1000, 1) for f in sel], "lat": [round(x, 5) for x in lats], "lon": [round(x, 5) for x in lons],
            "alt": [_num(f["alt"], 0) for f in sel], "cols": cols, "gaps": gaps}


# ---------------------------------------------------------------- textos
def route_label(start_name, end_name, start_pid=None, end_pid=None, places_only: bool = False) -> str | None:
    """"Casa → Trabalho"; mesma ponta = "Casa (manobra)". places_only=True (ntfy): só nomes de lugares salvos, nunca
    coordenada."""
    if places_only:
        start_name = start_name if start_pid else None
        end_name = end_name if end_pid else None
    if start_pid and start_pid == end_pid:
        return f"{start_name} (manobra)"
    if start_name and end_name:
        return f"{start_name} → {end_name}"
    if start_name:
        return f"saindo de {start_name}"
    if end_name:
        return f"chegando em {end_name}"
    return None


# ---------------------------------------------------------------- lugares automáticos
def _day(ts: int) -> tuple:
    lt = time.localtime(ts / 1000)
    return (lt.tm_year, lt.tm_yday)


def auto_place(con, tid: int, lat: float, lon: float, ts: int) -> int | None:
    """Destino sem lugar salvo que se repete (3 fins de viagem a menos de 80 m, em dias diferentes) vira lugar 'auto'."""
    for p in placesmod.load_places(con, with_deleted=True):
        if p["deleted"] and haversine_m(lat, lon, p["lat"], p["lon"]) <= max(AUTO_PLACE_M, p["radius_m"] or 0):
            return None                              # o dono apagou um lugar aqui: não insiste
    rows = con.execute("""SELECT g.trip_id, g.end_lat, g.end_lon, t.end_ts FROM trip_gps g JOIN trip t ON t.id = g.trip_id
                          WHERE g.end_place_id IS NULL AND g.end_lat IS NOT NULL AND g.trip_id != ?""", (tid,)).fetchall()
    near = [r for r in rows if haversine_m(lat, lon, r["end_lat"], r["end_lon"]) <= AUTO_PLACE_M]
    days = {_day(r["end_ts"] or 0) for r in near} | {_day(ts)}
    if len(days) < AUTO_PLACE_DAYS:
        return None
    la = (sum(r["end_lat"] for r in near) + lat) / (len(near) + 1)
    lo = (sum(r["end_lon"] for r in near) + lon) / (len(near) + 1)
    n = con.execute("SELECT COUNT(*) FROM place WHERE kind='auto'").fetchone()[0] + 1
    return placesmod.create_place(con, f"Lugar frequente {n}", "auto", round(la, 6), round(lo, 6), placesmod.DEFAULT_RADIUS_M, [], ts)


# ---------------------------------------------------------------- finalizador
TG_COLS = ("v", "n_fix", "coverage", "gps_km", "obd_km", "moving_s", "stopped_s", "n_stops", "start_lat", "start_lon", "start_src",
           "end_lat", "end_lon", "start_place_id", "end_place_id", "start_name", "end_name", "alt_min", "alt_max", "climb_m",
           "descent_m", "cost", "cost_km", "stops_json", "track_json", "flags", "updated_ts")


def _upsert(con, tid: int, d: dict) -> None:
    cols = [c for c in TG_COLS if c in d]
    con.execute(f"INSERT INTO trip_gps(trip_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})"
                f" ON CONFLICT(trip_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}",
                (tid, *[d[c] for c in cols]))


def _slow_series(con, a: int, b: int) -> dict:
    out = {}
    for _, key in SLOW_COLS:
        out[key] = [(r[0], r[1]) for r in con.execute(
            "SELECT s.ts, s.value FROM sample s JOIN signal g ON g.id = s.signal_id WHERE g.key=? AND s.ts BETWEEN ? AND ? ORDER BY s.ts",
            (key, a - SLOW_TOL_MS, b + SLOW_TOL_MS))]
    return out


def finalize(con, tid: int, ctx=None) -> dict | None:
    """Primeiro item de FINALIZERS. Lê o banco, chama as funções puras e grava trip_gps (uma linha por viagem)."""
    cfg = getattr(ctx, "cfg", None) or {}
    geo = getattr(ctx, "geo", None)
    t = con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
    if not t:
        return None
    a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
    now = int(time.time() * 1000)
    fixes = good_fixes(con, a, b, cfg)
    if len(fixes) < MIN_FIXES:
        d = {"v": VERSION, "n_fix": len(fixes), "coverage": 0.0, "flags": "sem_trajeto", "updated_ts": now}
        _upsert(con, tid, d)
        return d
    fast = con.execute("SELECT ts, speed, rpm, gear, load, map, tps, maf, fuel_lh FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts",
                       (a, b)).fetchall()
    obd_km = t["obd_km"] if t["obd_km"] is not None else placesmod.obd_km([(r["ts"], r["speed"]) for r in fast])
    dist = hybrid_distance([(f["ts"], f["speed"]) for f in fixes], [(r["ts"], r["speed"]) for r in fast], speedo_k(con), a, b, obd_km)
    plist = placesmod.load_places(con)
    flags = []
    # origem: a primeira posição boa, ou a de estacionamento quando ela explica o começo (partida a frio do GPS)
    first, last = fixes[0], fixes[-1]
    s_lat, s_lon, s_src = first["lat"], first["lon"], "gps"
    pk = placesmod.parked_before(con, a, max_hdop=float(cfg.get("max_hdop", 5.0)), min_sats=int(cfg.get("min_sats", 5)))
    if pk and pk["valid"]:
        explained = placesmod.obd_km([(r["ts"], r["speed"]) for r in fast if r["ts"] <= first["ts"]]) * 1000
        if haversine_m(first["lat"], first["lon"], pk["lat"], pk["lon"]) <= max(ORIGIN_PARKED_M, explained * EXPLAINED_SLACK[0] + EXPLAINED_SLACK[1]):
            s_lat, s_lon, s_src = pk["lat"], pk["lon"], "estacionado"
    if first["ts"] - a > ORIGIN_LATE_MS:
        flags.append("origem_tardia")
    s_name, s_pid = name_of(con, geo, s_lat, s_lon, plist)
    e_name, e_pid = name_of(con, geo, last["lat"], last["lon"], plist)
    if e_pid is None:
        try:
            pid = auto_place(con, tid, last["lat"], last["lon"], last["ts"])
            if pid:
                plist = placesmod.load_places(con)
                e_name, e_pid = name_of(con, geo, last["lat"], last["lon"], plist)
        except Exception:
            pass
    if s_pid and s_pid == e_pid:
        flags.append("manobra")
    stops = find_stops(fixes)
    for s in stops:
        p = placesmod.place_at(plist, s["lat"], s["lon"])
        if p:
            s["place_id"], s["name"] = p["id"], p["name"]
    alt = altitude(fixes)
    price = con.execute("SELECT price_l FROM fuel_fill WHERE price_l IS NOT NULL AND ts <= ? ORDER BY ts DESC LIMIT 1", (b,)).fetchone()
    cost = round(t["fuel_l"] * price[0], 2) if price and t["fuel_l"] else None
    km = t["distance_km"] or dist["km"]
    cost_km = round(cost / km, 3) if cost is not None and km and km > 0.1 else None
    limits = None
    if geo is not None:                              # F7: limite da via (geo.db) em cada posição
        try:
            from .roads import track_limits
            limits = track_limits(geo, fixes)
        except Exception:
            limits = None
    trk = build_track(fixes, fast, _slow_series(con, a, b), tid, limits=limits)
    sess = con.execute("SELECT clock_synced FROM session WHERE id=?", (t["session_id"],)).fetchone() if t["session_id"] else None
    if sess is not None and not sess[0]:
        flags.append("hora_aproximada")
    d = {"v": VERSION, "n_fix": len(fixes), "coverage": dist["coverage"], "gps_km": dist["gps_km"], "obd_km": round(obd_km, 3),
         "moving_s": dist["moving_s"], "stopped_s": dist["stopped_s"], "n_stops": len(stops),
         "start_lat": round(s_lat, 6), "start_lon": round(s_lon, 6), "start_src": s_src,
         "end_lat": round(last["lat"], 6), "end_lon": round(last["lon"], 6),
         "start_place_id": s_pid, "end_place_id": e_pid, "start_name": s_name, "end_name": e_name, **alt,
         "cost": cost, "cost_km": cost_km, "stops_json": json.dumps(stops, ensure_ascii=False),
         "track_json": json.dumps(trk, separators=(",", ":")) if trk else None, "flags": ",".join(flags) or None, "updated_ts": now}
    _upsert(con, tid, d)
    return d


def summary(con, tid: int, names: bool = True) -> dict:
    """{"route": "Casa → Trabalho" | None, "parts": [...]} para o resumo do ntfy. Sem trajeto: nada (resumo igual ao de antes)."""
    g = con.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    if not g or (g["n_fix"] or 0) < MIN_FIXES:
        return {"route": None, "parts": []}
    route = route_label(g["start_name"], g["end_name"], g["start_place_id"], g["end_place_id"], places_only=True) if names else None
    parts = []
    if g["cost_km"]:
        parts.append(f"R$ {_br(g['cost_km'], 2)}/km")
    return {"route": route, "parts": parts}
