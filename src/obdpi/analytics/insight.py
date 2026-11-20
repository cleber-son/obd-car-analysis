"""Análise aprofundada de um período (ou de uma viagem): um veredito por sistema do carro, em linguagem simples,
com os números que sustentam, a evolução dia a dia e, quando a mistura está errada, as causas prováveis em ordem.

Entrada: as varreduras completas (tabela sample, uma linha por sinal a cada ~5 s, todas com o mesmo ts por ciclo).
Não depende de IA nem de internet: roda no Pi e alimenta o painel, os relatórios e o contexto da análise por IA.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import statistics

from .tripdiag import expected_air_gs

KEYS = ("rpm", "speed", "ect", "iat", "maf", "map", "load", "stft1", "o2c1_lambda", "lambda_cmd", "o2s2_v", "batt",
        "tps", "tps_cmd", "timing")
RANK = {"ok": 0, "unk": 0, "warn": 1, "bad": 2}


def _med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def _share(v, pred):
    v = [x for x in v if x is not None]
    return sum(1 for x in v if pred(x)) / len(v) if v else None


def _r(x, n=1):
    return None if x is None else round(x, n)


def _pct(x):
    return None if x is None else round(x * 100)


def _num(label, value, unit="", ok=None):
    return {"l": label, "v": value, "u": unit, "ok": ok}


def cycles(con: sqlite3.Connection, a: int, b: int) -> list[dict]:
    rows: dict[int, dict] = {}
    q = f"""SELECT s.ts, g.key, s.value FROM sample s JOIN signal g ON g.id = s.signal_id
            WHERE s.ts >= ? AND s.ts < ? AND g.key IN ({','.join('?' * len(KEYS))})"""
    for ts, key, val in con.execute(q, (a, b, *KEYS)):
        rows.setdefault(ts, {"ts": ts})[key] = val
    return [rows[t] for t in sorted(rows)]


def _regimes(cyc: list[dict]) -> dict[str, list[dict]]:
    """Separa as leituras por situação do motor. 'idle' só conta com a marcha lenta já assentada (3 leituras seguidas)."""
    out = {"idle": [], "run": [], "cold": [], "on": []}
    streak = 0
    for c in cyc:
        rpm, spd, ect = c.get("rpm"), c.get("speed"), c.get("ect")
        if rpm is None or rpm < 400:
            streak = 0
            continue
        out["on"].append(c)
        if ect is not None and ect < 70:
            out["cold"].append(c)
            streak = 0
            continue
        calm = (spd or 0) == 0 and rpm < 1200 and (c.get("tps_cmd") is None or c["tps_cmd"] < 6)
        streak = streak + 1 if calm else 0
        if calm and streak >= 3:
            out["idle"].append(c)
        elif (spd or 0) > 10:
            out["run"].append(c)
    return out


def _air_ratio(rows: list[dict]) -> float | None:
    v = []
    for c in rows:
        maf, mp, rpm = c.get("maf"), c.get("map"), c.get("rpm")
        if not maf or not mp or not rpm or mp < 15:
            continue
        exp = expected_air_gs(mp, rpm, c.get("iat") if c.get("iat") is not None else 30.0)
        if exp > 0.3 and 0.15 < maf / exp < 1.6:
            v.append(maf / exp)
    return statistics.median(v) if len(v) >= 12 else None


def _mixture(reg: dict) -> tuple[dict, list[dict]]:
    idle, run = reg["idle"], reg["run"]
    si, sr = _med(c.get("stft1") for c in idle), _med(c.get("stft1") for c in run)
    peg = _share([c.get("stft1") for c in idle], lambda x: x >= 40)
    lam = _med((c["o2c1_lambda"] - c["lambda_cmd"]) for c in idle
               if c.get("o2c1_lambda") is not None and c.get("lambda_cmd") is not None and c["o2c1_lambda"] < 1.9)
    ai = _air_ratio(idle)
    al = _air_ratio([c for c in run if (c.get("map") or 0) >= 60])
    post = _med(c.get("o2s2_v") for c in idle + run)
    cmd_idle = _med(c.get("tps_cmd") for c in idle)
    nums = [_num("Ajuste de combustível parado", _r(si), "%", None if si is None else abs(si) <= 12),
            _num("Ajuste de combustível rodando", _r(sr), "%", None if sr is None else abs(sr) <= 12),
            _num("Tempo no limite de correção (parado)", _pct(peg), "%", None if peg is None else peg < 0.1),
            _num("Ar medido ÷ esperado, parado", _pct(ai), "%", None if ai is None else ai >= 0.55),
            _num("Ar medido ÷ esperado, com carga", _pct(al), "%", None if al is None else al >= 0.75),
            _num("Sonda depois do catalisador", _r(post, 2), "V", None)]
    if len(idle) < 12 and len(run) < 12:
        return {"status": "unk", "resumo": "Ainda sem leituras suficientes com o motor quente para avaliar a mistura.", "fazer": None, "numeros": nums}, []
    worst = max((abs(x) for x in (si, sr) if x is not None), default=0)
    lean = (si or 0) > 12 or (sr or 0) > 12
    rich = (si or 0) < -12 or (sr or 0) < -12
    if (si is not None and si > 25) or (peg or 0) > 0.3:
        st = "bad"
        resumo = (f"O motor está recebendo ar demais (ou combustível de menos) parado: a central precisou aumentar o combustível em "
                  f"{si:+.0f} %" + (f" e ficou {peg:.0%} do tempo no limite que ela consegue corrigir" if peg and peg > 0.1 else "") + ".")
    elif lean or rich:
        st = "warn"
        lado = "pobre (pouco combustível para o ar que entra)" if lean else "rica (combustível demais)"
        resumo = f"A mistura está um pouco {lado}: a central corrige {si if si is not None and abs(si) >= abs(sr or 0) else sr:+.0f} % (o normal é até ±10 %)."
    else:
        st = "ok"
        resumo = f"Mistura normal: a central corrige só {worst:.0f} % (o normal é até ±10 %)."
    hyp = []
    if lean:
        d = (si - sr) if si is not None and sr is not None else None
        leak = maf = fuel = 1.0
        pro_l, pro_m, pro_f, con_l, con_m, con_f = [], [], [], [], [], []
        if d is not None:
            if d > 10:
                leak += 3; pro_l.append(f"a correção é muito maior parado ({si:+.0f} %) do que rodando ({sr:+.0f} %)")
                con_m.append("um MAF com defeito erraria parecido parado e rodando"); con_f.append("falta de combustível pioraria com carga, não parado")
            elif d < -8:
                fuel += 3; pro_f.append(f"a correção cresce rodando ({sr:+.0f} %) em relação a parado ({si:+.0f} %)")
                con_l.append("uma entrada de ar pesa mais parado do que rodando")
            else:
                maf += 2; fuel += 1; pro_m.append(f"a correção é parecida parado ({si:+.0f} %) e rodando ({sr:+.0f} %)")
        if ai is not None and al is not None:
            if ai < 0.55 and ai / al < 0.75:
                leak += 3; pro_l.append(f"o MAF mede {ai:.0%} do ar esperado parado e {al:.0%} com carga: a diferença some conforme o motor puxa mais ar")
            elif ai < 0.55 and al < 0.75:
                maf += 3; pro_m.append(f"o MAF mede pouco em todas as cargas ({ai:.0%} parado, {al:.0%} com carga)")
            else:
                con_l.append(f"o MAF bate com o ar esperado ({ai:.0%} parado, {al:.0%} com carga)")
                con_m.append("o MAF bate com o ar esperado nas duas situações")
        elif ai is not None and ai < 0.55:
            leak += 1; maf += 1; pro_l.append(f"o MAF mede só {ai:.0%} do ar esperado parado (falta rodar com carga para separar de MAF sujo)")
        if cmd_idle is not None and cmd_idle < 2:
            leak += 1; pro_l.append(f"a central segura a borboleta quase fechada parado ({cmd_idle:.1f} %), como faz quando entra ar por outro lugar")
        tot = leak + maf + fuel
        hyp = sorted([
            {"nome": "Entrada falsa de ar (junta do corpo de borboleta, duto de admissão, mangueiras de vácuo)", "prob": round(leak / tot * 100),
             "a_favor": pro_l, "contra": con_l, "confirmar": "Motor quente parado e painel ao vivo aberto: borrife limpa-carburador em volta da junta, do duto e das mangueiras. Se a rotação ou o ajuste mudar num ponto, é ali."},
            {"nome": "Sensor MAF sujo ou lendo baixo", "prob": round(maf / tot * 100), "a_favor": pro_m, "contra": con_m,
             "confirmar": "Limpar com spray próprio para MAF (sem tocar no fio) e comparar o diagnóstico Ar × MAF depois de rodar."},
            {"nome": "Pouco combustível (bomba, filtro, bico, pressão)", "prob": round(fuel / tot * 100), "a_favor": pro_f, "contra": con_f,
             "confirmar": "Medir a pressão de combustível na oficina, principalmente em aceleração forte."},
        ], key=lambda h: -h["prob"])
    fazer = None
    if st != "ok":
        fazer = (hyp[0]["confirmar"] if hyp else "Conferir filtro de ar entupido, bicos vazando e pressão de combustível alta.")
    return {"status": st, "resumo": resumo, "fazer": fazer, "numeros": nums}, hyp


def _idle(reg: dict) -> dict:
    rpm = [c["rpm"] for c in reg["idle"]]
    if len(rpm) < 12:
        return {"status": "unk", "resumo": "Sem tempo suficiente parado com o motor quente para avaliar.", "fazer": None, "numeros": []}
    med, sd = statistics.median(rpm), statistics.pstdev(rpm)
    nums = [_num("Rotação parado", round(med), "rpm", 600 <= med <= 950), _num("Variação", round(sd), "rpm", sd < 40)]
    if sd >= 75 or not 550 <= med <= 1100:
        return {"status": "bad", "resumo": f"Marcha lenta instável: gira em torno de {med:.0f} rpm, variando ±{sd:.0f} rpm (o normal é menos de ±40).",
                "fazer": "Resolver primeiro a mistura (item acima). Depois fazer o reaprendizado da marcha lenta.", "numeros": nums}
    if sd >= 40:
        return {"status": "warn", "resumo": f"Marcha lenta um pouco irregular: {med:.0f} rpm, variando ±{sd:.0f} rpm.",
                "fazer": "Acompanhar. Costuma melhorar quando a mistura normaliza e depois do reaprendizado da marcha lenta.", "numeros": nums}
    return {"status": "ok", "resumo": f"Marcha lenta firme em {med:.0f} rpm (variação de ±{sd:.0f} rpm).", "fazer": None, "numeros": nums}


def _cooling(reg: dict) -> dict:
    hot = [c["ect"] for c in reg["on"] if c.get("ect") is not None and c["ect"] >= 70]
    if len(hot) < 12:
        return {"status": "unk", "resumo": "O motor ainda não ficou quente tempo suficiente nas leituras.", "fazer": None, "numeros": []}
    mx, med, over = max(hot), statistics.median(hot), _share(hot, lambda x: x > 102)
    run = _med(c["ect"] for c in reg["run"] if c.get("ect") is not None and (c.get("speed") or 0) > 40)
    nums = [_num("Temperatura normal de trabalho", round(med), "°C", 80 <= med <= 100), _num("Pico", round(mx), "°C", mx < 104),
            _num("Tempo acima de 102 °C", _pct(over), "%", over < 0.05), _num("Rodando acima de 40 km/h", _r(run, 0), "°C", None if run is None else run <= 98)]
    if mx >= 108 or over > 0.25:
        return {"status": "bad", "resumo": f"O motor esquentou demais: pico de {mx:.0f} °C e {over:.0%} do tempo acima de 102 °C.",
                "fazer": "Com o motor FRIO, conferir o nível do líquido de arrefecimento. Verificar ventoinha, tampa do radiador e vazamentos antes de rodar longe.", "numeros": nums}
    if mx >= 104 or over > 0.08 or med < 78:
        if med < 78:
            return {"status": "warn", "resumo": f"O motor trabalha frio ({med:.0f} °C). Costuma ser válvula termostática travada aberta.",
                    "fazer": "Trocar a válvula termostática: motor frio gasta mais e desgasta mais.", "numeros": nums}
        return {"status": "warn", "resumo": f"Temperatura no limite alto: pico de {mx:.0f} °C (a ventoinha deve segurar perto de 100 °C).",
                "fazer": "Com o motor frio, conferir o nível do reservatório. Evitar muito tempo parado com o motor ligado.", "numeros": nums}
    return {"status": "ok", "resumo": f"Temperatura normal: trabalha em {med:.0f} °C, pico de {mx:.0f} °C.", "fazer": None, "numeros": nums}


def _intake(reg: dict) -> dict:
    mov = _med(c["iat"] for c in reg["run"] if c.get("iat") is not None and (c.get("speed") or 0) > 30)
    stop = _med(c["iat"] for c in reg["idle"] if c.get("iat") is not None)
    if mov is None and stop is None:
        return {"status": "unk", "resumo": "Sem leituras do ar admitido.", "fazer": None, "numeros": []}
    nums = [_num("Rodando acima de 30 km/h", _r(mov, 0), "°C", None if mov is None else mov <= 55), _num("Parado", _r(stop, 0), "°C", None)]
    if mov is not None and mov > 55:
        return {"status": "warn", "resumo": f"O motor está puxando ar quente mesmo rodando ({mov:.0f} °C). Ar quente tira força e aumenta o consumo.",
                "fazer": "Conferir se a tomada de ar do filtro está encaixada e puxando ar de fora do cofre do motor.", "numeros": nums}
    if mov is None:
        return {"status": "ok", "resumo": f"Parado, o ar admitido fica em {stop:.0f} °C. Isso é normal com o capô quente; falta rodar para avaliar de verdade.",
                "fazer": None, "numeros": nums}
    return {"status": "ok", "resumo": f"Ar admitido normal rodando ({mov:.0f} °C)." + (f" Parado sobe para {stop:.0f} °C, o que é esperado." if stop and stop > mov + 10 else ""),
            "fazer": None, "numeros": nums}


def _electric(reg: dict, con: sqlite3.Connection, a: int, b: int) -> dict:
    v = [c["batt"] for c in reg["on"] if c.get("batt") is not None]
    if len(v) < 12:
        return {"status": "unk", "resumo": "Sem leituras de tensão com o motor ligado.", "fazer": None, "numeros": []}
    med, lo, hi = statistics.median(v), min(v), max(v)
    # só vale como "mínima na partida" o que realmente afundou; leitura já com o alternador carregando não é partida
    crank = con.execute("SELECT MIN(crank_v) FROM trip WHERE start_ts>=? AND start_ts<? AND crank_v IS NOT NULL AND crank_v < 12.6", (a, b)).fetchone()[0]
    nums = [_num("Carga com o motor ligado", _r(med), "V", 13.2 <= med <= 14.8), _num("Mínima", _r(lo), "V", lo >= 12.3), _num("Máxima", _r(hi), "V", hi <= 15.0),
            _num("Mínima na partida", _r(crank), "V", None if crank is None else crank >= 9.6)]
    if hi > 15.1:
        return {"status": "bad", "resumo": f"Tensão alta demais ({hi:.1f} V): o regulador do alternador pode estar com defeito.",
                "fazer": "Testar alternador e regulador numa autoelétrica: sobrecarga estraga bateria e módulos.", "numeros": nums}
    if med < 12.8 or (crank is not None and crank < 9.0):
        return {"status": "bad" if med < 12.4 else "warn", "resumo": f"Carga baixa com o motor ligado ({med:.1f} V)" + (f" e a bateria cai para {crank:.1f} V na partida" if crank else "") + ".",
                "fazer": "Testar bateria e alternador (teste de carga), conferir terminais e a correia.", "numeros": nums}
    return {"status": "ok", "resumo": f"Alternador carregando normal ({med:.1f} V com o motor ligado)." + (f" Na partida a bateria segura {crank:.1f} V." if crank else ""),
            "fazer": None, "numeros": nums}


def _throttle(con: sqlite3.Connection, a: int, b: int) -> dict:
    rows = con.execute("""SELECT title, severity, SUM(COALESCE(count,1)) n FROM alert WHERE ts>=? AND ts<? AND source IN ('throttle','dtc_throttle')
                          AND rule NOT IN ('R4','R5','R5b','R6') GROUP BY title ORDER BY n DESC""", (a, b)).fetchall()
    score = con.execute("SELECT AVG(tb_score) FROM trip WHERE start_ts>=? AND start_ts<? AND tb_score IS NOT NULL", (a, b)).fetchone()[0]
    nums = [_num(r[0], r[2], "×", False) for r in rows[:4]]
    if not rows:
        return {"status": "ok", "resumo": "Corpo de borboleta e pedal respondendo certo: a borboleta segue o comando e os sensores em dupla concordam.",
                "fazer": None, "numeros": nums}
    crit = any(r[1] == "crit" for r in rows)
    return {"status": "bad" if crit else "warn",
            "resumo": "Sinais fora do esperado no corpo de borboleta ou no pedal: " + "; ".join(f"{r[0].lower()} ({r[2]}×)" for r in rows[:3]) + ".",
            "fazer": "Com o motor desligado, reencaixar e limpar (limpa-contato) os conectores do corpo de borboleta e do pedal. Se repetir, a peça nova tem garantia.",
            "numeros": nums, "nota": _r(score, 0)}


def _codes(con: sqlite3.Connection, live: dict | None) -> dict:
    act = con.execute("SELECT code, kind FROM dtc_state WHERE active=1").fetchall()
    rd = (live or {}).get("readiness") or {}
    mons = rd.get("monitors") if isinstance(rd, dict) else None
    pend = [m for m in (mons or []) if isinstance(m, dict) and not m.get("ready")]
    nums = [_num("Códigos ativos", len(act), "", not act)]
    if act:
        conf = [r[0] for r in act if r[1] == "confirmed"]
        return {"status": "bad" if conf else "warn", "resumo": "A central registrou código de falha: " + ", ".join(sorted({r[0] for r in act})) + ".",
                "fazer": "Veja em Alertas o que cada código significa e o que fazer.", "numeros": nums}
    if pend:
        return {"status": "ok", "resumo": f"Nenhum código de falha. Mas a central ainda não terminou {len(pend)} autoteste(s): só depois deles ela confirma que está tudo certo.",
                "fazer": None, "numeros": nums}
    return {"status": "ok", "resumo": "Nenhum código de falha registrado pela central.", "fazer": None, "numeros": nums}


def _fuel(con: sqlite3.Connection, a: int, b: int) -> dict:
    t = con.execute("""SELECT COALESCE(SUM(distance_km),0), COALESCE(SUM(fuel_l),0), COALESCE(SUM(duration_s),0), COALESCE(SUM(idle_s),0), AVG(eco_score)
                       FROM trip WHERE start_ts>=? AND start_ts<?""", (a, b)).fetchone()
    km, lit, dur, idle, eco = t
    if dur < 300:
        return {"status": "unk", "resumo": "Sem viagens suficientes para avaliar consumo.", "fazer": None, "numeros": []}
    kml = km / lit if lit > 0.3 else None
    share = idle / dur if dur else 0
    idle_l = lit * min(1.0, share * 0.45) if lit else 0          # parado gasta ~45 % da média por hora
    nums = [_num("Distância", _r(km), "km", None), _num("Consumo médio", _r(kml), "km/L", None), _num("Tempo parado com motor ligado", _pct(share), "%", share < 0.3),
            _num("Combustível gasto parado (estimado)", _r(idle_l), "L", None), _num("Nota de direção econômica", _r(eco, 0), "/100", None if eco is None else eco >= 70)]
    if km < 15:
        return {"status": "unk", "resumo": f"Só {km:.1f} km rodados: ainda é pouco para um consumo confiável" + (f" (parcial: {kml:.1f} km/L)." if kml else "."),
                "fazer": "Rodar pelo menos 30–50 km em uso normal e registrar os tanques cheios." if share <= 0.4 else
                "Rodar pelo menos 30–50 km e evitar deixar o carro ligado parado: foi " + f"{share:.0%} do tempo.", "numeros": nums}
    if share > 0.4:
        return {"status": "warn", "resumo": f"Média de {kml:.1f} km/L, puxada para baixo por {share:.0%} do tempo parado com o motor ligado.",
                "fazer": "Desligar o motor em paradas de mais de 1 minuto.", "numeros": nums}
    return {"status": "ok", "resumo": f"Média de {kml:.1f} km/L em {km:.0f} km." if kml else f"{km:.0f} km rodados.", "fazer": None, "numeros": nums}


def _evolution(cyc: list[dict]) -> list[dict]:
    days: dict[str, list[dict]] = {}
    for c in cyc:
        days.setdefault(dt.datetime.fromtimestamp(c["ts"] / 1000).strftime("%Y-%m-%d"), []).append(c)
    out = []
    for d in sorted(days):
        reg = _regimes(days[d])
        if len(reg["on"]) < 12:
            continue
        rpm = [c["rpm"] for c in reg["idle"]]
        out.append({"dia": d, "leituras": len(reg["on"]),
                    "ajuste_parado": _r(_med(c.get("stft1") for c in reg["idle"])), "ajuste_rodando": _r(_med(c.get("stft1") for c in reg["run"])),
                    "ar_parado": _pct(_air_ratio(reg["idle"])), "rpm_variacao": round(statistics.pstdev(rpm)) if len(rpm) >= 12 else None,
                    "temp_max": _r(max((c["ect"] for c in reg["on"] if c.get("ect") is not None), default=None), 0)})
    return out


SYSTEMS = (("mistura", "⛽", "Mistura ar/combustível"), ("lenta", "〰️", "Marcha lenta"), ("borboleta", "🦋", "Corpo de borboleta e pedal"),
           ("arrefecimento", "🌡️", "Temperatura do motor"), ("admissao", "💨", "Ar admitido"), ("eletrica", "🔋", "Bateria e alternador"),
           ("codigos", "🧾", "Códigos de falha"), ("consumo", "💰", "Consumo"))


def build(con: sqlite3.Connection, a: int, b: int, live: dict | None = None) -> dict:
    cyc = cycles(con, a, b)
    reg = _regimes(cyc)
    # mistura e marcha lenta mudam depois de um conserto: vale o estado do último dia com dados, não a média do período
    last_day = dt.datetime.fromtimestamp(cyc[-1]["ts"] / 1000).strftime("%Y-%m-%d") if cyc else None
    first_day = dt.datetime.fromtimestamp(cyc[0]["ts"] / 1000).strftime("%Y-%m-%d") if cyc else None
    recent = reg
    if last_day and last_day != first_day:
        rl = _regimes([c for c in cyc if dt.datetime.fromtimestamp(c["ts"] / 1000).strftime("%Y-%m-%d") == last_day])
        if len(rl["idle"]) >= 12 or len(rl["run"]) >= 12:
            recent = rl
    mix, hyp = _mixture(recent)
    idle = _idle(recent)
    if recent is not reg:
        tag = f"No último dia com dados ({last_day[8:]}/{last_day[5:7]}): "
        for part in (mix, idle):
            if part["status"] != "unk":
                part["resumo"] = tag + part["resumo"][0].lower() + part["resumo"][1:]
    parts = {"mistura": mix, "lenta": idle, "borboleta": _throttle(con, a, b), "arrefecimento": _cooling(reg), "admissao": _intake(reg),
             "eletrica": _electric(reg, con, a, b), "codigos": _codes(con, live), "consumo": _fuel(con, a, b)}
    systems = [dict(parts[k], id=k, icone=i, nome=n) for k, i, n in SYSTEMS]
    evo = _evolution(cyc)
    trend = None
    pts = [e for e in evo if e["ajuste_parado"] is not None]
    if len(pts) >= 2:
        f, l = pts[0], pts[-1]
        d = l["ajuste_parado"] - f["ajuste_parado"]
        if abs(d) >= 8:
            trend = (f"A mistura parado {'melhorou' if abs(l['ajuste_parado']) < abs(f['ajuste_parado']) else 'piorou'}: o ajuste foi de "
                     f"{f['ajuste_parado']:+.0f} % em {f['dia'][8:]}/{f['dia'][5:7]} para {l['ajuste_parado']:+.0f} % em {l['dia'][8:]}/{l['dia'][5:7]}.")
    bad = [s for s in systems if s["status"] == "bad"]
    warn = [s for s in systems if s["status"] == "warn"]
    unk = [s for s in systems if s["status"] == "unk"]
    level = "bad" if bad else "warn" if warn else "ok"
    if bad:
        verdict = "Precisa de atenção: " + ", ".join(s["nome"].lower() for s in bad) + "."
    elif warn:
        verdict = "No geral bem, com pontos para acompanhar: " + ", ".join(s["nome"].lower() for s in warn) + "."
    elif len(unk) > 4:
        verdict = "Ainda há poucos dados neste período para avaliar o carro."
    else:
        verdict = "Tudo dentro do normal nos dados deste período."
    hours = len(reg["on"]) * 5 / 3600
    return {"nivel": level, "veredito": verdict, "tendencia": trend, "sistemas": systems, "hipoteses": hyp, "evolucao": evo,
            "base": {"leituras": len(cyc), "motor_ligado_h": round(hours, 1), "parado": len(reg["idle"]), "rodando": len(reg["run"]), "frio": len(reg["cold"])}}


def to_text(ins: dict) -> str:
    """Resumo em texto (notificação e contexto da IA)."""
    ic = {"ok": "🟢", "warn": "🟡", "bad": "🔴", "unk": "⚪"}
    lines = [f"{ic[ins['nivel']]} {ins['veredito']}"]
    if ins.get("tendencia"):
        lines.append("📈 " + ins["tendencia"])
    for s in ins["sistemas"]:
        if s["status"] in ("bad", "warn"):
            lines.append(f"{ic[s['status']]} {s['nome']}: {s['resumo']}" + (f" → {s['fazer']}" if s.get("fazer") else ""))
    if ins.get("hipoteses"):
        h = ins["hipoteses"][0]
        lines.append(f"🔎 Causa mais provável ({h['prob']} %): {h['nome']}")
    return "\n".join(lines)
