"""Regras do corpo de borboleta (DBW) sobre as amostras rápidas (~1 Hz).

R1 rastreio comando × posição   R2 pedal D × E divergentes   R3 borboleta travada / emergência
R4 marcha lenta oscilando       R5 marcha lenta irregular   R6 estol   R9 salto de sinal
(R10 = DTCs da lista de vigilância, tratado no coletor.)
Limiares iniciais; recalibrar após ~2 semanas de dados reais. Borboleta trocada → linha de base nova.
"""
from __future__ import annotations

import statistics
from collections import deque

from . import Alert


class ThrottleEngine:
    LEARN_MIN = 300  # amostras em regime antes de ligar R1/R2

    def __init__(self, state: dict | None = None):
        s = state or {}
        self.pedal_min = s.get("pedal_min", 100.0)
        self.pedal_max = s.get("pedal_max", 0.0)
        self.off_n = s.get("off_n", 0)
        self.off_mean = s.get("off_mean", 0.0)
        # regressões lineares aprendidas entre sensores redundantes: n, sx, sy, sxx, sxy
        self.regs = s.get("regs") or {"R2": s.get("reg", [0, 0.0, 0.0, 0.0, 0.0]), "R2t": [0, 0.0, 0.0, 0.0, 0.0]}
        self.r_streak = {"R2": 0, "R2t": 0}
        self.win: deque[dict] = deque(maxlen=40)
        self.r1_streak = 0
        self.r2_streak = 0
        self.idle_rpm: deque[float] = deque(maxlen=30)
        self.idle_std_hist: deque[float] = deque(maxlen=600)
        self.events: list[Alert] = []
        self.cooldown: dict[str, int] = {}

    # ---------- estado persistente ----------
    def state(self) -> dict:
        return {"pedal_min": self.pedal_min, "pedal_max": self.pedal_max, "off_n": self.off_n,
                "off_mean": self.off_mean, "regs": self.regs}

    def pedal_norm(self, pedal: float | None) -> float | None:
        if pedal is None:
            return None
        span = self.pedal_max - self.pedal_min
        if span < 20:
            return max(0.0, pedal - self.pedal_min) * 100 / 80 if self.pedal_min < 100 else None
        return max(0.0, min(100.0, (pedal - self.pedal_min) * 100 / span))

    def _emit(self, rule, sev, title, detail, ts, sig="tps", val=None, cool_s=60) -> list[Alert]:
        last = self.cooldown.get(rule)
        if last is not None and ts - last < cool_s * 1000:
            return []
        self.cooldown[rule] = ts
        return [Alert("throttle", rule, sev, title, detail, sig, val, ts)]

    # ---------- avaliação ----------
    def feed(self, s: dict, ts: int, ect: float | None) -> list[Alert]:
        out: list[Alert] = []
        rpm, tps, cmd = s.get("rpm"), s.get("tps"), s.get("tps_cmd")
        pd, pe, spd = s.get("pedal_d"), s.get("pedal_e"), s.get("speed")
        if pd is not None:
            self.pedal_min = min(self.pedal_min, pd)
            self.pedal_max = max(self.pedal_max, pd)
        pn = self.pedal_norm(pd)
        s = dict(s, pedal_norm=pn, ts=ts)
        prev = self.win[-1] if self.win else None
        self.win.append(s)
        running = rpm is not None and rpm > 400

        # R1 rastreio comando × posição (em regime: comando estável)
        if running and tps is not None and cmd is not None and prev and prev.get("tps_cmd") is not None:
            steady = abs(cmd - prev["tps_cmd"]) < 3
            diff = cmd - tps
            if steady:
                if self.off_n < self.LEARN_MIN:
                    self.off_n += 1
                    self.off_mean += (diff - self.off_mean) / self.off_n
                else:
                    err = abs(diff - self.off_mean)
                    if err > 5:
                        self.r1_streak += 1
                        if self.r1_streak >= 3:
                            sev = "crit" if err > 10 and self.r1_streak >= 2 else "warn"
                            out += self._emit("R1", sev, "Borboleta não segue o comando",
                                              f"Comando {cmd:.1f}% × posição {tps:.1f}% (erro {err:.1f} pp)", ts, "tps", err)
                    else:
                        self.r1_streak = 0
                        self.off_mean += (diff - self.off_mean) / 500

        # R2 pedal D × E e R2t borboleta A × B (sensores redundantes; precursores de P2138 / P2135)
        for rule, xk, yk, title, code in (("R2", "pedal_d", "pedal_e", "Sensores do pedal divergentes", "P2138"),
                                          ("R2t", "tps", "tps_b", "Sensores da borboleta A/B divergentes", "P2135")):
            x, y = s.get(xk), s.get(yk)
            if x is None or y is None:
                continue
            n, sx, sy, sxx, sxy = self.regs[rule]
            if n >= self.LEARN_MIN and (sxx - sx * sx / n) > 1e-6 and (sxx / n - (sx / n) ** 2) > 4:
                a = (sxy - sx * sy / n) / (sxx - sx * sx / n)
                b = (sy - a * sx) / n
                res = abs(y - (a * x + b))
                if res > 4:
                    self.r_streak[rule] += 1
                    if self.r_streak[rule] >= 2:
                        out += self._emit(rule, "crit" if res > 8 else "warn", title,
                                          f"{yk} {y:.1f}% esperado {a * x + b:.1f}% (desvio {res:.1f} pp) — precursor de {code}", ts, yk, res)
                else:
                    self.r_streak[rule] = 0
            if n < 20000:
                self.regs[rule] = [n + 1, sx + x, sy + y, sxx + x * x, sxy + x * y]

        # R3 borboleta travada / modo de emergência
        w = [x for x in list(self.win)[-10:] if x.get("tps") is not None and x.get("pedal_norm") is not None]
        if running and len(w) >= 8:
            pr = max(x["pedal_norm"] for x in w) - min(x["pedal_norm"] for x in w)
            ts_std = statistics.pstdev([x["tps"] for x in w])
            if pr > 20 and ts_std < 0.5:
                out += self._emit("R3", "crit", "Borboleta parada (possível modo de emergência)",
                                  f"Pedal variou {pr:.0f} pp e a borboleta ficou fixa em ~{w[-1]['tps']:.1f}%", ts, "tps", w[-1]["tps"], 120)
        self.r3b = getattr(self, "r3b", 0) + 1 if (running and pn is not None and pn > 30 and s.get("tps_rel") is not None and s["tps_rel"] < 5) else 0
        if self.r3b >= 3:
            out += self._emit("R3b", "crit", "Pedal pressionado sem abrir a borboleta",
                              f"Pedal {pn:.0f}% e borboleta relativa {s['tps_rel']:.1f}%", ts, "tps_rel", s["tps_rel"], 120)

        # R4/R5 marcha lenta quente
        idle = running and spd == 0 and (pn is None or pn < 2) and (ect or 0) >= 70 and rpm < 1500
        if idle:
            self.idle_rpm.append(rpm)
            if len(self.idle_rpm) == 30:
                r = list(self.idle_rpm)
                mean = statistics.fmean(r)
                p2p = max(r) - min(r)
                std = statistics.pstdev(r)
                cross = sum(1 for i in range(1, len(r)) if (r[i - 1] - mean) * (r[i] - mean) < 0)
                self.idle_std_hist.append(std)
                if p2p > 150 and cross >= 6:
                    out += self._emit("R4", "crit" if p2p > 300 else "warn", "Marcha lenta oscilando",
                                      f"Variação de {p2p:.0f} rpm (média {mean:.0f}) com {cross // 2} ciclos em 30 s", ts, "rpm", p2p, 180)
                elif std > 40:
                    out += self._emit("R5", "crit" if std > 75 else "warn", "Marcha lenta irregular",
                                      f"Desvio de {std:.0f} rpm em 30 s (média {mean:.0f})", ts, "rpm", std, 180)
                if mean > 1100 or mean < 550:
                    out += self._emit("R5b", "warn", "Marcha lenta fora do alvo",
                                      f"Média {mean:.0f} rpm com motor quente", ts, "rpm", mean, 300)
        else:
            self.idle_rpm.clear()

        # R6 estol: a queda de rpm só vira alerta se a ECU continuar respondendo (desligar a chave corta a ECU)
        pend = getattr(self, "r6_pending", None)
        if pend:
            if rpm is not None and rpm > 400:                       # religou logo depois: foi estol mesmo
                out += self._emit("R6", pend["sev"], pend["title"], pend["detail"], pend["ts"], "rpm", 0, 30)
                self.r6_pending = None
            elif ts - pend["ts"] >= 8000:                          # ECU seguiu respondendo com o motor parado
                out += self._emit("R6", pend["sev"], pend["title"], pend["detail"], pend["ts"], "rpm", 0, 30)
                self.r6_pending = None
        elif prev and rpm is not None and rpm < 100:
            hist = [x.get("rpm") for x in list(self.win)[-5:-1] if x.get("rpm") is not None]
            if hist and max(hist) > 400:
                was_idle = all((x.get("speed") or 0) == 0 for x in list(self.win)[-5:-1])
                pn_prev = prev.get("pedal_norm")
                if was_idle and (pn_prev is None or pn_prev < 2):
                    self.r6_pending = {"ts": ts, "sev": "crit", "title": "Motor morreu em marcha lenta",
                                       "detail": f"Rotação caiu de {max(hist):.0f} para {rpm:.0f} rpm sem acelerar"}
                else:
                    self.r6_pending = {"ts": ts, "sev": "info", "title": "Motor morreu (arrancada)",
                                       "detail": f"Rotação caiu de {max(hist):.0f} para {rpm:.0f} rpm"}

        # R9 salto de sinal (vai e volta em 1 amostra, sem mudança de comando/pedal)
        if len(self.win) >= 3 and running:
            a, b, c = list(self.win)[-3:]
            for k, ref in (("tps", "tps_cmd"), ("pedal_d", None)):
                if all(x.get(k) is not None for x in (a, b, c)):
                    jump = b[k] - a[k]
                    back = c[k] - b[k]
                    ref_ok = ref is None or (all(x.get(ref) is not None for x in (a, b)) and abs(b[ref] - a[ref]) < 3)
                    if abs(jump) > 15 and abs(back + jump) < 4 and ref_ok:
                        out += self._emit(f"R9:{k}", "warn", "Salto no sinal do sensor",
                                          f"{k} saltou {jump:+.1f} pp e voltou (mau contato / sensor)", ts, k, jump, 30)
        return out

    def idle_std_recent(self) -> float | None:
        return statistics.fmean(self.idle_std_hist) if self.idle_std_hist else None


def trip_score(alerts: list[Alert], idle_std: float | None) -> int:
    score = 100.0
    per_rule: dict[str, int] = {}
    for a in alerts:
        if a.source not in ("throttle", "dtc_throttle"):
            continue
        r = a.rule.split(":")[0]
        per_rule[r] = per_rule.get(r, 0) + 1
        if per_rule[r] > 2:
            continue
        score -= {"crit": 25, "warn": 8, "info": 2}[a.severity]
    if idle_std is not None:
        score -= min(15, max(0, idle_std - 20) / 4)
    return int(max(0, round(score)))
