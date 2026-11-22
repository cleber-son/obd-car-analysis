"""Trecho de referência: o mesmo pedaço de rua medido em cada passagem, antes e depois de um reparo
(docs/GPS-ARQUITETURA.md, seção 9.5).

O dono escolhe uma viagem, toca no início e no fim sobre o traçado e dá um nome (`create`). Ficam dois portões
(posição, raio de 35 m, rumo de entrada), o comprimento pelo caminho e a duração de referência. Cada passagem
(`find_passes`) entra pelo portão A no mesmo sentido (rumo a até 60°) e sai pelo B dentro de 3 × a duração de
referência, com o caminho a ±15 % do de referência; `measure` grava as médias do motor naquele intervalo.
Válida (`valid`) = motor quente, sem parar (a menos que a referência pare), velocidade normal para o trecho e GPS
bom o tempo todo; os motivos ficam em `why` (códigos de WHY). `impact_for` alimenta o "antes e depois" de extras.impact().
"""
from __future__ import annotations

import math
import statistics
import time

from ..analytics.tripdiag import expected_air_gs
from . import trip as tripmod
from .geo import _xy, ang_diff, bearing_deg, haversine_m

RADIUS_M = 35.0
HEADING_TOL = 60.0
EXIT_DUR_K = 3.0             # a saída tem de vir até 3 × a duração de referência depois da entrada
LEN_TOL = 0.15
PAIR_GAP_MS = 5000           # duas posições a mais de 5 s não formam um pedaço (buraco do GPS)
MIN_LEN_M, MIN_DUR_S = 200, 10
STOP_KMH, STOP_MIN_S = 3.0, 5.0
WARM_ECT = 75.0
SPEED_TOL = 0.20
MIX_DIFF = 0.3
SLOW_PAD_MS = 10_000         # sinais lentos (lidos a cada ~20 s no Pi): janela um pouco maior que o trecho
CMP_N = 5                    # antes e depois: até 5 passagens válidas de cada lado…
CMP_MIN = 2                  # …e pelo menos 2
WHY = {"frio": "motor frio na entrada", "sem_temp": "sem temperatura do motor", "parou": "parou no trecho",
       "gps": "GPS falhou no trecho", "vel": "velocidade fora do normal do trecho"}
PASS_COLS = ("trip_id", "ts_start", "ts_end", "dur_s", "km", "avg_speed", "stops", "fuel_l", "l100", "stft", "lam_err", "map", "load",
             "tps", "rpm", "timing", "air_ratio", "ect", "iat", "baro", "alt_gain", "fuel_mix", "valid", "why")


# ---------------------------------------------------------------- geometria das passagens
def _d(f, k):
    return f[k] if not isinstance(f, dict) else f.get(k)


def _approaches(fixes: list, lat: float, lon: float, radius: float) -> list[dict]:
    """Maior aproximação de cada ida ao portão: por pedaço entre duas posições (o carro rápido pode não ter posição
    dentro do raio), projeta o portão no pedaço; pedaços seguidos dentro do raio formam uma ida, e vale o mais perto.
    Devolve {ts, dist, i, f, hdg, pos_m} (instante interpolado, pedaço i→i+1, fração, rumo, metros desde a 1ª posição)."""
    out, run, cum = [], None, 0.0
    for i in range(len(fixes) - 1):
        p, q = fixes[i], fixes[i + 1]
        seg = haversine_m(_d(p, "lat"), _d(p, "lon"), _d(q, "lat"), _d(q, "lon"))
        if _d(q, "ts") - _d(p, "ts") > PAIR_GAP_MS:
            if run:
                out.append(run)
            run = None
            cum += seg
            continue
        ax, ay = _xy(lat, lon, _d(p, "lat"), _d(p, "lon"))
        bx, by = _xy(lat, lon, _d(q, "lat"), _d(q, "lon"))
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        f = max(0.0, min(1.0, -(ax * dx + ay * dy) / L2)) if L2 > 0.25 else 0.0
        dist = ((ax + f * dx) ** 2 + (ay + f * dy) ** 2) ** 0.5
        if dist <= radius:
            hdg = bearing_deg(_d(p, "lat"), _d(p, "lon"), _d(q, "lat"), _d(q, "lon")) if seg >= 3 else (_d(q, "heading") or _d(p, "heading"))
            cand = {"ts": _d(p, "ts") + f * (_d(q, "ts") - _d(p, "ts")), "dist": dist, "i": i, "f": f, "hdg": hdg, "pos_m": cum + f * seg}
            if run is None or dist < run["dist"]:
                if run is not None and hdg is None:
                    cand["hdg"] = run["hdg"]
                run = cand
            elif run["hdg"] is None:
                run["hdg"] = hdg
        elif run:
            out.append(run)
            run = None
        cum += seg
    if run:
        out.append(run)
    return out


