"""Cronômetro de 0–100 e retomadas pelo GPS (docs/GPS-ARQUITETURA.md, seção 9.8).

PerfDetector (motor ao vivo, sobre as posições a 5 Hz com a hora do receptor) acha sozinho as arrancadas e as retomadas
e também mede o teste armado pelo botão "⏱ 0–100" do modo direção: com GPS bom, o coletor só espelha o estado daqui
(`mirror`), e a tela não muda. Grava em accel_run (source = 'gps'), liga a rajada de 5 Hz ao suspeitar de uma corrida
ou de uma puxada de dinamômetro e publica live.gps.perf para a voz.

As funções puras (`crossing`, `launch_t0`, `measure_launch`, `measure_pair`) recebem listas e devolvem números; são elas
que os testes exercitam.
"""
from __future__ import annotations

import json
import statistics
from collections import deque

from . import precise
from . import routes as routesmod
from . import terrain

STOP_KMH, STOP_MS = 2.0, 1000        # parado: < 2 km/h por ≥ 1 s
GO_KMH = 5.0                         # …e depois ≥ 5 km/h: arrancada
T0_KMH = 1.0                         # largada = instante em que passou de 1 km/h
EARLY_KMH = 25.0                     # começo da arrancada (1ª, embreagem saindo): quase uma reta
CROSS_W_MS = (800, 1000)             # meia janela da reta em volta de cada marca: a 3 Hz ou mais / abaixo disso
SPLITS = (60, 80, 100)
DROP_KMH = 3.0                       # caiu mais que isso abaixo da máxima: acabou
MAX_LAUNCH_MS = 40_000
CREEP_MS = 5000                      # andando entre 2 e 5 km/h por mais que isso: não é arrancada
PAIRS = ((40, 80), (60, 100), (80, 120))
ROLL_PEDAL, ROLL_OFF = 0.8, 0.75     # retomada: pedal ≥ 0,8 mantido (solta abaixo de 0,75)
ROLL_MIN_KMH, ROLL_MAX_MS = 30.0, 30_000
AUTO_PEDAL, AUTO_T60, AUTO_T100 = 0.8, 12.0, 25.0     # corrida não armada só é gravada se foi de propósito…
AUTO_PEDAL_MS = 1000                 # …com o pedal ≥ 0,8 por 1 s ou mais (um pico na saída da embreagem não conta)…
AUTO_PEDAL_SHARE = 0.5               # …e em pelo menos metade da corrida (saída de semáforo com uma pisada em 3ª não conta)
SLOPE_MAX = 2.0                      # |inclinação| acima disso: vale, mas sai marcada e não disputa recorde
FRESH_MS = 1500                      # leitura do OBD vale por 1,5 s
GPS_FRESH_MS = 2000                  # o teste armado só fica com o GPS se a última posição precisa tem até 2 s
AFTER_MS = 900                       # espera depois de cruzar a marca (pontos dos dois lados para a conta)
QUICK_KMH, QUICK_MS = 20.0, 2000     # 20 km/h em até 2 s depois dos 5 km/h: suspeita de arrancada (liga a rajada)
BURST_LAUNCH_S, BURST_ROLL_S, BURST_PULL_S = 45, 35, 20
PULL_PEDAL, PULL_RPM = 0.85, 2000    # puxada de dinamômetro: liga a rajada (seção 9.9)
ARM_WAIT_MS, ARM_READY_MS = 180_000, 300_000
SHOW_MS = 60_000                     # live.gps.perf mostra a última corrida por 1 min
CURVE_MAX = 300
PEDAL_SPAN = 60.0


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


