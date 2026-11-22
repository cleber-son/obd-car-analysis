"""Inclinação e altitude × motor (docs/GPS-ARQUITETURA.md, seções 9.12 e 9.23).

`slope_series(fixes)` dá a inclinação de cada posição (altitude com mediana móvel de 9 s; regressão da altitude contra a
distância percorrida numa janela de 150 m, no mínimo 8 posições). `run_slope(fixes)` é a inclinação média de um trecho
curto (arrancada, puxada): pela velocidade vertical do receptor quando ela vem, senão pela altitude.

`finalize(con, tid, ctx)` (item de hub.FINALIZERS) grava trip_gps.terrain_json: por faixa de inclinação e por faixa de
200 m de altitude, segundos e somas de carga, MAP, borboleta, ajuste curto, erro da sonda, avanço, consumo e ar medido ÷
esperado (somas e contagens, para o período juntar viagens sem perder o peso de cada uma). `report(con, since)` junta o
período e faz a leitura automática; `altitude_power(con, since)` cruza altitude, pressão, MAP no fundo e as puxadas.
Nada aqui leva posição: a rota /api/gps/terrain é aberta na nuvem.
"""
from __future__ import annotations

import json
import math
import statistics
import time

from . import store
from .geo import haversine_m

VERSION = 1
ALT_WIN_MS = 4500            # mediana móvel de 9 s
WIN_M = 150.0                # janela da regressão
MIN_PTS = 8
MIN_SPAN_M = 40.0            # janela com menos que isso de distância não dá inclinação
MIN_KMH = 20.0               # abaixo disso, inclinação nula (o carro anda pouco e a altitude só tem ruído)
MAX_PCT = 20.0
GAP_MS = 3000                # buraco maior que isso interrompe a janela
CLIMB_SHARE = 0.8            # run_slope usa a velocidade vertical se ela vem em 80 % das posições
SLOPE_BANDS = ((-6.0, "≤ −6 %"), (-3.0, "−6 a −3 %"), (-1.0, "−3 a −1 %"), (1.0, "−1 a +1 %"), (3.0, "+1 a +3 %"),
               (6.0, "+3 a +6 %"), (math.inf, "> +6 %"))
ALT_BAND_M = 200
FAST_TOL_MS, SLOW_TOL_MS = 1500, 10_000
METRICS = ("load", "map", "tps", "stft", "lam_err", "timing", "air", "baro", "iat")
LEAK_DIFF = 8.0              # ajuste curto em descida/pé leve ≥ 8 pontos acima do da subida forte: entrada de ar
LEAK_MIN_S, LEAK_MIN_N = 20, 3
STEEP = 3.0                  # "subida forte" / "descida" (%)
MAP_LIGHT = 0.45             # pé leve: MAP ≤ 45 % da pressão ambiente (vácuo alto)
WOT_PEDAL, WOT_RPM = 0.85, 3000
RPM_BAND = 1000
RESTRICT_KPA = 8.0           # MAP no fundo abaixo da pressão ambiente em mais que isso: restrição na admissão


def _g(f, k):
    """Campo de uma posição (Fix, sqlite3.Row ou dict)."""
    try:
        return f[k]
    except (KeyError, IndexError, TypeError):
        return getattr(f, k, None)


