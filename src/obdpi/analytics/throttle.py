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


class BinModel:
    """Relação aprendida y = f(x) entre dois sinais redundantes, por faixas de 5 pp de x.

    Uma reta ajustada com quase todas as amostras em marcha lenta extrapola mal (era a causa dos falsos
    "sensores divergentes" e "não segue o comando" em aberturas grandes). Aqui cada faixa tem a sua média e a
    comparação só acontece onde já houve aprendizado suficiente, interpolando entre faixas vizinhas.
    """
    W = 5.0
    MIN_N = 12

    def __init__(self, bins: dict | None = None):
        self.bins: dict[int, list[float]] = {int(k): list(v) for k, v in (bins or {}).items()}   # faixa -> [n, média x, média y]

    def learn(self, x: float, y: float) -> None:
        b = self.bins.setdefault(int(x // self.W), [0, 0.0, 0.0])
        b[0] = min(b[0] + 1, 400)                      # teto: continua acompanhando desgaste lento
        b[1] += (x - b[1]) / b[0]
        b[2] += (y - b[2]) / b[0]

    def expected(self, x: float) -> float | None:
        """y esperado para x, ou None se essa região ainda não foi aprendida."""
        pts = sorted((b[1], b[2]) for b in self.bins.values() if b[0] >= self.MIN_N)
        if len(pts) < 2:
            return None
        lo = [p for p in pts if p[0] <= x]
        hi = [p for p in pts if p[0] >= x]
        if not lo or not hi:                           # fora da faixa aprendida: aceita até meia faixa de extrapolação
            edge = pts[0] if not lo else pts[-1]
            if abs(x - edge[0]) > self.W / 2:
                return None
            a, b = (pts[0], pts[1]) if not lo else (pts[-2], pts[-1])
        else:
            a, b = lo[-1], hi[0]
            if b[0] - a[0] > 4 * self.W:               # buraco grande entre faixas aprendidas: não inventa
                return None
        if b[0] == a[0]:
            return a[1]
        return a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])

    def ready(self) -> bool:
        return sum(1 for b in self.bins.values() if b[0] >= self.MIN_N) >= 2

    def state(self) -> dict:
        return {str(k): [v[0], round(v[1], 3), round(v[2], 3)] for k, v in self.bins.items()}


