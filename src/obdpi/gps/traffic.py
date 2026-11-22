"""Análise do trânsito pelo histórico do próprio carro (docs/GPS-ARQUITETURA.md, seção 9.19). Nenhum serviço de trânsito
de fora: tudo sai das viagens gravadas.

- `trip_traffic(fixes, fast_rows)` (pura): segundos parado com o motor ligado, segundos "arrastando" (3–15 km/h por
  janelas de 20 s ou mais) e os litros gastos em cada estado (integrando o consumo instantâneo do OBD).
- `finalize(con, tid, ctx)` (hub.FINALIZERS, antes das rotas): grava `trip_gps.traffic_json`.
- `report(con, since)`: por rota com 6+ viagens, mediana de duração, litros e atraso (em relação à viagem livre da
  rota, o percentil 10 das durações) por faixa de 30 min do horário de saída e por tipo de dia; o melhor horário
  ("Saindo às 7h10 em vez de 7h40 você ganha 9 min e 0,3 L") e os pontos onde o carro fica parado.

O atraso é calculado no relatório (não no fechamento): a viagem só entra na rota no finalizador seguinte (F4).
"""
from __future__ import annotations

import bisect
import json
import statistics
import time

from . import trip as tripmod
from .geo import cell

VERSION = 1
STOP_KMH = 1.5               # abaixo disso: parado
CRAWL = (3.0, 15.0)          # "arrastando": 3–15 km/h…
CRAWL_MIN_S = 20.0           # …por 20 s ou mais
MAX_DT_STOP_S = 35.0         # parado, a gravação é de 1 a cada 30 s
MAX_DT_S = 5.0               # andando, intervalo maior é buraco (túnel): não conta
ENGINE_RPM = 400
FAST_TOL_MS = 3000
MIN_TRIPS = 6                # rota com menos que isso: "poucos dados"
BAND_MIN = 30                # faixa de horário de saída
BAND_MIN_TRIPS = 3
BEST_WINDOW_MIN = 90         # melhor horário a até 90 min do habitual
GAIN_MIN_S = 120             # ganho menor que 2 min: "tanto faz"
FREE_PCT = 0.10              # viagem livre = percentil 10 das durações
HOT_DEG = 0.0005             # "onde eu fico parado": células de ~55 m…
HOT_FRAC = 0.30              # …com velocidade mediana < 30 % do percentil 85 da célula
HOT_MIN_N, HOT_MIN_TRIPS = 8, 3
HOT_MAX = 400
DAY_KINDS = ("útil", "sábado", "domingo")


# ---------------------------------------------------------------- funções puras
def _v(r, k):
    try:
        return r[k]
    except (KeyError, IndexError, TypeError):
        return None