def find_passes(seg: dict, fixes: list) -> list[tuple[int, int]]:
    """Pares (ts_a, ts_b) das passagens pelo trecho, em ordem, sem sobreposição."""
    if len(fixes) < 2 or not seg.get("length_m"):
        return []
    radius = seg.get("radius_m") or RADIUS_M
    ents = [e for e in _approaches(fixes, seg["a_lat"], seg["a_lon"], radius)
            if e["hdg"] is not None and (seg.get("a_heading") is None or ang_diff(e["hdg"], seg["a_heading"]) <= HEADING_TOL)]
    if not ents:
        return []
    exits = _approaches(fixes, seg["b_lat"], seg["b_lon"], radius)
    out, last = [], -1.0
    span = EXIT_DUR_K * (seg.get("ref_dur_s") or 600) * 1000
    for e in ents:
        if e["ts"] <= last:
            continue
        for x in exits:
            if x["ts"] <= e["ts"]:
                continue
            if x["ts"] > e["ts"] + span:
                break
            if abs((x["pos_m"] - e["pos_m"]) - seg["length_m"]) <= LEN_TOL * seg["length_m"]:
                out.append((int(round(e["ts"])), int(round(x["ts"]))))
                last = x["ts"]
                break
    return out


def _path_m(fixes: list, a: int, b: int) -> float:
    """Metros percorridos entre os instantes a e b (interpolando nas pontas)."""
    pts = []
    for i, f in enumerate(fixes):
        if a <= f["ts"] <= b:
            pts.append((f["lat"], f["lon"]))
    for ts, at_start in ((a, True), (b, False)):
        p = _interp(fixes, ts)
        if p:
            pts.insert(0, p) if at_start else pts.append(p)
    return sum(haversine_m(p[0], p[1], q[0], q[1]) for p, q in zip(pts, pts[1:]))


def _interp(fixes: list, ts: float):
    for p, q in zip(fixes, fixes[1:]):
        if p["ts"] <= ts <= q["ts"]:
            f = (ts - p["ts"]) / (q["ts"] - p["ts"]) if q["ts"] > p["ts"] else 0.0
            return (p["lat"] + (q["lat"] - p["lat"]) * f, p["lon"] + (q["lon"] - p["lon"]) * f)
    return None


# ---------------------------------------------------------------- medidas
def _series(con, key: str, a: int, b: int) -> list[tuple[int, float]]:
    return [(r[0], r[1]) for r in con.execute(
        "SELECT s.ts, s.value FROM sample s JOIN signal g ON g.id = s.signal_id WHERE g.key=? AND s.ts BETWEEN ? AND ? ORDER BY s.ts", (key, a, b))]


def _mean(xs, d: int = 3):
    v = [x for x in xs if x is not None]
    return round(statistics.fmean(v), d) if v else None


def _fixes(con, a: int, b: int, cfg: dict | None = None) -> list[dict]:
    return [dict(r) for r in tripmod.good_fixes(con, a, b, cfg)]


