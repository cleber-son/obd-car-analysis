"""Relógio do sistema: está sincronizado? De onde veio a hora? Funciona com systemd-timesyncd e com chrony (GPS).

Nada aqui levanta exceção: qualquer falha vira "não sincronizado" / "none".
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")
TIME_ERROR = 5               # adjtimex: bit STA_UNSYNC ligado (relógio do kernel fora de sincronia)
_libc = None
_src_cache: tuple[float, str] | None = None


def _adjtimex_state() -> int | None:
    global _libc
    import ctypes
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    buf = ctypes.create_string_buffer(256)                 # struct timex zerada (modes = 0: só leitura)
    return int(_libc.adjtimex(buf))


def system_synced() -> bool:
    """True se o relógio do sistema está sincronizado, com timesyncd OU chrony."""
    try:
        if TIMESYNC_FLAG.exists():
            return True
    except Exception:
        pass
    try:
        st = _adjtimex_state()
        return st is not None and 0 <= st < TIME_ERROR     # 0 TIME_OK … 4 TIME_WAIT; -1 = erro da chamada
    except Exception:
        return False


def source() -> str:
    """'ntp' | 'gps' | 'none'. Lê `chronyc -c tracking` no máximo uma vez por minuto (cache)."""
    global _src_cache
    now = time.monotonic()
    if _src_cache and now - _src_cache[0] < 60:
        return _src_cache[1]
    out = "none"
    try:
        synced = system_synced()
        ref = ""
        try:
            r = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0 and r.stdout:
                f = r.stdout.strip().split(",")
                ref = (f[1] if len(f) > 1 else "").strip().upper()
                if f and f[0].upper() == "47505300":              # refid "GPS\0" em hexadecimal
                    ref = "GPS"
        except Exception:
            pass
        if synced:
            out = "gps" if ref == "GPS" else "ntp"
    except Exception:
        out = "none"
    _src_cache = (now, out)
    return out