def trip_traffic(fixes, fast_rows) -> dict:
    """fixes: posições boas da viagem (ts, speed); fast_rows: (ts, rpm, fuel_lh) ou linhas com essas chaves.
    {"v", "stopped_s", "crawl_s", "moving_s", "stopped_l", "crawl_l", "moving_l", "fuel_l"}."""
    pts = [(int(_v(f, "ts")), float(_v(f, "speed") or 0.0)) for f in fixes if _v(f, "ts") is not None]
    fr = [(int(_v(r, "ts") if not isinstance(r, tuple) else r[0]),
           (_v(r, "rpm") if not isinstance(r, tuple) else r[1]),
           (_v(r, "fuel_lh") if not isinstance(r, tuple) else r[2])) for r in fast_rows or []]
    fts = [r[0] for r in fr]

    def engine_on(t: int) -> bool:
        i = bisect.bisect_left(fts, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(fr) and abs(fr[j][0] - t) <= FAST_TOL_MS and (best is None or abs(fr[j][0] - t) < abs(fr[best][0] - t)):
                best = j
        return True if best is None else (fr[best][1] or 0) > ENGINE_RPM       # sem leitura por perto: viagem = motor ligado

    # intervalos entre posições: (a, b, estado)
    iv: list[list] = []
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        dt = (tb - ta) / 1000
        if dt <= 0:
            continue
        if va < STOP_KMH:                             # o estado do intervalo é o da posição em que ele começa
            if dt > MAX_DT_STOP_S:
                continue
            st = "stopped" if engine_on((ta + tb) // 2) else "off"
        else:
            if dt > MAX_DT_S:
                continue
            st = "crawl" if CRAWL[0] <= va < CRAWL[1] else "moving"
        iv.append([ta, tb, st])
    # arrastando só vale em janelas de 20 s ou mais (o resto é andar)
    i = 0
    while i < len(iv):
        if iv[i][2] != "crawl":
            i += 1
            continue
        j = i
        while j + 1 < len(iv) and iv[j + 1][2] == "crawl" and iv[j + 1][0] == iv[j][1]:
            j += 1
        if (iv[j][1] - iv[i][0]) / 1000 < CRAWL_MIN_S:
            for k in range(i, j + 1):
                iv[k][2] = "moving"
        i = j + 1
    out = {"v": VERSION, "stopped_s": 0.0, "crawl_s": 0.0, "moving_s": 0.0, "stopped_l": 0.0, "crawl_l": 0.0, "moving_l": 0.0}
    for a, b, st in iv:
        if st != "off":
            out[f"{st}_s"] += (b - a) / 1000
    starts = [x[0] for x in iv]
    total_l = 0.0
    for p, q in zip(fr, fr[1:]):
        dt = min((q[0] - p[0]) / 1000, MAX_DT_S)
        if dt <= 0 or p[2] is None:
            continue
        lit = p[2] * dt / 3600
        total_l += lit
        k = bisect.bisect_right(starts, (p[0] + q[0]) // 2) - 1
        st = iv[k][2] if 0 <= k < len(iv) and iv[k][0] <= (p[0] + q[0]) // 2 <= iv[k][1] else "moving"
        if st == "off":
            st = "stopped"
        out[f"{st}_l"] += lit
    out["fuel_l"] = total_l
    for k in list(out):
        if k.endswith("_s"):
            out[k] = int(round(out[k]))
        elif k.endswith("_l"):
            out[k] = round(out[k], 3)
    return out


def percentile(xs: list[float], p: float) -> float | None:
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def day_kind(ts: int) -> str:
    wd = time.localtime(ts / 1000).tm_wday
    return "sábado" if wd == 5 else "domingo" if wd == 6 else "útil"


def minute_of_day(ts: int) -> int:
    lt = time.localtime(ts / 1000)
    return lt.tm_hour * 60 + lt.tm_min


def hm(minute: float) -> str:
    m = int(round(minute)) % 1440
    return f"{m // 60}h{m % 60:02d}"


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def bands(trips: list[dict]) -> list[dict]:
    """trips: {start_ts, dur_s, fuel_l, delay_s}. Faixas de 30 min (hora local) com mediana de duração, litros e atraso."""
    g: dict[int, list] = {}
    for t in trips:
        g.setdefault(minute_of_day(t["start_ts"]) // BAND_MIN, []).append(t)
    out = []
    for b in sorted(g):
        ts = g[b]
        med = lambda k: statistics.median([x[k] for x in ts if x.get(k) is not None]) if any(x.get(k) is not None for x in ts) else None
        out.append({"faixa": f"{hm(b * BAND_MIN)}–{hm((b + 1) * BAND_MIN)}", "band": b, "n": len(ts),
                    "saida": hm(statistics.median(minute_of_day(x["start_ts"]) for x in ts)),
                    "saida_min": statistics.median(minute_of_day(x["start_ts"]) for x in ts),
                    "dur_s": med("dur_s"), "fuel_l": med("fuel_l"), "delay_s": med("delay_s")})
    return out


def best_time(bs: list[dict]) -> dict | None:
    """Faixa mais rápida (3+ viagens) a até 90 min do horário habitual (a faixa com mais viagens; empate: a mais lenta)."""
    ok = [b for b in bs if b["n"] >= BAND_MIN_TRIPS and b["dur_s"] is not None]
    if not ok:
        return None
    habit = max(ok, key=lambda b: (b["n"], b["dur_s"]))
    near = [b for b in ok if abs(b["saida_min"] - habit["saida_min"]) <= BEST_WINDOW_MIN]
    best = min(near, key=lambda b: b["dur_s"])
    gain = habit["dur_s"] - best["dur_s"]
    gain_l = (habit["fuel_l"] - best["fuel_l"]) if habit["fuel_l"] is not None and best["fuel_l"] is not None else None
    out = {"melhor": best["saida"], "habitual": habit["saida"], "ganho_s": int(round(gain)),
           "ganho_l": round(gain_l, 2) if gain_l is not None else None}
    if best is habit or gain < GAIN_MIN_S:
        out["texto"] = (f"Seu horário de costume ({habit['saida']}) já é o mais rápido." if best is habit else
                        f"Sair às {best['saida']} ou às {habit['saida']} dá quase no mesmo.")
        out["ganha"] = False
    else:
        txt = f"Saindo às {best['saida']} em vez de {habit['saida']} você ganha {int(round(gain / 60))} min"
        if gain_l is not None and gain_l >= 0.05:
            txt += f" e {_br(gain_l, 1)} L"
        out["texto"] = txt + "."
        out["ganha"] = True
    return out


def hotspots(tracks: list[dict], size: float = HOT_DEG) -> list[list]:
    """Células onde a velocidade mediana fica abaixo de 30 % do percentil 85 da própria célula: "onde eu fico parado"."""
    cells: dict[int, list] = {}
    for n, trk in enumerate(tracks):
        spd = (trk.get("cols") or {}).get("speed") or []
        for la, lo, v in zip(trk.get("lat") or [], trk.get("lon") or [], spd):
            if la is None or lo is None or v is None:
                continue
            c = cells.setdefault(cell(la, lo, size), [[], set(), 0.0, 0.0])
            c[0].append(v)
            c[1].add(n)
            c[2] += la
            c[3] += lo
    out = []
    for c in cells.values():
        vs, trips = c[0], c[1]
        if len(vs) < HOT_MIN_N or len(trips) < HOT_MIN_TRIPS:
            continue
        med, p85 = statistics.median(vs), percentile(vs, 0.85)
        if p85 and p85 >= 15 and med < HOT_FRAC * p85:
            out.append([round(c[2] / len(vs), 5), round(c[3] / len(vs), 5), round(med, 1), round(p85, 1), len(vs)])
    out.sort(key=lambda x: x[2] / x[3])
    return out[:HOT_MAX]


# ---------------------------------------------------------------- banco
def finalize(con, tid: int, ctx=None) -> dict | None:
    """Finalizador: trip_gps.traffic_json da viagem (só com trajeto)."""
    cfg = getattr(ctx, "cfg", None) or {}
    g = con.execute("SELECT n_fix FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    t = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (tid,)).fetchone()
    if not g or not t:
        return None
    if (g["n_fix"] or 0) < tripmod.MIN_FIXES:
        con.execute("UPDATE trip_gps SET traffic_json=NULL WHERE trip_id=?", (tid,))
        return None
    a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
    fixes = tripmod.good_fixes(con, a, b, cfg)
    fast = con.execute("SELECT ts, rpm, fuel_lh FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
    d = trip_traffic(fixes, fast)
    con.execute("UPDATE trip_gps SET traffic_json=? WHERE trip_id=?", (json.dumps(d, separators=(",", ":")), tid))
    return d


def _route_trips(con, route_id: int, since: int) -> list[dict]:
    rows = con.execute("""SELECT t.id, t.start_ts, t.duration_s, t.fuel_l, g.flags, g.traffic_json
                          FROM trip_gps g JOIN trip t ON t.id = g.trip_id
                          WHERE g.route_id = ? AND t.start_ts >= ? AND t.finalized = 1 ORDER BY t.start_ts""", (route_id, since)).fetchall()
    out = []
    for r in rows:
        if "atipica" in (r["flags"] or "").split(","):
            continue
        try:
            tr = json.loads(r["traffic_json"]) if r["traffic_json"] else None
        except ValueError:
            tr = None
        out.append({"id": r["id"], "start_ts": r["start_ts"], "dur_s": r["duration_s"], "fuel_l": r["fuel_l"], "traffic": tr})
    return out


def route_report(con, route: dict, since: int) -> dict:
    ts = [t for t in _route_trips(con, route["id"], since) if t["dur_s"]]
    out = {"route_id": route["id"], "name": route["name"], "n": len(ts)}
    if len(ts) < MIN_TRIPS:
        out.update(status="poucos dados", texto=f"Poucos dados: {len(ts)} viagem(ns) nesta rota (são precisas {MIN_TRIPS}).")
        return out
    free = percentile([t["dur_s"] for t in ts], FREE_PCT)
    for t in ts:
        t["delay_s"] = max(0.0, t["dur_s"] - free)
    trs = [t["traffic"] for t in ts if t["traffic"]]
    med = lambda xs: round(statistics.median(xs), 2) if xs else None
    out.update(status="ok", free_s=int(round(free)), dur_s=med([t["dur_s"] for t in ts]), fuel_l=med([t["fuel_l"] for t in ts if t["fuel_l"]]),
               delay_s=med([t["delay_s"] for t in ts]),
               stopped_s=med([x["stopped_s"] for x in trs]), crawl_s=med([x["crawl_s"] for x in trs]),
               stopped_l=med([x["stopped_l"] for x in trs]), crawl_l=med([x["crawl_l"] for x in trs]))
    by_day = {}
    for k in DAY_KINDS:
        sub = [t for t in ts if day_kind(t["start_ts"]) == k]
        if not sub:
            continue
        bs = bands(sub)
        by_day[k] = {"n": len(sub), "faixas": [{x: b[x] for x in ("faixa", "n", "saida", "dur_s", "fuel_l", "delay_s")} for b in bs],
                     "melhor": best_time(bs)}
    out["por_dia"] = by_day
    main = next((by_day[k]["melhor"] for k in DAY_KINDS if k in by_day and by_day[k]["melhor"]), None)
    out["melhor"] = main
    out["texto"] = main["texto"] if main else "Ainda sem horários suficientes para comparar (3 viagens por faixa de 30 min)."
    return out


def report(con, since: int, now: int | None = None) -> dict:
    routes = [dict(r) for r in con.execute("SELECT id, name FROM route WHERE n > 0 ORDER BY n DESC, id")]
    items = [route_report(con, r, since) for r in routes]
    tracks = []
    for r in con.execute("""SELECT g.track_json FROM trip_gps g JOIN trip t ON t.id = g.trip_id WHERE t.start_ts >= ? AND g.track_json IS NOT NULL
                            AND COALESCE(t.hidden,0) = 0 ORDER BY t.start_ts DESC LIMIT 200""", (since,)):
        try:
            tracks.append(json.loads(r[0]))
        except ValueError:
            pass
    tot = {"stopped_s": 0, "crawl_s": 0, "stopped_l": 0.0, "crawl_l": 0.0, "fuel_l": 0.0, "trips": 0}
    for r in con.execute("SELECT g.traffic_json FROM trip_gps g JOIN trip t ON t.id = g.trip_id WHERE t.start_ts >= ? AND g.traffic_json IS NOT NULL", (since,)):
        try:
            x = json.loads(r[0])
        except ValueError:
            continue
        tot["trips"] += 1
        for k in ("stopped_s", "crawl_s", "stopped_l", "crawl_l", "fuel_l"):
            tot[k] += x.get(k) or 0
    tot = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tot.items()}
    return {"routes": items, "total": tot, "hotspots": hotspots(tracks)}
