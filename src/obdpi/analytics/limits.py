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
    # Honda usa carga controlada (ELD): com pouca carga elétrica o alternador pode ficar em ~12,5 V — normal
    ("batt", "running", 12.3, 15.0, 11.8, 15.5, 3, "Tensão de carga fora do normal"),
    ("ecu_volt", "running", 12.3, 15.0, 11.8, 15.5, 3, "Tensão no módulo fora do normal"),
    ("batt", "off", 12.2, None, 11.8, None, 3, "Bateria baixa"),
    ("stft1", "warm", -15, 15, -25, 25, 3, "Ajuste de combustível no limite (mistura pobre/rica)"),
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

STUCK_O2 = "o2s1_v"
SEVN = {"ok": 0, "info": 0, "warn": 1, "crit": 2}   # sonda deve oscilar em malha fechada


CTX_LABEL = {"running": " (motor ligado)", "warm": " (motor quente)", "idle_warm": " (marcha lenta, motor quente)",
             "off": " (motor desligado)", "any": "", "cruise": " (rodando)"}


def fmt_range(lo, hi) -> str:
    f = lambda x: f"{x:g}".replace(".", ",")
    if lo is not None and hi is not None:
        return f"{f(lo)} a {f(hi)}"
    if hi is not None:
        return f"até {f(hi)}"
    return f"acima de {f(lo)}"


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

    def __init__(self, baseline_rows: list | None = None, period_s: float = 20):
        # os "N leituras seguidas" foram pensados para 20 s; escala para o período real de coleta
        self.k = max(1, round(20 / period_s))
        self.MIN_BASELINE = 90 * self.k
        self.recent: dict[str, list] = defaultdict(list)
        self.frozen: set[str] = set()
        self.seen: set[str] = set()
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
        # valor que nunca muda com o motor ligado (ex.: PID 06 deste ECU fixo em 0xBC) não é medida real
        if engine_on:
            # (STFT parado no limite da ECU, ex. +46,9 % em marcha lenta, é real: mistura pobre além da correção — não ignorar)
            for sig in ("timing", "o2c1_lambda", "maf", "map"):
                if v.get(sig) is not None:
                    h = self.recent[sig] = (self.recent[sig] + [v[sig]])[-12 * self.k:]
                    if len(h) >= 12 * self.k:
                        (self.frozen.add if max(h) == min(h) else self.frozen.discard)(sig)
        for (sig, c, wl, wh, cl, ch, need, title) in RULES:
            key = (sig, c)
            x = v.get(sig)
            need = need * self.k
            if c not in ctx or x is None or sig in self.frozen:
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
        # sonda de oxigênio: convencional (V) deve oscilar; de faixa larga (λ, caso do Civic R18) não pode saturar
        closed_loop = int(v.get("fuel_status", 0)) & 0x02
        if "warm" in ctx and closed_loop:
            if STUCK_O2 in v:
                self.o2_hist = (self.o2_hist + [v[STUCK_O2]])[-6 * self.k:]
                if len(self.o2_hist) == 6 * self.k and max(self.o2_hist) - min(self.o2_hist) < 0.1:
                    alerts.append(Alert("limits", "o2_stuck", "warn", "Sonda lambda sem oscilar",
                                        f"B1S1 parada em ~{self.o2_hist[-1]:.2f} V por 2 min", STUCK_O2, self.o2_hist[-1], ts))
            lam = v.get("o2c1_lambda")
            if lam is not None:
                self.o2_hist = (self.o2_hist + [lam])[-3 * self.k:]
                if len(self.o2_hist) == 3 * self.k and (min(self.o2_hist) > 1.25 or max(self.o2_hist) < 0.8):
                    alerts.append(Alert("limits", "afr_extreme", "warn", "Mistura fora do lambda 1 em malha fechada",
                                        f"Sonda de faixa larga em λ {lam:.2f} por 1 min ({'pobre' if lam > 1 else 'rica'})", "o2c1_lambda", lam, ts))
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
                        if self.z_streak[bk] >= 3 * self.k:
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

    def status(self, v: dict, engine_on: bool) -> dict:
        """Estado instantâneo de cada sinal para o painel: {chave: [sev, motivo, faixa_normal]}."""
        ctx = context_of(v, engine_on)
        out: dict = {}
        for (sig, c, wl, wh, cl, ch, _need, title) in RULES:
            x = v.get(sig)
            if x is None or c not in ctx or sig in self.frozen:
                continue
            rng = fmt_range(wl, wh) + CTX_LABEL.get(c, "")
            sev = "crit" if ((cl is not None and x < cl) or (ch is not None and x > ch)) else \
                  "warn" if ((wl is not None and x < wl) or (wh is not None and x > wh)) else "ok"
            prev = out.get(sig)
            if not prev or SEVN[sev] > SEVN[prev[0]]:
                out[sig] = [sev, title if sev != "ok" else "", rng, f"{sig}:{c}"]
        for c in ("idle_warm", "cruise"):
            if c not in ctx:
                continue
            for sig, x in v.items():
                w = self.base.get(f"{sig}@{c}")
                if x is None or not w or w.n < self.MIN_BASELINE or w.std <= 0:
                    continue
                z = (x - w.mean) / max(w.std, abs(w.mean) * 0.02, 0.5)
                hab = f"habitual {w.mean - 2 * w.std:.1f} a {w.mean + 2 * w.std:.1f}".replace(".", ",")
                if sig not in out:
                    out[sig] = ["ok", "", hab, f"z:{sig}@{c}"]
                if abs(z) > self.Z_WARN and out[sig][0] == "ok":
                    out[sig] = ["warn", "Fora do comportamento habitual", hab, f"z:{sig}@{c}"]
        return out

    def check_missing(self, expected: set[str], got: dict, ts: int, engine_on: bool) -> list[Alert]:
        out = []
        if not engine_on:
            return out
        for s in expected:
            if s.endswith("_trim") or s.endswith("_ma"):
                continue                      # valores opcionais (0xFF = não usado) — não é sensor sumindo
            if s in got:
                self.missing[s] = 0
                self.seen.add(s)
            elif s not in self.seen:
                continue                      # nunca respondeu nesta execução: não é "parou de responder"
            else:
                self.missing[s] += 1
                if self.missing[s] == 10 * self.k:
                    out.append(Alert("limits", f"missing:{s}", "warn", "Sensor parou de responder",
                                     f"{s} sem resposta em 10 leituras seguidas", s, None, ts))
        return out

    def baseline_rows(self):
        return [(k, w.n, w.mean, w.m2) for k, w in self.base.items()]
