"""NMEA 0183 (RMC, GGA, GSA, VTG), montador de época, quadros UBX do u-blox e gerador de sentenças (simulador).

Só stdlib. Usado pela fonte serial direta (reserva sem gpsd), pelo ReplaySource (arquivos .nmea) e pelo simulador.
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

from . import Fix

TALKERS = ("GP", "GN", "GL")
TYPES = ("RMC", "GGA", "GSA", "VTG")
KNOT_KMH = 1.852


def checksum(body: str) -> str:
    """XOR de todos os caracteres entre '$' e '*', em 2 dígitos hexadecimais maiúsculos."""
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}"


def _f(s: str) -> float | None:
    try:
        return float(s) if s != "" else None
    except ValueError:
        return None


def _i(s: str) -> int | None:
    try:
        return int(s) if s != "" else None
    except ValueError:
        return None


def _coord(v: str, hemi: str, deg_digits: int) -> float | None:
    """'4807.038','N' → 48.1173. Campo vazio → None."""
    if not v or not hemi:
        return None
    try:
        d = int(v[:deg_digits]) + float(v[deg_digits:]) / 60
    except ValueError:
        return None
    return -d if hemi in ("S", "W") else d


def _utc_ms(date: str, hms: str) -> int | None:
    """'230394' + '123519.20' → ms UTC. Sem data (só GGA) → None."""
    if len(date) != 6 or len(hms) < 6:
        return None
    try:
        dd, mm, yy = int(date[:2]), int(date[2:4]), int(date[4:6])
        h, m, s = int(hms[:2]), int(hms[2:4]), float(hms[4:])
        base = datetime(2000 + yy, mm, dd, h, m, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(base.timestamp() * 1000) + int(round(s * 1000))


def parse(line: str) -> dict | None:
    """Uma sentença → dicionário com 'type' e os campos usados. None se não for RMC/GGA/GSA/VTG de GP/GN/GL,
    se a soma de verificação estiver errada ou faltar."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, _, ck = line[1:].partition("*")
    if checksum(body) != ck[:2].upper():
        return None
    p = body.split(",")
    tag = p[0]
    if len(tag) != 5 or tag[:2] not in TALKERS or tag[2:] not in TYPES:
        return None
    kind = tag[2:]
    try:
        if kind == "RMC":
            valid = p[2] == "A"
            spd = _f(p[7])
            return {"type": "RMC", "time": p[1], "valid": valid, "lat": _coord(p[3], p[4], 2), "lon": _coord(p[5], p[6], 3),
                    "speed": spd * KNOT_KMH if spd is not None else None, "track": _f(p[8]), "date": p[9],
                    "utc_ms": _utc_ms(p[9], p[1])}
        if kind == "GGA":
            return {"type": "GGA", "time": p[1], "lat": _coord(p[2], p[3], 2), "lon": _coord(p[4], p[5], 3),
                    "quality": _i(p[6]) or 0, "sats": _i(p[7]), "hdop": _f(p[8]), "alt": _f(p[9])}
        if kind == "GSA":
            return {"type": "GSA", "mode": _i(p[2]) or 1, "pdop": _f(p[15]), "hdop": _f(p[16]), "vdop": _f(p[17])}
        if kind == "VTG":
            return {"type": "VTG", "track": _f(p[1]), "speed": _f(p[7])}
    except IndexError:                       # sentença truncada
        return None
    return None