class PedalNorm:
    """Pedal normalizado 0–1 ao vivo: menor pedal visto = solto. Sem ter visto o pedal no fundo, assume um curso de 60
    pontos (o pedal D do Civic vai de ~15 a ~80 %); com 50, como nas marchas, uma saída normal de semáforo já parecia
    "pé no fundo" e virava arrancada de propósito."""

    def __init__(self, min_span: float = PEDAL_SPAN):
        self.lo = self.hi = None
        self.min_span = min_span

    def __call__(self, pd: float | None) -> float | None:
        if pd is None:
            return None
        self.lo = pd if self.lo is None else min(self.lo, pd)
        self.hi = pd if self.hi is None else max(self.hi, pd)
        return max(0.0, min(1.0, (pd - self.lo) / max(self.hi - self.lo, self.min_span)))


# ---------------------------------------------------------------- contas (puras)
def _fit(pts: list) -> tuple[float, float] | None:
    n = len(pts)
    if n < 2:
        return None
    mt = sum(p[0] for p in pts) / n
    mv = sum(p[1] for p in pts) / n
    stt = sum((p[0] - mt) ** 2 for p in pts)
    if stt <= 0:
        return None
    b = sum((p[0] - mt) * (p[1] - mv) for p in pts) / stt
    return mv - b * mt, b


def _hz(pts: list) -> float | None:
    dts = [b[0] - a[0] for a, b in zip(pts, pts[1:]) if b[0] > a[0]]
    return round(1000 / statistics.median(dts), 1) if dts else None


def crossing(pts: list, target: float, after: float | None = None) -> float | None:
    """Instante (ms) em que a velocidade passou de 'target': reta pelos pontos a ±0,8 s (5 Hz) ou ±1 s (1 Hz) da
    primeira leitura acima da marca, o que tira boa parte do ruído do Doppler; sem reta boa, interpolação linear."""
    i = next((j for j, p in enumerate(pts) if p[1] >= target and j > 0 and (after is None or p[0] >= after)), None)
    if i is None:
        return None
    (t1, v1), (t2, v2) = pts[i - 1][:2], pts[i][:2]
    hz = _hz(pts[max(0, i - 5):i + 5]) or 1.0
    w = CROSS_W_MS[0] if hz >= 3 else CROSS_W_MS[1]
    near = [(p[0] - t2, p[1]) for p in pts if abs(p[0] - t2) <= w + 1]
    lin = t1 + (t2 - t1) * (target - v1) / (v2 - v1) if v2 > v1 else float(t2)
    fit = _fit(near) if len(near) >= 3 else None
    if fit and fit[1] > 1e-4:
        tc = t2 + (target - fit[0]) / fit[1]
        if t1 - w <= tc <= t2 + w:
            return tc
    return lin


def launch_t0(pts: list) -> float | None:
    """Largada: reta pelo começo da aceleração (2 a 25 km/h) estendida até 1 km/h. Abaixo de ~2 km/h o Doppler é só
    ruído; o começo da arrancada é quase uma reta (1ª marcha, embreagem saindo). Sem pontos bastantes, interpolação."""
    go = next((j for j, p in enumerate(pts) if p[1] >= GO_KMH), None)
    if go is None:
        return None
    lo = go
    while lo > 0 and pts[lo - 1][1] >= T0_KMH:
        lo -= 1
    early = [p for p in pts[lo:] if 2.0 <= p[1] <= EARLY_KMH]
    early = early[:next((k for k in range(1, len(early)) if early[k][0] - early[k - 1][0] > 1500), len(early))]
    if len(early) < 2:                                 # 1 Hz: as duas primeiras leituras andando (até 50 km/h)
        early = [p for p in pts[lo:] if p[1] >= 2.0][:2]
        if len(early) < 2 or early[1][1] > 50 or early[1][0] - early[0][0] > 2500:
            early = []
    fit = _fit(early) if len(early) >= 2 else None
    if fit and fit[1] > 1e-4:
        t0 = (T0_KMH - fit[0]) / fit[1]
        last_stop = next((p[0] for p in reversed(pts[:go]) if p[1] < T0_KMH), pts[0][0])
        return max(min(t0, pts[go][0]), last_stop - 1000)
    return crossing(pts, T0_KMH)


