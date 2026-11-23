"""Detecta sozinho o ar-condicionado ligado (o OBD do Civic não informa).

Assinatura medida no carro (23/11, lenta quente): o compressor acopla e solta sozinho a cada ~20 s
(~17 s acoplado com a carga do motor em ~58 %, ~5 s solto em ~33 %). Cada acoplamento é um salto de
carga de 20–40 pontos em 1–2 s com o carro parado e sem pé no acelerador. Ventoinha, farol e vidro
elétrico mexem só 2–5 pontos.

Só olha com o carro parado em marcha lenta quente; rodando, mantém o último estado.
- liga: 2 saltos em até 2 min (compressor ciclando) ou carga parada ≥ nível sem ar + 15 por 60 s
  (compressor acoplado direto, cabine quente);
- desliga: 90 s parado em lenta sem nenhum salto e com a carga perto do nível sem ar.
O nível sem ar começa em 34 % e vai sendo aprendido nas folgas do compressor.
"""
from __future__ import annotations

from collections import deque

KICK = 15.0          # pontos de carga que um acoplamento sobe
WIN_MS = 4_000       # janela para achar o salto
PAIR_MS = 120_000    # 2 saltos dentro disso = compressor ciclando
SUSTAIN_MS = 60_000  # carga alta direto por isso = compressor acoplado direto
OFF_MS = 90_000      # parado em lenta sem salto por isso = ar desligado
HOLD_MS = 180_000    # depois de o dono tocar no ❄️, o automático espera isso para discordar


class AcDetector:
    def __init__(self, base_off: float = 34.0):
        self.on = False
        self.base = base_off                 # carga de lenta quente sem compressor (aprendida)
        self.buf: deque[tuple[int, float]] = deque()
        self.kicks: deque[int] = deque()
        self.last_kick = 0
        self.idle_since: int | None = None
        self.high_since: int | None = None
        self.low_since: int | None = None
        self.hold_until = 0

    def reset(self) -> None:
        """Motor desligado: o compressor também."""
        self.on = False
        self.buf.clear(); self.kicks.clear()
        self.idle_since = self.high_since = self.low_since = None
        self.hold_until = 0

    def manual(self, on: bool, ts: int) -> None:
        """O dono tocou no ❄️: vale o que ele marcou, e o automático segura um pouco antes de discordar."""
        self.on = bool(on)
        self.hold_until = ts + HOLD_MS
        self.kicks.clear()
        self.high_since = self.low_since = None
        self.last_kick = ts if on else 0

    @staticmethod
    def idle(v: dict) -> bool:
        rpm, spd, load = v.get("rpm"), v.get("speed"), v.get("load")
        if rpm is None or load is None or spd is None:
            return False
        if spd > 1 or not 550 <= rpm <= 1100:
            return False
        if (v.get("ect") or 0) < 70:                       # frio: a lenta e a carga ficam altas sozinhas
            return False
        tr = v.get("tps_rel")
        return tr is None or tr <= 6                       # sem pé no acelerador

    def feed(self, ts: int, v: dict) -> bool | None:
        """Uma leitura (~1/s). Devolve True/False quando o estado muda, senão None."""
        if not self.idle(v):
            self.idle_since = self.high_since = self.low_since = None
            self.buf.clear()
            return None
        load = float(v["load"])
        if self.idle_since is None:
            self.idle_since = ts
        self.buf.append((ts, load))
        while self.buf and ts - self.buf[0][0] > WIN_MS:
            self.buf.popleft()
        lo = min(x for _, x in self.buf)
        kick = load - lo >= KICK and ts - self.last_kick > WIN_MS
        if kick:
            self.last_kick = ts
            self.kicks.append(ts)
            if lo < self.base + 10:                        # folga do compressor: ensina o nível sem ar
                self.base += (lo - self.base) * 0.2
        while self.kicks and ts - self.kicks[0] > PAIR_MS:
            self.kicks.popleft()
        # carga alta / baixa sustentada (comparada com o nível sem ar)
        self.high_since = (self.high_since or ts) if load >= self.base + KICK else None
        self.low_since = (self.low_since or ts) if load <= self.base + 8 else None
        if ts < self.hold_until:
            return None
        if not self.on:
            if len(self.kicks) >= 2 or (self.high_since is not None and ts - self.high_since >= SUSTAIN_MS):
                self.on = True
                return True
        else:
            quiet = ts - max(self.last_kick, self.idle_since) >= OFF_MS
            if quiet and self.low_since is not None and ts - self.low_since >= OFF_MS - WIN_MS:
                self.on = False
                return False
        return None