class ThrottleEngine:
    PAIRS = {  # regra: (x, y, título, código, tolerância base em pp, tolerância relativa)
        "R1": ("tps_cmd", "tps", "Borboleta não segue o comando", None, 6.0, 0.06),
        "R2": ("pedal_d", "pedal_e", "Sensores do pedal divergentes", "P2138", 4.0, 0.05),
        "R2t": ("tps", "tps_b", "Sensores da borboleta A/B divergentes", "P2135", 4.0, 0.05),
    }

    def __init__(self, state: dict | None = None):
        s = state or {}
        self.pedal_min = s.get("pedal_min", 100.0)
        self.pedal_max = s.get("pedal_max", 0.0)
        self.models = {r: BinModel((s.get("models") or {}).get(r)) for r in self.PAIRS}
        self.r_streak = {r: 0 for r in self.PAIRS}
        self.idle_ok = 0
        self.win: deque[dict] = deque(maxlen=40)
        self.idle_rpm: deque[float] = deque(maxlen=30)
        self.idle_std_hist: deque[float] = deque(maxlen=600)
        self.events: list[Alert] = []
        self.cooldown: dict[str, int] = {}

    # ---------- estado persistente ----------
    def state(self) -> dict:
        return {"pedal_min": self.pedal_min, "pedal_max": self.pedal_max,
                "models": {r: m.state() for r, m in self.models.items()}}

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

        # R1 comando × posição, R2 pedal D × E, R2t borboleta A × B: cada par comparado com a relação aprendida
        # por faixas. Só em regime (os dois sinais parados em relação à leitura anterior): os PIDs de um mesmo ciclo
        # são lidos com ~0,1–0,3 s de diferença, então em transição eles nunca batem.
        for rule, (xk, yk, title, code, tol, rel) in self.PAIRS.items():
            x, y = s.get(xk), s.get(yk)
            if x is None or y is None or not prev or prev.get(xk) is None or prev.get(yk) is None:
                continue
            if rule == "R1" and not running:
                continue
            if abs(x - prev[xk]) > 2 or abs(y - prev[yk]) > 2:
                self.r_streak[rule] = 0
                continue
            m = self.models[rule]
            exp = m.expected(x)
            if exp is None:
                m.learn(x, y)
                continue
            res = abs(y - exp)
            lim = tol + rel * x
            if res > lim:
                self.r_streak[rule] += 1
                if self.r_streak[rule] >= 4:           # ~3 s seguidos fora da relação, em regime
                    sev = "crit" if res > 2 * lim else "warn"
                    what = f"Comando {x:.1f}% → posição {y:.1f}% (esperado {exp:.1f}%, desvio {res:.1f} pp)" if rule == "R1" else \
                        f"{yk} {y:.1f}% esperado {exp:.1f}% (desvio {res:.1f} pp) — precursor de {code}"
                    out += self._emit(rule, sev, title, what, ts, yk, res, 120)
            else:
                self.r_streak[rule] = 0
                m.learn(x, y)

        # R3 borboleta travada / modo de emergência: pedal pisado de verdade (os DOIS sensores do pedal subiram) por
        # ~3 s seguidos e nem o comando nem a posição saíram do lugar. Um toque rápido no pedal ou um pico de leitura
        # em um sensor só não conta.
        w = [x for x in list(self.win)[-6:] if x.get("tps") is not None and x.get("pedal_norm") is not None]
        if running and len(w) == 6:
            base, held = w[0], w[2:]
            pe0 = base.get("pedal_e")
            pressed = all(x["pedal_norm"] - base["pedal_norm"] > 20 for x in held) and base["pedal_norm"] < 10
            both = pe0 is None or all(x.get("pedal_e") is not None and x["pedal_e"] - pe0 > 4 for x in held)
            stuck = all(abs(x["tps"] - base["tps"]) < 1.5 for x in held)
            if pressed and both and stuck:
                cmd_moved = cmd is not None and base.get("tps_cmd") is not None and cmd - base["tps_cmd"] > 5
                out += self._emit("R3", "crit", "Borboleta parada (possível modo de emergência)",
                                  f"Pedal pisado {held[-1]['pedal_norm']:.0f}% por ~3 s e a borboleta ficou em ~{w[-1]['tps']:.1f}% "
                                  + ("(a ECU comandou abrir e ela não abriu)" if cmd_moved else "(a ECU não comandou abertura)"),
                                  ts, "tps", w[-1]["tps"], 120)
        self.r3b = getattr(self, "r3b", 0) + 1 if (running and pn is not None and pn > 30 and s.get("tps_rel") is not None and s["tps_rel"] < 5) else 0
        if self.r3b >= 3:
            out += self._emit("R3b", "crit", "Pedal pressionado sem abrir a borboleta",
                              f"Pedal {pn:.0f}% e borboleta relativa {s['tps_rel']:.1f}%", ts, "tps_rel", s["tps_rel"], 120)

        # R4/R5 marcha lenta quente
        # só conta depois de ~5 s com o pé fora do pedal e a rotação já de volta (a queda depois de uma acelerada
        # não é marcha lenta irregular)
        calm = running and spd == 0 and (pn is None or pn < 2) and (ect or 0) >= 70 and rpm < 1200
        self.idle_ok = self.idle_ok + 1 if calm else 0
        idle = self.idle_ok > 6
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
            else:
                # ECU seguiu respondendo com o motor parado. Só conta resposta de verdade (rpm 0 lido), nunca leitura
                # vazia; e pede 6 leituras em >= 12 s: ao desligar a chave a central ainda responde uns segundos.
                if rpm is not None and rpm < 100:
                    pend["zeros"] = pend.get("zeros", 0) + 1
                if pend.get("zeros", 0) >= 6 and ts - pend["ts"] >= 12000:
                    out += self._emit("R6", pend["sev"], pend["title"], pend["detail"], pend["ts"], "rpm", 0, 30)
                    self.r6_pending = None
                elif ts - pend["ts"] >= 60000:                     # meio minuto sem confirmar: foi a chave, esquece
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

        # R9 salto de sinal: UM sensor vai e volta em 1 leitura enquanto o seu par redundante fica parado.
        # Se o par acompanha (pedal D e E juntos, borboleta A e B juntas), foi movimento de verdade: toque no pedal.
        if len(self.win) >= 3 and running:
            a, b, c = list(self.win)[-3:]
            for k, twin, ref in (("tps", "tps_b", "tps_cmd"), ("pedal_d", "pedal_e", None), ("pedal_e", "pedal_d", None), ("tps_b", "tps", "tps_cmd")):
                if not all(x.get(k) is not None and x.get(twin) is not None for x in (a, b, c)):
                    continue                           # sem o sensor par não dá para distinguir de um toque rápido
                jump, back = b[k] - a[k], c[k] - b[k]
                twin_still = abs(b[twin] - a[twin]) < 3 and abs(c[twin] - b[twin]) < 3
                ref_ok = ref is None or (all(x.get(ref) is not None for x in (a, b)) and abs(b[ref] - a[ref]) < 3)
                if abs(jump) > 15 and abs(back + jump) < 4 and twin_still and ref_ok:
                    out += self._emit(f"R9:{k}", "warn", "Salto no sinal do sensor",
                                      f"{k} saltou {jump:+.1f} pp e voltou, com {twin} parado (mau contato / sensor)", ts, k, jump, 30)
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
