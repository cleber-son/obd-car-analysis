"""GPS (VK-172 / u-blox 7 via gpsd): posição, velocidade real, altitude e hora exata. Plano em docs/GPS-ARQUITETURA.md.

Regra do projeto: o GPS nunca derruba o coletor. Sem receptor tudo funciona como antes ("GPS não conectado").
"""
from __future__ import annotations

from dataclasses import dataclass

# flags de gps_fix
FLAG_BURST = 1           # bit 0: rajada a 5 Hz (janela de desempenho)
FLAG_STOPPED = 2         # bit 1: gravada pela regra "parado" (1 a cada 30 s)

# hora do receptor só vale dentro desta faixa (clones com a virada de semana do GPS mandam datas de 2006, 2019…)
MIN_GPS_MS = 1767225600000       # 2026-01-01 00:00 UTC
MAX_GPS_MS = 2208988800000       # 2040-01-01 00:00 UTC


@dataclass(slots=True)
class Fix:
    t_gps: int | None      # hora UTC do receptor em ms (None se inválida)
    mono: float            # time.monotonic() na chegada
    lat: float
    lon: float
    alt: float | None = None
    speed: float | None = None     # km/h (Doppler)
    heading: float | None = None
    climb: float | None = None     # m/s
    hdop: float | None = None
    sats: int | None = None
    mode: int = 0                  # 0/1 sem posição, 2 = 2D, 3 = 3D
    ts: int = 0            # relógio do coletor; preenchido pelo GpsHub


def valid_time(ms: int | None) -> bool:
    return ms is not None and MIN_GPS_MS <= ms < MAX_GPS_MS


def good(f: Fix, max_hdop: float = 5.0, min_sats: int = 5) -> bool:
    """Posição boa: mode >= 2 e, quando o receptor informa, HDOP e satélites dentro dos limites."""
    if f.mode < 2:
        return False
    if f.hdop is not None and f.hdop > max_hdop:
        return False
    if f.sats is not None and f.sats < min_sats:
        return False
    return True


def precise(f: Fix) -> bool:
    """Posição precisa: 3D, HDOP <= 2 e 7 satélites ou mais."""
    return f.mode == 3 and f.hdop is not None and f.hdop <= 2.0 and f.sats is not None and f.sats >= 7