def measure(con, seg: dict, ts_a: int, ts_b: int, fixes: list | None = None, trip_id: int | None = None) -> dict:
    """Linha de segment_pass: duração, km, velocidade, paradas, litros, L/100 km e as médias do motor no intervalo.
    `why` aqui leva só os motivos que não dependem das outras passagens (frio, parada, GPS); `revalidate` completa."""
    fx = fixes if fixes is not None else _fixes(con, ts_a - 10_000, ts_b + 10_000)
    inside = [f for f in fx if ts_a <= f["ts"] <= ts_b]
    dur = (ts_b - ts_a) / 1000
    km = _path_m(fx, ts_a, ts_b) / 1000
    fast = con.execute("SELECT ts, speed, rpm, map, load, tps, maf, fuel_lh FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts",
                       (ts_a, ts_b)).fetchall()
    fuel = 0.0
    for p, q in zip(fast, fast[1:]):
        dt = min((q["ts"] - p["ts"]) / 1000, 5)
        fuel += (p["fuel_lh"] or 0) * dt / 3600
    fuel_l = round(fuel, 4) if any(r["fuel_lh"] is not None for r in fast) else None
    sa, sb = ts_a - SLOW_PAD_MS, ts_b + SLOW_PAD_MS
    slow = {k: _series(con, k, sa, sb) for k in ("stft1", "o2c1_lambda", "lambda_cmd", "timing", "iat", "baro")}
    lam, cmd = slow["o2c1_lambda"], slow["lambda_cmd"]
    cts = [c[0] for c in cmd]
    lam_err = []
    for ts, v in lam:
        j = tripmod._nearest(cts, ts, 6000)
        if j is not None and v is not None and v <= 1.9 and cmd[j][1] is not None:
            lam_err.append(v - cmd[j][1])
    iat = _mean((v for _, v in slow["iat"]), 1)
    air = [r["maf"] / expected_air_gs(r["map"], r["rpm"], iat if iat is not None else 30.0)
           for r in fast if r["maf"] and r["map"] and r["rpm"] and r["rpm"] > 500]
    ect_rows = _series(con, "ect", ts_a - 60_000, ts_b)
    before = [v for ts, v in ect_rows if ts <= ts_a]
    ect = before[-1] if before else (ect_rows[0][1] if ect_rows else None)
    # paradas (< 3 km/h por ≥ 5 s) e buracos do GPS com o carro andando
    stops, i = 0, 0
    while i < len(inside):
        if (inside[i]["speed"] or 0) < STOP_KMH:
            j = i
            while j + 1 < len(inside) and (inside[j + 1]["speed"] or 0) < STOP_KMH:
                j += 1
            end = inside[j + 1]["ts"] if j + 1 < len(inside) else ts_b
            if (end - inside[i]["ts"]) / 1000 >= STOP_MIN_S:
                stops += 1
            i = j + 1
        else:
            i += 1
    span = [f for f in fx if f["ts"] >= ts_a - PAIR_GAP_MS and f["ts"] <= ts_b + PAIR_GAP_MS]
    gps_bad = not inside or any(q["ts"] - p["ts"] > PAIR_GAP_MS and min(p["speed"] or 0, q["speed"] or 0) >= STOP_KMH
                                for p, q in zip(span, span[1:]) if q["ts"] >= ts_a and p["ts"] <= ts_b)

    def alt_at(ts):
        v = [f["alt"] for f in fx if abs(f["ts"] - ts) <= 3000 and f["alt"] is not None]
        return statistics.fmean(v) if v else None
    aa, ab = alt_at(ts_a), alt_at(ts_b)
    mix = con.execute("SELECT mix_after FROM fuel_fill WHERE ts <= ? AND mix_after IS NOT NULL ORDER BY ts DESC LIMIT 1", (ts_a,)).fetchone()
    why = []
    if ect is None:
        why.append("sem_temp")
    elif ect < WARM_ECT:
        why.append("frio")
    if gps_bad:
        why.append("gps")
    if stops and not seg.get("_ref_stops") and (trip_id is None or trip_id != seg.get("ref_trip_id")):
        why.append("parou")
    avg = km / (dur / 3600) if dur > 0 else None
    return {"trip_id": trip_id, "ts_start": ts_a, "ts_end": ts_b, "dur_s": round(dur, 1), "km": round(km, 3),
            "avg_speed": round(avg, 1) if avg is not None else None, "stops": stops, "fuel_l": fuel_l,
            "l100": round(fuel_l / km * 100, 2) if fuel_l is not None and km > 0.1 else None,
            "stft": _mean(v for _, v in slow["stft1"]), "lam_err": _mean(lam_err, 4),
            "map": _mean(r["map"] for r in fast), "load": _mean(r["load"] for r in fast), "tps": _mean(r["tps"] for r in fast),
            "rpm": _mean((r["rpm"] for r in fast), 0), "timing": _mean(v for _, v in slow["timing"]),
            "air_ratio": _mean(air, 4), "ect": ect, "iat": iat, "baro": _mean(v for _, v in slow["baro"]),
            "alt_gain": round(ab - aa, 1) if aa is not None and ab is not None else None,
            "fuel_mix": mix[0] if mix else None, "valid": 0 if why else 1, "why": ",".join(why) or None}


