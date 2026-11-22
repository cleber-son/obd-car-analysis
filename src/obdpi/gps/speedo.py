"""Aferição do velocímetro (da central) e dos pneus pelo GPS (docs/GPS-ARQUITETURA.md, seção 9.11).

Pares (velocidade do OBD, velocidade do GPS) em regime vão para faixas de 10 km/h do OBD; com 5 faixas de 60 pares ou
mais, uma reta v_gps = a·v_obd + b pelas medianas (peso igual por faixa: a cidade não domina a estrada). k = a é o fator
gravado em gps_model['speedo']['k'] (trip.speedo_k() e GpsHub.true_speed() leem dali); até lá k = 1 ("ainda aprendendo").
"""
from __future__ import annotations

import statistics
from collections import deque

from . import precise, store

MIN_KMH = 20.0               # GPS ≥ 20 km/h
MAX_ACC = 0.3                # |aceleração| < 0,3 m/s² (regime)
BIN_KMH = 10
MIN_BINS, MIN_PAIRS = 5, 60
KEEP_PAIRS = 240             # por faixa, os mais recentes (~2 min a 2 Hz: segue pneu trocado; o modelo fica pequeno)
REFIT_EVERY = 30
K_RANGE = (0.85, 1.15)       # o mesmo trip.SPEEDO_K_RANGE
ACC_WIN_MS = 1000            # aceleração pela velocidade do GPS 1 s antes e 1 s depois
INTERP_MS = 1000
SAVE_MS = 300_000            # o modelo (até ~60 kB com as faixas cheias) é regravado no máximo a cada 5 min
TYRE_DROP = 0.01             # k_5ª caiu 1 % ou mais desde o primeiro mês: pneus mais gastos ou murchos


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def bin_point(ps: list) -> tuple[float, float, int]:
    """Ponto da faixa: média do OBD e média do GPS dos pares, sem os pares fora da curva (razão GPS ÷ OBD a mais de
    3 desvios da mediana da faixa). Média, e não mediana de cada eixo: com velocidades diferentes na mesma faixa, as
    duas medianas podem cair em pares diferentes e entortar a reta."""
    rs = [g / o for o, g in ps if o > 0]
    m = statistics.median(rs)
    mad = statistics.median(abs(r - m) for r in rs) * 1.4826
    tol = max(3 * mad, 0.01 * m)
    keep = [(o, g) for o, g in ps if o > 0 and abs(g / o - m) <= tol] or list(ps)
    return sum(o for o, _ in keep) / len(keep), sum(g for _, g in keep) / len(keep), len(keep)


def fit(bins: dict, min_bins: int = MIN_BINS, min_pairs: int = MIN_PAIRS) -> dict:
    """bins: {faixa: [(obd, gps), …]}. Mínimos quadrados sobre o ponto de cada faixa cheia (peso igual por faixa).
    {"ok", "k", "b", "bins": [{lo, n, obd, gps}], "n_bins"}; sem faixas suficientes ok=False e k=1."""
    rows = []
    for lo in sorted(int(b) for b in bins):
        ps = bins.get(lo, bins.get(str(lo))) or []
        if not ps:
            continue
        x, y, _ = bin_point(ps)
        rows.append({"lo": lo * BIN_KMH, "n": len(ps), "obd": round(x, 2), "gps": round(y, 2)})
    full = [r for r in rows if r["n"] >= min_pairs]
    out = {"ok": False, "k": 1.0, "b": 0.0, "bins": rows, "n_bins": len(full)}
    if len(full) < min_bins:
        return out
    xs, ys = [r["obd"] for r in full], [r["gps"] for r in full]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return out
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = my - a * mx
    if not (K_RANGE[0] <= a <= K_RANGE[1]):
        out["fora"] = round(a, 4)
        return out
    out.update(ok=True, k=round(a, 4), b=round(b, 2))
    return out


def text(m: dict) -> str:
    """"A central informa 100 km/h quando o carro está a 96,2 (−3,8 %)"."""
    if not m or not m.get("ok"):
        return "Ainda aprendendo: são precisos trechos em velocidade constante em pelo menos 5 faixas (20, 30, 40… km/h)."
    true100 = 100 * m["k"] + m.get("b", 0)
    pct = true100 - 100
    if abs(pct) < 0.5:
        return f"A central informa 100 km/h quando o carro está a {_br(true100)}: a velocidade da central está certa."
    sign = "−" if pct < 0 else "+"
    return f"A central informa 100 km/h quando o carro está a {_br(true100)} ({sign}{_br(abs(pct))} %)."


