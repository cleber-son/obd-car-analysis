"""Diagnósticos por viagem: entrada falsa de ar (MAF × ar esperado), direção econômica, aquecimento, bateria na partida."""
from __future__ import annotations

import statistics

from . import Alert

DISPL_L = 1.799          # R18A
R_AIR = 287.05


def expected_air_gs(map_kpa: float, rpm: float, iat_c: float) -> float:
    """Ar que o motor admite com eficiência volumétrica 100 % (speed-density), em g/s."""
    return map_kpa * 1000 * DISPL_L / 1000 * (rpm / 120) / (R_AIR * (iat_c + 273.15)) * 1000


BUCKETS = [("marcha_lenta", "Marcha lenta"), ("leve", "Carga leve (MAP < 50)"), ("media", "Carga média (MAP 50–75)"), ("alta", "Carga alta (MAP > 75)")]
# faixas típicas de "eficiência volumétrica aparente" (MAF / ar esperado) para um motor aspirado saudável
TYPICAL = {"marcha_lenta": (0.55, 0.85), "leve": (0.65, 0.90), "media": (0.75, 0.95), "alta": (0.80, 1.00)}


def air_check(rows: list, iat_c: float | None) -> tuple[dict, str | None, str]:
    """rows: fast_sample com maf, map, rpm, speed. Retorna (buckets, veredito, explicação)."""
    iat = iat_c if iat_c is not None else 30.0
    acc: dict[str, list] = {k: [] for k, _ in BUCKETS}
    for r in rows:
        maf, mp, rpm, spd = r["maf"], r["map"], r["rpm"], r["speed"]
        if not maf or not mp or not rpm or rpm < 600 or mp < 15:
            continue
        exp = expected_air_gs(mp, rpm, iat)
        if exp <= 0.3:
            continue
        ratio = maf / exp
        if not 0.15 < ratio < 1.6:
            continue
        b = "marcha_lenta" if (spd or 0) == 0 and rpm < 1200 else "leve" if mp < 50 else "media" if mp < 75 else "alta"
        acc[b].append(ratio)
    out = {k: {"n": len(v), "ratio": round(statistics.median(v), 3) if v else None, "typical": TYPICAL[k]} for k, v in acc.items()}
    idle = out["marcha_lenta"]["ratio"] if out["marcha_lenta"]["n"] >= 30 else None
    loads = [out[k]["ratio"] for k in ("media", "alta") if out[k]["n"] >= 15]
    load = max(loads) if loads else (out["leve"]["ratio"] if out["leve"]["n"] >= 30 else None)
    if idle is None:
        return out, None, "Dados insuficientes de marcha lenta."
    if load is None:
        if idle < TYPICAL["marcha_lenta"][0]:
            return out, "suspeito", (f"Em marcha lenta o MAF mede só {idle:.0%} do ar esperado (típico 55–85 %). "
                                     "Rode com cargas maiores para separar entrada de ar de MAF lendo baixo.")
        return out, "normal", "Marcha lenta dentro do típico. Faltam dados com carga."
    rel = idle / load if load else 1
    if idle < TYPICAL["marcha_lenta"][0] and rel < 0.75:
        return out, "entrada_de_ar", (f"MAF mede {idle:.0%} do esperado em marcha lenta mas {load:.0%} com carga. A diferença "
                                      "diminui com carga, que é a assinatura de ar entrando depois do MAF (vazamento de vácuo / junta).")
    if load < TYPICAL["media"][0] and idle < TYPICAL["marcha_lenta"][0]:
        return out, "maf_baixo", (f"MAF baixo em todas as cargas ({idle:.0%} parado, {load:.0%} com carga): MAF sujo/defeituoso "
                                  "ou erro proporcional, não vazamento.")
    return out, "normal", f"MAF coerente com o ar esperado (parado {idle:.0%}, com carga {load:.0%})."