# ---------------------------------------------------------------- banco
def get(con, seg_id: int) -> dict | None:
    r = con.execute("SELECT * FROM segment WHERE id=?", (seg_id,)).fetchone()
    return dict(r) if r else None


def label(con, seg_id: int) -> str:
    """"Trecho N" (posição pelo id): é o que aparece no "antes e depois" público, sem o nome dado pelo dono."""
    n = con.execute("SELECT COUNT(*) FROM segment WHERE id <= ?", (seg_id,)).fetchone()[0]
    return f"Trecho {n}"


def _ref_stops(con, seg: dict) -> int:
    r = con.execute("SELECT stops FROM segment_pass WHERE segment_id=? AND trip_id=? ORDER BY ts_start LIMIT 1",
                    (seg["id"], seg["ref_trip_id"])).fetchone()
    return (r[0] or 0) if r else 0


def _upsert(con, seg_id: int, p: dict) -> None:
    cols = PASS_COLS
    con.execute(f"INSERT INTO segment_pass(segment_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})"
                f" ON CONFLICT(segment_id, ts_start) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}",
                (seg_id, *[p.get(c) for c in cols]))


def revalidate(con, seg_id: int) -> None:
    """Critério que depende do conjunto: velocidade média a ±20 % da mediana do trecho (com 3+ passagens boas)."""
    rows = con.execute("SELECT id, avg_speed, why FROM segment_pass WHERE segment_id=?", (seg_id,)).fetchall()
    base = {r["id"]: [w for w in (r["why"] or "").split(",") if w and w != "vel"] for r in rows}
    spd = [r["avg_speed"] for r in rows if not base[r["id"]] and r["avg_speed"]]
    med = statistics.median(spd) if len(spd) >= 3 else None
    for r in rows:
        w = base[r["id"]]
        if med and r["avg_speed"] and abs(r["avg_speed"] - med) > SPEED_TOL * med:
            w = w + ["vel"]
        why = ",".join(w) or None
        if why != r["why"]:
            con.execute("UPDATE segment_pass SET why=?, valid=? WHERE id=?", (why, 0 if w else 1, r["id"]))
        else:
            con.execute("UPDATE segment_pass SET valid=? WHERE id=?", (0 if w else 1, r["id"]))


def _scan(con, seg: dict, tid: int, fixes: list) -> int:
    """Passagens deste trecho nesta viagem: grava as novas, atualiza as existentes, apaga as que sumiram."""
    seg = dict(seg, _ref_stops=_ref_stops(con, seg))
    found = find_passes(seg, fixes)
    keep = []
    for a, b in found:
        _upsert(con, seg["id"], measure(con, seg, a, b, fixes, tid))
        keep.append(a)
    q = "DELETE FROM segment_pass WHERE segment_id=? AND trip_id=?"
    con.execute(q + (f" AND ts_start NOT IN ({','.join('?' * len(keep))})" if keep else ""), (seg["id"], tid, *keep))
    return len(found)


def _trip_fixes(con, tid: int, cfg=None) -> list[dict]:
    t = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (tid,)).fetchone()
    if not t:
        return []
    return _fixes(con, t["start_ts"], t["end_ts"] or t["start_ts"], cfg)


