"""Dinamômetro de rua pelo GPS (docs/GPS-ARQUITETURA.md, seções 9.9 e 9.23).

    F = m·δ_g·a + m·g·sen(θ) + m·g·Crr·cos(θ) + ½·ρ·CdA·v²
    P_roda = F·v        P_motor = P_roda ÷ η        T = P_motor ÷ ω

`finalize(con, tid, ctx)` (item de hub.FINALIZERS) acha as puxadas da viagem (pedal no fundo na mesma marcha, sobre as
rajadas de 5 Hz) e grava dyno_run com a curva de potência e torque. DynoLive (motor ao vivo) dá a inclinação, a potência
estimada e a potência disponível na altitude para o modo direção, e conduz o assistente de descida em ponto morto
(coastdown) que calibra CdA e Crr. As funções puras (`pull_curve`, `fit_coast`, `vehicle`) são as que os testes usam.

É uma estimativa (±8–10 % sem calibração, ±5 % com): o valor está em comparar o carro com ele mesmo.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import deque

from .. import config
from . import gears as gearsmod
from . import precise, store
from . import routes as routesmod
from . import terrain

G = 9.81
DELTA = {1: 1.25, 2: 1.12, 3: 1.08, 4: 1.06, 5: 1.05}      # inércia das partes que giram, por marcha
DELTA_N = 1.03                                             # em ponto morto
PASSENGER_KG = 75                                          # cada ocupante além do motorista
ACC_HALF_MS = 600                                          # ao vivo: diferença central em ±0,6 s
PULL_ACC_MS = 1000                                         # puxada: reta da velocidade em ±1 s…
BIN_RPM = 250                                              # …curva em caixas de 250 rpm (mediana)
MIN_BIN_N = 2
PULL_PEDAL, PULL_MIN_MS, PULL_GAP_MS = 0.85, 3000, 1500    # puxada: pedal ≥ 0,85 por ≥ 3 s
PULL_GEARS = (2, 3, 4)
PULL_RANGE_RPM = 1500
RATIO_TOL = 0.04                                           # todas as leituras na mesma relação (±4 %)
MIN_HZ = 3.0                                               # precisa da rajada (5 Hz)
WARM_ECT = 75.0
SLOPE_FLAG = 3.0                                           # |inclinação| acima disso: vale, mas sai marcada
CORR_RANGE = (0.90, 1.25)
SANE_CV = (60, 160)                                        # corrigido fora disso: "suspeito" (não é apagado)
KW_CV, NM_KGFM = 1.35962, 1 / 9.80665
REF = {"cv": 140, "cv_rpm": 6300, "kgfm": 17.7}             # R18A flex (referência de fábrica, aproximada)
# coastdown
COAST_RPM, COAST_MIN_KMH, COAST_MAX_KMH = 1000, 42.0, 100.0   # abaixo de 42 km/h a lenta coincide com a 5ª
COAST_MIN_S, COAST_MIN_DV = 25.0, 25.0
COAST_ACC_MS = 8000
COAST_OPPOSITE = 150.0                                     # sentidos opostos: rumos a 150° ou mais
COAST_TIMEOUT_MS = 30 * 60_000
COAST_OK = {"cda": (0.45, 0.90), "crr": (0.007, 0.020)}
LIVE_SLOPE_EVERY_MS = 1000


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def rho_air(baro_kpa: float | None, t_c: float | None) -> float:
    """ρ = baro × 1000 ÷ (287,05 × (T + 273,15)); sem as leituras, o ar padrão a 760 m e 30 °C."""
    if not baro_kpa or t_c is None:
        return 1.06
    return baro_kpa * 1000 / (287.05 * (t_c + 273.15))


def corr_factor(baro_kpa: float | None, t_c: float | None) -> float:
    """Correção para a condição padrão: (101,3 ÷ baro) × √((T + 273) ÷ 293), limitada a 0,90–1,25."""
    if not baro_kpa or t_c is None:
        return 1.0
    c = (101.3 / baro_kpa) * math.sqrt((t_c + 273) / 293)
    return round(max(CORR_RANGE[0], min(CORR_RANGE[1], c)), 4)


def vehicle(con, vcfg: dict | None = None) -> dict:
    """Massa, CdA, Crr e η: configuração ([vehicle]), o que o dono ajustou no painel (gps_model['vehicle']) e a
    calibração por coastdown aceita (gps_model['coast'])."""
    v = {**config.DEFAULTS["vehicle"], **(vcfg or {})}
    try:
        over = store.model_get(con, "vehicle") or {}
        coast = store.model_get(con, "coast") or {}
    except Exception:
        over, coast = {}, {}
    if isinstance(over, dict):
        for k in ("mass_kg", "occupants", "cda", "crr"):
            if over.get(k) is not None:
                v[k] = over[k]
    occ = int(v.get("occupants") or 1)
    out = {"mass_base": float(v["mass_kg"]), "occupants": occ, "mass": float(v["mass_kg"]) + PASSENGER_KG * max(0, occ - 1),
           "cda": float(v["cda"]), "crr": float(v["crr"]), "eff": float(v.get("drivetrain_eff") or 0.88), "aero_src": "padrão"}
    if isinstance(over, dict) and (over.get("cda") is not None or over.get("crr") is not None):
        out["aero_src"] = "informado"
    if isinstance(coast, dict) and coast.get("ok") and coast.get("cda") and coast.get("crr"):
        out.update(cda=float(coast["cda"]), crr=float(coast["crr"]), aero_src="calibrado", coast_ts=coast.get("ts"))
    return out


# ---------------------------------------------------------------- puxada (pura)
def _interp(pts: list, t: float) -> float | None:
    lo, hi = 0, len(pts) - 1
    if not pts or t < pts[0][0] or t > pts[-1][0]:
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (t1, v1), (t2, v2) = pts[lo], pts[hi]
    return v1 if t2 == t1 else v1 + (v2 - v1) * (t - t1) / (t2 - t1)


def accel_series(pts: list, half_ms: int = ACC_HALF_MS) -> list[float | None]:
    """a (m/s²) em cada ponto: inclinação da reta da velocidade nos pontos a ±1 s (só com a janela cheia dos dois
    lados). O plano pedia diferença central em ±0,6 s e mediana de 3; com o ruído do Doppler (0,15 m/s) isso deixava
    cada caixa de 250 rpm com ~5 % de ruído e o pico errava mais que 3 %."""
    out: list[float | None] = []
    lo = hi = 0
    n = len(pts)
    for i in range(n):
        t = pts[i][0]
        while pts[lo][0] < t - half_ms:
            lo += 1
        while hi + 1 < n and pts[hi + 1][0] <= t + half_ms:
            hi += 1
        w = pts[lo:hi + 1]
        if len(w) < 4 or w[0][0] > t - half_ms + 150 or w[-1][0] < t + half_ms - 150:
            out.append(None)
            continue
        mt = sum(p[0] for p in w) / len(w) / 1000
        mv = sum(p[1] for p in w) / len(w) / 3.6
        stt = sum((p[0] / 1000 - mt) ** 2 for p in w)
        out.append(sum((p[0] / 1000 - mt) * (p[1] / 3.6 - mv) for p in w) / stt if stt > 0 else None)
    return out


def pull_curve(pts: list, k_g: float, gear: int, veh: dict, rho: float, slope_pct: float = 0.0,
               baro: float | None = None, iat: float | None = None) -> dict | None:
    """pts: [(ts, km/h do GPS)] da puxada (5 Hz). Rotação de cada ponto pela relação aprendida (v ÷ k_g × 1000).
    Curva = mediana por caixa de 250 rpm, alisada em 3 caixas; o pico fica onde a média de 5 caixas é maior (o topo da
    curva do R18A é largo: entre 6.000 e 6.900 rpm a potência varia ~3 %, menos que o ruído de uma caixa) e o valor é o
    da curva de 3 caixas ali. Devolve também o fator de correção para a condição padrão."""
    if len(pts) < 10:
        return None
    acc = accel_series(pts, PULL_ACC_MS)
    keep = [i for i, a in enumerate(acc) if a is not None and a > 0.3]   # só o trecho acelerando (tira o fim, quando alivia)
    if len(keep) < 8:
        return None
    th = math.atan((slope_pct or 0.0) / 100)
    m, d = veh["mass"], DELTA.get(gear, 1.06)
    bins: dict[int, list] = {}
    for i in range(keep[0], keep[-1] + 1):
        a = acc[i]
        if a is None:
            continue
        v = pts[i][1] / 3.6
        F = m * d * a + m * G * math.sin(th) + m * G * veh["crr"] * math.cos(th) + 0.5 * rho * veh["cda"] * v * v
        p_eng = F * v / veh["eff"]
        rpm = pts[i][1] / k_g * 1000
        if rpm < 800:
            continue
        omega = rpm * 2 * math.pi / 60
        bins.setdefault(int(rpm // BIN_RPM), []).append((p_eng, p_eng / omega))
    rows = [{"rpm": b * BIN_RPM + BIN_RPM // 2, "kw": statistics.median(x[0] for x in xs) / 1000,
             "nm": statistics.median(x[1] for x in xs), "n": len(xs)} for b, xs in sorted(bins.items()) if len(xs) >= MIN_BIN_N]
    if len(rows) < 5:
        return None
    n = len(rows)
    for i, r in enumerate(rows):                                # média de 3 caixas (a curva mostrada)
        w = rows[max(0, i - 1):i + 2]
        r["kw_s"] = sum(x["kw"] for x in w) / len(w)
        r["nm_s"] = sum(x["nm"] for x in w) / len(w)

    def peak(key: str) -> dict:
        inner = range(2, n - 2) if n >= 5 else range(n)
        j = max(inner, key=lambda i: sum(x[key] for x in rows[i - 2:i + 3]) / 5 if n >= 5 else rows[i][key])
        return rows[j]
    pk, pt = peak("kw"), peak("nm")
    corr = corr_factor(baro, iat)
    return {"rpm_min": rows[0]["rpm"] - BIN_RPM // 2, "rpm_max": rows[-1]["rpm"] + BIN_RPM // 2,
            "peak_kw": round(pk["kw_s"], 1), "peak_kw_rpm": pk["rpm"], "peak_nm": round(pt["nm_s"], 1), "peak_nm_rpm": pt["rpm"],
            "corr": corr, "curve": {"rpm": [r["rpm"] for r in rows], "kw": [round(r["kw_s"], 1) for r in rows],
                                    "nm": [round(r["nm_s"], 1) for r in rows], "n": [r["n"] for r in rows]}}


# ---------------------------------------------------------------- coastdown (puro)
def _slope_v(seg: list, i: int, half_ms: int = COAST_ACC_MS) -> tuple[float, float] | None:
    """(v em m/s, dv/dt em m/s²) pela reta da velocidade nos pontos a ±8 s, só com a janela cheia dos dois lados: na
    descida a aceleração muda devagar e a janela longa tira o ruído do Doppler (com ±1 s o CdA errava 10 % ou mais).
    A velocidade é a da reta no meio da janela (a mesma hora da derivada)."""
    t = seg[i][0]
    w = [(p[0] / 1000, p[1] / 3.6) for p in seg if abs(p[0] - t) <= half_ms]
    if len(w) < 8 or w[0][0] * 1000 > t - half_ms + 300 or w[-1][0] * 1000 < t + half_ms - 300:
        return None
    mt = sum(x for x, _ in w) / len(w)
    mv = sum(y for _, y in w) / len(w)
    stt = sum((x - mt) ** 2 for x, _ in w)
    return (mv, sum((x - mt) * (y - mv) for x, y in w) / stt) if stt > 0 else None


def fit_segment(seg: list, mass: float) -> dict | None:
    """Uma descida em ponto morto [(ts, km/h)]: mínimos quadrados de −m·δ0·dv/dt = A + B·v² (fórmula fechada)."""
    xs, ys = [], []
    for i in range(len(seg)):
        r = _slope_v(seg, i)
        if r is None:
            continue
        v, a = r
        xs.append(v * v)
        ys.append(-mass * DELTA_N * a)
    if len(xs) < 20:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    B = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return {"A": my - B * mx, "B": B, "n": n, "v0": round(seg[0][1], 1), "v1": round(seg[-1][1], 1)}


def fit_coast(segments: list[list], mass: float, rho: float) -> dict:
    """Média dos dois sentidos (cancela inclinação e vento): Crr = A ÷ (m·g), CdA = 2B ÷ ρ. Aceita só CdA entre 0,45 e
    0,90 e Crr entre 0,007 e 0,020."""
    fits = [f for f in (fit_segment(s, mass) for s in segments) if f]
    if len(fits) < 2:
        return {"ok": False, "motivo": "precisa de uma descida em cada sentido", "segs": fits}
    A = sum(f["A"] for f in fits) / len(fits)
    B = sum(f["B"] for f in fits) / len(fits)
    cda, crr = 2 * B / rho, A / (mass * G)
    ok = COAST_OK["cda"][0] <= cda <= COAST_OK["cda"][1] and COAST_OK["crr"][0] <= crr <= COAST_OK["crr"][1]
    out = {"ok": ok, "cda": round(cda, 3), "crr": round(crr, 4), "A": round(A, 1), "B": round(B, 4), "rho": round(rho, 3),
           "mass": mass, "segs": fits}
    if not ok:
        out["motivo"] = (f"resultado fora do esperado (CdA {_br(cda, 2)}, Crr {_br(crr, 3)}): vento, rua com curva ou "
                         "inclinação diferente nos dois sentidos. Repita numa reta plana, sem vento.")
    return out


# ---------------------------------------------------------------- fechamento da viagem
def _slow_last(con, key: str, a: int, b: int) -> list:
    return [(r[0], r[1]) for r in con.execute(
        "SELECT s.ts, s.value FROM sample s JOIN signal g ON g.id = s.signal_id WHERE g.key=? AND s.ts BETWEEN ? AND ? ORDER BY s.ts",
        (key, a, b))]


def find_pulls(fast_rows: list) -> list[tuple[int, int]]:
    """Intervalos (ts) com o pedal normalizado ≥ 0,85 por ≥ 3 s (leituras a até 1,5 s uma da outra)."""
    pn = terrain.pedal_norm_series([r["pedal_d"] for r in fast_rows])
    out, cur = [], None
    for r, p in zip(fast_rows, pn):
        if p is not None and p >= PULL_PEDAL:
            if cur and r["ts"] - cur[1] <= PULL_GAP_MS:
                cur[1] = r["ts"]
            else:
                if cur and cur[1] - cur[0] >= PULL_MIN_MS:
                    out.append(tuple(cur))
                cur = [r["ts"], r["ts"]]
    if cur and cur[1] - cur[0] >= PULL_MIN_MS:
        out.append(tuple(cur))
    return out


def analyze_pull(fixes: list, fast: list, *, veh: dict, table: dict, ect: float | None, baro: float | None,
                 iat: float | None) -> dict:
    """Uma puxada: fixes (linhas de gps_fix do intervalo), fast (fast_sample do intervalo). Devolve a linha de
    dyno_run (valid = 0 com o motivo quando recusada)."""
    why, flags = [], []
    gs = {r["gear"] for r in fast if r["gear"] is not None}
    gear = next(iter(gs)) if len(gs) == 1 else None
    pts = [(f["ts"], f["speed"]) for f in fixes if f["speed"] is not None]
    if gear is None or gear == 0 or len(gs) != 1:
        why.append("trocou de marcha (ou embreagem) no meio")
    elif gear not in PULL_GEARS:
        why.append(f"em {gear}ª (vale só 2ª, 3ª ou 4ª)")
    hz = None
    if len(pts) >= 2:
        dts = [b[0] - a[0] for a, b in zip(pts, pts[1:]) if b[0] > a[0]]
        hz = 1000 / statistics.median(dts) if dts else None
    if not hz or hz < MIN_HZ:
        why.append("sem a rajada de 5 Hz (posição 1 vez por segundo)")
    if fixes and not all(terrain.precise_row(f) for f in fixes):
        why.append("GPS impreciso")
    if ect is not None and ect < WARM_ECT:
        why.append("motor frio")
    # todas as leituras na mesma relação (velocidade do GPS ÷ rotação)
    if gear and not why and pts:
        rs = []
        for r in fast:
            v = _interp(pts, r["ts"])
            if v and r["rpm"]:
                rs.append(v / (r["rpm"] / 1000))
        if rs:
            med = statistics.median(rs)
            if any(abs(x / med - 1) > RATIO_TOL for x in rs):
                why.append("relação velocidade ÷ rotação variou (troca de marcha ou embreagem)")
    k_g = (table or gearsmod.FACTORY).get(gear) if gear else None
    base = {"gear": gear, "baro": baro, "iat": iat, "mass_kg": veh["mass"], "alt": None, "slope_pct": None}
    alts = [f["alt"] for f in fixes if f["alt"] is not None]
    if alts:
        base["alt"] = round(statistics.median(alts), 1)
    if why:
        return {**base, "valid": 0, "why": "; ".join(why)}
    sl, src = terrain.run_slope(fixes)
    rho = rho_air(baro, iat)
    c = pull_curve(pts, k_g, gear, veh, rho, sl or 0.0, baro, iat)
    if not c:
        return {**base, "valid": 0, "why": "puxada curta demais para a curva"}
    if c["rpm_max"] - c["rpm_min"] < PULL_RANGE_RPM:
        return {**base, "valid": 0, "why": f"faixa de rotação curta ({c['rpm_min']}–{c['rpm_max']} rpm)"}
    if sl is not None and abs(sl) > SLOPE_FLAG:
        flags.append(f"{'subida' if sl > 0 else 'descida'} de {_br(abs(sl))} %")
    if c["rpm_max"] < REF["cv_rpm"] - 300:
        flags.append(f"subiu só até {c['rpm_max']} rpm: não chegou ao pico de potência (~{REF['cv_rpm']} rpm)")
    cv_corr = c["peak_kw"] * c["corr"] * KW_CV
    if not SANE_CV[0] <= cv_corr <= SANE_CV[1]:
        flags.append("suspeito (fora de 60–160 cv corrigidos)")
    c["curve"].update(slope_src=src, rho=round(rho, 3), k=k_g, cda=veh["cda"], crr=veh["crr"], aero=veh["aero_src"])
    return {**base, "valid": 1, "why": "; ".join(flags) or None, "slope_pct": sl, "rpm_min": c["rpm_min"], "rpm_max": c["rpm_max"],
            "peak_kw": c["peak_kw"], "peak_kw_rpm": c["peak_kw_rpm"], "peak_nm": c["peak_nm"], "peak_nm_rpm": c["peak_nm_rpm"],
            "corr": c["corr"], "curve_json": json.dumps(c["curve"], separators=(",", ":"))}


DYNO_COLS = ("gear", "rpm_min", "rpm_max", "peak_kw", "peak_kw_rpm", "peak_nm", "peak_nm_rpm", "corr", "slope_pct", "alt", "baro",
             "iat", "fuel_mix", "mass_kg", "valid", "why", "curve_json")


def finalize(con, tid: int, ctx=None) -> list | None:
    """Item de FINALIZERS. Idempotente: apaga e refaz as puxadas da viagem."""
    cfg = getattr(ctx, "cfg", None) or {}
    t = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (tid,)).fetchone()
    g = con.execute("SELECT n_fix FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    if not t or not g or (g["n_fix"] or 0) < 10:
        return None
    a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
    con.execute("DELETE FROM dyno_run WHERE trip_id=?", (tid,))
    fast = con.execute("SELECT ts, rpm, speed, gear, pedal_d FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
    pulls = find_pulls(fast)
    if not pulls:
        return []
    from .trip import good_fixes
    veh = vehicle(con, getattr(ctx, "vehicle", None))
    gm = store.model_get(con, "gears") or {}
    table = None
    try:
        md = gearsmod._load(gm)
        if md["n"] >= gearsmod.MIN_SAMPLES:
            res = gearsmod.cluster(md["hist"])
            table = res["table"] if res["learned"] else None
    except Exception:
        table = None
    moving_iat = [v for ts, v in _slow_last(con, "iat", a, b)]
    iat = min(moving_iat) if moving_iat else None                 # o sensor aquece parado: a menor da viagem
    out = []
    for p0, p1 in pulls:
        fx = good_fixes(con, p0 - 500, p1 + 500, cfg)
        fr = [r for r in fast if p0 <= r["ts"] <= p1]
        ect = next((v for ts, v in reversed(_slow_last(con, "ect", a - 60_000, p0))), None)
        baro_s = _slow_last(con, "baro", p0 - 60_000, p1 + 60_000)
        baro = statistics.median(v for _, v in baro_s) if baro_s else None
        row = analyze_pull(fx, fr, veh=veh, table=table, ect=ect, baro=baro, iat=iat)
        row["fuel_mix"] = routesmod._mix_at(con, p0)
        con.execute(f"INSERT INTO dyno_run(ts, trip_id, {', '.join(DYNO_COLS)}) VALUES (?,?{', ?' * len(DYNO_COLS)})",
                    (p0, tid, *[row.get(c) for c in DYNO_COLS]))
        out.append({"ts": p0, **row})
    return out


# ---------------------------------------------------------------- relatório (/api/gps/dyno)
def _cv(kw):
    return round(kw * KW_CV, 1) if kw is not None else None


def report(con, vcfg: dict | None = None, limit: int = 50) -> dict:
    """Puxadas (com as curvas), parâmetros do carro e a calibração. Sem posição: rota aberta na nuvem."""
    runs = []
    for r in con.execute("SELECT * FROM dyno_run ORDER BY ts DESC LIMIT ?", (limit,)):
        d = dict(r)
        try:
            d["curve"] = json.loads(d.pop("curve_json") or "null")
        except ValueError:
            d["curve"] = None
        c = d["corr"] or 1.0
        d.update(peak_cv=_cv(d["peak_kw"]), peak_cv_corr=_cv(d["peak_kw"] * c) if d["peak_kw"] else None,
                 peak_kgfm=round(d["peak_nm"] * NM_KGFM, 1) if d["peak_nm"] else None,
                 peak_kgfm_corr=round(d["peak_nm"] * c * NM_KGFM, 1) if d["peak_nm"] else None, fuel=routesmod.fuel_kind(d["fuel_mix"]))
        runs.append(d)
    coast = store.model_get(con, "coast") or None
    veh = vehicle(con, vcfg)
    valid = [x for x in runs if x["valid"]]
    full = [x for x in valid if not (x["why"] or "").count("subiu só até")] or valid
    best = max(full, key=lambda x: x["peak_cv_corr"] or 0) if full else None
    return {"runs": runs, "n_valid": len(valid), "best": best and {k: best[k] for k in ("id", "ts", "gear", "peak_cv", "peak_cv_corr",
                                                                                      "peak_kw_rpm", "peak_kgfm_corr", "peak_nm_rpm")},
            "vehicle": veh, "coast": coast, "ref": REF,
            "precisao": "±5 % (calibrado por descida em ponto morto)" if veh["aero_src"] == "calibrado" else "±8–10 % (sem calibração)"}


# ---------------------------------------------------------------- motor ao vivo
class DynoLive:
    """Inclinação, potência estimada agora e % disponível na altitude (live.gps.slope, power_kw, avail_pct) e o
    assistente de descida em ponto morto ({"cmd": "coast_arm"} / {"cmd": "coast_cancel"} em gps_cmd)."""

    def __init__(self, hub):
        self.hub, self.con = hub, hub.con
        self.vcfg = getattr(hub, "vehicle", None)
        self.veh = vehicle(self.con, self.vcfg)       # tolera banco sem os modelos (fica com a configuração)
        self.veh_ts = None
        self.ring: deque = deque(maxlen=80)            # (ts, km/h, precisa, alt, climb, heading) ~16 s a 5 Hz
        self.sring: deque = deque(maxlen=60)           # 1 por segundo, para a inclinação ao vivo
        self.slope: float | None = None
        self.pw: deque = deque(maxlen=3)
        self.avail: float | None = None
        self.coast: dict | None = None
        self.last_slope_ts = None

    def on_trip_start(self, tid, ts, merged) -> None:
        pass

    def shift(self, off: int) -> None:
        self.ring = deque(((t + off, *x) for t, *x in self.ring), maxlen=self.ring.maxlen)
        self.sring = deque(({**d, "ts": d["ts"] + off} for d in self.sring), maxlen=self.sring.maxlen)
        if self.last_slope_ts is not None:
            self.last_slope_ts += off
        c = self.coast
        if c:
            for k in ("since", "end_ts"):
                if c.get(k) is not None:
                    c[k] += off
            for s in c.get("segs", []) + ([c["cur"]] if c.get("cur") else []):
                s["pts"] = [(t + off, v) for t, v in s["pts"]]

    def command(self, c: dict) -> bool:
        if c.get("cmd") == "coast_arm":
            ts = self.ring[-1][0] if self.ring else None             # sem posição ainda: conta a partir da primeira
            self.coast = {"state": "armado", "since": ts, "segs": [], "cur": None, "msg": "Em reta plana, chegue a ~90 km/h e ponha em ponto morto."}
            return True
        if c.get("cmd") == "coast_cancel":
            self.coast = None
            return True
        return False

    def live(self) -> dict:
        out = {}
        if self.slope is not None:
            out["slope"] = round(self.slope, 1)
        if self.pw:
            out["power_kw"] = round(statistics.median(self.pw))
        if self.avail is not None:
            out["avail_pct"] = round(self.avail)
        c = self.coast
        if c:
            out["coast"] = {"state": c["state"], "n": len(c["segs"]), "msg": c.get("msg"),
                            **({k: c[k] for k in ("cda", "crr") if k in c})}
        return out

    def feed(self, ctx) -> list:
        f = ctx.fix
        if f.speed is None:
            return []
        ts, v = f.ts, float(f.speed)
        self.ring.append((ts, v, precise(f), f.alt, f.climb, f.heading))
        vals, vts = ctx.values or {}, ctx.value_ts or {}
        baro, iat = vals.get("baro"), vals.get("iat")
        self.avail = terrain.avail_pct(baro, iat)
        if self.veh_ts is None or ts - self.veh_ts > 300_000:           # o dono pode mudar a massa ou calibrar
            self.veh_ts = ts
            try:
                self.veh = vehicle(self.con, self.vcfg)
            except Exception:
                pass
        if not self.sring or ts // 1000 != self.sring[-1]["ts"] // 1000:
            self.sring.append({"ts": ts, "lat": f.lat, "lon": f.lon, "alt": f.alt, "speed": v, "climb": f.climb,
                               "hdop": f.hdop, "sats": f.sats, "mode": f.mode})
            try:
                self.slope = terrain.slope_series(list(self.sring), centered=False)[-1]
            except Exception:
                self.slope = None
        self._power(ts, v, baro, iat)
        if self.coast:
            self._coast(ts, v, vals, vts)
        return []

    def _power(self, ts: int, v: float, baro, iat) -> None:
        pts = [(p[0], p[1]) for p in self.ring]
        tc = ts - ACC_HALF_MS
        a0, a1 = _interp(pts, tc - ACC_HALF_MS), _interp(pts, ts)
        vc = _interp(pts, tc)
        if a0 is None or a1 is None or vc is None or vc < 10:
            if v < 10:
                self.pw.clear()
            return
        a = (a1 - a0) / 3.6 / (2 * ACC_HALF_MS / 1000)
        veh, vm = self.veh, vc / 3.6
        th = math.atan((self.slope or 0.0) / 100)
        F = veh["mass"] * DELTA[3] * a + veh["mass"] * G * (math.sin(th) + veh["crr"] * math.cos(th)) + 0.5 * rho_air(baro, iat) * veh["cda"] * vm * vm
        self.pw.append(max(0.0, F * vm / veh["eff"] / 1000))

    def _coast(self, ts: int, v: float, vals: dict, vts: dict) -> None:
        c = self.coast
        if c["state"] in ("concluido", "recusado"):
            if ts - c.get("end_ts", ts) > 300_000:
                self.coast = None
            return
        if c["since"] is None:
            c["since"] = ts
        if ts - c["since"] > COAST_TIMEOUT_MS:
            self.coast = {**c, "state": "recusado", "msg": "Tempo esgotado (30 min). Arme de novo quando estiver na reta.", "end_ts": ts}
            return
        rpm = vals.get("rpm") if vts.get("rpm") is not None and abs(ts - vts["rpm"]) <= 1500 else None
        neutral = rpm is not None and rpm <= COAST_RPM and COAST_MIN_KMH <= v <= COAST_MAX_KMH
        if neutral and rpm:                            # razão fora de qualquer marcha (em marcha, a rotação segue a velocidade)
            tab = (self.hub.gear_table() if hasattr(self.hub, "gear_table") else None) or gearsmod.FACTORY
            neutral = gearsmod.match(v / (rpm / 1000), tab, 0.06) is None
        cur = c.get("cur")
        if neutral:
            if cur is None or ts - cur["pts"][-1][0] > 1500:
                cur = c["cur"] = {"pts": [], "hdg": [], "precise": True}
                c.update(state="descendo", msg="Descendo em ponto morto: mantenha até ~45 km/h.")
                fn = getattr(self.hub, "start_burst", None)
                if fn:
                    fn(90)
            cur["pts"].append((ts, v))
            hd = self.ring[-1][5]
            if hd is not None:
                cur["hdg"].append(hd)
            cur["precise"] = cur["precise"] and self.ring[-1][2]
            return
        if cur is not None and ts - cur["pts"][-1][0] > 1500:
            c["cur"] = None
            dur = (cur["pts"][-1][0] - cur["pts"][0][0]) / 1000
            dv = cur["pts"][0][1] - cur["pts"][-1][1]
            if dur >= COAST_MIN_S and dv >= COAST_MIN_DV and cur["precise"] and cur["hdg"]:
                h = math.degrees(math.atan2(sum(math.sin(math.radians(x)) for x in cur["hdg"]),
                                            sum(math.cos(math.radians(x)) for x in cur["hdg"]))) % 360
                c["segs"].append({"pts": cur["pts"], "hdg": round(h)})
                c.update(state="armado", msg="Uma descida medida. Agora faça no sentido contrário, no mesmo trecho.")
                self._try_fit(ts, vals)
            else:
                c.update(state="armado", msg="Descida curta ou interrompida (precisa de ~90 → 45 km/h em ponto morto, por 25 s ou mais, GPS bom). Tente de novo.")

    def _try_fit(self, ts: int, vals: dict) -> None:
        c = self.coast
        segs = c["segs"]
        last = segs[-1]
        opp = next((s for s in reversed(segs[:-1]) if abs((s["hdg"] - last["hdg"] + 180) % 360 - 180) >= COAST_OPPOSITE), None)
        if opp is None:
            return
        rho = rho_air(vals.get("baro"), vals.get("iat"))
        res = fit_coast([opp["pts"], last["pts"]], self.veh["mass"], rho)
        res["ts"] = ts
        res["segs"] = [{k: x[k] for k in ("A", "B", "n", "v0", "v1")} for x in res.get("segs", [])]
        prev = store.model_get(self.con, "coast") or None
        if res["ok"]:
            res["prev"] = ({k: prev.get(k) for k in ("cda", "crr", "ts")} if isinstance(prev, dict) and prev.get("ok")
                           else {"cda": self.veh["cda"], "crr": self.veh["crr"], "ts": None})
            store.model_set(self.con, "coast", res, ts)
            self.veh = vehicle(self.con, self.vcfg)
            c.update(state="concluido", cda=res["cda"], crr=res["crr"], end_ts=ts,
                     msg=f"Calibrado: CdA {_br(res['cda'], 2)} m², Crr {_br(res['crr'], 3)}. O dinamômetro passa a usar estes valores.")
        else:
            c.update(state="recusado", end_ts=ts, msg=res.get("motivo") or "resultado fora do esperado")
