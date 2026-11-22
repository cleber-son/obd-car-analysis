"""Marchas aprendidas pelo GPS e embreagem patinando (docs/GPS-ARQUITETURA.md, seção 9.6).

Relação de cada marcha = km/h verdadeiros (GPS) por 1.000 rpm. Depende só da caixa, do diferencial e da circunferência
real do pneu. GearEngine (motor ao vivo) junta as razões estáveis num histograma de ln(r) em caixas de 1 %, acha as 5
relações a partir da tabela de fábrica e vigia a patinação; o modelo fica em gps_model['gears'].

As funções puras (`cluster`, `match`, `slip_ratio`) recebem números e devolvem números; são elas que os testes exercitam.
"""
from __future__ import annotations

import json
import math
import time
from collections import deque

from ..analytics import Alert
from . import precise, store

# km/h por 1000 rpm — Civic 8ª geração 1.8 manual, pneu 205/55 R16 (o mesmo Collector.GEAR_KMH_PER_KRPM)
FACTORY = {1: 8.83, 2: 14.84, 3: 22.46, 4: 29.26, 5: 38.16}
MIN_KMH, MIN_RPM = 8.0, 900          # amostra válida para aprender
STABLE_N, STABLE_TOL = 3, 0.02       # razão estável: 3 leituras de rotação seguidas variando < 2 %
BIN = 0.01                           # caixa do histograma de ln(r) (~1 %)
MIN_SAMPLES = 600                    # amostras antes de aprender
MIN_GEAR = 60                        # amostras (peso) para uma marcha contar como aprendida
WIN = math.log(1.06)                 # centro = mediana dos membros a ±6 %
SANITY = 0.10                        # mais de 10 % longe da fábrica: descartado ("relação ou pneu fora do esperado")
HALF_LIFE = 2000                     # média exponencial: pneu trocado desloca as relações devagar
DECAY = 0.5 ** (1 / HALF_LIFE)
RECLUSTER_EVERY = 50
MATCH_TOL = 0.03                     # leitura "na marcha g" (regime e patinação); a marcha exibida segue com 8 %
INTERP_MS = 1000                     # a velocidade do GPS é interpolada entre posições a até 1 s uma da outra
# patinação
SLIP_MIN_G, SLIP_KMH, SLIP_RPM = 3, 30.0, 2500
SLIP_LOAD, SLIP_PEDAL = 70.0, 0.6
SLIP_LO, SLIP_HI = 0.04, 0.22
SLIP_N, SLIP_SPAN_MS = 3, 1000       # ≥ 3 leituras (a 2 Hz: do primeiro ao último ≥ 1 s, ~1,5 s de patinação)
SLIP_GAP_MS = 1500                   # leituras seguidas da mesma patinação
SLIP_BACK_MS = 3000                  # depois, a razão precisa voltar à mesma marcha em até 3 s
AFTER_SHIFT_MS = 2000
CRIT_N, CRIT_DAYS = 3, 7             # 3 patinações em 7 dias: crítico


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def factory_table() -> dict[int, float]:
    return dict(FACTORY)


def match(r: float, table: dict, tol: float = MATCH_TOL) -> int | None:
    """Marcha cuja relação está a menos de 'tol' de r (ou None)."""
    if not r or r <= 0:
        return None
    g, ref = min(table.items(), key=lambda kv: abs(math.log(r / kv[1])))
    return g if abs(r / ref - 1) < tol else None


def slip_ratio(rpm: float, kmh: float, k: float) -> float:
    """s = rotação medida ÷ rotação esperada pela velocidade do GPS − 1."""
    return rpm / (kmh / k * 1000) - 1


def _wmedian(items: list[tuple[int, float]]) -> float | None:
    """Mediana ponderada de caixas (índice, peso), com interpolação dentro da caixa (cada caixa = faixa de 1 %)."""
    tot = sum(w for _, w in items)
    if tot <= 0:
        return None
    half, acc = tot / 2, 0.0
    for b, w in sorted(items):
        if acc + w >= half:
            return (b - 0.5 + (half - acc) / w) * BIN
        acc += w
    return sorted(items)[-1][0] * BIN