def precise_row(f) -> bool:
    """gps.precise() para Fix, linha de gps_fix ou dict: 3D, HDOP ≤ 2 e 7 satélites ou mais."""
    hd, sats = _g(f, "hdop"), _g(f, "sats")
    return (_g(f, "mode") or 0) == 3 and hd is not None and hd <= 2.0 and sats is not None and sats >= 7


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def _median_alt(ts: list[int], alts: list) -> list[float | None]:
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


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Mínimos quadrados y = a + b·x (fórmula fechada)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def slope_series(fixes: list, centered: bool = True, win_m: float = WIN_M, min_pts: int = MIN_PTS,
                 use_climb: bool = True) -> list[float | None]:
    """Inclinação (%) de cada posição. Trabalha com uma posição por segundo (as rajadas de 5 Hz não pesam mais) e devolve
    um valor para cada item de 'fixes' (o do seu segundo). centered=True (fechamento): janela de 150 m centrada na
    posição; False (ao vivo): os últimos 150 m. Nula abaixo de 20 km/h, com posição imprecisa ou sem altitude.
    Na janela, a subida vem da velocidade vertical do receptor (∫ climb dt ÷ distância) quando ela existe em 80 % das
    posições; senão, da regressão da altitude suavizada contra a distância (o plano original). Com 4 m de ruído de
    altitude, a regressão em 150 m erra ~2–3 % em cada ponto (a média de um trecho longo fica a ±0,6 %)."""
    if not fixes:
        return []
    sub, sec_of, last = [], [], None
    for f in fixes:
        s = _g(f, "ts") // 1000
        if s != last:
            sub.append(f)
            last = s
        sec_of.append(len(sub) - 1)
    ts = [_g(f, "ts") for f in sub]
    sm = _median_alt(ts, [_g(f, "alt") for f in sub])
    dist = [0.0]
    for p, q in zip(sub, sub[1:]):
        d = haversine_m(_g(p, "lat"), _g(p, "lon"), _g(q, "lat"), _g(q, "lon")) if _g(q, "ts") - _g(p, "ts") <= GAP_MS else 0.0
        dist.append(dist[-1] + d)
    climb = [_g(f, "climb") for f in sub] if use_climb else [None] * len(sub)
    n = len(sub)
    out_sub: list[float | None] = []
    for i, f in enumerate(sub):
        if (_g(f, "speed") or 0) < MIN_KMH or not precise_row(f) or sm[i] is None:
            out_sub.append(None)
            continue
        half = win_m / 2 if centered else win_m
        lo = hi = i
        while lo > 0 and ts[lo] - ts[lo - 1] <= GAP_MS and sm[lo - 1] is not None and dist[i] - dist[lo - 1] <= half:
            lo -= 1
        if centered:
            while hi + 1 < n and ts[hi + 1] - ts[hi] <= GAP_MS and sm[hi + 1] is not None and dist[hi + 1] - dist[i] <= half:
                hi += 1
        while hi - lo + 1 < min_pts:                  # poucas posições na janela (andando rápido): alarga até 8
            grow = False
            if lo > 0 and ts[lo] - ts[lo - 1] <= GAP_MS and sm[lo - 1] is not None:
                lo, grow = lo - 1, True
            if centered and hi - lo + 1 < min_pts and hi + 1 < n and ts[hi + 1] - ts[hi] <= GAP_MS and sm[hi + 1] is not None:
                hi, grow = hi + 1, True
            if not grow:
                break
        xs, ys = dist[lo:hi + 1], sm[lo:hi + 1]
        if len(xs) < min_pts or xs[-1] - xs[0] < MIN_SPAN_M:
            out_sub.append(None)
            continue
        cl = climb[lo:hi + 1]
        if sum(c is not None for c in cl) >= CLIMB_SHARE * len(cl):          # velocidade vertical do receptor (Doppler)
            dh = sum((cl[k - 1] + cl[k]) / 2 * (ts[lo + k] - ts[lo + k - 1]) / 1000
                     for k in range(1, len(cl)) if cl[k - 1] is not None and cl[k] is not None)
            pct = dh / (xs[-1] - xs[0]) * 100
        else:
            fit = _fit(xs, ys)
            if fit is None:
                out_sub.append(None)
                continue
            pct = fit[1] * 100
        out_sub.append(round(max(-MAX_PCT, min(MAX_PCT, pct)), 2))
    return [out_sub[j] for j in sec_of]