def tyres(hist: dict | None) -> dict | None:
    """Resumo mensal de k_5ª (gps_model['speedo_hist'] = {"AAAA-MM": k}). Queda ≥ 1 % desde o primeiro mês: pneus gastos
    ou com menos pressão; alta ≥ 1 %: pneus novos (ou maiores)."""
    if not isinstance(hist, dict) or len(hist) < 2:
        return None
    months = sorted(hist)
    k0, k1 = float(hist[months[0]]), float(hist[months[-1]])
    d = k1 / k0 - 1
    out = {"first": months[0], "last": months[-1], "k_first": k0, "k_last": k1, "pct": round(d * 100, 1), "texto": None}
    if d <= -TYRE_DROP:
        out["texto"] = f"Pneus mais gastos ou com menos pressão: {_br(d * 100)} % de circunferência desde {months[0]}."
    elif d >= TYRE_DROP:
        out["texto"] = f"A circunferência dos pneus aumentou {_br(d * 100)} % desde {months[0]} (pneus novos ou de outra medida?)."
    return out


def _load(m) -> dict:
    try:
        if isinstance(m, dict) and isinstance(m.get("pairs"), dict):
            return {int(b): [(float(o), float(g)) for o, g in ps][-KEEP_PAIRS:] for b, ps in m["pairs"].items()}
    except (TypeError, ValueError):
        pass
    return {}


class SpeedoEngine:
    """Motor ao vivo: pares OBD × GPS em regime; trabalha ~1 s atrás (a aceleração usa a velocidade 1 s depois)."""

    def __init__(self, hub):
        self.con = hub.con
        try:
            m = store.model_get(self.con, "speedo")
        except Exception:
            m = None
        self.pairs = _load(m)
        self.res = fit(self.pairs)
        self.n_total = int((m or {}).get("n_total") or 0) if isinstance(m, dict) else 0
        self.ring: deque = deque(maxlen=60)
        self.pending: deque = deque()
        self.last_ts = None
        self.since = 0
        self.dirty = False
        self.saved_ts = None

    @property
    def k(self) -> float:
        k = self.res.get("k") or 1.0
        return k if self.res.get("ok") and K_RANGE[0] <= k <= K_RANGE[1] else 1.0

    def model(self) -> dict:
        ts = self.ring[-1][0] if self.ring else None
        if not self.dirty or (self.saved_ts is not None and ts is not None and 0 <= ts - self.saved_ts < SAVE_MS):
            return {}
        self.dirty, self.saved_ts = False, ts
        self.res = fit(self.pairs)
        return {"speedo": {**self.res, "k": self.k, "n_total": self.n_total,
                           "pairs": {str(b): [[o, g] for o, g in ps] for b, ps in sorted(self.pairs.items())},
                           "updated_ts": self.ring[-1][0] if self.ring else None}}

    def live(self) -> dict:
        if not self.res.get("ok"):
            return {}
        return {"speedo_err": round((1 / self.k - 1) * 100, 1)}       # % que a central marca acima da velocidade verdadeira

    def on_trip_start(self, tid, ts, merged) -> None:
        pass

    def shift(self, off: int) -> None:
        self.ring = deque(((t + off, *x) for t, *x in self.ring), maxlen=self.ring.maxlen)
        self.pending = deque((t + off, v) for t, v in self.pending)
        for k in ("last_ts", "saved_ts"):
            if getattr(self, k) is not None:
                setattr(self, k, getattr(self, k) + off)

    def _speed_at(self, t: int) -> float | None:
        a = b = None
        for x in self.ring:
            if x[0] <= t:
                a = x
            elif b is None:
                b = x
                break
        if a is None or b is None or b[0] - a[0] > INTERP_MS or a[1] is None or b[1] is None or not (a[2] and b[2]):
            return None
        f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
        return a[1] + (b[1] - a[1]) * f

    def add(self, obd: float, gps: float) -> None:
        b = int(obd // BIN_KMH)
        ps = self.pairs.setdefault(b, [])
        ps.append((int(obd), round(gps, 2)))
        if len(ps) > KEEP_PAIRS:
            del ps[: len(ps) - KEEP_PAIRS]
        self.n_total += 1
        self.since += 1
        self.dirty = True
        if self.since >= REFIT_EVERY:
            self.since = 0
            self.res = fit(self.pairs)

    def feed(self, ctx) -> list:
        f = ctx.fix
        self.ring.append((f.ts, f.speed, precise(f)))
        vals, vts = ctx.values or {}, ctx.value_ts or {}
        t = vts.get("speed")
        if t is not None and t != self.last_ts and vals.get("speed") is not None:
            self.last_ts = t
            self.pending.append((t, float(vals["speed"])))
        while self.pending and self.pending[0][0] + ACC_WIN_MS <= f.ts:
            t, obd = self.pending.popleft()
            v, v0, v1 = self._speed_at(t), self._speed_at(t - ACC_WIN_MS), self._speed_at(t + ACC_WIN_MS)
            if v is None or v0 is None or v1 is None or v < MIN_KMH or obd <= 0:
                continue
            if abs((v1 - v0) / 3.6 / (2 * ACC_WIN_MS / 1000)) >= MAX_ACC:
                continue
            self.add(obd, v)
        return []
