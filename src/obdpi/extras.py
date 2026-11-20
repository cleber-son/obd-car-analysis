"""Recursos de apoio ao dono: impressão digital do motor saudável, antes e depois de um conserto, plano de manutenção
pelos km reais e custo dos hábitos de direção. Tudo calculado no Pi a partir do que já é gravado."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time

from .analytics import insight

DAY = 86400_000


# =====================================================================================================================
# 1. Impressão digital do motor
# =====================================================================================================================
# sinal: (nome, unidade, piso do desvio-padrão, diferença que já importa na prática, relativa?)
FP_SIG = {
    "maf": ("Ar medido (MAF)", "g/s", 0.3, 0.12, True),
    "map": ("Vácuo do coletor (MAP)", "kPa", 2.0, 4.0, False),
    "stft1": ("Correção de combustível", "%", 3.0, 8.0, False),
    "timing": ("Avanço de ignição", "°", 2.0, 4.0, False),
    "lam": ("Sonda (mistura)", "λ", 0.02, 0.04, False),
    "tps": ("Abertura da borboleta", "%", 1.0, 2.5, False),
}
FP_WHY = {
    ("maf", 1): "O motor está puxando mais ar do que puxava na mesma rotação e carga.",
    ("maf", -1): "O MAF mede menos ar do que media: sensor sujo ou ar entrando sem passar por ele.",
    ("map", 1): "Menos vácuo no coletor do que antes: entrada de ar, válvulas ou ponto atrasado.",
    ("map", -1): "Mais vácuo no coletor do que antes: admissão restrita (filtro de ar sujo?).",
    ("stft1", 1): "A central está adicionando mais combustível do que no carro saudável: ar falso, MAF lendo baixo ou falta de combustível.",
    ("stft1", -1): "A central está tirando combustível: mistura rica (bico vazando, pressão alta, filtro de ar sujo).",
    ("timing", 1): "Ponto mais adiantado do que o habitual.",
    ("timing", -1): "A central está atrasando o ponto: combustível ruim, detonação ou motor mais quente.",
    ("lam", 1): "A sonda vê mistura mais pobre do que via.",
    ("lam", -1): "A sonda vê mistura mais rica do que via.",
    ("tps", 1): "A borboleta precisa abrir mais para o mesmo trabalho: corpo de borboleta sujo ou motor com menos força.",
    ("tps", -1): "A borboleta abre menos para o mesmo trabalho: costuma ser ar entrando por outro lugar.",
}


def fp_profile(con: sqlite3.Connection, a: int, b: int) -> dict:
    """Média e desvio de cada sinal por faixa de rotação (500 rpm) e carga (20 %), só com o motor quente."""
    acc: dict[str, dict[str, list[float]]] = {}
    for c in insight.cycles(con, a, b):
        rpm, load, ect = c.get("rpm"), c.get("load"), c.get("ect")
        if rpm is None or load is None or rpm < 500 or ect is None or ect < 75:
            continue
        key = f"{int(rpm // 500) * 500}:{int(min(load, 99) // 20) * 20}"
        vals = dict(c)
        if c.get("o2c1_lambda") is not None and c.get("lambda_cmd") is not None and c["o2c1_lambda"] < 1.9:
            vals["lam"] = c["o2c1_lambda"] - c["lambda_cmd"]
        for s in FP_SIG:
            if vals.get(s) is not None:
                acc.setdefault(key, {}).setdefault(s, []).append(vals[s])
    out = {k: {s: [len(v), round(statistics.fmean(v), 4), round(statistics.pstdev(v), 4)] for s, v in sigs.items() if len(v) >= 6}
           for k, sigs in acc.items()}
    return {k: v for k, v in out.items() if v}


def fp_points(profile: dict) -> int:
    return sum(max((x[0] for x in sigs.values()), default=0) for sigs in profile.values())


def fp_compare(ref: dict, cur: dict) -> list[dict]:
    res = []
    for s, (name, unit, floor, practical, rel) in FP_SIG.items():
        zs, diffs, ws, worst = [], [], [], None
        for k in ref.keys() & cur.keys():
            r, c = ref[k].get(s), cur[k].get(s)
            if not r or not c or r[0] < 8 or c[0] < 8:
                continue
            d = c[1] - r[1]
            z = d / max(r[2], floor)
            w = min(r[0], c[0])
            zs.append(z * w); diffs.append((d / abs(r[1]) if rel and abs(r[1]) > 1e-6 else d) * w); ws.append(w)
            if worst is None or abs(z) > abs(worst[1]):
                worst = (k, z, r[1], c[1])
        if not ws:
            res.append({"key": s, "nome": name, "unidade": unit, "status": "unk", "faixas": 0, "texto": "Ainda sem leituras nas mesmas faixas de rotação e carga da referência."})
            continue
        z, d = sum(zs) / sum(ws), sum(diffs) / sum(ws)
        big = abs(d) >= practical
        wd = (worst[3] - worst[2]) / (abs(worst[2]) if rel and abs(worst[2]) > 1e-6 else 1)      # desvio na pior faixa
        local = abs(worst[1]) >= 4 and abs(wd) >= practical * 1.5                                 # uma faixa só, mas muito fora
        status = "bad" if (abs(z) >= 3 and big) or local else "warn" if abs(z) >= 2 and abs(d) >= practical * 0.6 else "ok"
        rpm, load = (worst[0].split(":") + ["0"])[:2]
        desvio = f"{d * 100:+.0f} %" if rel else f"{d:+.2f} {unit}" if s == "lam" else f"{d:+.1f} {unit}"
        res.append({"key": s, "nome": name, "unidade": unit, "status": status, "z": round(z, 1), "desvio": desvio, "faixas": len(ws),
                    "texto": ("Igual ao carro saudável." if status == "ok" else FP_WHY[(s, 1 if d > 0 else -1)]),
                    "pior": f"{rpm}–{int(rpm) + 500} rpm, carga {load}–{int(load) + 20} %: {worst[2]:.2f} → {worst[3]:.2f} {unit}"})
    order = {"bad": 0, "warn": 1, "ok": 2, "unk": 3}
    return sorted(res, key=lambda x: order[x["status"]])


def _fp_table(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS fingerprint (id INTEGER PRIMARY KEY, ts INTEGER, label TEXT, days INTEGER, profile TEXT)")


def fp_status(con: sqlite3.Connection, days: int = 7) -> dict:
    now = int(time.time() * 1000)
    try:
        ref = con.execute("SELECT * FROM fingerprint ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        ref = None
    cur = fp_profile(con, now - days * DAY, now + 1)
    out = {"atual": {"dias": days, "leituras": fp_points(cur), "faixas": len(cur)}, "referencia": None, "sinais": [], "nivel": "unk",
           "pode_gravar": fp_points(cur) >= 300 and len(cur) >= 4}
    if not ref:
        out["texto"] = ("Ainda não há uma referência gravada. Quando o carro estiver em ordem (mistura corrigida, sem alertas), grave a referência: "
                        "dali em diante o painel avisa de qualquer desvio pequeno, antes de virar defeito."
                        + ("" if out["pode_gravar"] else " Faltam leituras: rode mais um pouco com o motor quente, em várias rotações."))
        return out
    prof = json.loads(ref["profile"])
    # a comparação usa só o que foi lido DEPOIS da referência
    cur = fp_profile(con, max(ref["ts"], now - days * DAY), now + 1)
    sig = fp_compare(prof, cur)
    worst = min((s["status"] for s in sig), key=lambda x: {"bad": 0, "warn": 1, "ok": 2, "unk": 3}[x], default="unk")
    known = [s for s in sig if s["status"] != "unk"]
    out.update(referencia={"ts": ref["ts"], "label": ref["label"], "dias": ref["days"], "leituras": fp_points(prof), "faixas": len(prof)},
               sinais=sig, nivel=worst if known else "unk", atual={"dias": days, "leituras": fp_points(cur), "faixas": len(cur)})
    bad = [s["nome"].lower() for s in sig if s["status"] in ("bad", "warn")]
    out["texto"] = ("Ainda sem leituras novas suficientes depois da referência para comparar." if not known else
                    "O motor está se comportando como na referência saudável." if not bad else
                    "Mudou em relação ao carro saudável: " + ", ".join(bad) + ".")
    return out


def fp_save(con: sqlite3.Connection, label: str, days: int = 7) -> dict:
    _fp_table(con)
    now = int(time.time() * 1000)
    prof = fp_profile(con, now - days * DAY, now + 1)
    if fp_points(prof) < 300 or len(prof) < 4:
        raise ValueError("Poucas leituras com o motor quente para servir de referência. Rode mais (várias rotações e cargas) e tente de novo.")
    con.execute("INSERT INTO fingerprint(ts, label, days, profile) VALUES (?,?,?,?)", (now, label[:120] or "Carro saudável", days, json.dumps(prof)))
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (now, "referencia", f"Referência do motor saudável gravada ({fp_points(prof)} leituras)"))
    con.commit()
    return {"ok": True, "leituras": fp_points(prof), "faixas": len(prof)}


# =====================================================================================================================
# 2. Antes e depois de um conserto
# =====================================================================================================================
# métrica: (nome, unidade, melhor quando, mudança mínima que conta, casas)
IMPACT = {
    "stft_idle": ("Correção de combustível parado", "%", "zero", 4.0, 0),
    "stft_run": ("Correção de combustível rodando", "%", "zero", 4.0, 0),
    "idle_sd": ("Variação da marcha lenta", "rpm", "menor", 12.0, 0),
    "air_idle": ("Ar medido ÷ esperado, parado", "%", "maior", 6.0, 0),
    "ect_max": ("Temperatura máxima", "°C", "menor", 3.0, 0),
    "kml": ("Consumo", "km/L", "maior", 0.7, 1),
    "alerts": ("Alertas por hora de motor", "", "menor", 1.0, 1),
}


def _metrics(con: sqlite3.Connection, a: int, b: int) -> dict:
    reg = insight._regimes(insight.cycles(con, a, b))
    rpm = [c["rpm"] for c in reg["idle"]]
    air = insight._air_ratio(reg["idle"])
    t = con.execute("SELECT COALESCE(SUM(distance_km),0), COALESCE(SUM(fuel_l),0), COALESCE(SUM(duration_s),0) FROM trip WHERE start_ts>=? AND start_ts<?", (a, b)).fetchone()
    n_al = con.execute("SELECT COALESCE(SUM(COALESCE(count,1)),0) FROM alert WHERE ts>=? AND ts<? AND severity!='info'", (a, b)).fetchone()[0]
    ects = [c["ect"] for c in reg["on"] if c.get("ect") is not None]
    return {"n": len(reg["on"]), "horas": round(len(reg["on"]) * 5 / 3600, 2),
            "stft_idle": insight._med(c.get("stft1") for c in reg["idle"]) if len(reg["idle"]) >= 12 else None,
            "stft_run": insight._med(c.get("stft1") for c in reg["run"]) if len(reg["run"]) >= 12 else None,
            "idle_sd": statistics.pstdev(rpm) if len(rpm) >= 12 else None,
            "air_idle": air * 100 if air is not None else None,
            "ect_max": max(ects) if len(ects) >= 12 else None,
            "kml": t[0] / t[1] if t[0] >= 8 and t[1] > 0.3 else None,
            "alerts": n_al / (t[2] / 3600) if t[2] >= 600 else None}


def impact(con: sqlite3.Connection, days: int = 180, window_days: int = 4) -> list[dict]:
    now = int(time.time() * 1000)
    out = []
    for an in con.execute("SELECT * FROM annotation WHERE kind='manutencao' AND ts>? ORDER BY ts DESC LIMIT 20", (now - days * DAY,)):
        ts = an["ts"]
        bef, aft = _metrics(con, ts - window_days * DAY, ts), _metrics(con, ts, min(now, ts + window_days * DAY) + 1)
        rows, better, worse = [], 0, 0
        for k, (name, unit, goal, minch, dec) in IMPACT.items():
            x, y = bef.get(k), aft.get(k)
            if x is None or y is None:
                continue
            gain = (abs(x) - abs(y)) if goal == "zero" else (x - y) if goal == "menor" else (y - x)
            v = "melhorou" if gain >= minch else "piorou" if gain <= -minch else "igual"
            better += v == "melhorou"; worse += v == "piorou"
            rows.append({"nome": name, "unidade": unit, "antes": round(x, dec), "depois": round(y, dec), "veredito": v})
        if aft["n"] < 60:
            verdict, level = "Ainda sem dados suficientes depois do serviço. Rode uns 15 minutos com o motor quente.", "unk"
        elif bef["n"] < 60:
            verdict, level = "Não há dados de antes do serviço para comparar.", "unk"
        elif not rows:
            verdict, level = "Os dados de antes e de depois não cobrem as mesmas situações (parado/rodando).", "unk"
        elif better and not worse:
            verdict, level = f"Funcionou: {better} indicador(es) melhoraram e nenhum piorou.", "ok"
        elif worse and not better:
            verdict, level = f"Piorou depois do serviço: {worse} indicador(es) piores.", "bad"
        elif better or worse:
            verdict, level = f"Resultado misto: {better} melhor(es), {worse} pior(es).", "warn"
        else:
            verdict, level = "Sem mudança mensurável nos indicadores.", "warn"
        out.append({"id": an["id"], "ts": ts, "texto": an["text"], "veredito": verdict, "nivel": level, "indicadores": rows,
                    "antes_h": bef["horas"], "depois_h": aft["horas"], "janela_dias": window_days})
    return out


# =====================================================================================================================
# 3. Plano de manutenção pelos km reais
# =====================================================================================================================
# chave: (nome, km, meses, vale uso severo?, observação). Intervalos típicos; o manual do carro manda.
MAINT = [
    ("oleo", "Óleo do motor e filtro de óleo", 10000, 12, True, "0W-20 ou 5W-30 conforme o manual. Em uso severo a Honda pede metade do intervalo."),
    ("filtro_ar", "Filtro de ar do motor", 20000, 24, True, "Antes disso se rodar em estrada de terra ou muita poeira."),
    ("filtro_cabine", "Filtro de ar da cabine", 10000, 12, False, "Ar-condicionado fraco ou com cheiro pede troca antes."),
    ("fluido_freio", "Fluido de freio", None, 36, False, "Por tempo, não por km: absorve umidade."),
    ("velas", "Velas de ignição (irídio)", 100000, None, False, "NGK IZFR6K13 ou Denso SKJ20DR-M13; confira no manual."),
    ("arrefecimento", "Líquido de arrefecimento", 100000, 60, False, "Honda Type 2, já diluído. Nunca completar com água de torneira."),
    ("oleo_cambio", "Óleo do câmbio manual", 60000, 72, True, "Honda MTF."),
    ("correia", "Correia de acessórios", 100000, None, False, "Inspecionar trincas a cada revisão."),
    ("pneus", "Rodízio de pneus, alinhamento e balanceamento", 10000, None, False, ""),
    ("bateria", "Bateria (teste de carga)", None, 12, False, "O painel mede a partida; vale um teste de carga por ano depois do 3º ano."),
]


def _maint_tables(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS maint (key TEXT PRIMARY KEY, last_odo REAL, last_ts INTEGER, km_int REAL, month_int REAL, note TEXT)")


def _meta(con, key: str):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def odometer(con: sqlite3.Connection) -> dict:
    """Hodômetro estimado = leitura informada pelo dono + km que o Pi registrou depois dela."""
    base, ts = _meta(con, "odo_base_km"), _meta(con, "odo_base_ts")
    if base is None:
        return {"km": None, "base_km": None, "base_ts": None}
    since = con.execute("SELECT COALESCE(SUM(distance_km),0) FROM trip WHERE start_ts>=?", (int(ts),)).fetchone()[0]
    return {"km": round(float(base) + since, 1), "base_km": float(base), "base_ts": int(ts), "rodados_desde": round(since, 1)}


def severe_use(con: sqlite3.Connection) -> dict:
    """Uso severo pelos dados: muito tempo parado com o motor ligado ou trajetos curtos (motor mal aquece)."""
    now = int(time.time() * 1000)
    t = con.execute("""SELECT COUNT(*), COALESCE(SUM(distance_km),0), COALESCE(SUM(duration_s),0), COALESCE(SUM(idle_s),0)
                       FROM trip WHERE finalized=1 AND start_ts>? AND duration_s>=120""", (now - 60 * DAY,)).fetchone()
    n, km, dur, idle = t
    if n < 4 or dur < 3600:
        return {"severo": False, "motivo": "Ainda poucos dados para avaliar o tipo de uso.", "conhecido": False}
    share, avg = idle / dur, km / n
    why = []
    if share > 0.35:
        why.append(f"{share:.0%} do tempo parado com o motor ligado")
    if avg < 8:
        why.append(f"trajetos curtos (média de {avg:.1f} km)")
    return {"severo": bool(why), "conhecido": True, "motivo": "Uso severo: " + " e ".join(why) + "." if why else "Uso normal pelos dados dos últimos 60 dias."}


def maintenance(con: sqlite3.Connection) -> dict:
    now = int(time.time() * 1000)
    odo = odometer(con)
    sev = severe_use(con)
    try:
        saved = {r["key"]: dict(r) for r in con.execute("SELECT * FROM maint")}
    except sqlite3.Error:
        saved = {}
    items = []
    for key, name, km_int, month_int, sev_applies, note in MAINT:
        s = saved.get(key, {})
        km_i, mo_i = s.get("km_int") or km_int, s.get("month_int") or month_int
        factor = 0.5 if sev["severo"] and sev_applies else 1.0
        km_eff = km_i * factor if km_i else None
        mo_eff = mo_i * factor if mo_i else None
        last_odo, last_ts = s.get("last_odo"), s.get("last_ts")
        km_left = (last_odo + km_eff - odo["km"]) if km_eff and last_odo is not None and odo["km"] is not None else None
        mo_left = ((last_ts + mo_eff * 30.44 * DAY) - now) / (30.44 * DAY) if mo_eff and last_ts else None
        if last_ts is None and last_odo is None:
            status, txt = "unk", "Informe quando foi feito pela última vez."
        else:
            fr = min([x for x in ((km_left / km_eff) if km_left is not None else None, (mo_left / mo_eff) if mo_left is not None else None) if x is not None], default=None)
            parts = ([f"{km_left:,.0f} km".replace(",", ".")] if km_left is not None else []) + ([f"{mo_left:.0f} meses" if abs(mo_left) >= 1.5 else f"{mo_left * 30.44:.0f} dias"] if mo_left is not None else [])
            if fr is None:
                status, txt = "unk", "Informe o hodômetro para calcular por km."
            elif fr < 0:
                status, txt = "bad", "Vencido: passou " + " / ".join(p.replace("-", "") for p in parts)
            elif fr < 0.12:
                status, txt = "warn", "Está chegando: faltam " + " ou ".join(parts)
            else:
                status, txt = "ok", "Faltam " + " ou ".join(parts)
        items.append({"key": key, "nome": name, "km_int": km_i, "month_int": mo_i, "km_efetivo": km_eff, "meses_efetivo": mo_eff,
                      "severo": factor < 1, "last_odo": last_odo, "last_ts": last_ts, "km_restante": round(km_left) if km_left is not None else None,
                      "meses_restante": round(mo_left, 1) if mo_left is not None else None, "status": status, "texto": txt, "obs": note, "nota": s.get("note")})
    order = {"bad": 0, "warn": 1, "unk": 2, "ok": 3}
    items.sort(key=lambda x: (order[x["status"]], x["km_restante"] if x["km_restante"] is not None else 9e9))
    return {"hodometro": odo, "uso": sev, "itens": items}


def maint_set_odometer(con: sqlite3.Connection, km: float, ts: int | None = None) -> None:
    ts = ts or int(time.time() * 1000)
    for k, v in (("odo_base_km", km), ("odo_base_ts", ts)):
        con.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    con.commit()


def maint_done(con: sqlite3.Connection, key: str, odo: float | None, ts: int | None, note: str | None = None,
               km_int: float | None = None, month_int: float | None = None) -> None:
    if key not in {m[0] for m in MAINT}:
        raise ValueError("item desconhecido")
    _maint_tables(con)
    ts = ts or int(time.time() * 1000)
    if odo is None:
        odo = odometer(con)["km"]
    con.execute("""INSERT INTO maint(key, last_odo, last_ts, km_int, month_int, note) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET last_odo=excluded.last_odo, last_ts=excluded.last_ts, note=excluded.note,
                   km_int=COALESCE(excluded.km_int, km_int), month_int=COALESCE(excluded.month_int, month_int)""",
                (key, odo, ts, km_int, month_int, (note or "")[:200] or None))
    name = next(m[1] for m in MAINT if m[0] == key)
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (ts, "manutencao", f"{name}" + (f" aos {odo:,.0f} km".replace(",", ".") if odo else "") + (f" ({note})" if note else "")))
    con.commit()


# =====================================================================================================================
# 4. Quanto cada hábito custou
# =====================================================================================================================
def habits(con: sqlite3.Connection, a: int, b: int) -> dict:
    tot = idle_l = high_l = idle_s = high_s = dur = 0.0
    prev = None
    for r in con.execute("SELECT ts, speed, rpm, fuel_lh FROM fast_sample WHERE ts>=? AND ts<? ORDER BY ts", (a, b)):
        if prev is not None:
            dt = (r[0] - prev[0]) / 1000
            if 0 < dt <= 5 and (prev[2] or 0) > 400:
                lit = (prev[3] or 0) * dt / 3600
                tot += lit; dur += dt
                if (prev[1] or 0) == 0:
                    idle_l += lit; idle_s += dt
                elif (prev[2] or 0) > 3000:
                    high_l += lit; high_s += dt
        prev = r
    price = con.execute("SELECT price_l FROM fuel_fill WHERE price_l IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    p = price[0] if price else None
    money = lambda lit: round(lit * p, 2) if p else None
    return {"litros": round(tot, 2), "segundos": int(dur), "preco_l": p,
            "parado": {"litros": round(idle_l, 2), "segundos": int(idle_s), "parte": round(idle_l / tot, 3) if tot > 0.02 else None, "custo": money(idle_l)},
            "giro_alto": {"litros": round(high_l, 2), "segundos": int(high_s), "parte": round(high_l / tot, 3) if tot > 0.02 else None, "custo": money(high_l)},
            "custo_total": money(tot)}


# =====================================================================================================================
# 5. Gasolina × etanol pelos tanques do próprio carro
# =====================================================================================================================
def kml_by_fuel(fills: list[dict]) -> dict:
    """km/L real de cada combustível: o rendimento medido num abastecimento é do que estava no tanque desde o anterior."""
    acc = {"gasolina": [], "etanol": []}
    fs = sorted(fills, key=lambda f: f["ts"])
    for prev, f in zip(fs, fs[1:]):
        if f.get("real_kml") and prev.get("mix_after") is not None:
            if prev["mix_after"] <= 0.25:
                acc["gasolina"].append(f["real_kml"])
            elif prev["mix_after"] >= 0.75:
                acc["etanol"].append(f["real_kml"])
    return {k: {"kml": round(statistics.fmean(v[-4:]), 1), "tanques": len(v)} if v else None for k, v in acc.items()}