def run_slope(fixes: list) -> tuple[float | None, str | None]:
    """Inclinação média (%) de um trecho curto e de onde ela veio: 'vertical' (∫ velocidade vertical do receptor ÷
    distância) quando ela vem em 80 % das posições, senão 'altitude' (regressão da altitude contra a distância)."""
    pts = [f for f in fixes if _g(f, "speed") is not None]
    if len(pts) < 3:
        return None, None
    dist, dh, dists = 0.0, 0.0, [0.0]
    n_climb = sum(1 for f in pts if _g(f, "climb") is not None)
    for p, q in zip(pts, pts[1:]):
        dt = (_g(q, "ts") - _g(p, "ts")) / 1000
        if dt <= 0 or dt > 3:
            dists.append(dist)
            continue
        dist += (_g(p, "speed") + _g(q, "speed")) / 2 / 3.6 * dt
        dists.append(dist)
        cp, cq = _g(p, "climb"), _g(q, "climb")
        if cp is not None and cq is not None:
            dh += (cp + cq) / 2 * dt
    if dist < 30:
        return None, None
    if n_climb >= CLIMB_SHARE * len(pts):
        return round(dh / dist * 100, 2), "vertical"
    alts = [(d, _g(f, "alt")) for d, f in zip(dists, pts) if _g(f, "alt") is not None]
    fit = _fit([a for a, _ in alts], [b for _, b in alts]) if len(alts) >= 3 else None
    return (round(fit[1] * 100, 2), "altitude") if fit else (None, None)


def slope_band(s: float) -> int:
    return next(i for i, (hi, _) in enumerate(SLOPE_BANDS) if s <= hi)


def pedal_norm_series(pedals: list) -> list[float | None]:
    """Pedal normalizado 0–1 na viagem: menor valor visto = solto; curso mínimo de 50 pontos (mesma regra das marchas)."""
    vals = [p for p in pedals if p is not None]
    if not vals:
        return [None] * len(pedals)
    lo = min(vals)
    span = max(max(vals) - lo, 50.0)
    return [None if p is None else max(0.0, min(1.0, (p - lo) / span)) for p in pedals]


# ---------------------------------------------------------------- acumuladores (somas, para juntar viagens)
def _acc() -> dict:
    return {"s": 0, "km": 0.0, "fuel_l": 0.0, "sum": {}, "n": {}}


def _add(acc: dict, row: dict) -> None:
    acc["s"] += 1
    acc["km"] += (row.get("speed") or 0) / 3600
    if row.get("fuel_lh") is not None:
        acc["fuel_l"] += row["fuel_lh"] / 3600
    for k in METRICS:
        v = row.get(k)
        if v is not None:
            acc["sum"][k] = acc["sum"].get(k, 0.0) + v
            acc["n"][k] = acc["n"].get(k, 0) + 1


def _merge(a: dict, b: dict) -> dict:
    out = {"s": a["s"] + b.get("s", 0), "km": a["km"] + b.get("km", 0.0), "fuel_l": a["fuel_l"] + b.get("fuel_l", 0.0),
           "sum": dict(a["sum"]), "n": dict(a["n"])}
    for k, v in (b.get("sum") or {}).items():
        out["sum"][k] = out["sum"].get(k, 0.0) + v
    for k, v in (b.get("n") or {}).items():
        out["n"][k] = out["n"].get(k, 0) + v
    return out


def _means(acc: dict) -> dict:
    out = {"s": acc["s"], "km": round(acc["km"], 2)}
    for k in METRICS:
        n = acc["n"].get(k, 0)
        out[k] = round(acc["sum"][k] / n, 3 if k in ("lam_err", "air") else 1) if n else None
    out["n_stft"] = acc["n"].get("stft", 0)
    out["l100"] = round(acc["fuel_l"] / acc["km"] * 100, 1) if acc["km"] > 0.2 and acc["fuel_l"] > 0 else None
    return out


# ---------------------------------------------------------------- fechamento da viagem
def _nearest(ts_list, t, tol):
    from .trip import _nearest as near
    return near(ts_list, t, tol)


