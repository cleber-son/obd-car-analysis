"""Alertas para QUALQUER sensor fora do padrão.

1. Faixas absolutas por sinal e contexto (valem desde o 1º dia).
2. Linha de base aprendida (média/desvio por contexto) → "fora do habitual" (|z| > 4).
3. Sensor que sumiu (era suportado e parou de responder).
"""
from __future__ import annotations

import math
from collections import defaultdict

from . import Alert

# (sinal, contexto, aviso_baixo, aviso_alto, crit_baixo, crit_alto, leituras_seguidas, título)
# contexto: any | running | warm | idle_warm | off   (None = sem limite)
RULES = [
    ("ect", "running", None, 102, None, 108, 2, "Temperatura do motor alta"),
    ("batt", "running", 13.0, 15.0, 12.4, 15.5, 3, "Tensão de carga fora do normal"),
    ("ecu_volt", "running", 13.0, 15.0, 12.4, 15.5, 3, "Tensão no módulo fora do normal"),
    ("batt", "off", 12.2, None, 11.8, None, 3, "Bateria baixa"),
    ("stft1", "warm", -15, 15, -25, 25, 3, "Ajuste curto de combustível extremo"),
    ("ltft1", "warm", -15, 15, -25, 25, 2, "Ajuste longo de combustível extremo"),
    ("iat", "running", None, 60, None, 70, 3, "Ar admitido muito quente"),
    ("rpm", "idle_warm", 600, 950, 500, 1200, 3, "Marcha lenta fora do alvo"),
    ("rpm", "running", None, 6900, None, None, 1, "Rotação acima do corte"),
    ("map", "idle_warm", None, 45, None, 60, 3, "Vácuo baixo em marcha lenta (MAP alto)"),
    ("timing", "idle_warm", -5, None, None, None, 3, "Avanço de ignição negativo em marcha lenta"),
    ("load", "idle_warm", None, 45, None, 60, 3, "Carga alta em marcha lenta"),
    ("cat_b1s1", "running", None, 850, None, 950, 2, "Catalisador muito quente"),
    ("oil_temp", "running", None, 125, None, 135, 2, "Óleo muito quente"),
    ("fuel_level", "any", 10, None, 5, None, 3, "Combustível na reserva"),
]

STUCK_O2 = "o2s1_v"   # sonda deve oscilar em malha fechada


def total_trim(v: dict) -> float | None:
    if "stft1" in v and "ltft1" in v:
        return v["stft1"] + v["ltft1"]
    return None


def context_of(v: dict, engine_on: bool) -> set[str]:
    ctx = {"any"}
    if not engine_on:
        ctx.add("off")
        return ctx
    ctx.add("running")
    ect = v.get("ect")
    run_time = v.get("run_time", 999)
    if ect is not None and ect >= 75 and run_time > 120:
        ctx.add("warm")
        if v.get("speed", 1) == 0 and v.get("rpm", 9999) < 1300 and v.get("pedal_d_norm", 0) < 3:
            ctx.add("idle_warm")
        elif v.get("speed", 0) > 40:
            ctx.add("cruise")
    return ctx


class Welford:
    __slots__ = ("n", "mean", "m2")

    def __init__(self, n=0, mean=0.0, m2=0.0):
        self.n, self.mean, self.m2 = n, mean, m2

    def add(self, x: float):
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


