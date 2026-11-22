"""Simulador: viagem com GPS e OBD coerentes entre si, a partir de um modelo simples do carro (Civic 1.8 R18A, manual).

Serve para os testes, para desenvolver a interface e para ensaiar no Pi antes de o receptor chegar:

  python -m obdpi.gps.sim --scenario commute --out /tmp/commute.nmea      # arquivo para gps.source = "replay"
  python -m obdpi.gps.sim --scenario commute --days 14 --to-db /tmp/dev/obd.db
  python -m obdpi.gps.sim --serve 2947 --scenario commute                 # finge ser o gpsd (pare o gpsd antes)

Determinístico (random.Random(seed)). Coordenadas inventadas em torno de um ponto genérico, nunca o endereço do dono.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import Fix
from . import nmea as nmeamod
from .geo import bearing_deg, haversine_m, offset

DT = 0.1                                  # passo do modelo (s)
ACC, BRAKE = 1.5, 2.0                     # m/s² (aceleração normal e freada)
G, RHO = 9.81, 1.2
EFF = 0.88
IDLE_RPM = 780
# relação km/h por 1000 rpm (a mesma do coletor, Collector.GEAR_KMH_PER_KRPM)
GEAR_KMH_PER_KRPM = {1: 8.83, 2: 14.84, 3: 22.46, 4: 29.26, 5: 38.16}
TORQUE = [(800, 110), (1000, 120), (2000, 150), (3000, 158), (4300, 172), (5500, 166), (6300, 160), (7000, 140)]   # Nm, R18A aprox.
FAST_COLS = ("rpm", "speed", "map", "tps", "tps_rel", "tps_b", "tps_cmd", "pedal_d", "pedal_e", "fuel_lh", "load", "gear", "maf")
T0 = 1795000000000                        # 2026-11-19 (UTC), início padrão das viagens simuladas
HOME = (-23.5000, -46.6000)               # ponto genérico inventado


@dataclass
class Leg:
    to: tuple[float, float]        # (lat, lon) do fim do trecho
    speed: float                   # km/h de cruzeiro
    stop_s: float = 0              # parada no fim (semáforo)
    grade: float = 0.0             # inclinação em %
    pedal: float | None = None     # None = segue a velocidade; 1.0 = pé no fundo (arrancada/puxada)
    hold_gear: int | None = None   # mantém a marcha (retomada, dinamômetro)


@dataclass
class SimTrip:
    t0: int                        # ms UTC do início
    truth: list[dict]              # a cada 0,1 s: t, lat, lon, alt, v, a, gear, rpm, hdg, grade (a verdade, para os testes)
    fixes: list[Fix]               # o que o receptor entregaria (com ruído, na taxa pedida, com os buracos)
    obd: list[dict]                # leituras como o coletor veria: ts + colunas de fast_sample; "slow": True = sinais lentos
    dropouts: tuple = ()
    meta: dict = field(default_factory=dict)


def torque_nm(rpm: float) -> float:
    pts = TORQUE
    if rpm <= pts[0][0]:
        return pts[0][1]
    for (r1, t1), (r2, t2) in zip(pts, pts[1:]):
        if rpm <= r2:
            return t1 + (t2 - t1) * (rpm - r1) / (r2 - r1)
    return pts[-1][1]


def _collector_gear(speed, rpm):
    """Mesma conta de Collector.gear()."""
    if not speed or not rpm or speed < 5 or rpm < 700:
        return None
    r = speed / (rpm / 1000)
    g, ref = min(GEAR_KMH_PER_KRPM.items(), key=lambda kv: abs(kv[1] - r))
    return g if abs(r - ref) / ref < 0.08 else 0


def simulate(legs: list[Leg], *, start: tuple[float, float], t0: int, rate_hz: float = 5, obd_hz: float = 2, seed: int = 1,
             speedo: float = 1.0, mass: float = 1350, cda: float = 0.63, crr: float = 0.011, alt0: float = 760,
             leak: bool = False, slip: tuple[float, float, float] | None = None,
             dropouts: tuple[tuple[float, float], ...] = (), noise: tuple[float, float, float] = (2.5, 0.15, 4.0),
             mix: float = 0.0, ect0: float = 30) -> SimTrip:
    from ..analytics import fuel as fuelmod
    from ..analytics.tripdiag import expected_air_gs
    rnd = random.Random(seed)
    lat, lon = start
    alt, v, t = float(alt0), 0.0, 0.0
    gear, target, shift_left, rpm = 1, 1, 0.0, float(IDLE_RPM)
    truth: list[dict] = []

    def record(a, grade, hdg, pedal):
        kmh = v * 3.6
        r = rpm * (1 + slip[2]) if slip and slip[0] <= t < slip[0] + slip[1] and kmh > 3 else rpm
        truth.append({"t": round(t, 3), "lat": lat, "lon": lon, "alt": alt, "v": kmh, "a": a, "gear": (0 if shift_left > 0 else gear) if kmh >= 3 else None,
                      "rpm": r, "hdg": hdg, "grade": grade, "pedal": pedal})

    def update_gear(leg, a):
        nonlocal gear, target, shift_left, rpm
        kmh = v * 3.6
        if leg.hold_gear:
            gear = leg.hold_gear
        elif shift_left > 0:
            shift_left -= DT
            if shift_left <= 0:
                gear = target
        elif kmh < 3:
            gear = 1
        else:
            rg = kmh / GEAR_KMH_PER_KRPM[gear] * 1000
            up = 6300 if leg.pedal else 2800
            if rg > up and gear < 5:
                target, shift_left = gear + 1, 0.6
            elif rg < 1300 and gear > 1:
                target, shift_left = gear - 1, 0.6
        if kmh < 3:
            want = IDLE_RPM if a <= 0.05 else 1200 + (1500 if leg.pedal else 0)
        elif shift_left > 0:
            want = kmh / GEAR_KMH_PER_KRPM[target] * 1000
            rpm += (want - rpm) * min(1.0, DT / 0.3)        # razão solta: rotação vai para a da marcha nova
            return
        else:
            want = kmh / GEAR_KMH_PER_KRPM[gear] * 1000
            if gear == 1 and kmh < 10 and a > 0:            # embreagem saindo
                want = max(want, 1200 + (1500 if leg.pedal else 0))
        rpm = max(float(IDLE_RPM) if kmh < 3 else 650.0, want)

    for i, leg in enumerate(legs):
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        v_end = 0.0 if (leg.stop_s > 0 or nxt is None) else min(leg.speed, nxt.speed) / 3.6
        hdg = bearing_deg(lat, lon, *leg.to)
        while True:
            rem = haversine_m(lat, lon, *leg.to)
            if rem < 0.3:
                lat, lon = leg.to
                break
            hdg = bearing_deg(lat, lon, *leg.to)
            vt = leg.speed / 3.6
            if leg.pedal:
                k = GEAR_KMH_PER_KRPM[gear]
                force = torque_nm(rpm) * leg.pedal * EFF * (2 * math.pi / 60 * 3600) / k
                res = mass * G * (crr + leg.grade / 100) + 0.5 * RHO * cda * v * v
                amax = max(0.0, (force - res) / mass) if shift_left <= 0 else 0.0
            else:
                amax = ACC
            vn = min(v + amax * DT, vt) if v <= vt else max(v - BRAKE * DT, vt)
            vb = math.sqrt(v_end * v_end + 2 * BRAKE * max(0.0, rem - 1.0))
            vn = min(vn, vb)
            if vn < 1.0:
                vn = 1.0 if rem > 1.0 else 0.5               # chega devagar até a faixa (nunca fica parado no meio)
            step = (v + vn) / 2 * DT
            a = (vn - v) / DT
            if step >= rem:
                lat, lon = leg.to
            else:
                lat, lon = offset(lat, lon, step * math.cos(math.radians(hdg)), step * math.sin(math.radians(hdg)))
            alt += step * leg.grade / 100
            v = vn
            t += DT
            update_gear(leg, a)
            record(a, leg.grade, hdg, leg.pedal)
            if step >= rem:
                break
        if leg.stop_s > 0 or nxt is None:
            v = 0.0
        for _ in range(int(round(leg.stop_s / DT))):
            t += DT
            update_gear(leg, 0.0)
            record(0.0, leg.grade, hdg, None)

    # ---------------------------------------------------------- GPS (o receptor)
    fixes: list[Fix] = []
    en = ee = ea = 0.0
    phi = 0.95
    sig_p, sig_v, sig_a = noise
    k_ar = math.sqrt(1 - phi * phi)
    dur = truth[-1]["t"] if truth else 0
    n = int(dur * rate_hz)
    last_drop_end = None
    for j in range(n + 1):
        tf = j / rate_hz
        en = phi * en + k_ar * sig_p * rnd.gauss(0, 1)
        ee = phi * ee + k_ar * sig_p * rnd.gauss(0, 1)
        ea = phi * ea + k_ar * sig_a * rnd.gauss(0, 1)
        if any(a <= tf < b for a, b in dropouts):
            last_drop_end = next(b for a, b in dropouts if a <= tf < b)
            continue
        s = truth[min(len(truth) - 1, int(round(tf / DT)))]
        la, lo = offset(s["lat"], s["lon"], en, ee)
        spd = max(0.0, s["v"] + rnd.gauss(0, sig_v) * 3.6) if s["v"] > 0.5 else abs(rnd.gauss(0, sig_v)) * 1.5
        weak = last_drop_end is not None and tf - last_drop_end < 3
        fixes.append(Fix(t_gps=t0 + int(round(tf * 1000)), mono=tf, lat=la, lon=lo, alt=s["alt"] + ea, speed=spd,
                         heading=(s["hdg"] + rnd.gauss(0, 1.5)) % 360 if s["v"] >= 3 else None,
                         climb=s["v"] / 3.6 * s["grade"] / 100, hdop=6.0 if weak else round(0.8 + rnd.random() * 0.4, 2),
                         sats=4 if weak else 9, mode=3))

    # ---------------------------------------------------------- OBD (o coletor)
    obd: list[dict] = []
    k = 0
    next_slow = 0.0
    while True:
        to = k / obd_hz + rnd.uniform(-0.05, 0.05)
        k += 1
        if to > dur:
            break
        to = max(0.0, to)
        s = truth[min(len(truth) - 1, int(round(to / DT)))]
        vm, kmh = s["v"] / 3.6, s["v"]
        rpm_s = s["rpm"]
        idle = kmh < 3
        p_req = (mass * s["a"] + mass * G * (crr + s["grade"] / 100) + 0.5 * RHO * cda * vm * vm) * vm / EFF
        p_max = torque_nm(rpm_s) * rpm_s * 2 * math.pi / 60
        if idle:
            load = 22.0 + rnd.gauss(0, 0.8)
        elif p_req <= 0:
            load = 18.0
        else:
            load = min(100.0, max(18.0, p_req / p_max * 100))
        if s.get("pedal"):
            load = max(load, 85.0 * s["pedal"])
        mp = 28 + 0.72 * load
        iat = 30.0
        maf = expected_air_gs(mp, rpm_s, iat) * 0.85
        if leak and idle:
            maf *= 0.6
        lam_cmd = 0.88 if load > 85 else 1.0
        lh = fuelmod.fuel_lh(maf, lam_cmd, mix, rpm=rpm_s, load=load)
        pedal_d = 15 + load * 0.6 if not idle and p_req > 0 else 15.0
        tps = 2 + load * 0.8 if not idle else 6.0
        spd_obd = int(kmh * speedo)
        row = {"ts": t0 + int(round(to * 1000)), "rpm": round(rpm_s), "speed": spd_obd, "map": round(mp), "tps": round(tps, 1),
               "tps_rel": round(tps * 0.9, 1), "tps_b": round(tps + 15, 1), "tps_cmd": round(tps, 1), "pedal_d": round(pedal_d, 1),
               "pedal_e": round(pedal_d / 2, 1), "fuel_lh": round(lh, 2) if lh is not None else None, "load": round(load, 1),
               "gear": _collector_gear(spd_obd, rpm_s), "maf": round(maf, 2)}
        obd.append(row)
        if to >= next_slow:
            next_slow += 5.0
            stft = rnd.gauss(0, 2.0) + ((35 if idle else 5) if leak else 0)
            obd.append({"ts": row["ts"] + 1, "slow": True, "ect": round(90 - (90 - ect0) * math.exp(-to / 160), 1), "iat": iat,
                        "stft1": round(stft, 1), "lambda_cmd": lam_cmd, "timing": round(12 + 20 * (1 - load / 100), 1),
                        "baro": round(101.325 * (1 - 2.25577e-5 * s["alt"]) ** 5.25588), "run_time": round(30 + to),
                        "batt": 14.1})
    return SimTrip(t0=t0, truth=truth, fixes=fixes, obd=obd, dropouts=tuple(dropouts),
                   meta={"start": start, "end": (lat, lon), "km": sum(haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(truth, truth[1:])) / 1000})


# -------------------------------------------------------------- saídas
def to_nmea(trip: SimTrip) -> list[str]:
    """GGA + RMC por época, com checksum (arquivo para ReplaySource / NmeaSerialSource)."""
    out: list[str] = []
    for f in trip.fixes:
        out += nmeamod.to_nmea(f)
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


DEVICE = "/dev/ttyACM0"


def _gpsd_head() -> list[dict]:
    return [{"class": "VERSION", "release": "3.25", "rev": "3.25", "proto_major": 3, "proto_minor": 15},
            {"class": "DEVICES", "devices": [{"class": "DEVICE", "path": DEVICE, "driver": "u-blox", "activated": _iso(int(time.time() * 1000)),
                                               "native": 1, "bps": 9600, "cycle": 1.0}]},
            {"class": "WATCH", "enable": True, "json": True}]


def _gpsd_msgs(f: Fix, shift_ms: int = 0) -> list[dict]:
    t = f.t_gps + shift_ms if f.t_gps is not None else None
    sky = {"class": "SKY", "device": DEVICE, "hdop": f.hdop, "uSat": f.sats, "nSat": (f.sats or 0) + 3}
    tpv = {"class": "TPV", "device": DEVICE, "mode": f.mode, "time": _iso(t) if t else None, "lat": round(f.lat, 8), "lon": round(f.lon, 8),
           "altMSL": round(f.alt, 2) if f.alt is not None else None, "speed": round((f.speed or 0) / 3.6, 3),
           "track": round(f.heading, 1) if f.heading is not None else None, "climb": round(f.climb or 0, 2)}
    return [sky, {k: v for k, v in tpv.items() if v is not None}]


def to_gpsd(trip: SimTrip) -> list[str]:
    """Linhas JSON como o gpsd envia (VERSION, DEVICES, WATCH, SKY, TPV; TPV mode 1 a cada segundo nos buracos)."""
    out = [json.dumps(m) for m in _gpsd_head()]
    prev = None
    for f in trip.fixes:
        if prev is not None and f.t_gps - prev > 1500:                 # buraco (túnel)
            for tt in range(prev + 1000, f.t_gps, 1000):
                out.append(json.dumps({"class": "TPV", "device": DEVICE, "mode": 1, "time": _iso(tt)}))
        out += [json.dumps(m) for m in _gpsd_msgs(f)]
        prev = f.t_gps
    return out


def to_db(con, trip: SimTrip, *, with_gps: bool = True, finalize: bool = False) -> int:
    """Grava como o coletor gravaria (relógio sincronizado: ts = hora do GPS); devolve o id da viagem."""
    from ..obd.pids import SIGNALS
    from .hub import Decimator, gps_cfg, make_ctx, run_finalizers
    from . import store
    from . import trip as tripmod
    fast = [r for r in trip.obd if not r.get("slow")]
    slow = [r for r in trip.obd if r.get("slow")]
    a, b = fast[0]["ts"], fast[-1]["ts"]
    sid = con.execute("INSERT INTO session(started_ts, last_ts, clock_synced, adapter, app_version, clock_source) VALUES (?,?,1,'sim','sim','sistema')",
                      (a, b)).lastrowid
    tid = con.execute("INSERT INTO trip(session_id, start_ts, end_ts, end_reason, cold_start, start_ect) VALUES (?,?,?,?,?,?)",
                      (sid, a, b, "motor_desligado", 1, slow[0]["ect"] if slow else None)).lastrowid
    con.executemany(f"INSERT OR REPLACE INTO fast_sample(ts, trip_id, {', '.join(FAST_COLS)}) VALUES (?,?,{','.join('?' * len(FAST_COLS))})",
                    [(r["ts"], tid, *[r.get(c) for c in FAST_COLS]) for r in fast])
    ids: dict[str, int] = {}
    for r in con.execute("SELECT id, key FROM signal"):
        ids[r[1]] = r[0]
    rows = []
    for r in slow:
        for key, val in r.items():
            if key in ("ts", "slow") or val is None:
                continue
            if key not in ids:
                s = SIGNALS.get(key)
                con.execute("INSERT OR IGNORE INTO signal(key, pid, name, unit, decimals) VALUES (?,?,?,?,?)",
                            (key, s.pid if s else None, s.name if s else key, s.unit if s else "", s.decimals if s else 0))
                ids[key] = con.execute("SELECT id FROM signal WHERE key=?", (key,)).fetchone()[0]
            rows.append((ids[key], r["ts"], val))
    con.executemany("INSERT OR REPLACE INTO sample(signal_id, ts, value) VALUES (?,?,?)", rows)
    if with_gps:
        cfg = gps_cfg({})
        dec = Decimator(cfg["store_hz"], cfg["stopped_every_s"])
        groups: dict[int, list[Fix]] = {}
        for f in trip.fixes:
            g = Fix(t_gps=f.t_gps, mono=f.mono, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, heading=f.heading, climb=f.climb,
                    hdop=f.hdop, sats=f.sats, mode=f.mode, ts=f.t_gps)
            fl = dec.pick(g)
            if fl is not None:
                groups.setdefault(fl, []).append(g)
        for fl, fs in groups.items():
            store.insert_fixes(con, fs, tid, fl)
    if finalize:
        dist = fuel_l = idle = 0.0
        for p, r in zip(fast, fast[1:]):
            dt = min((r["ts"] - p["ts"]) / 1000, 5)
            dist += (p["speed"] or 0) * dt / 3600
            fuel_l += (p["fuel_lh"] or 0) * dt / 3600
            if (p["speed"] or 0) == 0 and (p["rpm"] or 0) > 400:
                idle += dt
        spd = [r["speed"] for r in fast]
        obd_km, gps_km = dist, None
        g = tripmod.trip_distance(con, a, b, dist, gps_cfg({}))      # mesma conta do coletor (distância híbrida, seção 9.3)
        if g:
            dist, gps_km = g["km"], g["gps_km"]
        con.execute("""UPDATE trip SET finalized=1, duration_s=?, distance_km=?, obd_km=?, gps_km=?, idle_s=?, avg_speed=?, max_speed=?,
                       max_rpm=?, fuel_l=?, km_l=?, hidden=CASE WHEN ? < 180 THEN 2 ELSE 0 END WHERE id=?""",
                    (int((b - a) / 1000), round(dist, 2), round(obd_km, 2), gps_km, int(idle), sum(spd) / len(spd), max(spd),
                     max(r["rpm"] for r in fast), round(fuel_l, 3), round(dist / fuel_l, 1) if fuel_l > 0.05 else None,
                     int((b - a) / 1000), tid))
        run_finalizers(con, tid, make_ctx(con, {}, trip_id=tid, ts=b))
    con.commit()
    return tid


# -------------------------------------------------------------- gpsd falso
class FakeGpsd(threading.Thread):
    """Servidor TCP local que fala o protocolo do gpsd a partir de um SimTrip (ou de linhas prontas, como o arquivo
    tests/fixtures/gps/gpsd-session.jsonl). rebase=True manda as horas como "agora" (a viagem cai no presente)."""

    def __init__(self, trip, port: int = 0, speed: float = 1.0, loop: bool = False, rebase: bool = True, line_gap_s: float = 0.02):
        super().__init__(daemon=True, name="fake-gpsd")
        self.trip, self.speed, self.loop, self.rebase, self.line_gap = trip, max(0.01, speed), loop, rebase, line_gap_s
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(4)
        self.srv.settimeout(0.3)
        self.port = self.srv.getsockname()[1]
        self.received: list[str] = []
        self.plugged = True
        self.clients: list[socket.socket] = []
        self._halt = threading.Event()
        self.sent = 0

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()
        try:
            self.srv.close()
        except OSError:
            pass

    def _send(self, c, msg) -> None:
        c.sendall(((msg if isinstance(msg, str) else json.dumps(msg)) + "\n").encode())
        self.sent += 1

    def _reader(self, c) -> None:
        buf = b""
        while not self._halt.is_set():
            try:
                d = c.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not d:
                return
            buf += d
            while b";" in buf:
                cmd, buf = buf.split(b";", 1)
                self.received.append(cmd.decode(errors="replace").strip())

    def _serve(self, c) -> None:
        self.clients.append(c)
        c.settimeout(0.3)
        threading.Thread(target=self._reader, args=(c,), daemon=True).start()
        try:
            if isinstance(self.trip, list):                 # linhas prontas (arquivo do gpsd)
                for line in self.trip:
                    if self._halt.is_set():
                        return
                    if line.strip():
                        self._send(c, line.strip())
                    time.sleep(self.line_gap / self.speed)
                while not self._halt.wait(0.2):
                    pass
                return
            head = _gpsd_head()
            self._send(c, head[0])
            t_end = time.monotonic() + 2
            while time.monotonic() < t_end and not any("WATCH" in r for r in self.received):
                time.sleep(0.02)
            for m in head[1:]:
                self._send(c, m)
            while True:
                fx = self.trip.fixes
                t_first = fx[0].t_gps
                shift = int(time.time() * 1000) - t_first if self.rebase else 0
                start = time.monotonic()
                was = True
                for f in fx:
                    wait = (f.t_gps - t_first) / 1000 / self.speed - (time.monotonic() - start)
                    if wait > 0 and self._halt.wait(wait):
                        return
                    if self._halt.is_set():
                        return
                    if self.plugged != was:
                        was = self.plugged
                        self._send(c, {"class": "DEVICE", "path": DEVICE, "driver": "u-blox", "activated": _iso(int(time.time() * 1000))}
                                   if was else {"class": "DEVICE", "path": DEVICE, "activated": 0})
                    if self.plugged:
                        for m in _gpsd_msgs(f, shift + (int((f.t_gps - t_first) * (1 / self.speed - 1)) if self.rebase else 0)):
                            self._send(c, m)
                if not self.loop:
                    break
            while not self._halt.wait(0.2):
                pass
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def unplug(self) -> None:
        self.plugged = False

    def plug(self) -> None:
        self.plugged = True

    def stop(self) -> None:
        self._halt.set()
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass


# -------------------------------------------------------------- cenários
def _legs(start, moves) -> list[Leg]:
    """moves: (norte_m, leste_m, km/h, parada_s[, inclinação %]) a partir do ponto anterior."""
    out, p = [], start
    for m in moves:
        p = offset(p[0], p[1], m[0], m[1])
        out.append(Leg(to=p, speed=m[2], stop_s=m[3], grade=m[4] if len(m) > 4 else 0.0))
    return out


COMMUTE = [(0, 0, 10, 20),                       # 20 s parado ligado antes de sair
           (0, 800, 40, 0), (1200, 0, 50, 35), (0, 1500, 60, 0), (900, 300, 50, 40), (0, 2000, 60, 30),
           (1500, 0, 50, 0), (0, 1800, 60, 45), (600, 600, 40, 30), (1000, 0, 50, 35), (0, 400, 30, 10)]


def commute(seed: int = 1, t0: int = T0, reverse: bool = False, **kw) -> SimTrip:
    """12 km urbanos com 6 semáforos, casa → trabalho (reverse=True: trabalho → casa pelo mesmo caminho)."""
    moves = COMMUTE
    start = HOME
    if reverse:
        pts = [HOME]
        for m in moves:
            pts.append(offset(pts[-1][0], pts[-1][1], m[0], m[1]))
        legs = [Leg(to=pts[i], speed=moves[i][2] if i else 30, stop_s=moves[i][3] if i else 10) for i in range(len(pts) - 2, -1, -1)]
        return simulate([Leg(to=pts[-1], speed=10, stop_s=20)] + legs, start=pts[-1], t0=t0, seed=seed, **kw)
    return simulate(_legs(start, moves), start=start, t0=t0, seed=seed, **kw)


def tunnel(seed: int = 1, t0: int = T0, **kw) -> SimTrip:
    """~6 km a 60 km/h com 90 s sem GPS no meio."""
    legs = _legs(HOME, [(0, 0, 10, 15), (0, 3000, 60, 0), (300, 2700, 60, 20)])
    return simulate(legs, start=HOME, t0=t0, seed=seed, dropouts=((150.0, 240.0),), **kw)


def towed(seed: int = 1, t0: int = T0, **kw) -> list[SimTrip]:
    """Duas sessões: estaciona; a próxima partida acontece 2 km adiante (guincho ou roubo)."""
    a = simulate(_legs(HOME, [(0, 0, 10, 15), (0, 1500, 50, 30), (1500, 0, 50, 15)]), start=HOME, t0=t0, seed=seed, **kw)
    end = a.meta["end"]
    p = offset(end[0], end[1], 0, 2000)
    t1 = a.fixes[-1].t_gps + 2 * 3600_000
    b = simulate(_legs(p, [(0, 0, 10, 15), (-1500, 0, 50, 20)]), start=p, t0=t1, seed=seed + 1, **kw)
    return [a, b]


def straight(km: float = 10.0, seed: int = 1, t0: int = T0, speed: float = 60, **kw) -> SimTrip:
    """Reta de 'km' quilômetros a 'speed' km/h (distância verdadeira conhecida: hodômetro, velocímetro)."""
    legs = _legs(HOME, [(0, 0, 10, 10), (0, km * 1000, speed, 10)])
    return simulate(legs, start=HOME, t0=t0, seed=seed, **kw)


def commute_end() -> tuple[float, float]:
    """Ponta final do commute() (o "Trabalho")."""
    p = HOME
    for m in COMMUTE:
        p = offset(p[0], p[1], m[0], m[1])
    return p


def add_places(con) -> None:
    """Casa e Trabalho nas pontas do commute() (banco de desenvolvimento), se ainda não existirem."""
    from . import places
    have = {r[0] for r in con.execute("SELECT name FROM place WHERE COALESCE(deleted,0)=0")}
    for name, kind, (la, lo) in (("Casa", "casa", HOME), ("Trabalho", "trabalho", commute_end())):
        if name not in have:
            places.create_place(con, name, kind, la, lo)
    con.commit()


SCENARIOS = {"commute": commute, "tunnel": tunnel, "towed": towed, "straight": straight}


def _scenario(name: str, seed: int, t0: int) -> list[SimTrip]:
    if name not in SCENARIOS:
        sys.exit(f"cenário desconhecido: {name} (há: {', '.join(SCENARIOS)})")
    r = SCENARIOS[name](seed=seed, t0=t0)
    return r if isinstance(r, list) else [r]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="obdctl gps sim", description="viagem simulada com GPS e OBD coerentes")
    ap.add_argument("--scenario", default="commute", choices=sorted(SCENARIOS))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="arquivo .nmea ou .jsonl (para gps.source = \"replay\")")
    ap.add_argument("--to-db", help="grava as viagens já fechadas neste banco")
    ap.add_argument("--days", type=int, default=1, help="com --to-db: quantos dias de viagens (ida e volta)")
    ap.add_argument("--serve", type=int, help="finge ser o gpsd nesta porta TCP (127.0.0.1)")
    ap.add_argument("--speed", type=float, default=1.0, help="velocidade da reprodução (--serve)")
    a = ap.parse_args(argv)
    if a.serve:
        trip = _scenario(a.scenario, a.seed, T0)[0]
        srv = FakeGpsd(trip, port=a.serve, speed=a.speed, loop=True)
        srv.start()
        print(f"gpsd falso em 127.0.0.1:{srv.port} ({a.scenario}, {len(trip.fixes)} posições). Ctrl+C para sair.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return
    if a.out:
        trips = _scenario(a.scenario, a.seed, T0)
        lines = [ln for t in trips for ln in (to_gpsd(t) if a.out.endswith(".jsonl") else to_nmea(t))]
        with open(a.out, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"{a.out}: {len(lines)} linhas, {sum(len(t.fixes) for t in trips)} posições")
    if a.to_db:
        from .. import db
        con = db.init(a.to_db)
        base = int(time.time() // 86400 * 86400 * 1000) - a.days * 86400_000 + 11 * 3600_000     # 08:00 em Brasília
        n = 0
        if a.scenario == "commute":
            add_places(con)
        for d in range(a.days):
            for k, rev in ((0, False), (10 * 3600_000, True)):
                seed = a.seed + d * 2 + rev
                trips = [commute(seed=seed, t0=base + d * 86400_000 + k, reverse=rev)] if a.scenario == "commute" else \
                    _scenario(a.scenario, seed, base + d * 86400_000 + k)
                for t in trips:
                    to_db(con, t, finalize=True)
                    n += 1
        print(f"{a.to_db}: {n} viagens simuladas gravadas")
    if not (a.out or a.to_db):
        trips = _scenario(a.scenario, a.seed, T0)
        for t in trips:
            print(f"{a.scenario}: {t.meta['km']:.2f} km, {len(t.fixes)} posições, {sum(1 for r in t.obd if not r.get('slow'))} leituras OBD,"
                  f" {t.truth[-1]['t'] / 60:.1f} min")


if __name__ == "__main__":
    main()