def _slow(con, a: int, b: int) -> dict:
    out = {}
    for key in ("stft1", "o2c1_lambda", "lambda_cmd", "timing", "iat", "baro"):
        out[key] = [(r[0], r[1]) for r in con.execute(
            "SELECT s.ts, s.value FROM sample s JOIN signal g ON g.id = s.signal_id WHERE g.key=? AND s.ts BETWEEN ? AND ? ORDER BY s.ts",
            (key, a - SLOW_TOL_MS, b + SLOW_TOL_MS))]
    return out


def analyze(fixes: list, fast_rows: list, slow: dict) -> dict:
    """Função pura do fechamento: fixes (linhas de gps_fix boas), fast_rows (ts, speed, rpm, load, map, tps, maf, fuel_lh,
    pedal_d), slow {chave: [(ts, valor)]}. Devolve o terrain_json."""
    from ..analytics.tripdiag import expected_air_gs
    from .score import one_hz
    fx = one_hz(fixes)
    sl = slope_series(fx)
    sm = _median_alt([f["ts"] for f in fx], [f["alt"] for f in fx]) if fx else []
    fts = [r["ts"] for r in fast_rows]
    pn = pedal_norm_series([r["pedal_d"] for r in fast_rows])
    sts = {k: [x[0] for x in v] for k, v in slow.items()}

    def sv(key, t):
        j = _nearest(sts.get(key) or [], t, SLOW_TOL_MS)
        return slow[key][j][1] if j is not None else None

    bands = [_acc() for _ in SLOPE_BANDS]
    alts: dict[int, dict] = {}
    groups = {"leve": _acc(), "subida": _acc()}
    for f, s, a in zip(fx, sl, sm):
        if (f["speed"] or 0) < 3:
            continue
        j = _nearest(fts, f["ts"], FAST_TOL_MS)
        r = fast_rows[j] if j is not None else None
        row = {"speed": f["speed"]}
        if r is not None:
            row.update(load=r["load"], map=r["map"], tps=r["tps"], fuel_lh=r["fuel_lh"])
        st, lam, lc, tim, iat, baro = (sv(k, f["ts"]) for k in ("stft1", "o2c1_lambda", "lambda_cmd", "timing", "iat", "baro"))
        row.update(stft=st, timing=tim, iat=iat, baro=baro, lam_err=round(lam - lc, 3) if lam is not None and lc is not None else None)
        if r is not None and r["maf"] and r["map"] and r["rpm"] and iat is not None:
            exp = expected_air_gs(r["map"], r["rpm"], iat)
            row["air"] = round(r["maf"] / exp, 3) if exp else None
        if a is not None:
            ab = int(a // ALT_BAND_M * ALT_BAND_M)
            x = alts.setdefault(ab, {**_acc(), "wot_map": [0.0, 0], "maf_max": {}})
            _add(x, row)
            if r is not None and j is not None and (pn[j] or 0) >= WOT_PEDAL and (r["rpm"] or 0) >= WOT_RPM and r["map"] is not None:
                x["wot_map"][0] += r["map"]
                x["wot_map"][1] += 1
            if r is not None and r["maf"] and r["rpm"]:
                rb = str(int(r["rpm"] // RPM_BAND * RPM_BAND))
                x["maf_max"][rb] = max(x["maf_max"].get(rb, 0.0), round(r["maf"], 1))
        if s is None:
            continue
        _add(bands[slope_band(s)], row)
        if s >= STEEP:
            _add(groups["subida"], row)
        elif s <= -STEEP or (row.get("map") is not None and baro and row["map"] <= MAP_LIGHT * baro):
            _add(groups["leve"], row)
    return {"v": VERSION, "bands": [dict(b, band=SLOPE_BANDS[i][1]) for i, b in enumerate(bands)],
            "alts": {str(k): v for k, v in sorted(alts.items())}, "groups": groups,
            "n_s": sum(b["s"] for b in bands), "n_slope": sum(1 for x in sl if x is not None)}


def reading(groups: dict) -> dict:
    """Leitura automática (texto simples, sem IA): ajuste curto em descida ou pé leve × em subida forte."""
    lv, up = groups.get("leve") or _acc(), groups.get("subida") or _acc()
    nl, nu = (lv.get("n") or {}).get("stft", 0), (up.get("n") or {}).get("stft", 0)
    if lv.get("s", 0) < LEAK_MIN_S or up.get("s", 0) < LEAK_MIN_S or nl < LEAK_MIN_N or nu < LEAK_MIN_N:
        return {"diff": None, "msg": None, "texto": "Ainda faltam trechos de descida (ou pé leve) e de subida forte para comparar o ajuste curto."}
    a, b = lv["sum"]["stft"] / nl, up["sum"]["stft"] / nu
    d = round(a - b, 1)
    if d >= LEAK_DIFF:
        msg = "A mistura empobrece quando o vácuo é alto: assinatura de entrada de ar."
        txt = (f"Em descida ou com o pé leve (muito vácuo) a central adiciona {_br(a)} % de combustível; em subida forte, {_br(b)} %. "
               f"São {_br(d)} pontos a mais justamente quando o vácuo é alto: é o que um vazamento de ar depois do sensor faz "
               "(mangueira, junta da admissão, borboleta). É a mesma conta do teste de ar, agora com a estrada como bancada.")
    else:
        msg = None
        txt = f"Ajuste curto parecido em descida/pé leve ({_br(a)} %) e em subida forte ({_br(b)} %): sem sinal de entrada de ar pela estrada."
    return {"diff": d, "leve": round(a, 1), "subida": round(b, 1), "msg": msg, "texto": txt}


def finalize(con, tid: int, ctx=None) -> dict | None:
    """Item de FINALIZERS: grava trip_gps.terrain_json (nada sem trajeto)."""
    cfg = getattr(ctx, "cfg", None) or {}
    t = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (tid,)).fetchone()
    g = con.execute("SELECT n_fix FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    if not t or not g or (g["n_fix"] or 0) < 10:
        return None
    from .trip import good_fixes
    a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
    fixes = good_fixes(con, a, b, cfg)
    fast = con.execute("SELECT ts, speed, rpm, load, map, tps, maf, fuel_lh, pedal_d FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts",
                       (a, b)).fetchall()
    d = analyze(fixes, fast, _slow(con, a, b))
    d["leitura"] = reading(d["groups"])
    con.execute("UPDATE trip_gps SET terrain_json=? WHERE trip_id=?", (json.dumps(d, ensure_ascii=False, separators=(",", ":")), tid))
    return d


# ---------------------------------------------------------------- período (/api/gps/terrain)
def _trips(con, since: int) -> list[dict]:
    out = []
    for r in con.execute("""SELECT g.terrain_json FROM trip_gps g JOIN trip t ON t.id = g.trip_id
                            WHERE t.start_ts >= ? AND g.terrain_json IS NOT NULL AND COALESCE(t.hidden, 0) = 0""", (since,)):
        try:
            out.append(json.loads(r[0]))
        except ValueError:
            pass
    return out


def report(con, since: int) -> dict:
    """Junta as viagens do período: médias por faixa de inclinação e de altitude e a leitura automática."""
    tj = _trips(con, since)
    bands = [_acc() for _ in SLOPE_BANDS]
    groups = {"leve": _acc(), "subida": _acc()}
    for d in tj:
        for i, b in enumerate((d.get("bands") or [])[:len(SLOPE_BANDS)]):
            bands[i] = _merge(bands[i], b)
        for k in groups:
            groups[k] = _merge(groups[k], (d.get("groups") or {}).get(k) or {})
    rows = [{"band": SLOPE_BANDS[i][1], **_means(b)} for i, b in enumerate(bands)]
    alt = altitude_power(con, since, tj)
    return {"trips": len(tj), "bands": rows, "alts": alt["bands"], "leitura": reading(groups), "power": alt}


def avail_pct(baro: float | None, iat: float | None) -> float | None:
    """Potência disponível agora (% da do nível do mar a 25 °C): (baro ÷ 101,3) × √(298 ÷ (T_ar + 273))."""
    if not baro or iat is None:
        return None
    return round(100 * (baro / 101.3) * math.sqrt(298 / (iat + 273)), 1)


def altitude_power(con, since: int, tj: list | None = None) -> dict:
    """Por faixa de 200 m de altitude (GPS): pressão média, MAP com o pé no fundo, MAF máximo por faixa de rotação e o pico
    das puxadas (medido e corrigido), com a linha teórica (~−1 % a cada 100 m)."""
    tj = _trips(con, since) if tj is None else tj
    alts: dict[int, dict] = {}
    for d in tj:
        for k, v in (d.get("alts") or {}).items():
            ab = int(k)
            x = alts.setdefault(ab, {**_acc(), "wot_map": [0.0, 0], "maf_max": {}})
            m = _merge(x, v)
            m["wot_map"] = [x["wot_map"][0] + (v.get("wot_map") or [0, 0])[0], x["wot_map"][1] + (v.get("wot_map") or [0, 0])[1]]
            m["maf_max"] = dict(x["maf_max"])
            for rb, mx in (v.get("maf_max") or {}).items():
                m["maf_max"][rb] = max(m["maf_max"].get(rb, 0.0), mx)
            alts[ab] = m
    runs: dict[int, list] = {}
    for r in con.execute("SELECT alt, peak_kw, corr FROM dyno_run WHERE ts >= ? AND valid = 1 AND alt IS NOT NULL AND peak_kw IS NOT NULL",
                         (since,)):
        runs.setdefault(int(r["alt"] // ALT_BAND_M * ALT_BAND_M), []).append((r["peak_kw"], r["corr"] or 1.0))
    keys = sorted(set(alts) | set(runs))
    bands = []
    for ab in keys:
        x = alts.get(ab)
        m = _means(x) if x else {"s": 0, "baro": None}
        wm = x["wot_map"] if x else [0, 0]
        pk = runs.get(ab) or []
        row = {"alt": ab, "alt_hi": ab + ALT_BAND_M, "s": m["s"], "baro": m.get("baro"), "iat": m.get("iat"),
               "map_wot": round(wm[0] / wm[1], 1) if wm[1] else None, "n_wot": wm[1],
               "maf_max": dict(sorted((x or {}).get("maf_max", {}).items(), key=lambda kv: int(kv[0]))),
               "pulls": len(pk), "peak_kw": round(statistics.median(p for p, _ in pk), 1) if pk else None,
               "peak_kw_corr": round(statistics.median(p * c for p, c in pk), 1) if pk else None,
               "theory_pct": round(100 * (1 - 2.25577e-5 * (ab + ALT_BAND_M / 2)) ** 5.25588, 1)}
        row["restricao"] = bool(row["map_wot"] is not None and row["baro"] and row["baro"] - row["map_wot"] > RESTRICT_KPA)
        bands.append(row)
    msgs = []
    if any(b["restricao"] for b in bands):
        b = next(b for b in bands if b["restricao"])
        msgs.append(f"Com o pé no fundo o MAP ficou {_br(b['baro'] - b['map_wot'])} kPa abaixo da pressão do ambiente: o ar está "
                    "encontrando restrição na admissão (filtro de ar sujo, duto amassado ou borboleta que não abre toda).")
    withp = [b for b in bands if b["peak_kw_corr"]]
    if len(withp) >= 2:
        lo, hi = withp[0], withp[-1]
        drop = (1 - hi["peak_kw_corr"] / lo["peak_kw_corr"]) * 100
        if drop > 5:
            msgs.append(f"A potência corrigida cai {_br(drop)} % entre {lo['alt']} m e {hi['alt']} m: além do ar rarefeito há algo "
                        "segurando o motor (mistura, ignição ou restrição).")
    return {"bands": bands, "msgs": msgs, "band_m": ALT_BAND_M}
