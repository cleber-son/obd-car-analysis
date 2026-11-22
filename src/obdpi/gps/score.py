"""Nota de condução por viagem, pelo GPS (docs/GPS-ARQUITETURA.md, seção 9.10): segurança e suavidade.

A nota de direção econômica (tripdiag.eco_score, pelo OBD) continua; esta mede freadas, arrancadas e curvas fortes,
excesso de velocidade, motor frio forçado e giro no limite. `drive_score` é pura (listas → nota e detalhe); `finalize`
é o item de hub.FINALIZERS: grava trip.drive_score, trip_gps.drive_score/drive_json e um geo_event por acontecimento.
Os limites de velocidade da via entram na F7 (`limits`); sem eles, o excesso conta acima de 115 km/h.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from bisect import bisect_right

from . import store
from .geo import bearing_deg, haversine_m

VERSION = 1
BRAKE_KMHS = -11.0           # freada forte: ≤ −11 km/h em 1 s inteiro
LAUNCH_KMHS = 9.0            # arrancada forte: ≥ +9 km/h em 1 s
CURVE_MS2, CURVE_KMH = 3.5, 20.0
OVER_KMH = 115.0             # sem limite da via
COLD_ECT, COLD_RPM, COLD_LOAD = 60.0, 3000, 80.0
REDLINE = 5500
MIN_KM, MIN_S, MIN_COV = 2.0, 180, 0.7
PAIR_MS = (800, 1300)        # duas posições de 1 Hz seguidas
EVENT_KINDS = ("freada", "arrancada", "curva")
#          desconto por evento a cada 10 km / teto
W = {"freada": (5, 25), "arrancada": (4, 20), "curva": (4, 20)}
NAMES = {"freada": "freada forte", "arrancada": "arrancada forte", "curva": "curva forte"}


def _get(r, k):
    try:
        return r[k]
    except (KeyError, IndexError, TypeError):
        return None


def one_hz(fixes: list) -> list:
    """Uma posição por segundo cheio (as rajadas de 5 Hz da F6 não contam duas vezes)."""
    out, last = [], None
    for f in fixes:
        s = f["ts"] // 1000
        if s != last and f["speed"] is not None:
            out.append(f)
            last = s
    return out


def _signed(a: float, b: float) -> float:
    return (b - a + 540) % 360 - 180


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j + 1 < n and flags[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def events(fx: list) -> list[dict]:
    """Freadas, arrancadas e curvas fortes sobre posições de 1 Hz. Segundos seguidos acima do limiar = 1 evento."""
    out = []
    n = len(fx)
    acc = [None] * n                          # km/h por segundo entre a posição i e a i+1
    for i in range(n - 1):
        dt = fx[i + 1]["ts"] - fx[i]["ts"]
        if PAIR_MS[0] <= dt <= PAIR_MS[1]:
            acc[i] = (fx[i + 1]["speed"] - fx[i]["speed"]) / (dt / 1000)
    for kind, test in (("freada", lambda a: a is not None and a <= BRAKE_KMHS), ("arrancada", lambda a: a is not None and a >= LAUNCH_KMHS)):
        for i, j in _runs([test(a) for a in acc]):
            k = min(range(i, j + 1), key=lambda x: acc[x]) if kind == "freada" else max(range(i, j + 1), key=lambda x: acc[x])
            a, b = fx[k], fx[k + 1]
            out.append({"kind": kind, "ts": a["ts"], "lat": (a["lat"] + b["lat"]) / 2, "lon": (a["lon"] + b["lon"]) / 2,
                        "value": round(acc[k], 1), "kmh": round(a["speed"])})
    # curvas: ω = variação do rumo (graus/s), mediana de 3; aceleração lateral = v·ω
    hdg = []
    for i, f in enumerate(fx):
        h = _get(f, "heading")
        if h is None and i + 1 < n and (f["speed"] or 0) >= 3:
            h = bearing_deg(f["lat"], f["lon"], fx[i + 1]["lat"], fx[i + 1]["lon"])
        hdg.append(h)
    om = [None] * n
    for i in range(n - 1):
        dt = fx[i + 1]["ts"] - fx[i]["ts"]
        if PAIR_MS[0] <= dt <= PAIR_MS[1] and hdg[i] is not None and hdg[i + 1] is not None:
            om[i] = _signed(hdg[i], hdg[i + 1]) / (dt / 1000)
    lat_a = [None] * n
    for i in range(1, n - 1):
        w = [om[i - 1], om[i], om[i + 1]]
        v = (fx[i]["speed"] + fx[i + 1]["speed"]) / 2
        if None in w or v < CURVE_KMH:
            continue
        lat_a[i] = v / 3.6 * abs(math.radians(statistics.median(w)))
    for i, j in _runs([x is not None and x > CURVE_MS2 for x in lat_a]):
        k = max(range(i, j + 1), key=lambda x: lat_a[x])
        a, b = fx[k], fx[k + 1]
        out.append({"kind": "curva", "ts": a["ts"], "lat": (a["lat"] + b["lat"]) / 2, "lon": (a["lon"] + b["lon"]) / 2,
                    "value": round(lat_a[k], 2), "kmh": round(a["speed"])})
    out.sort(key=lambda e: e["ts"])
    return out


def _ect_at(ect: list, t: int):
    i = bisect_right([x[0] for x in ect], t) - 1 if ect else -1
    return ect[i][1] if i >= 0 and t - ect[i][0] <= 120_000 else None


def drive_score(fixes: list, fast_rows: list, limits=None, *, ect: list | None = None, km: float | None = None,
                dur_s: float | None = None, coverage: float | None = None) -> tuple[int | None, dict]:
    """Nota 0–100 (100 − descontos) e o detalhe. fixes: linhas/dicionários com ts, lat, lon, speed, heading (posições
    boas); fast_rows: ts, rpm, load; ect: [(ts, °C)]; limits: limite (km/h) por posição de 1 Hz, ou None (F7).
    Viagem com menos de 2 km, 3 min ou GPS em menos de 70 % do tempo andando: (None, {"motivo": …})."""
    fx = one_hz(fixes)
    if km is None:
        km = sum(haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(fx, fx[1:])) / 1000
    if dur_s is None:
        dur_s = (fx[-1]["ts"] - fx[0]["ts"]) / 1000 if fx else 0
    if coverage is None:
        coverage = 1.0
    if km < MIN_KM or dur_s < MIN_S or coverage < MIN_COV or len(fx) < 30:
        why = "viagem curta" if km < MIN_KM or dur_s < MIN_S else "pouco GPS na viagem"
        return None, {"motivo": why, "km": round(km, 2)}
    ev = events(fx)
    cnt = {k: sum(1 for e in ev if e["kind"] == k) for k in EVENT_KINDS}
    per10 = 10 / max(km, 0.1)
    pen = {k: min(W[k][1], cnt[k] * per10 * W[k][0]) for k in EVENT_KINDS}
    # excesso de velocidade: % do tempo em movimento acima do limite × 1,1 + 3 (sem limite: 115 km/h)
    mov = over = 0
    for i, f in enumerate(fx):
        v = f["speed"] or 0
        if v <= 3:
            continue
        mov += 1
        lim = limits[i] if limits and i < len(limits) and limits[i] else None
        if v > (lim * 1.1 + 3 if lim else OVER_KMH):
            over += 1
    over_pct = over / mov * 100 if mov else 0.0
    pen["velocidade"] = min(20, over_pct * 0.5)
    # motor frio forçado e giro no limite (pelo OBD)
    cold = red = 0.0
    ect = ect or []
    for p, r in zip(fast_rows, fast_rows[1:]):
        dt = (r["ts"] - p["ts"]) / 1000
        if not 0 < dt <= 5:
            continue
        rpm, load = p["rpm"] or 0, _get(p, "load") or 0
        e = _ect_at(ect, p["ts"])
        if e is not None and e < COLD_ECT and (rpm > COLD_RPM or load > COLD_LOAD):
            cold += dt
        if rpm > REDLINE:
            red += dt
    pen["motor_frio"] = min(10, cold / 12)
    pen["giro_limite"] = min(5, red / 5)
    score = int(round(max(0, 100 - sum(pen.values()))))
    tips = []
    if pen["freada"] >= 5:
        tips.append("Antecipe as freadas: tirar o pé mais cedo evita as freadas fortes (e o corte de combustível é consumo zero).")
    if pen["arrancada"] >= 4:
        tips.append("Arranque de forma mais progressiva: arrancadas fortes gastam pneu, embreagem e combustível.")
    if pen["curva"] >= 4:
        tips.append("Entre nas curvas mais devagar: a força lateral alta cansa a suspensão e os pneus e diminui a margem de segurança.")
    if pen["velocidade"] >= 2:
        tips.append("Parte da viagem foi acima do limite" + (" da via." if limits else " de 115 km/h."))
    if pen["motor_frio"] >= 2:
        tips.append("Com o motor frio (abaixo de 60 °C), evite passar de 3.000 rpm ou pisar fundo: o óleo ainda não protege tudo.")
    if pen["giro_limite"] >= 1:
        tips.append("Rotação acima de 5.500 rpm por vários segundos: só vale em ultrapassagem.")
    det = {"v": VERSION, "penalidades": {k: round(v, 1) for k, v in pen.items()}, "eventos": ev,
           "freadas": cnt["freada"], "arrancadas": cnt["arrancada"], "curvas": cnt["curva"],
           "acima_pct": round(over_pct, 1), "motor_frio_s": int(cold), "giro_limite_s": int(red), "km": round(km, 2),
           "limites": "da via" if limits else "sem limites da via (acima de 115 km/h)", "dicas": tips}
    return score, det


def finalize(con, tid: int, ctx=None) -> dict | None:
    """Item de FINALIZERS (depois de trip.finalize, que cria a linha de trip_gps). Idempotente: refaz os eventos."""
    cfg = getattr(ctx, "cfg", None) or {}
    t = con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
    if not t:
        return None
    from .trip import good_fixes, _slow_series
    a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
    g = con.execute("SELECT coverage, n_fix FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    con.execute(f"DELETE FROM geo_event WHERE trip_id=? AND kind IN ({','.join('?' * len(EVENT_KINDS))})", (tid, *EVENT_KINDS))
    fixes = good_fixes(con, a, b, cfg) if g and (g["n_fix"] or 0) > 0 else []
    if not fixes:
        score, det = None, {"motivo": "sem GPS"}
    else:
        fast = con.execute("SELECT ts, rpm, load FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
        ect = _slow_series(con, a, b).get("ect") or []
        score, det = drive_score(fixes, fast, None, ect=ect, km=t["distance_km"], dur_s=(b - a) / 1000,
                                 coverage=g["coverage"] if g and g["coverage"] is not None else None)
        for e in det.get("eventos") or []:
            store.add_event(con, e["ts"], e["kind"], trip_id=tid, lat=round(e["lat"], 6), lon=round(e["lon"], 6), value=e["value"],
                            detail=json.dumps({"kmh": e["kmh"]}, ensure_ascii=False))
    con.execute("UPDATE trip SET drive_score=? WHERE id=?", (score, tid))
    js = json.dumps(det, ensure_ascii=False)
    con.execute("INSERT INTO trip_gps(trip_id, drive_score, drive_json, updated_ts) VALUES (?,?,?,?)"
                " ON CONFLICT(trip_id) DO UPDATE SET drive_score=excluded.drive_score, drive_json=excluded.drive_json",
                (tid, score, js, int(time.time() * 1000)))
    return {"score": score, **det}


def summary_part(con, tid: int) -> str | None:
    """"nota 87" para o resumo do ntfy (só quando a viagem tem nota)."""
    r = con.execute("SELECT drive_score FROM trip WHERE id=?", (tid,)).fetchone()
    return f"nota {r[0]}" if r and r[0] is not None else None


def trend(con, since: int) -> dict:
    """Nota de cada viagem do período e a tendência (sem posição nem nomes: rota aberta na nuvem)."""
    items = []
    for r in con.execute("""SELECT t.id, t.start_ts, t.distance_km, t.drive_score, t.eco_score, g.drive_json FROM trip t
                            LEFT JOIN trip_gps g ON g.trip_id = t.id
                            WHERE t.start_ts >= ? AND t.drive_score IS NOT NULL AND COALESCE(t.hidden, 0) = 0 ORDER BY t.start_ts""", (since,)):
        try:
            d = json.loads(r["drive_json"] or "{}")
        except ValueError:
            d = {}
        items.append({"trip_id": r["id"], "start_ts": r["start_ts"], "km": r["distance_km"], "score": r["drive_score"], "eco": r["eco_score"],
                      "freadas": d.get("freadas", 0), "arrancadas": d.get("arrancadas", 0), "curvas": d.get("curvas", 0),
                      "acima_pct": d.get("acima_pct"), "penalidades": d.get("penalidades") or {}, "dicas": d.get("dicas") or []})
    sc = [x["score"] for x in items]
    out = {"items": items, "n": len(items), "media": round(statistics.mean(sc), 1) if sc else None, "tendencia": None}
    if len(sc) >= 6:
        h = len(sc) // 2
        d = statistics.mean(sc[h:]) - statistics.mean(sc[:h])
        out["tendencia"] = {"delta": round(d, 1), "texto": "melhorando" if d >= 3 else "piorando" if d <= -3 else "estável"}
    tot = {k: sum(x[k] for x in items) for k in ("freadas", "arrancadas", "curvas")}
    km = sum(x["km"] or 0 for x in items)
    out["por_100km"] = {k: round(v / km * 100, 1) for k, v in tot.items()} if km > 0 else None
    return out