def cluster(hist: dict, factory: dict | None = None, min_gear: float = MIN_GEAR, iters: int = 20) -> dict:
    """k-médias de 5 centros sobre o histograma de ln(r) {caixa: peso}, partindo da tabela de fábrica. Na primeira volta
    o centro é a mediana de todos os membros mais próximos dele; depois, a mediana dos membros a ±6 %. Devolve {"table": {g: k}, "count": {g: peso}, "learned": [g…], "rejected": [{gear, k, factory}]}."""
    factory = factory or FACTORY
    hist = {int(b): float(w) for b, w in hist.items() if w > 0}
    cen = {g: math.log(k) for g, k in factory.items()}
    for it in range(iters):
        groups: dict[int, list] = {g: [] for g in cen}
        for b, w in hist.items():
            x = b * BIN
            g = min(cen, key=lambda gg: abs(cen[gg] - x))
            if it == 0 or abs(cen[g] - x) <= WIN:      # 1ª volta: todos os mais próximos (o centro pode estar longe da fábrica)
                groups[g].append((b, w))
        new = {g: (_wmedian(m) if m else cen[g]) for g, m in groups.items()}
        if all(abs(new[g] - cen[g]) < 1e-6 for g in cen):
            break
        cen = new
    table, count, learned, rejected = {}, {}, [], []
    for g in sorted(cen):
        w = sum(ww for b, ww in hist.items() if abs(b * BIN - cen[g]) <= WIN)
        k = math.exp(cen[g])
        count[g] = round(w, 1)
        if w >= min_gear and abs(k / factory[g] - 1) <= SANITY:
            table[g] = round(k, 3)
            learned.append(g)
        else:
            table[g] = factory[g]
            if w >= min_gear:
                rejected.append({"gear": g, "k": round(k, 3), "factory": factory[g]})
    return {"table": table, "count": count, "learned": learned, "rejected": rejected}


def _load(m) -> dict:
    """gps_model['gears'] tolerante: qualquer formato estranho vira modelo vazio."""
    try:
        if isinstance(m, dict) and isinstance(m.get("hist"), dict):
            return {"hist": {int(b): float(w) for b, w in m["hist"].items()}, "n": int(m.get("n") or 0),
                    "rejected": list(m.get("rejected") or [])}
    except (TypeError, ValueError):
        pass
    return {"hist": {}, "n": 0, "rejected": []}


