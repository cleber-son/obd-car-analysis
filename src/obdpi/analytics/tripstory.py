"""Diagnóstico da viagem: conta como foi a viagem, em linguagem simples, só com regras fixas (sem IA, sem internet).

Entrada: o mesmo documento de `/api/trips/{id}` (campos da viagem, séries resumidas, alertas, hábitos) mais um contexto
pequeno (anotações do diário na janela da viagem, viagens anteriores para comparar, números do GPS). Saída: um JSON
pequeno (alguns kB) com manchete, narrativa, veredito por sistema com as evidências, dinheiro, direção, destaques,
linha do tempo, mini gráficos e os textos de "Ouvir" e "Copiar". O Pi serve em `/api/trips/{id}/story` e o obd-sync
manda o mesmo documento para o painel online.

Regras da casa: toda frase sai de um número medido (que vai junto, em `ev`); sem dado suficiente o sistema fica como
"sem dados", nunca como "ok". Nada de posição nem nome de lugar aqui (o documento é público no painel online): o mapa
do trajeto vem das rotas `/api/gps/*`, que têm a senha de localização.
"""
from __future__ import annotations

import bisect
import statistics
import time

VERSION = 1
RANK = {"na": 0, "ok": 0, "note": 1, "warn": 2, "bad": 3}
IDLE_RULES = {"R4", "R5"}                 # regras de marcha lenta do coletor (oscilando / irregular)
SPARK_N = 72                              # pontos por mini gráfico
SIG = {"load": "carga do motor", "stft1": "ajuste de combustível", "ltft1": "ajuste longo de combustível", "o2s2_trim": "sonda depois do catalisador",
       "o2s2_v": "sonda depois do catalisador", "evap_cmd": "purga do cânister", "rpm": "rotação", "map": "pressão do coletor", "maf": "medidor de ar",
       "ect": "temperatura do motor", "iat": "temperatura do ar", "batt": "tensão da bateria", "ecu_volt": "tensão da central", "timing": "ponto de ignição",
       "tps": "borboleta", "o2c1_lambda": "sonda λ", "fuel_lh": "vazão de combustível", "speed": "velocidade"}


# ------------------------------------------------------------------ texto
def _n(x, d: int = 0) -> str:
    """Número no formato brasileiro (vírgula decimal, ponto de milhar)."""
    if x is None:
        return "—"
    s = f"{x:,.{d}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _sg(x, d: int = 0) -> str:
    """Com sinal: +2, −3, 0."""
    r = round(x, d)
    if r == 0:
        return _n(0, d)
    return ("+" if r > 0 else "−") + _n(abs(r), d)


def _rs(x) -> str:
    return "R$ " + _n(x, 2)


def _dur(s) -> str:
    if s is None:
        return "—"
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{int(round(s / 60))} min"
    return f"{s // 3600} h {s % 3600 // 60:02d}"


def _hm(ts: int, tz_ms: int) -> str:
    return time.strftime("%H:%M", time.gmtime((ts + tz_ms) / 1000))


def _date(ts: int, tz_ms: int) -> str:
    return time.strftime("%d/%m", time.gmtime((ts + tz_ms) / 1000))


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _join(xs: list[str]) -> str:
    xs = [x for x in xs if x]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " e " + xs[-1] if xs else ""


def _uniq(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(xs))


def _med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


# ------------------------------------------------------------------ séries
def _pts(series: dict, key: str) -> list[tuple]:
    """[(ts, valor, mín, máx)] — as séries vêm como [ts, v] ou, quando resumidas, [ts, média, mín, máx]."""
    out = []
    for p in (series or {}).get(key) or []:
        if p is None or len(p) < 2 or p[1] is None:
            continue
        out.append((p[0], p[1], p[2] if len(p) > 2 and p[2] is not None else p[1], p[3] if len(p) > 3 and p[3] is not None else p[1]))
    return out


class _At:
    """Valor mais próximo de uma série num instante (até `tol` ms de distância)."""

    def __init__(self, pts: list[tuple], tol: int = 20_000):
        self.p, self.ts, self.tol = pts, [x[0] for x in pts], tol

    def __call__(self, ts: int, col: int = 1):
        if not self.p:
            return None
        i = bisect.bisect_left(self.ts, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.p) and (best is None or abs(self.ts[j] - ts) < abs(self.ts[best] - ts)):
                best = j
        return self.p[best][col] if best is not None and abs(self.ts[best] - ts) <= self.tol else None


def _groups(ts_list: list[int], gap_ms: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for t in sorted(ts_list):
        if out and t - out[-1][1] <= gap_ms:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b) for a, b in out]


def _spark(pts: list[tuple], t0: int, t1: int, n: int = SPARK_N, d: int = 0, peak: bool = False) -> list:
    """Série em n trechos iguais entre a saída e o fim (só os valores; o passo vai em spark.dt). Trecho sem leitura repete
    o anterior. peak=True guarda o pico de cada trecho (rotação)."""
    if not pts or t1 <= t0:
        return []
    acc: list[list] = [[] for _ in range(n)]
    for p in pts:
        i = int((p[0] - t0) / (t1 - t0) * n)
        if 0 <= i <= n:
            acc[min(i, n - 1)].append(p[3] if peak else p[1])
    out, last = [], None
    for c in acc:
        if c:
            v = max(c) if peak else sum(c) / len(c)
            last = round(v, d) if d else int(round(v))
        out.append(last)
    return out