def scan_trip(con, tid: int, ctx=None) -> int:
    """Finalizador: procura todos os trechos ativos nesta viagem. Sem trechos ou sem posição: nada (nem consulta o trajeto)."""
    segs = [dict(r) for r in con.execute("SELECT * FROM segment WHERE COALESCE(active, 1) = 1 ORDER BY id")]
    if not segs:
        return 0
    fixes = _trip_fixes(con, tid, getattr(ctx, "cfg", None))
    if len(fixes) < 2:
        return 0
    n = 0
    for s in segs:
        if not any(haversine_m(f["lat"], f["lon"], s["a_lat"], s["a_lon"]) <= 500 for f in fixes):
            if con.execute("SELECT 1 FROM segment_pass WHERE segment_id=? AND trip_id=? LIMIT 1", (s["id"], tid)).fetchone():
                con.execute("DELETE FROM segment_pass WHERE segment_id=? AND trip_id=?", (s["id"], tid))
                revalidate(con, s["id"])
            continue
        n += _scan(con, s, tid, fixes)
        revalidate(con, s["id"])
    return n


def rescan(con, seg_id: int) -> int:
    """Histórico inteiro de um trecho: todas as viagens com posição a até ~500 m do portão A. Idempotente
    (UNIQUE(segment_id, ts_start)); passagens que deixaram de existir são apagadas."""
    seg = get(con, seg_id)
    if not seg:
        return 0
    dlat = 500 / 111_000
    dlon = dlat / max(0.2, abs(math.cos(math.radians(seg["a_lat"]))))
    tids = [r[0] for r in con.execute("""SELECT DISTINCT t.id FROM gps_fix f JOIN trip t ON f.ts BETWEEN t.start_ts AND COALESCE(t.end_ts, t.start_ts)
                                         WHERE f.lat BETWEEN ? AND ? AND f.lon BETWEEN ? AND ? ORDER BY t.id""",
                                      (seg["a_lat"] - dlat, seg["a_lat"] + dlat, seg["a_lon"] - dlon, seg["a_lon"] + dlon))]
    # a viagem de referência primeiro: as paradas dela decidem se parar no trecho invalida as outras
    tids.sort(key=lambda t: t != seg["ref_trip_id"])
    n = 0
    for tid in tids:
        n += _scan(con, seg, tid, _trip_fixes(con, tid))
    others = [r[0] for r in con.execute("SELECT DISTINCT COALESCE(trip_id, -1) FROM segment_pass WHERE segment_id=?", (seg_id,))]
    for tid in others:
        if tid not in tids:
            con.execute("DELETE FROM segment_pass WHERE segment_id=? AND COALESCE(trip_id, -1)=?", (seg_id, tid))
    revalidate(con, seg_id)
    return n


def validate(body: dict) -> dict:
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Dê um nome ao trecho.")
    try:
        tid = int(body.get("trip_id"))
        a, b = int(body.get("t_a")), int(body.get("t_b"))
    except (TypeError, ValueError):
        raise ValueError("Escolha a viagem e marque o início e o fim no traçado.") from None
    if b < a:
        a, b = b, a
    return {"name": name, "trip_id": tid, "t_a": a, "t_b": b}