class NmeaAssembler:
    """Junta as sentenças de uma mesma época (hhmmss.ss) numa posição. O u-blox manda RMC, VTG, GGA, GSA… por época;
    a posição sai quando RMC e GGA da época chegaram, ou quando começa outra época. GSA (sem hora) vale para as seguintes."""

    def __init__(self):
        self.cur: dict | None = None         # época em montagem: {"time", "rmc", "gga", "vtg", "mono", "done"}
        self.gsa_mode: int | None = None
        self.gsa_hdop: float | None = None
        self.sats_seen: int | None = None
        self.last_date = ""

    def _build(self, e: dict) -> Fix | None:
        rmc, gga, vtg = e.get("rmc"), e.get("gga"), e.get("vtg")
        if (rmc and not rmc["valid"]) or (gga and gga["quality"] == 0) or not (rmc or gga):
            return None
        src = gga if gga and gga["lat"] is not None else rmc
        if not src or src["lat"] is None or src["lon"] is None:
            return None
        t = rmc["utc_ms"] if rmc else (_utc_ms(self.last_date, gga["time"]) if self.last_date else None)
        speed = rmc["speed"] if rmc and rmc["speed"] is not None else (vtg["speed"] if vtg else None)
        track = rmc["track"] if rmc and rmc["track"] is not None else (vtg["track"] if vtg else None)
        sats = gga["sats"] if gga else None
        hdop = gga["hdop"] if gga and gga["hdop"] is not None else self.gsa_hdop
        if self.gsa_mode is not None:
            mode = self.gsa_mode if self.gsa_mode >= 2 else 2
        else:
            mode = 3 if gga and gga["alt"] is not None and (sats or 0) >= 4 else 2
        return Fix(t_gps=t, mono=e["mono"], lat=src["lat"], lon=src["lon"], alt=gga["alt"] if gga else None,
                   speed=speed, heading=track, hdop=hdop, sats=sats, mode=mode)

    def feed(self, line: str, mono: float) -> Fix | None:
        d = parse(line)
        if not d:
            return None
        k = d["type"]
        if k == "GSA":
            self.gsa_mode, self.gsa_hdop = d["mode"], d["hdop"]
            return None
        if k == "VTG":
            if self.cur is not None and not self.cur["done"]:
                self.cur["vtg"] = d
            return None
        if k == "RMC" and d["date"]:
            self.last_date = d["date"]
        if k == "GGA" and d["sats"] is not None:
            self.sats_seen = d["sats"]
        out = None
        if self.cur is None or self.cur["time"] != d["time"]:
            if self.cur is not None and not self.cur["done"]:
                out = self._build(self.cur)          # época anterior incompleta: entrega o que tinha
            self.cur = {"time": d["time"], "mono": mono, "done": False}
        if self.cur["done"]:
            return out
        self.cur[k.lower()] = d
        if "rmc" in self.cur and "gga" in self.cur:
            self.cur["done"] = True
            return self._build(self.cur) or out
        return out


# ---------------------------------------------------------------- UBX (u-blox)
def ubx(cls: int, mid: int, payload: bytes) -> bytes:
    """Quadro UBX: B5 62, classe, id, tamanho (LE), carga, soma Fletcher-8 sobre classe..carga."""
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


def ubx_cfg_rate(hz: float) -> bytes:
    """UBX-CFG-RATE: período de medida em ms, 1 navegação por medida, referência hora GPS."""
    ms = max(50, min(10000, int(round(1000 / hz))))
    return ubx(0x06, 0x08, struct.pack("<HHH", ms, 1, 1))


def ubx_cfg_nav5(dyn_model: int = 4) -> bytes:
    """UBX-CFG-NAV5 só com o modelo dinâmico (máscara bit 0). 4 = automotivo."""
    payload = struct.pack("<HB", 0x0001, dyn_model) + bytes(33)
    return ubx(0x06, 0x24, payload)


# ---------------------------------------------------------------- gerador (simulador)
def _fmt_coord(v: float, deg_digits: int, pos: str, neg: str) -> tuple[str, str]:
    a = abs(v)
    d = int(a)
    m = (a - d) * 60
    return f"{d:0{deg_digits}d}{m:08.5f}", pos if v >= 0 else neg


def _sentence(body: str) -> str:
    return f"${body}*{checksum(body)}"


def to_nmea(f: Fix) -> list[str]:
    """GGA + RMC da posição, com soma de verificação. Sem hora válida não há o que gerar."""
    if f.t_gps is None:
        return []
    dt = datetime.fromtimestamp(f.t_gps / 1000, tz=timezone.utc)
    hms = f"{dt:%H%M%S}.{dt.microsecond // 10000:02d}"
    date = f"{dt:%d%m%y}"
    la, ns = _fmt_coord(f.lat, 2, "N", "S")
    lo, ew = _fmt_coord(f.lon, 3, "E", "W")
    q = 1 if f.mode >= 2 else 0
    alt = f"{f.alt:.1f}" if f.alt is not None else ""
    hdop = f"{f.hdop:.1f}" if f.hdop is not None else ""
    gga = _sentence(f"GPGGA,{hms},{la},{ns},{lo},{ew},{q},{f.sats or 0:02d},{hdop},{alt},M,-5.0,M,,")
    kn = f"{(f.speed or 0) / KNOT_KMH:.3f}"
    trk = f"{f.heading:.1f}" if f.heading is not None else ""
    rmc = _sentence(f"GPRMC,{hms},{'A' if q else 'V'},{la},{ns},{lo},{ew},{kn},{trk},{date},,,A")
    return [rmc, gga]