# ------------------------------------------------------------------ análise
def build(trip: dict, ctx: dict | None = None) -> dict:
    """trip: documento de /api/trips/{id}. ctx: {"now", "tz_ms", "annotations": [{ts, kind, text}], "baseline":
    {"kml_usual", "kml_n", "similar": [{id, start_ts, duration_s, distance_km, km_l, fuel_l}], "similar_by"},
    "gps": {drive_score, freadas, arrancadas, curvas, acima_pct, n_stops, climb_m, coverage}}. Tudo opcional."""
    ctx = ctx or {}
    t = trip
    tz = int(ctx.get("tz_ms") if ctx.get("tz_ms") is not None else -3 * 3600_000)
    S = t.get("series") or {}
    rpm, spd = _pts(S, "rpm"), _pts(S, "speed")
    ect, stft, lam = _pts(S, "ect"), _pts(S, "stft1"), _pts(S, "o2c1_lambda")
    batt, load, timing, fuel = _pts(S, "batt"), _pts(S, "load"), _pts(S, "timing"), _pts(S, "fuel_lh")
    pedal = _pts(S, "pedal_d")
    t0 = t.get("start_ts") or (rpm[0][0] if rpm else 0)
    last_ts = max([p[-1][0] for p in (rpm, spd, ect) if p] or [t0])
    t1 = t.get("end_ts") or last_ts
    now = int(ctx.get("now") or t1)
    finalized = bool(t.get("finalized"))
    in_progress = not finalized and now - t1 < 180_000
    unclosed = not finalized and not in_progress
    hm = lambda ts: _hm(ts, tz)
    rpm_at, spd_at, ect_at = _At(rpm), _At(spd), _At(ect, 60_000)
    load_at, tim_at = _At(load), _At(timing)

    # ---------------- percurso (dos campos da viagem; se ainda não fechou, das séries)
    dur_s = t.get("duration_s") or max(0, (t1 - t0) / 1000)
    km_i = moving_s = stopped_s = 0.0
    stops: list[list[int]] = []                 # [início, fim] com o carro parado e o motor ligado
    if len(spd) >= 2:
        dts = sorted((b[0] - a[0]) / 1000 for a, b in zip(spd, spd[1:]))
        cap = max(5.0, dts[len(dts) // 2] * 3)
        cur = None
        for a, b in zip(spd, spd[1:]):
            dt = (b[0] - a[0]) / 1000
            if not 0 < dt <= cap:
                cur = None
                continue
            km_i += a[1] * dt / 3600
            on = (rpm_at(a[0]) or 0) > 400
            if a[3] >= 1:
                moving_s += dt
                cur = None
            elif on:
                stopped_s += dt
                if cur is None:
                    cur = [a[0], b[0]]
                    stops.append(cur)
                cur[1] = b[0]
            else:
                cur = None
    km = t.get("distance_km") if t.get("distance_km") is not None else (round(km_i, 2) if spd else None)
    if t.get("idle_s") is not None and finalized:
        stopped_s = float(t["idle_s"])
        moving_s = max(0.0, dur_s - stopped_s)
    max_speed = t.get("max_speed") if t.get("max_speed") is not None else (max(p[3] for p in spd) if spd else None)
    max_rpm = t.get("max_rpm") if t.get("max_rpm") is not None else (max(p[3] for p in rpm) if rpm else None)
    long_stops = [(a, (b - a) / 1000) for a, b in stops if (b - a) / 1000 >= 90]
    first_stop = stops[0] if stops and stops[0][0] - t0 < 20_000 else None      # parado desde a partida (aquecendo?)
    short = dur_s < 180 or (km is not None and km < 0.3 and dur_s < 600)

    # ---------------- ar-condicionado marcado pelo dono (❄️ do modo direção)
    notes = sorted((a for a in ctx.get("annotations") or [] if a.get("ts") is not None and t0 - 5_000 <= a["ts"] <= t1 + 5_000), key=lambda a: a["ts"])
    ac: list[list[int]] = []
    for a in notes:
        if a.get("kind") != "ar":
            continue
        off = "deslig" in str(a.get("text") or "").lower()
        if not off and not (ac and ac[-1][1] is None):
            ac.append([a["ts"], None])
        elif off and ac and ac[-1][1] is None:
            ac[-1][1] = a["ts"]
    ac_win = [(a, b if b is not None else t1) for a, b in ac]
    ac_notes = [a for a in notes if a.get("kind") == "ar" and "deslig" not in str(a.get("text") or "").lower()]
    ac_by = "detectado pelo Delfos" if ac_notes and all("detectado" in str(a.get("text") or "") for a in ac_notes) else "marcado por você"
    in_ac = lambda ts, pad=0: any(a - pad <= ts <= b + pad for a, b in ac_win)
    ac_s = sum((b - a) / 1000 for a, b in ac_win)

    # ---------------- marcha lenta com o motor quente
    ped_rest = min((p[2] for p in pedal), default=None)
    ped_at = _At(pedal, 5_000)
    ped_ts = [p[0] for p in pedal]
    idle_fast = []
    for p in rpm:
        if p[0] - t0 < 20_000 or not 400 <= p[1] <= 1300 or (spd_at(p[0], 3) or 0) >= 1 or (ect_at(p[0]) or 0) < 70:
            continue
        if ped_rest is not None and (ped_at(p[0], 3) or ped_rest) > ped_rest + 2:
            continue                                            # pé no acelerador com o carro parado não é marcha lenta
        if any(0 <= p[0] - a < 6_000 for a, _ in stops if a > t0 + 20_000):
            continue                                            # acabou de parar: a rotação ainda está assentando
        idle_fast.append(p)
    idle_rpm = _med(p[1] for p in idle_fast) if len(idle_fast) >= 15 else None
    idle_slow = lambda ts: (spd_at(ts, 3) or 0) < 1 and 400 <= (rpm_at(ts) or 0) <= 1300
    warm = lambda ts: (ect_at(ts) or 0) >= 70
    idle_load = _med(p[1] for p in load if idle_slow(p[0]) and warm(p[0]))
    idle_tim = _med(p[1] for p in timing if idle_slow(p[0]) and warm(p[0]))
    # "carga entrando" parado: a carga do motor salta e/ou o ponto de ignição recua (compressor do ar, ventoinha, direção)
    def resting(ts: int) -> bool:                               # parado, quente, sem pé no acelerador e sem estar saindo
        if not (idle_slow(ts) and warm(ts)):
            return False
        if ped_rest is not None and any(abs(p[0] - ts) <= 5_000 and p[3] > ped_rest + 2 for p in pedal[bisect.bisect_left(ped_ts, ts - 5_000):bisect.bisect_right(ped_ts, ts + 5_000)]):
            return False
        return not any(-2_000 <= b - ts <= 6_000 for _, b in stops if b < t1 - 3_000)
    kick_ts = []
    if idle_load is not None:
        kick_ts += [p[0] for p in load if p[3] >= idle_load + 12 and resting(p[0])]
    if idle_tim is not None:
        kick_ts += [p[0] for p in timing if p[2] <= idle_tim - 10 and resting(p[0])]
    kicks = []
    for a, b in _groups(kick_ts, 25_000):
        lo = [p[3] for p in load if a - 3_000 <= p[0] <= b + 3_000]
        ti = [p[2] for p in timing if a - 3_000 <= p[0] <= b + 3_000]
        kicks.append({"a": a, "b": b, "load": max(lo) if lo else None, "timing": min(ti) if ti else None})
    near_kick = lambda ts, pad: any(k["a"] - pad <= ts <= k["b"] + pad for k in kicks)
    dips = []
    if idle_rpm is not None:
        for a, b in _groups([p[0] for p in idle_fast if p[2] < idle_rpm - 90], 20_000):
            lo = min(p[2] for p in idle_fast if a <= p[0] <= b)
            why = "ar" if in_ac(a, 10_000) else "carga" if near_kick(a, 12_000) or near_kick(b, 12_000) else None
            dips.append({"ts": a, "rpm": lo, "why": why})
    idle_sd = statistics.pstdev([p[1] for p in idle_fast]) if len(idle_fast) >= 15 else None

    # ---------------- alertas: o que tem explicação, o que é "fora do habitual" e o que é de verdade
    alerts = []
    for a in t.get("alerts") or []:
        rule, sev, ts = str(a.get("rule") or ""), a.get("severity") or "info", a.get("ts") or t0
        if sev == "info":
            continue
        idle_alert = rule in IDLE_RULES or (a.get("signal") == "rpm" and "lenta" in str(a.get("title") or "").lower())
        kind, why = "real", None
        if idle_alert and in_ac(ts, 45_000):
            kind, why = "explicado", "ar"
        elif idle_alert and near_kick(ts, 45_000):               # a regra olha os 30 s anteriores
            kind, why = "explicado", "carga"
        elif rule.startswith("z:load@idle") and ac_s > 0:
            kind, why = "explicado", "ar"
        elif rule.startswith("z:") or a.get("source") == "baseline":
            kind = "habitual"
        alerts.append({"ts": ts, "sev": sev, "title": a.get("title") or "", "detail": a.get("detail") or "", "count": a.get("count") or 1,
                       "rule": rule, "kind": kind, "why": why, "signal": a.get("signal")})
    al_real = [a for a in alerts if a["kind"] == "real"]
    al_expl = [a for a in alerts if a["kind"] == "explicado"]
    al_hab = [a for a in alerts if a["kind"] == "habitual"]
    dtcs = sorted({str(e.get("code")) for e in t.get("dtc_events") or [] if e.get("code") and str(e.get("event") or "").lower() not in ("cleared", "apagado", "gone")})

    systems: list[dict] = []

    def sysadd(key, icon, name, status, txt, ev=None):
        systems.append({"key": key, "icon": icon, "name": name, "status": status, "txt": txt, "ev": [e for e in (ev or []) if e]})

    # ---------------- mistura (ajuste curto e sonda λ)
    closed = lambda ts: warm(ts) and (rpm_at(ts) or 0) > 400
    st_idle = [p[1] for p in stft if closed(p[0]) and idle_slow(p[0]) and not near_kick(p[0], 6_000)]
    st_run = [p[1] for p in stft if closed(p[0]) and (spd_at(p[0]) or 0) >= 30]
    lam_w = [p[1] for p in lam if closed(p[0]) and 0.7 <= p[1] < 1.5]             # sem corte de combustível (λ ≥ 1,5)
    m_idle = _med(st_idle) if len(st_idle) >= 8 else None
    m_run = _med(st_run) if len(st_run) >= 8 else None
    m_lam = _med(lam_w) if len(lam_w) >= 10 else None
    mix = {"stft_idle": None if m_idle is None else round(m_idle, 1), "stft_run": None if m_run is None else round(m_run, 1),
           "lam": None if m_lam is None else round(m_lam, 3)}
    worst = max([abs(x) for x in (m_idle, m_run) if x is not None], default=None)
    if worst is None and m_lam is None:
        sysadd("mistura", "⛽", "Mistura ar-combustível", "na", "Sem leitura suficiente com o motor quente para julgar a mistura nesta viagem.")
    else:
        lam_off = m_lam is not None and not 0.97 <= m_lam <= 1.03
        stt = "bad" if (worst or 0) > 25 else "warn" if (worst or 0) > 10 or lam_off else "ok"
        side = None
        if stt != "ok" and worst is not None:
            big = m_idle if m_idle is not None and abs(m_idle) == worst else m_run
            side = "pobre (a central está colocando combustível a mais para compensar)" if big > 0 else "rica (a central está tirando combustível)"
        parts = []
        if m_idle is not None:
            parts.append(f"{_sg(m_idle)} % parado")
        if m_run is not None:
            parts.append(f"{_sg(m_run)} % rodando")
        txt = ("Mistura no ponto: " if stt == "ok" else f"Mistura {side}: " if side else "Sonda fora do centro: ") \
            + (f"ajuste de combustível de {_join(parts)}" if parts else "ajuste sem leitura suficiente") \
            + (f"; sonda em λ {_n(m_lam, 2)}" if m_lam is not None else "") + " (normal: dentro de ±10 % e λ perto de 1,00)."
        if stt != "ok" and m_idle is not None and m_run is not None and m_idle - m_run > 8:
            txt += " Pior parado do que rodando: é o padrão de ar entrando sem ser medido (mangueira ou junta)."
        sysadd("mistura", "⛽", "Mistura ar-combustível", stt, txt,
               [f"Ajuste curto parado (motor quente): {_sg(m_idle, 1)} % em {len(st_idle)} leituras" if m_idle is not None else None,
                f"Ajuste curto rodando (30 km/h ou mais): {_sg(m_run, 1)} % em {len(st_run)} leituras" if m_run is not None else None,
                f"Sonda λ (mediana, motor quente): {_n(m_lam, 3)}" if m_lam is not None else None])

    # ---------------- temperatura
    start_ect = t.get("start_ect") if t.get("start_ect") is not None else (ect[0][1] if ect else None)
    max_ect = max([p[3] for p in ect], default=t.get("max_ect"))
    if t.get("max_ect") is not None and finalized:
        max_ect = max(max_ect or 0, t["max_ect"])
    warm_s = t.get("warmup_s")
    if warm_s is None and start_ect is not None and start_ect < 70:
        hit = next((p[0] for p in ect if p[1] >= 80), None)
        warm_s = (hit - t0) / 1000 if hit else None
    fan = None                                                    # pico ≥ 92 °C seguido de queda ≥ 3 °C: a ventoinha entrou
    for i, p in enumerate(ect):
        if p[1] >= 92 and (fan is None or p[1] > fan[1]):
            after = [q[1] for q in ect[i + 1:] if q[0] - p[0] <= 300_000]
            if after and min(after) <= p[1] - 3:
                fan = (p[0], p[1], min(after))
    if max_ect is None:
        sysadd("temperatura", "🌡️", "Temperatura do motor", "na", "Sem leitura de temperatura nesta viagem.")
    else:
        cold = start_ect is not None and start_ect < 50
        never = cold and warm_s is None and max_ect < 80
        stt = "bad" if max_ect >= 110 else "warn" if max_ect >= 105 or (never and dur_s > 25 * 60) or (warm_s or 0) > 20 * 60 else "ok"
        if max_ect >= 105:
            txt = f"Esquentou demais: chegou a {_n(max_ect)} °C (o normal do Civic é trabalhar entre 85 e 100 °C)."
        elif never and dur_s > 25 * 60:
            txt = f"Em {_dur(dur_s)} o motor não passou de {_n(max_ect)} °C. Motor que não aquece costuma ser válvula termostática aberta."
        elif (warm_s or 0) > 20 * 60:
            txt = f"Demorou {_dur(warm_s)} para chegar a 80 °C (o comum é de 5 a 12 min). Vale olhar a válvula termostática."
        else:
            txt = (f"Saiu com {_n(start_ect)} °C e chegou a 80 °C em {_dur(warm_s)}; " if warm_s and start_ect is not None else
                   f"Saiu com {_n(start_ect)} °C e ainda estava aquecendo (máxima de {_n(max_ect)} °C); " if never else "") \
                + (f"máxima de {_n(max_ect)} °C, dentro do normal." if not never else "viagem curta demais para chegar à temperatura de trabalho.")
            txt = txt[0].upper() + txt[1:]
            if fan:
                txt += f" Subiu a {_n(fan[1])} °C e recuou para {_n(fan[2])} °C: é o esperado quando a ventoinha entra."
        sysadd("temperatura", "🌡️", "Temperatura do motor", stt, txt,
               [f"Na partida: {_n(start_ect)} °C" if start_ect is not None else None,
                f"Tempo até 80 °C: {_dur(warm_s)}" if warm_s else None, f"Máxima: {_n(max_ect)} °C",
                f"Pico de {_n(fan[1])} °C às {hm(fan[0])}, depois {_n(fan[2])} °C" if fan else None])

    # ---------------- bateria e alternador
    run_v = [p[1] for p in batt if p[0] - t0 > 30_000 and (rpm_at(p[0]) or 0) > 400]
    v_med, v_min, v_max = (_med(run_v), min(run_v), max(run_v)) if len(run_v) >= 5 else (None, None, None)
    crank, rest = t.get("crank_v"), t.get("rest_v")
    crank = crank if crank is not None and crank > 3 else None
    rest = rest if rest is not None and rest > 6 else None
    if v_med is None and crank is None and rest is None:
        sysadd("eletrica", "🔋", "Bateria e alternador", "na", "Sem leitura suficiente de tensão nesta viagem.")
    else:
        stt = "ok"
        bits = []
        if v_med is not None:
            if v_max > 15.5 or v_med < 12.0:
                stt = "bad"
            elif v_max > 15.1 or v_med < 12.6:
                stt = "warn"
            if v_max > 15.1:
                bits.append(f"alternador carregando demais (chegou a {_n(v_max, 1)} V; o teto saudável é 14,8 V)")
            elif v_med < 12.6:
                bits.append(f"alternador carregando pouco ({_n(v_med, 1)} V com o motor ligado; o esperado é 13,5 a 14,5 V)")
            elif v_med < 13.2:
                bits.append(f"alternador em {_n(v_med, 1)} V (o Civic baixa a carga sozinho quando a bateria está cheia; chegou a {_n(v_max, 1)} V)")
            else:
                bits.append(f"alternador carregando em {_n(v_med, 1)} V")
        if crank is not None:
            if crank < 8.5:
                stt = "bad"
            elif crank < 9.6:
                stt = max(stt, "warn", key=RANK.get)
            bits.append(f"na partida a bateria caiu até {_n(crank, 1)} V" + (" (fraca: o bom é ficar acima de 9,6 V)" if crank < 9.6 else " (boa: acima de 9,6 V)"))
        if rest is not None:
            if rest < 12.2:
                stt = max(stt, "warn", key=RANK.get)
            bits.append(f"em repouso estava com {_n(rest, 1)} V" + (" (baixa: saudável é 12,4 a 12,7 V)" if rest < 12.2 else ""))
        txt = _join(bits) + "."
        sysadd("eletrica", "🔋", "Bateria e alternador", stt, txt[0].upper() + txt[1:],
               [f"Com o motor ligado: mediana {_n(v_med, 1)} V (de {_n(v_min, 1)} a {_n(v_max, 1)} V)" if v_med is not None else None,
                f"Menor tensão na partida: {_n(crank, 1)} V" if crank is not None else None,
                f"Repouso antes de ligar: {_n(rest, 2)} V" if rest is not None else None])

    # ---------------- marcha lenta
    if idle_rpm is None:
        sysadd("lenta", "〰️", "Marcha lenta", "na", "Pouco tempo parado com o motor quente para julgar a marcha lenta.")
    else:
        unexpl = [d for d in dips if not d["why"]]
        expl = [d for d in dips if d["why"]]
        low = min((d["rpm"] for d in unexpl), default=None)
        stt = "warn" if len(unexpl) >= 2 or (low is not None and low < 520) else "note" if unexpl or expl else "ok"
        txt = f"Marcha lenta em {_n(idle_rpm)} rpm com o motor quente"
        if not dips:
            txt += ", firme."
        if expl:
            w = f"o ar-condicionado estava ligado ({ac_by})" if all(d["why"] == "ar" for d in expl) else \
                "a carga do motor saltou no mesmo instante, que é o jeito de uma carga pesada entrando (quase sempre o compressor do ar-condicionado)"
            txt += f". Caiu {_plural(len(expl), 'vez', 'vezes')} ({_join(_uniq([hm(d['ts']) for d in expl])[:4])}, mínimo de {_n(min(d['rpm'] for d in expl))} rpm), mas {w}"
            txt += "." if not unexpl else ""
        if unexpl:
            txt += ("; e " if expl else ". ") + f"{'caiu' if expl else 'Caiu'} {_plural(len(unexpl), 'vez', 'vezes')} sem causa aparente " \
                   f"({_join(_uniq([hm(d['ts']) for d in unexpl])[:4])}, mínimo de {_n(low)} rpm)."
        sysadd("lenta", "〰️", "Marcha lenta", stt, txt,
               [f"Rotação parada (motor quente): mediana {_n(idle_rpm)} rpm, desvio de {_n(idle_sd)} rpm em {len(idle_fast)} leituras",
                f"Carga do motor parado: {_n(idle_load)} %" if idle_load is not None else None]
               + [f"{hm(k['a'])}: carga foi a {_n(k['load'])} %" + (f" e o ponto de ignição a {_sg(k['timing'])}°" if k["timing"] is not None else "") for k in kicks[:4]]
               + [f"{hm(d['ts'])}: rotação caiu a {_n(d['rpm'])} rpm" + ("" if d["why"] else " (sem explicação nos dados)") for d in dips[:4]])

    # ---------------- avisos e códigos
    if dtcs:
        sysadd("avisos", "🚨", "Avisos e códigos de falha", "bad",
               f"A central registrou {_plural(len(dtcs), 'código de falha', 'códigos de falha')} nesta viagem: {_join(dtcs[:6])}. Veja em Diagnóstico o que cada um significa.",
               [f"Código {c}" for c in dtcs[:6]])
    elif not alerts:
        sysadd("avisos", "🔔", "Avisos e códigos de falha", "ok", "Nenhum aviso do Delfos e nenhum código de falha nesta viagem.")
    else:
        crit = [a for a in al_real if a["sev"] == "crit"]
        stt = "bad" if crit else "warn" if al_real else "note"
        bits = []
        if al_real:
            bits.append(f"{_plural(len(al_real), 'aviso para olhar', 'avisos para olhar')}: " + _join([f"{a['title']} ({hm(a['ts'])})" for a in al_real[:4]]))
        if al_expl:
            by_ac = all(a["why"] == "ar" for a in al_expl)
            bits.append(f"{_plural(len(al_expl), 'aviso', 'avisos')} de marcha lenta com cara de alarme falso: "
                        + (f"o ar-condicionado estava ligado ({ac_by})" if by_ac else
                           "bateram com os instantes em que a carga do motor saltou com o carro parado, o jeito do compressor do ar-condicionado entrando")
                        + ("" if by_ac else "; o Delfos não reconheceu o ar aqui: se estava ligado, toque no ❄️ do modo direção"))
        if al_hab:
            sigs = _join(sorted({SIG.get(str(a["signal"]), str(a["signal"])) for a in al_hab if a["signal"]})[:5])
            mix_ok = any(s["key"] == "mistura" and s["status"] == "ok" for s in systems) and any(a["signal"] in ("stft1", "o2c1_lambda") for a in al_hab)
            bits.append(f"{_plural(len(al_hab), 'aviso', 'avisos')} de \"fora do habitual\" ({sigs}): o Delfos compara com o que aprendeu como normal; "
                        "se o carro passou por conserto ou o ar estava ligado, o habitual antigo deixa de valer"
                        + (f". O ajuste de combustível medido nesta viagem está normal ({_sg(m_idle if m_idle is not None else m_run)} %)" if mix_ok else ""))
        txt = ". ".join(b[0].upper() + b[1:] for b in bits) + "."
        sysadd("avisos", "🔔", "Avisos e códigos de falha", stt, txt,
               [f"{hm(a['ts'])} · {a['title']}: {a['detail']}" + (f" ({a['count']}×)" if a["count"] > 1 else "")
                + {"explicado": " → provável alarme falso", "habitual": " → comparação com o habitual", "real": ""}[a["kind"]] for a in alerts[:8]])

    # ---------------- dinheiro
    H = t.get("habits") or {}
    par, gir = H.get("parado") or {}, H.get("giro_alto") or {}
    liters = t.get("fuel_l") if t.get("fuel_l") else (H.get("litros") if (H.get("litros") or 0) > 0.02 else None)
    price = H.get("preco_l")
    cost = t.get("cost") or (H.get("custo_total") if liters is not None and not t.get("fuel_l") else None) or (round(liters * price, 2) if liters and price else None)
    kml = t.get("km_l") if t.get("km_l") else (round(km / liters, 1) if liters and km and km >= 0.5 else None)
    base = ctx.get("baseline") or {}
    usual = base.get("kml_usual")
    idle_lh = (par["litros"] / par["segundos"] * 3600) if par.get("litros") and (par.get("segundos") or 0) >= 60 else \
        _med(p[1] for p in fuel if idle_slow(p[0]) and warm(p[0]))
    money = {"litros": liters, "custo": cost, "preco_l": price, "km_l": kml, "km_l_usual": usual, "km_l_n": base.get("kml_n"),
             "parado": {"s": par.get("segundos"), "litros": par.get("litros"), "custo": par.get("custo"), "parte": par.get("parte")} if par else None,
             "giro_alto": {"s": gir.get("segundos"), "litros": gir.get("litros"), "custo": gir.get("custo"), "parte": gir.get("parte")} if gir else None,
             "custo_km": round(cost / km, 2) if cost and km and km >= 1 else None}
    tips: list[dict] = []
    if idle_lh and idle_lh > 0.2:
        save = lambda secs: (round(idle_lh * secs / 3600, 2), round(idle_lh * secs / 3600 * price, 2) if price else None)
        used = None
        if first_stop and (first_stop[1] - first_stop[0]) / 1000 >= 150:
            d = (first_stop[1] - first_stop[0]) / 1000
            lit, rs = save(d - 30)
            used = first_stop
            tips.append({"icon": "🏁", "save_l": lit, "save_rs": rs,
                         "txt": f"Você ficou {_dur(d)} parado logo na saída, com o motor ligado. Se tivesse saído depois de uns 30 s, rodando leve "
                                f"(motor moderno aquece mais rápido andando do que parado), teria poupado cerca de {_n(lit, 2)} L"
                                + (f", uns {_rs(rs)}." if rs else ".")})
        rest_long = [(a, d) for a, d in long_stops if not used or a != used[0]]
        if rest_long:
            excess = sum(d - 60 for a, d in rest_long)
            lit, rs = save(excess)
            if lit >= 0.03:
                tips.append({"icon": "🅿️", "save_l": lit, "save_rs": rs,
                             "txt": f"Teve {_plural(len(rest_long), 'parada', 'paradas')} de mais de 1 min com o motor ligado ({_dur(sum(d for _, d in rest_long))} no total). "
                                    f"Se desse para desligar depois do primeiro minuto, seriam uns {_n(lit, 2)} L a menos"
                                    + (f" ({_rs(rs)})." if rs else ".") + " No semáforo comum não compensa; esperando alguém, sim."})
    if (gir.get("custo") or 0) >= 0.15 or (gir.get("litros") or 0) >= 0.05 and not price:
        tips.append({"icon": "⚙️", "save_l": gir.get("litros"), "save_rs": gir.get("custo"),
                     "txt": f"{_dur(gir.get('segundos'))} acima de 3.000 rpm levaram {_n(gir.get('litros'), 2)} L"
                            + (f" ({_rs(gir['custo'])})" if gir.get("custo") else "") + ". Subindo a marcha por volta de 2.500 rpm, boa parte disso fica no tanque."})
    money["economia"] = round(sum(x["save_rs"] or 0 for x in tips), 2) if any(x.get("save_rs") for x in tips) else None

    # ---------------- direção
    G = ctx.get("gps") or {}
    eco = t.get("eco_json") or {}
    drive_score = t.get("drive_score") if t.get("drive_score") is not None else G.get("drive_score")
    brakes = G.get("freadas") if G.get("freadas") is not None else eco.get("freadas_bruscas")
    launches = G.get("arrancadas") if G.get("arrancadas") is not None else eco.get("aceleracoes_bruscas")
    driving = {"drive_score": drive_score, "eco_score": t.get("eco_score"), "freadas": brakes, "arrancadas": launches, "curvas": G.get("curvas"),
               "acima_pct": G.get("acima_pct"), "fonte": "GPS" if G.get("freadas") is not None else "OBD" if eco else None,
               "dicas": list((eco.get("dicas") or [])[:2])}

    # ---------------- comparação com viagens parecidas
    compare = None
    sim = [x for x in base.get("similar") or [] if x.get("duration_s")]
    if len(sim) >= 2 and not in_progress and not short:
        d_med = _med(x["duration_s"] for x in sim)
        k_med = _med(x.get("km_l") for x in sim)
        compare = {"n": len(sim), "by": base.get("similar_by") or "distancia", "dur_s": d_med, "km_l": None if k_med is None else round(k_med, 1),
                   "d_dur_s": int(dur_s - d_med), "d_km_l": round(kml - k_med, 1) if kml and k_med else None}
        which = "neste mesmo trajeto" if compare["by"] == "rota" else "em viagens de distância parecida"
        dd = compare["d_dur_s"]
        bits = [f"{_dur(abs(dd))} {'mais demorada' if dd > 0 else 'mais rápida'} que o seu normal ({_dur(d_med)})" if abs(dd) >= 90 else f"no tempo de sempre ({_dur(d_med)})"]
        if compare["d_km_l"] is not None:
            dk = compare["d_km_l"]
            bits.append(f"consumo {_n(abs(dk), 1)} km/L {'melhor' if dk > 0 else 'pior'} ({_n(kml, 1)} contra {_n(k_med, 1)})" if abs(dk) >= 0.3 else f"consumo igual ao de sempre ({_n(kml, 1)} km/L)")
        compare["txt"] = f"Comparando com {_plural(len(sim), 'viagem anterior', 'viagens anteriores')} {which}: {_join(bits)}."

    # ---------------- destaques
    hi = []
    add_hi = lambda icon, label, value, sub="": hi.append({"icon": icon, "l": label, "v": value, "s": sub})
    if km is not None:
        add_hi("🛣️", "Distância", f"{_n(km, 1)} km", _dur(dur_s))
    if moving_s or stopped_s:
        add_hi("⏱️", "Andando / parado", f"{_dur(moving_s)} / {_dur(stopped_s)}", "parado com o motor ligado")
    if cost:
        add_hi("💸", "Custo", _rs(cost), f"{_n(liters, 2)} L" if liters else "")
    elif liters:
        add_hi("⛽", "Combustível", f"{_n(liters, 2)} L", "sem preço registrado")
    if kml:
        add_hi("📊", "Consumo", f"{_n(kml, 1)} km/L", f"seu normal: {_n(usual, 1)}" if usual else "")
    if max_speed is not None and max_speed > 0:
        add_hi("🚀", "Máxima", f"{_n(max_speed)} km/h")
    if max_rpm is not None:
        add_hi("🌀", "Giro máximo", f"{_n(max_rpm)} rpm")
    if max_ect is not None:
        add_hi("🌡️", "Temp. máxima", f"{_n(max_ect)} °C")
    if drive_score is not None:
        add_hi("🏅", "Nota de condução", f"{drive_score}/100", "pelo GPS")
    elif t.get("eco_score") is not None:
        add_hi("🍃", "Direção econômica", f"{t['eco_score']}/100")
    if brakes is not None:
        add_hi("🛑", "Freadas fortes", str(brakes), f"arrancadas fortes: {launches}" if launches is not None else "")
    if long_stops:
        a, d = max(long_stops, key=lambda x: x[1])
        add_hi("🅿️", "Maior parada", _dur(d), f"às {hm(a)}")
    if ac_s:
        add_hi("❄️", "Ar-condicionado", _dur(ac_s), ac_by)

    # ---------------- linha do tempo
    tl = [{"ts": t0, "icon": "🔑", "txt": "Partida" + (f" com o motor a {_n(start_ect)} °C" if start_ect is not None else "")}]
    if warm_s and start_ect is not None and start_ect < 70:
        tl.append({"ts": int(t0 + warm_s * 1000), "icon": "🌡️", "txt": f"Motor chegou a 80 °C ({_dur(warm_s)} depois da partida)"})
    if first_stop and (first_stop[1] - first_stop[0]) >= 60_000:
        tl.append({"ts": first_stop[1], "icon": "🚗", "txt": f"Saiu do lugar depois de {_dur((first_stop[1] - first_stop[0]) / 1000)} parado"})
    for a, b in ac_win:
        tl.append({"ts": a, "icon": "❄️", "txt": "Ar-condicionado ligado" + (f" (até {hm(b)})" if b < t1 else "")})
    for k in sorted(kicks, key=lambda k: -(k["load"] or 0))[:3]:
        tl.append({"ts": k["a"], "icon": "⚡", "txt": f"Carga do motor saltou para {_n(k['load'])} % com o carro parado" + (" (ar ligado)" if in_ac(k["a"], 10_000) else " (carga extra entrando)")})
    for a in (al_real + al_expl)[:8]:
        tl.append({"ts": a["ts"], "icon": "🔔" if a["kind"] != "real" else "⚠️", "txt": a["title"] + (" (provável alarme falso)" if a["kind"] == "explicado" else "")})
    if al_hab:
        tl.append({"ts": min(a["ts"] for a in al_hab), "icon": "🔔", "txt": f"{_plural(len(al_hab), 'aviso', 'avisos')} de sensor fora do habitual"})
    for a, d in sorted(long_stops, key=lambda x: -x[1])[:3]:
        if not first_stop or a != first_stop[0]:
            tl.append({"ts": a, "icon": "🅿️", "txt": f"Parado por {_dur(d)} com o motor ligado"})
    if fan:
        tl.append({"ts": fan[0], "icon": "🌬️", "txt": f"Pico de {_n(fan[1])} °C e a temperatura recuou (ventoinha)"})
    if max_speed and spd:
        p = max(spd, key=lambda x: x[3])
        if p[3] >= 20:
            tl.append({"ts": p[0], "icon": "🚀", "txt": f"Velocidade máxima: {_n(p[3])} km/h"})
    for a in notes:
        if a.get("kind") not in ("ar", "alertas", "teste") and a.get("text"):
            tl.append({"ts": a["ts"], "icon": "📝", "txt": str(a["text"])[:120]})
    tl.append({"ts": t1, "icon": "⏳" if in_progress else "🏁", "txt": "Viagem em andamento (última leitura)" if in_progress else "Motor desligado" if finalized else "Última leitura"})
    tl.sort(key=lambda x: x["ts"])
    timeline = [{"t": hm(x["ts"]), "s": int((x["ts"] - t0) / 1000), "icon": x["icon"], "txt": x["txt"]} for x in tl]

    # ---------------- manchete e humor
    level = max((s["status"] for s in systems), key=RANK.get, default="na")
    has_data = any(s["status"] != "na" for s in systems)
    if level == "na" and has_data:
        level = "ok"
    core = sum(1 for s in systems if s["key"] != "avisos" and s["status"] != "na")      # sistemas medidos de verdade
    bad_names = _join([s["name"].lower() for s in sorted(systems, key=lambda s: -RANK[s["status"]]) if s["status"] in ("warn", "bad")][:3])
    if level == "bad":
        emoji, head = "🚨", f"Viagem com alerta importante: {bad_names}"
    elif level == "warn":
        emoji, head = "🤔", f"Viagem com ponto de atenção: {bad_names}"
    elif not has_data:
        emoji, head = "🤏", "Pouco dado para julgar o carro nesta viagem"
    elif level == "note":
        emoji, head = "🙂", "Viagem tranquila" + (": os sustos tinham explicação" if al_expl or any(d["why"] for d in dips) else ", com observações")
    elif core < 2:
        emoji, head = "🙂", "Nada de errado apareceu, mas faltou leitura para julgar o carro inteiro"
    else:
        emoji, head = "😎", "Viagem redonda: carro saudável do começo ao fim"
    if in_progress:
        head = "Viagem em andamento: " + {"bad": "tem alerta importante", "warn": "tem ponto de atenção", "note": "tudo certo até aqui, com observações"}.get(level, "tudo certo até aqui" if has_data else "ainda com pouco dado")
        emoji = emoji if level in ("bad", "warn") else "⏳"
    elif short and level not in ("bad", "warn"):
        emoji, head = "🤏", "Viagem curtinha: só deu para medir o básico"
    sub = []
    if in_progress:
        sub.append("Os números ainda vão mudar; abra de novo no fim da viagem.")
    elif unclosed:
        sub.append("O Pi ainda não fechou esta viagem (ele fecha na próxima partida): números parciais, calculados das leituras.")
    if short and not in_progress:
        sub.append("Viagem de menos de 3 minutos: pouco dado, e o que não deu para medir fica sem veredito.")
    ok_n = sum(1 for s in systems if s["status"] in ("ok", "note"))
    judged = sum(1 for s in systems if s["status"] != "na")
    if judged:
        sub.append(f"{ok_n} de {judged} sistemas avaliados sem problema.")

    # ---------------- narrativa
    nar = []
    if start_ect is not None:
        temp = "frio" if start_ect < 50 else "morno" if start_ect < 70 else "já quente"
        s = f"Saiu às {hm(t0)} com o motor {temp} ({_n(start_ect)} °C)"
        if warm_s and start_ect < 70:
            s += f"; em {_dur(warm_s)} já estava na temperatura de trabalho"
        nar.append(s + ".")
    else:
        nar.append(f"Saiu às {hm(t0)}.")
    if km is not None:
        s = f"{'Rodou' if not in_progress else 'Até agora rodou'} {_n(km, 1)} km em {_dur(dur_s)}"
        if moving_s or stopped_s:
            s += f": {_dur(moving_s)} andando e {_dur(stopped_s)} parado com o motor ligado"
        if max_speed:
            s += f". Máxima de {_n(max_speed)} km/h"
        nar.append(s + ".")
    good = [s for s in systems if s["key"] in ("mistura", "temperatura", "eletrica") and s["status"] == "ok"]
    if good:
        bits = []
        if any(s["key"] == "mistura" for s in good):
            bits.append("mistura no ponto" + (f" (λ {_n(m_lam, 2)})" if m_lam is not None else ""))
        if any(s["key"] == "temperatura" for s in good) and max_ect is not None and max_ect >= 70:
            bits.append(f"temperatura máxima de {_n(max_ect)} °C")
        if any(s["key"] == "eletrica" for s in good) and v_med is not None:
            bits.append(f"alternador em {_n(v_med, 1)} V")
        if bits:
            nar.append(("O motor foi bem: " if level in ("ok", "note", "na") else "Do lado bom: ") + _join(bits) + ".")
    for s in systems:
        if s["status"] in ("warn", "bad") or (s["status"] == "note" and s["key"] in ("lenta", "avisos")):
            if s["key"] == "avisos" and s["status"] == "note":   # versão curta: o detalhe está no quadro dos sistemas
                bits = ([f"{len(al_expl)} de marcha lenta com cara de alarme falso"] if al_expl else []) \
                    + ([f"{len(al_hab)} de comparação com o habitual que o Delfos aprendeu"] if al_hab else [])
                nar.append(f"O Delfos deu {_plural(len(alerts), 'aviso', 'avisos')} nesta viagem: {_join(bits)}. Nenhum é falha confirmada.")
                continue
            nar.append(s["txt"])
    if cost or liters:
        s = f"Gastou {_n(liters, 2)} L" + (f", cerca de {_rs(cost)}" if cost else "")
        if kml:
            s += f" ({_n(kml, 1)} km/L"
            if usual and not in_progress:
                dk = kml - usual
                s += f"; o seu normal é {_n(usual, 1)}" + (", então foi melhor" if dk >= 0.5 else ", então foi pior" if dk <= -0.5 else "")
            s += ")"
        nar.append(s + ".")
        if par.get("parte") and par["parte"] >= 0.1 and par.get("litros"):
            nar.append(f"De cada 100 mL, {_n(par['parte'] * 100)} foram com o carro parado" + (f": {_rs(par['custo'])}." if par.get("custo") else "."))
        if usual and kml and kml - usual <= -0.5 and (start_ect or 99) < 50 and (km or 99) < 8:
            nar.append("Viagem curta com motor frio gasta mais mesmo: boa parte do caminho foi com o motor ainda aquecendo.")
    else:
        nar.append("Sem dados de consumo nesta viagem (o Pi precisa da vazão de combustível ou do MAF para calcular).")
    if tips:
        nar.append(tips[0]["txt"])
    if drive_score is not None:
        nar.append(f"Nota de condução pelo GPS: {drive_score} de 100" + (f", com {_plural(brakes, 'freada forte', 'freadas fortes')}" if brakes else "") + ".")
    if compare:
        nar.append(compare["txt"])

    title = t.get("title") or f"Viagem de {_date(t0, tz)} às {hm(t0)}"
    share = "\n".join([f"🚗 Delfos · {title}", f"{emoji} {head}", ""] + nar + [""]
                      + [{"ok": "✅", "note": "🟢", "warn": "⚠️", "bad": "🛑", "na": "▫️"}[s["status"]] + f" {s['name']}: {s['txt']}" for s in systems]
                      + ([""] + [f"💡 {x['txt']}" for x in tips] if tips else []))

    marks = [{"s": int((k["a"] - t0) / 1000), "k": "carga"} for k in kicks[:6]] + [{"s": int((a["ts"] - t0) / 1000), "k": "aviso"} for a in al_real[:6]]
    return {
        "v": VERSION, "trip_id": t.get("id"), "title": title, "start_ts": t0, "end_ts": t1, "when": f"{_date(t0, tz)} · {hm(t0)} → {hm(t1)}",
        "in_progress": in_progress, "unclosed": unclosed, "short": short,
        "mood": {"emoji": emoji, "level": level if has_data else "na", "headline": head, "sub": " ".join(sub)},
        "narrative": nar, "systems": systems, "highlights": hi, "timeline": timeline,
        "numbers": {"km": km, "dur_s": int(dur_s), "moving_s": int(moving_s), "stopped_s": int(stopped_s), "max_speed": max_speed, "max_rpm": max_rpm,
                    "max_ect": max_ect, "start_ect": start_ect, "warmup_s": None if warm_s is None else int(warm_s), "idle_rpm": None if idle_rpm is None else round(idle_rpm),
                    "volt": None if v_med is None else round(v_med, 2), "ac_s": int(ac_s), **mix},
        "money": money, "tips": tips, "driving": driving, "compare": compare,
        "alerts": {"real": len(al_real), "explicados": len(al_expl), "habitual": len(al_hab), "dtc": dtcs},
        "spark": {"dt": round((t1 - t0) / 1000 / SPARK_N, 1), "speed": _spark(spd, t0, t1), "rpm": _spark(rpm, t0, t1, peak=True),
                  "ect": _spark(ect, t0, t1), "stft1": _spark(stft, t0, t1, d=1), "marks": marks},
        "share": share,
    }


# ------------------------------------------------------------------ contexto (banco do Pi)
def context(con, trip: dict, now: int, tz_ms: int) -> dict:
    """Junta o que `build` precisa além do documento da viagem. Só leitura; nada de posição nem nome de lugar."""
    a, b = trip["start_ts"], trip.get("end_ts") or now
    notes = [dict(r) for r in con.execute("SELECT ts, kind, text FROM annotation WHERE ts BETWEEN ? AND ? ORDER BY ts", (a - 5_000, b + 5_000))]
    prev = [dict(r) for r in con.execute(
        """SELECT id, start_ts, duration_s, distance_km, km_l, fuel_l FROM trip WHERE finalized=1 AND COALESCE(hidden,0)=0 AND start_ts < ?
           AND duration_s >= 180 ORDER BY start_ts DESC LIMIT 60""", (a,))]
    kmls = [p["km_l"] for p in prev if p["km_l"] and (p["distance_km"] or 0) >= 2][:20]
    base: dict = {"kml_usual": round(statistics.median(kmls), 1) if len(kmls) >= 3 else None, "kml_n": len(kmls), "similar": [], "similar_by": None}
    gps = None
    route = None
    try:
        g = con.execute("SELECT drive_score, drive_json, n_stops, climb_m, coverage, route_id FROM trip_gps WHERE trip_id=?", (trip["id"],)).fetchone()
    except Exception:                                            # banco antigo, sem as tabelas do GPS
        g = None
    if g:
        import json
        try:
            dj = json.loads(g["drive_json"] or "{}")
        except ValueError:
            dj = {}
        gps = {"drive_score": g["drive_score"], "freadas": dj.get("freadas"), "arrancadas": dj.get("arrancadas"), "curvas": dj.get("curvas"),
               "acima_pct": dj.get("acima_pct"), "n_stops": g["n_stops"], "climb_m": g["climb_m"], "coverage": g["coverage"]}
        route = g["route_id"]
    if route is not None:
        ids = {r[0] for r in con.execute("SELECT trip_id FROM trip_gps WHERE route_id=? AND trip_id != ?", (route, trip["id"]))}
        base["similar"] = [p for p in prev if p["id"] in ids][:12]
        base["similar_by"] = "rota"
    km = trip.get("distance_km")
    if len(base["similar"]) < 2 and km and km >= 1:
        base["similar"] = [p for p in prev if p["distance_km"] and abs(p["distance_km"] - km) <= max(0.5, km * 0.2)][:12]
        base["similar_by"] = "distancia"
    return {"now": now, "tz_ms": tz_ms, "annotations": notes, "baseline": base, "gps": gps}


def ai_question(story: dict) -> tuple[str, str]:
    """Pergunta para a IA (opcional) a partir do diagnóstico já montado pelas regras. Devolve (pergunta, rótulo curto)."""
    if not isinstance(story, dict) or not story.get("share"):
        raise ValueError("diagnóstico da viagem vazio")
    q = ("Abaixo está o diagnóstico de UMA viagem, montado por regras fixas a partir das leituras do OBD. Comente em português simples, "
         "em até 8 linhas: (1) você concorda com o veredito? (2) algo nos números merece atenção que as regras não pegaram? "
         "(3) uma dica prática para a próxima viagem. Não invente medição que não esteja aqui; se faltar dado, diga.\n\n"
         + str(story["share"])[:6000] + "\n\nEvidências por sistema:\n"
         + "\n".join(f"- {s.get('name')}: " + "; ".join(str(e) for e in (s.get("ev") or [])[:8]) for s in (story.get("systems") or [])[:8])[:4000])
    return q, f"🩺 Comentário sobre a {str(story.get('title') or 'viagem')[:80]}"