def measure_launch(pts: list) -> dict:
    """pts: [(ts, km/h)] da parada até o fim. Largada, parciais (s desde a largada), distância até 100 e taxa."""
    t0 = launch_t0(pts)
    out = {"t0": t0, "splits": {}, "hz": _hz(pts), "vmax": max((p[1] for p in pts), default=0.0), "dist_m": None}
    if t0 is None:
        return out
    for k in SPLITS:
        tc = crossing(pts, k, after=t0)
        if tc is None:
            break
        out["splits"][str(k)] = round((tc - t0) / 1000, 2)
        out[f"tc{k}"] = tc
    t_end = out.get("tc100") or pts[-1][0]
    d = 0.0
    for a, b in zip(pts, pts[1:]):
        if b[0] <= t0 or a[0] >= t_end:
            continue
        ta, tb = max(a[0], t0), min(b[0], t_end)
        d += (a[1] + b[1]) / 2 / 3.6 * (tb - ta) / 1000
    out["dist_m"] = round(d, 1)
    return out


def measure_pair(pts: list, a: float, b: float) -> dict | None:
    """Retomada de 'a' a 'b' km/h: tempo entre as duas passagens (mesma conta das parciais)."""
    ta = crossing(pts, a)
    tb = crossing(pts, b, after=ta) if ta is not None else None
    if ta is None or tb is None:
        return None
    return {"ta": ta, "tb": tb, "t_s": round((tb - ta) / 1000, 2), "hz": _hz(pts)}


def _kind_label(kind: str, gear: int | None = None) -> str:
    a, b = kind.split("-")
    return f"{a}–{b}" + (f" em {gear}ª" if gear else "")