class GearEngine:
    """Motor ao vivo: aprende as relações e vigia a patinação. Trabalha ~1 s atrás (a velocidade do GPS é interpolada
    para o instante de cada leitura de rotação, value_ts['rpm'])."""

    def __init__(self, hub):
        self.con = hub.con
        try:
            m = _load(store.model_get(self.con, "gears"))
        except Exception:
            m = _load(None)
        self.hist, self.n, self.rejected_log = m["hist"], m["n"], m["rejected"]
        self.result = cluster(self.hist) if self.n >= MIN_SAMPLES else None
        self.since = 0
        self.ring: deque = deque(maxlen=60)       # (ts, km/h, precisa, lat, lon)
        self.pending: deque = deque()
        self.last_rpm_ts = None
        self.win: deque = deque(maxlen=STABLE_N)
        self.gear: int | None = None              # marcha do último regime estável
        self.gear_ts: int | None = None
        self.shift_ts: int | None = None
        self.cand: dict | None = None
        self.ped = [None, None]                   # menor e maior pedal visto (normalização)
        self.trip_id = None
        self.dirty = False

    # ---------------------------------------------------------- consultas
    def table(self) -> dict | None:
        """Relações aprendidas (as que faltam, da fábrica) ou None se nenhuma foi aprendida ainda."""
        r = self.result
        return dict(r["table"]) if r and r["learned"] else None

    def _tab(self) -> dict:
        return self.table() or FACTORY

    def model(self) -> dict:
        if not self.dirty:
            return {}
        self.dirty = False
        r = cluster(self.hist)
        if self.n >= MIN_SAMPLES:
            self.result = r                       # o que fica gravado é o que está em uso
        out = {"gears": {"hist": {str(b): round(w, 3) for b, w in self.hist.items()}, "n": self.n,
                         "table": r["table"], "count": r["count"], "learned": r["learned"] if self.n >= MIN_SAMPLES else [],
                         "rejected": self.rejected_log[-10:], "updated_ts": self.ring[-1][0] if self.ring else None}}
        if self.n >= MIN_SAMPLES and 5 in r["learned"] and self.ring:      # resumo mensal de k_5ª (pneus, 9.11)
            ts = self.ring[-1][0]
            month = time.strftime("%Y-%m", time.gmtime(ts / 1000))
            try:
                hist = store.model_get(self.con, "speedo_hist") or {}
            except Exception:
                hist = {}
            if not isinstance(hist, dict):
                hist = {}
            hist[month] = r["table"][5]
            out["speedo_hist"] = dict(sorted(hist.items())[-36:])
        return out

    def reset(self) -> None:
        self.hist, self.n, self.result, self.since, self.rejected_log = {}, 0, None, 0, []
        self.win.clear()
        self.gear = self.cand = None
        self.dirty = True

    def command(self, c: dict) -> bool:
        if c.get("cmd") == "gears_reset":
            self.reset()
            return True
        return False

    def live(self) -> dict:
        ts = self.ring[-1][0] if self.ring else None
        g = self.gear if self.gear_ts is not None and ts is not None and ts - self.gear_ts <= 3000 else None
        return {"gear": g, "slip": bool(self.cand and self.cand["n"] >= SLIP_N)}

    # ---------------------------------------------------------- ciclo de vida
    def on_trip_start(self, tid, ts, merged) -> None:
        self.trip_id = tid

    def shift(self, off: int) -> None:
        self.ring = deque(((t + off, *x) for t, *x in self.ring), maxlen=self.ring.maxlen)
        self.pending = deque((t + off, *x) for t, *x in self.pending)
        for k in ("last_rpm_ts", "gear_ts", "shift_ts"):
            if getattr(self, k) is not None:
                setattr(self, k, getattr(self, k) + off)
        if self.cand:
            for k in ("start", "last", "end"):
                if self.cand.get(k) is not None:
                    self.cand[k] += off

    # ---------------------------------------------------------- alimentação
    def _speed_at(self, t: int):
        """Velocidade do GPS interpolada no instante t (posições precisas a até 1 s uma da outra) e a posição mais próxima."""
        a = b = None
        for x in self.ring:
            if x[0] <= t:
                a = x
            elif b is None:
                b = x
                break
        if a is None or b is None or b[0] - a[0] > INTERP_MS or a[1] is None or b[1] is None or not (a[2] and b[2]):
            if a is not None and a[0] == t and a[1] is not None and a[2]:
                return a[1], a
            return None, None
        f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
        return a[1] + (b[1] - a[1]) * f, (a if f < 0.5 else b)

    def _pedal(self, pd) -> float | None:
        if pd is None:
            return None
        lo, hi = self.ped
        self.ped = [pd if lo is None else min(lo, pd), pd if hi is None else max(hi, pd)]
        span = max(self.ped[1] - self.ped[0], 50.0)       # sem ter visto o pedal no fundo, assume ~50 pontos de curso
        return max(0.0, min(1.0, (pd - self.ped[0]) / span))

    def feed(self, ctx) -> list:
        f = ctx.fix
        self.ring.append((f.ts, f.speed, precise(f), f.lat, f.lon))
        vals, vts = ctx.values or {}, ctx.value_ts or {}
        t = vts.get("rpm")
        if t is not None and t != self.last_rpm_ts and vals.get("rpm") is not None:
            self.last_rpm_ts = t
            self.pending.append((t, float(vals["rpm"]), vals.get("load"), vals.get("pedal_d")))
        out = []
        while self.pending and self.pending[0][0] <= f.ts:
            out += self._reading(ctx, *self.pending.popleft())
        while self.pending and f.ts - self.pending[0][0] > 5000:        # sem posição depois (buraco): descarta
            self.pending.popleft()
        return out

    def learn(self, r: float) -> None:
        b = round(math.log(r) / BIN)
        for k in list(self.hist):
            w = self.hist[k] * DECAY
            if w < 1e-3:
                del self.hist[k]
            else:
                self.hist[k] = w
        self.hist[b] = self.hist.get(b, 0.0) + 1.0
        self.n += 1
        self.since += 1
        self.dirty = True
        if self.n >= MIN_SAMPLES and (self.result is None or self.since >= RECLUSTER_EVERY):
            self.since = 0
            prev = {x["gear"] for x in (self.result or {}).get("rejected", [])}
            self.result = cluster(self.hist)
            for x in self.result["rejected"]:
                if x["gear"] not in prev:
                    self.rejected_log.append(dict(x, ts=self.ring[-1][0] if self.ring else None,
                                                  motivo="relação ou pneu fora do esperado"))

    def _reading(self, ctx, t: int, rpm: float, load, pd) -> list:
        v, near = self._speed_at(t)
        pn = self._pedal(pd)
        if v is None:
            self.win.clear()
            self.cand = None
            return []
        r = v / (rpm / 1000) if rpm > 0 and v > 0 else None
        if r is None or v < MIN_KMH or rpm < MIN_RPM:
            self.win.clear()
        else:
            self.win.append(r)
        stable = len(self.win) == STABLE_N and (max(self.win) - min(self.win)) / (sum(self.win) / STABLE_N) < STABLE_TOL
        if stable:
            self.learn(r)
        tab = self._tab()
        gm = match(r, tab) if r else None
        if stable and gm:
            if gm != self.gear:
                self.shift_ts = t
                self.gear = gm
            self.gear_ts = t
        return self._slip(ctx, t, v, rpm, load, pn, r, gm, near, tab)

    def _slip(self, ctx, t, v, rpm, load, pn, r, gm, near, tab) -> list:
        c = self.cand
        g = c["gear"] if c else self.gear
        if g is None:
            return []
        s = slip_ratio(rpm, v, tab[g]) if v > 0 else None
        in_range = s is not None and SLIP_LO <= s <= SLIP_HI and v >= SLIP_KMH and rpm >= SLIP_RPM
        if c:
            if in_range and t - c["last"] <= SLIP_GAP_MS:
                c["n"] += 1
                c["last"] = t
                if s > c["smax"]:
                    c["smax"], c["pos"], c["v"], c["rpm"] = s, near, v, rpm
                return []
            if gm == c["gear"] and t - c["last"] <= SLIP_BACK_MS:       # aliviou e a razão voltou para a mesma marcha
                self.cand = None
                if c["n"] >= SLIP_N and c["last"] - c["start"] >= SLIP_SPAN_MS:
                    return self._event(ctx, c)
                return []
            if t - c["last"] > SLIP_BACK_MS or s is None or s > SLIP_HI or s < -SLIP_LO or (gm and gm != c["gear"]) or rpm < 1100:
                self.cand = None                   # trocou de marcha, pisou na embreagem ou caiu para a lenta: não é patinação
            return []
        if (in_range and g >= SLIP_MIN_G and self.shift_ts is not None and t - self.shift_ts >= AFTER_SHIFT_MS
                and ((load or 0) >= SLIP_LOAD or (pn or 0) >= SLIP_PEDAL)):
            self.cand = {"gear": g, "start": t, "last": t, "n": 1, "smax": s, "pos": near, "v": v, "rpm": rpm}
        return []

    def _event(self, ctx, c) -> list:
        ts = c["start"]
        pos = c.get("pos")
        dur = (c["last"] - c["start"]) / 1000 + 0.5
        tid = ctx.trip_id if ctx.trip_id is not None else self.trip_id
        store.add_event(self.con, ts, "patinacao", trip_id=tid, lat=round(pos[3], 6) if pos else None, lon=round(pos[4], 6) if pos else None,
                        value=round(c["smax"], 3),
                        detail=json.dumps({"gear": c["gear"], "dur_s": round(dur, 1), "kmh": round(c["v"]), "rpm": round(c["rpm"]),
                                           "n": c["n"]}, ensure_ascii=False))
        n7 = self.con.execute("SELECT COUNT(*) FROM geo_event WHERE kind='patinacao' AND ts >= ?", (ts - CRIT_DAYS * 86400_000,)).fetchone()[0]
        sev = "crit" if n7 >= CRIT_N else "warn"
        pct = round(c["smax"] * 100)
        txt = (f"Em {c['gear']}ª, com o pé no acelerador, a rotação ficou {pct} % acima do que a velocidade explica por "
               f"{_br(dur)} s e depois voltou ao normal na mesma marcha. É o sinal típico de embreagem gasta (ou mal regulada).")
        if n7 >= CRIT_N:
            txt += f" Já são {n7} vezes em {CRIT_DAYS} dias."
        return [Alert("gps", "gps:clutch_slip", sev, "Embreagem patinando", txt, value=pct, ts=ts)]