def create(con, name: str, trip_id: int, t_a: int, t_b: int, radius_m: float = RADIUS_M) -> int:
    """Grava o trecho a partir de dois instantes de uma viagem (ms, relógio do coletor) e busca no histórico."""
    fx = _trip_fixes(con, trip_id)
    if len(fx) < 10:
        raise ValueError("Esta viagem não tem trajeto gravado.")
    near = lambda ts: min(range(len(fx)), key=lambda i: abs(fx[i]["ts"] - ts))
    ia, ib = near(t_a), near(t_b)
    if ib <= ia:
        raise ValueError("O fim do trecho tem de vir depois do início.")
    length = sum(haversine_m(p["lat"], p["lon"], q["lat"], q["lon"]) for p, q in zip(fx[ia:ib], fx[ia + 1:ib + 1]))
    dur = (fx[ib]["ts"] - fx[ia]["ts"]) / 1000
    if length < MIN_LEN_M or dur < MIN_DUR_S:
        raise ValueError(f"Trecho curto demais ({length:.0f} m). Marque pelo menos {MIN_LEN_M} m de rua.")
    a = fx[ia]
    # rumo de entrada: do portão A até a primeira posição 25 m adiante (mais firme que o rumo do receptor)
    hdg = next((bearing_deg(a["lat"], a["lon"], f["lat"], f["lon"]) for f in fx[ia + 1:ib + 1]
                if haversine_m(a["lat"], a["lon"], f["lat"], f["lon"]) >= 25), a["heading"])
    sid = con.execute("""INSERT INTO segment(name, a_lat, a_lon, b_lat, b_lon, a_heading, radius_m, length_m, ref_dur_s, ref_trip_id, active, created_ts)
                         VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                      (name, round(a["lat"], 7), round(a["lon"], 7), round(fx[ib]["lat"], 7), round(fx[ib]["lon"], 7),
                       round(hdg, 1) if hdg is not None else None, radius_m, round(length, 1), round(dur, 1), trip_id,
                       int(time.time() * 1000))).lastrowid
    rescan(con, sid)
    return sid


def delete(con, seg_id: int) -> None:
    con.execute("DELETE FROM segment_pass WHERE segment_id=?", (seg_id,))
    con.execute("DELETE FROM segment WHERE id=?", (seg_id,))


# ---------------------------------------------------------------- histórico, regressão e antes/depois
def regression(passes: list[dict]):
    """L/100 km = a + b × velocidade média, sobre as passagens válidas (3+ e velocidades diferentes). None sem dados."""
    pts = [(p["avg_speed"], p["l100"]) for p in passes if p["valid"] and p["avg_speed"] and p["l100"]]
    if len(pts) < 3:
        return None
    mx, my = statistics.fmean(x for x, _ in pts), statistics.fmean(y for _, y in pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx > 1e-6 else 0.0
    return {"a": round(my - b * mx, 4), "b": round(b, 5), "n": len(pts)}


def _metrics():
    from .. import extras                            # mesmos mínimos de mudança do "antes e depois" (extras.IMPACT)
    I = extras.IMPACT
    return (("stft", "Ajuste curto", "%", "zero", I["stft_run"][3], 1, lambda p: p["stft"]),
            ("ar", "Ar medido ÷ esperado", "%", "maior", I["air_idle"][3], 0,
             lambda p: p["air_ratio"] * 100 if p["air_ratio"] is not None else None),
            ("kml", "Consumo", "km/L", "maior", I["kml"][3], 1,
             lambda p: p["km"] / p["fuel_l"] if p["fuel_l"] and p["fuel_l"] > 0.005 and p["km"] else None))


def compare(passes: list[dict], ts: int, window_ms: int | None = None) -> dict:
    """Mediana das até 5 passagens válidas antes do serviço × as até 5 depois."""
    ok = [p for p in passes if p["valid"] and (window_ms is None or abs(p["ts_start"] - ts) <= window_ms)]
    bef = sorted((p for p in ok if p["ts_start"] < ts), key=lambda p: p["ts_start"])[-CMP_N:]
    aft = sorted((p for p in ok if p["ts_start"] >= ts), key=lambda p: p["ts_start"])[:CMP_N]
    rows, better, worse = [], 0, 0
    for key, name, unit, goal, minch, dec, fn in _metrics():
        xs = [v for v in map(fn, bef) if v is not None]
        ys = [v for v in map(fn, aft) if v is not None]
        if len(xs) < CMP_MIN or len(ys) < CMP_MIN:
            continue
        x, y = statistics.median(xs), statistics.median(ys)
        gain = (abs(x) - abs(y)) if goal == "zero" else (x - y) if goal == "menor" else (y - x)
        v = "melhorou" if gain >= minch else "piorou" if gain <= -minch else "igual"
        better += v == "melhorou"
        worse += v == "piorou"
        rows.append({"key": key, "nome": name, "unidade": unit, "antes": round(x, dec), "depois": round(y, dec), "veredito": v})
    if len(bef) < CMP_MIN or len(aft) < CMP_MIN or not rows:
        verdict = "sem dados"
        text = (f"Poucas passagens válidas pelo trecho ({len(bef)} antes, {len(aft)} depois; são precisas {CMP_MIN} de cada lado).")
    elif better and not worse:
        verdict, text = "melhorou", f"Mesmo lugar, mesma velocidade: {better} indicador(es) melhoraram e nenhum piorou."
    elif worse and not better:
        verdict, text = "piorou", f"No mesmo trecho, {worse} indicador(es) pioraram depois do serviço."
    elif better or worse:
        verdict, text = "misto", f"Resultado misto no trecho: {better} melhor(es), {worse} pior(es)."
    else:
        verdict, text = "igual", "Sem mudança mensurável no trecho."
    summ = lambda ps: {"n": len(ps), "de": ps[0]["ts_start"] if ps else None, "ate": ps[-1]["ts_start"] if ps else None}
    return {"antes": summ(bef), "depois": summ(aft), "veredito": verdict, "texto": text, "indicadores": rows}


def _passes(con, seg_id: int) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM segment_pass WHERE segment_id=? ORDER BY ts_start", (seg_id,))]


def history(con, seg_id: int) -> dict | None:
    """Passagens em ordem (com o resíduo do consumo e a marca de mistura diferente), anotações de manutenção do
    período e o antes/depois de cada uma."""
    seg = get(con, seg_id)
    if not seg:
        return None
    ps = _passes(con, seg_id)
    reg = regression(ps)
    valid = [p for p in ps if p["valid"]]
    mixes = [p["fuel_mix"] for p in valid if p["fuel_mix"] is not None]
    mix_med = statistics.median(mixes) if mixes else None
    l100s = [p["l100"] for p in valid if p["l100"]]
    l100_med = statistics.median(l100s) if l100s else None
    for p in ps:
        exp = (reg["a"] + reg["b"] * p["avg_speed"]) if reg and p["avg_speed"] else l100_med
        p["l100_esperado"] = round(exp, 2) if exp else None
        p["residuo_pct"] = round((p["l100"] - exp) / exp * 100, 1) if p["l100"] and exp else None
        p["mistura_diferente"] = mix_med is not None and p["fuel_mix"] is not None and abs(p["fuel_mix"] - mix_med) > MIX_DIFF
        p["motivos"] = [WHY.get(w, w) for w in (p["why"] or "").split(",") if w]
    services = []
    if ps:
        a, b = ps[0]["ts_start"] - 30 * 86400_000, ps[-1]["ts_start"] + 30 * 86400_000
        for an in con.execute("SELECT id, ts, text FROM annotation WHERE kind='manutencao' AND ts BETWEEN ? AND ? ORDER BY ts", (a, b)):
            services.append(dict(dict(an), **compare(ps, an["ts"])))
    seg.pop("_ref_stops", None)
    return {"segment": dict(seg, label=label(con, seg_id)), "passes": ps, "regression": reg, "services": services,
            "n": len(ps), "n_valid": len(valid)}


def list_segments(con) -> list[dict]:
    out = []
    for s in con.execute("SELECT * FROM segment ORDER BY id").fetchall():
        d = dict(s)
        st = con.execute("""SELECT COUNT(*), COALESCE(SUM(valid), 0), MAX(ts_start) FROM segment_pass WHERE segment_id=?""", (s["id"],)).fetchone()
        durs = [r[0] for r in con.execute("SELECT dur_s FROM segment_pass WHERE segment_id=? AND valid=1", (s["id"],))]
        d.update(label=label(con, s["id"]), n=st[0], n_valid=st[1], last_ts=st[2], dur_med=round(statistics.median(durs)) if durs else None)
        out.append(d)
    return out


def impact_for(con, ts: int, window_days: int = 30) -> list[dict]:
    """Para extras.impact(): para cada trecho com passagens válidas perto do serviço, o antes e depois no trecho.
    Identificado só como "Trecho N" (o /api/impact é público na nuvem)."""
    out = []
    w = window_days * 86400_000
    for s in con.execute("SELECT id FROM segment ORDER BY id").fetchall():
        ps = [dict(r) for r in con.execute("SELECT * FROM segment_pass WHERE segment_id=? AND valid=1 AND ts_start BETWEEN ? AND ? ORDER BY ts_start",
                                           (s["id"], ts - w, ts + w))]
        if not any(p["ts_start"] < ts for p in ps) or not any(p["ts_start"] >= ts for p in ps):
            continue                                 # sem passagem de um dos lados: o trecho não tem o que dizer
        c = compare(ps, ts)
        out.append({"trecho": label(con, s["id"]), **c})
    return out