class LimitsEngine:
    MIN_BASELINE = 90          # amostras (~30 min de motor) antes de usar a linha de base
    Z_WARN = 4.5

    def __init__(self, baseline_rows: list | None = None):
        self.streak: dict[tuple, int] = defaultdict(int)
        self.z_streak: dict[str, int] = defaultdict(int)
        self.base: dict[str, Welford] = {}
        for r in baseline_rows or []:
            self.base[r["key"]] = Welford(r["n"], r["mean"], r["m2"])
        self.o2_hist: list[float] = []
        self.missing: dict[str, int] = defaultdict(int)

    def evaluate(self, v: dict, engine_on: bool, ts: int, learn: bool = True) -> list[Alert]:
        alerts: list[Alert] = []
        ctx = context_of(v, engine_on)
        for (sig, c, wl, wh, cl, ch, need, title) in RULES:
            key = (sig, c)
            x = v.get(sig)
            if c not in ctx or x is None:
                self.streak[key] = 0
                continue
            sev = None
            if (cl is not None and x < cl) or (ch is not None and x > ch):
                sev = "crit"
            elif (wl is not None and x < wl) or (wh is not None and x > wh):
                sev = "warn"
            if sev:
                self.streak[key] += 1
                if self.streak[key] >= need:
                    lim = [f"{a}" for a in (wl, wh) if a is not None]
                    alerts.append(Alert("limits", f"{sig}:{c}", sev, title,
                                        f"{x:g} fora da faixa normal ({' a '.join(lim) if len(lim) == 2 else ('mín ' + lim[0] if wl is not None else 'máx ' + lim[0])})",
                                        sig, x, ts))
            else:
                self.streak[key] = 0
        # soma dos ajustes de combustível
        tt = total_trim(v)
        if tt is not None and "warm" in ctx and abs(tt) > 25:
            alerts.append(Alert("limits", "fuel_trim_total", "warn", "Mistura muito " + ("pobre" if tt > 0 else "rica"),
                                f"STFT+LTFT = {tt:+.1f}% (entrada falsa de ar, bico, sonda ou combustível)", "ltft1", tt, ts))
        # sonda O2 parada em malha fechada
        if "warm" in ctx and STUCK_O2 in v and v.get("fuel_status", 2) & 0x02:
            self.o2_hist = (self.o2_hist + [v[STUCK_O2]])[-6:]
            if len(self.o2_hist) == 6 and max(self.o2_hist) - min(self.o2_hist) < 0.1:
                alerts.append(Alert("limits", "o2_stuck", "warn", "Sonda lambda sem oscilar",
                                    f"B1S1 parada em ~{self.o2_hist[-1]:.2f} V por 2 min", STUCK_O2, self.o2_hist[-1], ts))
        # linha de base aprendida
        for c in ("idle_warm", "cruise"):
            if c not in ctx:
                continue
            for sig, x in v.items():
                if sig in ("run_time", "dist_clr", "time_clr", "warmups", "dist_mil", "time_mil", "fuel_level", "speed") \
                        or sig.startswith("raw_") or x is None:
                    continue
                bk = f"{sig}@{c}"
                w = self.base.setdefault(bk, Welford())
                if w.n >= self.MIN_BASELINE and w.std > 0:
                    z = (x - w.mean) / max(w.std, abs(w.mean) * 0.02, 0.5)
                    if abs(z) > self.Z_WARN:
                        self.z_streak[bk] += 1
                        if self.z_streak[bk] >= 3:
                            alerts.append(Alert("baseline", f"z:{bk}", "warn", "Sensor fora do comportamento habitual",
                                                f"{sig} = {x:g} (habitual {w.mean:.1f} ± {w.std:.1f} em {'marcha lenta' if c == 'idle_warm' else 'cruzeiro'})",
                                                sig, x, ts))
                    else:
                        self.z_streak[bk] = 0
                        if learn:
                            w.add(x)
                elif learn:
                    w.add(x)
        return alerts

    def check_missing(self, expected: set[str], got: dict, ts: int, engine_on: bool) -> list[Alert]:
        out = []
        if not engine_on:
            return out
        for s in expected:
            if s in got:
                self.missing[s] = 0
            else:
                self.missing[s] += 1
                if self.missing[s] == 5:
                    out.append(Alert("limits", f"missing:{s}", "warn", "Sensor parou de responder",
                                     f"{s} sem resposta em 5 leituras seguidas", s, None, ts))
        return out

    def baseline_rows(self):
        return [(k, w.n, w.mean, w.m2) for k, w in self.base.items()]