def report(con, now: int, days: int = 90) -> dict:
    """Para /api/gps/gears (aberta na nuvem: sem posição nem nomes). Relações de fábrica × aprendidas e as patinações."""
    m = store.model_get(con, "gears")
    md = _load(m)
    n = md["n"]
    res = cluster(md["hist"]) if md["hist"] else {"table": dict(FACTORY), "count": {g: 0 for g in FACTORY}, "learned": [], "rejected": []}
    learned = res["learned"] if n >= MIN_SAMPLES else []
    rows = []
    for g in sorted(FACTORY):
        k = res["table"][g] if g in learned else None
        rows.append({"gear": g, "factory": FACTORY[g], "learned": k, "count": round(res["count"].get(g, 0)),
                     "diff_pct": round((k / FACTORY[g] - 1) * 100, 1) if k else None})
    slips = []
    for e in con.execute("SELECT ts, trip_id, value, detail FROM geo_event WHERE kind='patinacao' AND ts >= ? ORDER BY ts DESC LIMIT 200",
                         (now - days * 86400_000,)):
        try:
            d = json.loads(e["detail"] or "{}")
        except ValueError:
            d = {}
        slips.append({"ts": e["ts"], "trip_id": e["trip_id"], "pct": round((e["value"] or 0) * 100, 1), "gear": d.get("gear"),
                      "dur_s": d.get("dur_s"), "kmh": d.get("kmh"), "rpm": d.get("rpm")})
    n7 = sum(1 for s in slips if s["ts"] >= now - CRIT_DAYS * 86400_000)
    if n < MIN_SAMPLES:
        status, txt = "aprendendo", (f"Aprendendo as marchas: {n} de {MIN_SAMPLES} leituras em regime. Até lá a marcha exibida usa a tabela de fábrica.")
    elif len(learned) == 5:
        status, txt = "aprendido", "As 5 relações foram aprendidas com o GPS e são as usadas na marcha exibida."
    else:
        falta = ", ".join(f"{g}ª" for g in FACTORY if g not in learned)
        status, txt = "parcial", f"Relações aprendidas para {len(learned)} marcha(s); falta rodar mais em {falta} (essas usam a tabela de fábrica)."
    return {"status": status, "texto": txt, "n": n, "min_samples": MIN_SAMPLES, "min_gear": MIN_GEAR, "gears": rows,
            "rejected": (m or {}).get("rejected", []) if isinstance(m, dict) else [], "updated_ts": (m or {}).get("updated_ts") if isinstance(m, dict) else None,
            "slips": slips, "slips_7d": n7, "days": days}