# ---------------------------------------------------------------- motor ao vivo
class PerfDetector:
    """Arrancadas (parado → 100 km/h) e retomadas (pedal no fundo na mesma marcha, 40→80, 60→100, 80→120)."""

    def __init__(self, hub):
        self.hub, self.con = hub, hub.con
        self.ring: deque = deque(maxlen=500)          # (ts, km/h, precisa, alt, climb) ~100 s a 5 Hz
        self.stop_since: int | None = None
        self.ready = False
        self.move_ts: int | None = None                # primeira leitura ≥ 2 km/h depois da parada
        self.run: dict | None = None                   # arrancada em curso
        self.roll: dict | None = None                  # retomada em curso
        self.arm: dict | None = None                   # teste armado pelo botão (espelhado no coletor)
        self.last: dict | None = None                  # última corrida gravada (live.gps.perf)
        self.pedal = PedalNorm()
        self.pn: float | None = None
        self.last_ped_ts = None
        self.last_fix_ts: int | None = None
        self.last_precise_ts: int | None = None
        self.burst_until = 0
        self.trip_id = None

    # ---------------------------------------------------------- ciclo de vida
    def on_trip_start(self, tid, ts, merged) -> None:
        self.trip_id = tid

    def shift(self, off: int) -> None:
        self.ring = deque(((t + off, *x) for t, *x in self.ring), maxlen=self.ring.maxlen)
        for k in ("stop_since", "move_ts", "last_ped_ts", "last_fix_ts", "last_precise_ts"):
            if getattr(self, k) is not None:
                setattr(self, k, getattr(self, k) + off)
        if self.burst_until:
            self.burst_until += off
        for d in (self.run, self.roll):
            if d:
                d["pts"] = [(t + off, *x) for t, *x in d["pts"]]
                for k in ("start", "reach"):
                    if d.get(k) is not None:
                        d[k] += off
        for d in (self.arm, self.last):
            if d:
                for k in ("armed_ts", "ready_ts", "ts", "end_ts"):
                    if d.get(k) is not None:
                        d[k] += off

    def live(self) -> dict:
        if self.run and self.run.get("shown"):
            return {"perf": {"state": "correndo", "kind": "0-100", "elapsed": round((self.ring[-1][0] - self.run["t_go"]) / 1000, 1)}}
        if self.last and self.last_fix_ts is not None and self.last_fix_ts - self.last["end_ts"] <= SHOW_MS:
            return {"perf": {k: v for k, v in self.last.items() if k != "end_ts"}}
        return {}

    # ---------------------------------------------------------- teste armado (coletor)
    def arm_test(self, now: int) -> None:
        self.arm = {"state": "aguardando_parar", "armed_ts": now, "splits": {}}
        if self.ready:
            self.arm.update(state="pronto", ready_ts=now)

    def cancel_test(self) -> None:
        self.arm = None
        if self.run:
            self.run["armed"] = False

    def mirror(self, now: int) -> dict | None:
        """Estado do teste armado para o coletor espelhar em self.accel, ou None quando o GPS não está bom (o coletor
        segue pelo OBD). Uma corrida já começada pelo GPS termina pelo GPS (sem posição por 2 s: incompleta)."""
        a = self.arm
        if not a:
            return None
        fresh = self.last_precise_ts is not None and 0 <= now - self.last_precise_ts <= GPS_FRESH_MS
        if a["state"] == "correndo" and not fresh and self.run:
            self._finish_launch(self.ring[-1][0] if self.ring else now)
        if not fresh and a["state"] in ("aguardando_parar", "pronto"):
            return None
        return {**a, "src": "gps", "pts": [], "last0": now, "splits": dict(a.get("splits") or {})}

    # ---------------------------------------------------------- alimentação
    def _burst(self, ts: int, seconds: float) -> None:
        if ts + seconds * 1000 - self.burst_until < 5000:     # a rajada já cobre (quase) tudo: não repete o pedido
            return
        self.burst_until = ts + int(seconds * 1000)
        fn = getattr(self.hub, "start_burst", None)
        if fn:
            fn(seconds)

    def feed(self, ctx) -> list:
        f = ctx.fix
        if f.speed is None:
            return []
        ts, v, pr = f.ts, float(f.speed), precise(f)
        self.ring.append((ts, v, pr, f.alt, f.climb))
        self.last_fix_ts = ts
        if pr:
            self.last_precise_ts = ts
        vals, vts = ctx.values or {}, ctx.value_ts or {}
        pt = vts.get("pedal_d")
        ped_dt = 0
        if pt is not None and pt != self.last_ped_ts and vals.get("pedal_d") is not None:
            prev_pn = self.pn
            ped_dt = min(1000, pt - self.last_ped_ts) if self.last_ped_ts is not None and prev_pn is not None and prev_pn >= AUTO_PEDAL else 0
            self.last_ped_ts = pt
            self.pn = self.pedal(vals["pedal_d"])
            if self.pn < AUTO_PEDAL:
                ped_dt = 0
        fresh = lambda k: vts.get(k) is not None and abs(ts - vts[k]) <= FRESH_MS
        pn = self.pn if fresh("pedal_d") else None
        rpm = vals.get("rpm") if fresh("rpm") else None
        gear = vals.get("gear") if fresh("gear") else None
        tid = ctx.trip_id if ctx.trip_id is not None else self.trip_id
        if pn is not None and pn >= PULL_PEDAL and (rpm or 0) >= PULL_RPM:
            self._burst(ts, BURST_PULL_S)              # puxada: o dinamômetro precisa dos 5 Hz gravados
        self._arm_tick(ts)
        if self.run:
            self.run["ped_ms"] += ped_dt
            self._launch_step(ts, v, pn, rpm, tid)
        else:
            self._watch_stop(ts, v, tid)
        if not self.run:
            self._roll_step(ts, v, pr, pn, gear, rpm, tid)
        return []

    def _arm_tick(self, ts: int) -> None:
        a = self.arm
        if not a:
            return
        if a["state"] == "aguardando_parar":
            if self.ready:
                a.update(state="pronto", ready_ts=ts)
            elif ts - a["armed_ts"] > ARM_WAIT_MS:
                a["state"] = "cancelado"
        elif a["state"] == "pronto" and not self.run and ts - a.get("ready_ts", ts) > ARM_READY_MS:
            a["state"] = "cancelado"                  # armado e esquecido

    def _watch_stop(self, ts: int, v: float, tid) -> None:
        if v < STOP_KMH:
            if self.stop_since is None:
                self.stop_since = ts
            if ts - self.stop_since >= STOP_MS:
                self.ready = True
            self.move_ts = None
            return
        self.stop_since = None
        if not self.ready:
            return
        if self.move_ts is None:
            self.move_ts = ts
        if v >= GO_KMH:
            last_stop = next((p[0] for p in reversed(self.ring) if p[1] < STOP_KMH), ts)
            pts = [p for p in self.ring if p[0] >= last_stop - 1000]
            armed = bool(self.arm and self.arm["state"] == "pronto")
            self.run = {"pts": pts, "start": last_stop, "t_go": ts, "armed": armed, "pmax": 0.0, "vmax": v, "rpm": 0.0,
                        "reach": None, "tid": tid, "shown": armed, "ped_ms": 0}
            self.ready, self.move_ts = False, None
            if armed:
                self.arm.update(state="correndo", elapsed=0.0)
                self._burst(ts, BURST_LAUNCH_S)
        elif ts - self.move_ts > CREEP_MS:
            self.ready, self.move_ts = False, None    # saiu devagar, manobrando: não é arrancada

    def _launch_step(self, ts: int, v: float, pn, rpm, tid) -> None:
        r = self.run
        r["pts"].append(self.ring[-1])
        r["pmax"] = max(r["pmax"], pn or 0.0)
        r["vmax"] = max(r["vmax"], v)
        r["rpm"] = max(r["rpm"], rpm or 0.0)
        if not r["shown"] and ((pn or 0) >= AUTO_PEDAL or (v >= QUICK_KMH and ts - r["t_go"] <= QUICK_MS)):
            r["shown"] = True                          # suspeita de arrancada de propósito: rajada e tela
            self._burst(r["start"], BURST_LAUNCH_S)
        if self.arm and self.arm["state"] == "correndo" and r["armed"]:
            self.arm["elapsed"] = round((ts - r["start"]) / 1000, 2)
            for k in SPLITS:                           # parciais provisórias para a tela (a conta final sai no fim)
                if str(k) not in self.arm["splits"] and v >= k:
                    tc = crossing([p[:2] for p in r["pts"]], k)
                    if tc is not None:
                        self.arm["splits"][str(k)] = round((tc - r["start"]) / 1000, 2)
        if r["reach"] is None and v >= 100:
            r["reach"] = ts
        if (r["reach"] is not None and ts - r["reach"] >= AFTER_MS) or v < r["vmax"] - DROP_KMH or ts - r["t_go"] > MAX_LAUNCH_MS:
            self._finish_launch(ts)

    # ---------------------------------------------------------- gravação
    def _validity(self, pts: list, ta: float, tb: float) -> tuple[bool, list[str], float | None, str | None]:
        seg = [p for p in pts if ta - 1000 <= p[0] <= tb + 1000]
        why = []
        if not all(p[2] for p in seg):
            why.append("GPS impreciso")
        sl, src = terrain.run_slope([{"ts": p[0], "speed": p[1], "alt": p[3], "climb": p[4]} for p in seg])
        if sl is not None and sl > SLOPE_MAX:
            why.append("em subida")
        elif sl is not None and sl < -SLOPE_MAX:
            why.append("em descida")
        return not why, why, sl, src

    def _best(self, kind: str, fuel: str | None, exclude: int | None = None) -> float | None:
        best = None
        for r in self.con.execute("SELECT id, t_s, fuel_mix FROM accel_run WHERE source='gps' AND kind=? AND t_s IS NOT NULL AND note IS NULL",
                                  (kind,)):
            if r["id"] != exclude and routesmod.fuel_kind(r["fuel_mix"]) == fuel and (best is None or r["t_s"] < best):
                best = r["t_s"]
        return best

    def _insert(self, *, ts, tid, kind, t_s, splits, v0, v1, gear, dist_m, slope, hz, auto, pts, max_rpm, why, src, armed) -> dict:
        mix = routesmod._mix_at(self.con, int(ts))
        fuel = routesmod.fuel_kind(mix)
        valid = not why
        best = self._best(kind, fuel) if valid and t_s is not None else None
        record = bool(valid and t_s is not None and (best is None or t_s < best))
        step = max(1, len(pts) // CURVE_MAX)
        curve = [[round((p[0] - ts) / 1000, 2), round(p[1], 1)] for p in pts[::step]]
        js = {"curve": curve, "valid": valid, "why": why, "armed": armed, "slope_src": src, "record": record, "fuel": fuel,
              "prev_best": best, "label": _kind_label(kind, gear)}
        self.con.execute("""INSERT INTO accel_run(ts, trip_id, t60, t80, t100, max_rpm, samples, hz, fuel_mix, note, source, kind, v0, v1,
                            t_s, gear, dist_m, slope_pct, auto, json) VALUES (?,?,?,?,?,?,?,?,?,?,'gps',?,?,?,?,?,?,?,?,?)""",
                         (int(ts), tid, splits.get("60"), splits.get("80"), splits.get("100"), max_rpm or None, len(pts), hz, mix,
                          ", ".join(why) or None, kind, v0, v1, t_s, gear, dist_m, slope, int(auto), json.dumps(js, ensure_ascii=False)))
        return {"state": "concluido" if t_s is not None else "incompleto", "kind": kind, "label": js["label"], "t_s": t_s,
                "splits": splits, "record": record, "valid": valid, "note": ", ".join(why) or None, "auto": int(auto), "hz": hz,
                "fuel": fuel, "gear": gear, "ts": int(ts)}

    def _finish_launch(self, ts: int) -> None:
        r, self.run = self.run, None
        pts = r["pts"]
        m = measure_launch([p[:2] for p in pts])
        sp, t0 = m["splits"], m["t0"]
        armed = r["armed"]
        span = (pts[-1][0] - t0) if t0 is not None else 0
        purposeful = (r["ped_ms"] >= max(AUTO_PEDAL_MS, AUTO_PEDAL_SHARE * span)
                      and ((sp.get("60") or 99) <= AUTO_T60 or (sp.get("100") or 99) <= AUTO_T100))
        out = None
        if t0 is not None and (armed or purposeful):
            _, why, sl, src = self._validity(pts, t0, m.get("tc100") or pts[-1][0])
            out = self._insert(ts=t0, tid=r["tid"], kind="0-100", t_s=sp.get("100"), splits=sp, v0=0.0, v1=round(m["vmax"], 1),
                               gear=None, dist_m=m["dist_m"], slope=sl, hz=m["hz"], auto=not armed, pts=pts, max_rpm=r["rpm"],
                               why=why, src=src, armed=armed)
            self.last = {**out, "end_ts": ts}
        if armed and self.arm:
            self.arm.update(state="concluido" if sp.get("100") else "incompleto", splits=sp, hz=m["hz"],
                            elapsed=sp.get("100") or (round((pts[-1][0] - t0) / 1000, 2) if t0 else None),
                            max_rpm=r["rpm"] or None, record=bool(out and out["record"]), note=out["note"] if out else None)
        self.ready = False

    def _roll_step(self, ts: int, v: float, pr: bool, pn, gear, rpm, tid) -> None:
        r = self.roll
        if r is None:
            if pn is not None and pn >= ROLL_PEDAL and gear and gear > 0 and ROLL_MIN_KMH <= v < PAIRS[-1][1]:
                pts = [p for p in self.ring if p[0] >= ts - 1500]
                self.roll = {"gear": int(gear), "pts": pts, "start": ts, "v0": pts[0][1], "done": set(), "tid": tid, "rpm": rpm or 0.0}
                self._burst(ts, BURST_ROLL_S)
            return
        r["pts"].append(self.ring[-1])
        r["rpm"] = max(r["rpm"], rpm or 0.0)
        if (pn is not None and pn < ROLL_OFF) or (gear is not None and gear != r["gear"]) or ts - r["start"] > ROLL_MAX_MS:
            self.roll = None                           # soltou, trocou de marcha (ou pisou na embreagem) ou demorou demais
            return
        for a, b in PAIRS:
            key = f"{a}-{b}"
            if key in r["done"] or r["v0"] >= a - 1 or v < b:
                continue
            p2 = [p[:2] for p in r["pts"]]
            tb_raw = next((p[0] for p in p2 if p[1] >= b), ts)
            if ts - tb_raw < AFTER_MS:
                continue
            r["done"].add(key)
            m = measure_pair(p2, a, b)
            if not m or m["t_s"] > ROLL_MAX_MS / 1000:
                continue
            _, why, sl, src = self._validity(r["pts"], m["ta"], m["tb"])
            seg = [p for p in r["pts"] if m["ta"] - 1000 <= p[0] <= m["tb"] + 1000]
            d = sum((p[1] + q[1]) / 2 / 3.6 * (q[0] - p[0]) / 1000 for p, q in zip(seg, seg[1:]) if m["ta"] <= p[0] and q[0] <= m["tb"])
            out = self._insert(ts=m["ta"], tid=r["tid"], kind=key, t_s=m["t_s"], splits={}, v0=float(a), v1=float(b), gear=r["gear"],
                               dist_m=round(d, 1), slope=sl, hz=m["hz"], auto=True, pts=seg, max_rpm=r["rpm"], why=why, src=src, armed=False)
            self.last = {**out, "end_ts": ts}


# ---------------------------------------------------------------- relatório (/api/gps/perf)
def report(con, limit: int = 200) -> dict:
    """Corridas (GPS e OBD) e recordes por tipo e combustível. Sem posição: rota aberta na nuvem."""
    runs = []
    for r in con.execute("SELECT * FROM accel_run ORDER BY ts DESC LIMIT ?", (limit,)):
        d = dict(r)
        try:
            js = json.loads(d.pop("json") or "{}")
        except ValueError:
            js = {}
        d["fuel"] = routesmod.fuel_kind(d["fuel_mix"])
        d["kind"] = d["kind"] or "0-100"
        d["t_s"] = d["t_s"] if d["t_s"] is not None else d["t100"]
        d["label"] = js.get("label") or _kind_label(d["kind"], d["gear"])
        d["valid"] = js.get("valid", d["note"] is None)
        d["record"] = bool(js.get("record"))
        d["curve"] = js.get("curve")
        d["source"] = d["source"] or "obd"
        runs.append(d)
    rec: dict[str, dict] = {}
    for d in runs:
        if d["t_s"] is None or not d["valid"]:
            continue
        key = f"{d['kind']}|{d['gear'] or ''}|{d['fuel'] or ''}"
        if key not in rec or d["t_s"] < rec[key]["t_s"]:
            rec[key] = {"kind": d["kind"], "gear": d["gear"], "label": d["label"], "fuel": d["fuel"], "t_s": d["t_s"], "ts": d["ts"],
                        "id": d["id"], "source": d["source"], "hz": d["hz"]}
    order = {"0-100": 0, "40-80": 1, "60-100": 2, "80-120": 3}
    records = sorted(rec.values(), key=lambda x: (order.get(x["kind"], 9), x["gear"] or 0, x["fuel"] or ""))
    return {"runs": runs, "records": records, "n": len(runs)}