def eco_score(rows: list) -> tuple[int | None, dict]:
    """Nota de direção econômica 0–100 a partir das amostras rápidas (speed, rpm, load, gear, ts)."""
    pts = [r for r in rows if r["rpm"] is not None]
    if len(pts) < 60:
        return None, {}
    total = moving = idle = high_rpm = over_speed = lug = 0.0
    harsh_acc = harsh_brk = 0
    prev = None
    for r in pts:
        if prev is not None:
            dt = (r["ts"] - prev["ts"]) / 1000
            if 0 < dt <= 5:
                total += dt
                spd, rpm, load = r["speed"] or 0, r["rpm"] or 0, r["load"] or 0
                if spd > 2:
                    moving += dt
                elif rpm > 400:
                    idle += dt
                if spd > 2 and rpm > 3000 and load < 60:
                    high_rpm += dt                       # giro alto sem precisar (deveria subir a marcha)
                if spd > 2 and rpm < 1300 and load > 80 and (r["gear"] or 0) >= 3:
                    lug += dt                            # motor "afogando" em marcha alta
                if spd > 110:
                    over_speed += dt
                if dt <= 2.5 and prev["speed"] is not None and r["speed"] is not None:
                    a = (r["speed"] - prev["speed"]) / dt   # km/h por segundo
                    if a > 9:
                        harsh_acc += 1
                    elif a < -12:
                        harsh_brk += 1
        prev = r
    if total < 60:
        return None, {}
    mins = max(total / 60, 1)
    pen = {
        "giro_alto": min(25, high_rpm / max(moving, 1) * 100),
        "aceleracoes_bruscas": min(20, harsh_acc / mins * 10),
        "freadas_bruscas": min(20, harsh_brk / mins * 10),
        "parado_ligado": min(15, max(0, idle / total * 100 - 15) * 0.5),
        "acima_110": min(10, over_speed / max(moving, 1) * 50),
        "marcha_alta_forcando": min(10, lug / max(moving, 1) * 100),
    }
    score = int(round(max(0, 100 - sum(pen.values()))))
    tips = []
    if pen["giro_alto"] > 5:
        tips.append("Suba a marcha mais cedo: acima de 3000 rpm com pouca carga gasta à toa (ideal 2000–2500 rpm).")
    if pen["aceleracoes_bruscas"] > 4:
        tips.append("Acelere de forma mais progressiva: arrancadas fortes gastam muito mais combustível.")
    if pen["freadas_bruscas"] > 4:
        tips.append("Antecipe as freadas tirando o pé (o corte de combustível em desaceleração é consumo zero).")
    if pen["parado_ligado"] > 3:
        tips.append("Muito tempo parado com o motor ligado: acima de ~1 min, desligar compensa.")
    if pen["acima_110"] > 2:
        tips.append("Acima de 110 km/h o arrasto aerodinâmico aumenta muito o consumo.")
    if pen["marcha_alta_forcando"] > 2:
        tips.append("Evite acelerar fundo em marcha alta com giro baixo: reduza uma marcha.")
    return score, {"penalidades": {k: round(v, 1) for k, v in pen.items()}, "aceleracoes_bruscas": harsh_acc, "freadas_bruscas": harsh_brk,
                   "parado_pct": round(idle / total * 100, 1), "giro_alto_s": int(high_rpm), "acima_110_s": int(over_speed), "dicas": tips}


def trip_alerts(trip: dict, ts: int) -> list[Alert]:
    out = []
    v = trip.get("air_verdict")
    if v in ("entrada_de_ar", "suspeito"):
        out.append(Alert("diag", "air_leak", "warn", "Possível entrada falsa de ar", trip.get("air_txt", ""), "maf", None, ts))
    elif v == "maf_baixo":
        out.append(Alert("diag", "maf_low", "warn", "MAF lendo abaixo do esperado", trip.get("air_txt", ""), "maf", None, ts))
    cv = trip.get("crank_v")
    if cv is not None and cv > 3:
        if cv < 8.5:
            out.append(Alert("diag", "crank_weak", "crit", "Bateria muito fraca na partida", f"Tensão caiu para {cv:.1f} V ao dar partida (bom: > 9,6 V)", "batt", cv, ts))
        elif cv < 9.6:
            out.append(Alert("diag", "crank_weak", "warn", "Bateria fraca na partida", f"Tensão caiu para {cv:.1f} V ao dar partida (bom: > 9,6 V)", "batt", cv, ts))
    rv = trip.get("rest_v")
    if rv is not None and 6 < rv < 12.2:
        out.append(Alert("diag", "batt:off", "warn", "Bateria baixa antes da partida", f"{rv:.2f} V em repouso (saudável 12,4–12,7 V)", "batt", rv, ts))
    w, se = trip.get("warmup_s"), trip.get("start_ect")
    if se is not None and se < 50:
        if w is None and (trip.get("duration_s") or 0) > 25 * 60:
            out.append(Alert("diag", "thermostat", "warn", "Motor não chegou a 80 °C", "Mais de 25 min sem aquecer: válvula termostática travada aberta?", "ect", None, ts))
        elif w is not None and w > 20 * 60:
            out.append(Alert("diag", "thermostat", "warn", "Motor demorou para aquecer", f"{w // 60} min para chegar a 80 °C (típico 5–12 min rodando)", "ect", w, ts))
    return out
