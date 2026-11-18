"""Descoberta e pareamento do adaptador OBD via bluetoothctl (o PIN é respondido pelo bt-agent)."""
from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger("bt")


def _ctl(*args: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return str(e)


def paired_devices() -> list[tuple[str, str]]:
    out = _ctl("devices", "Paired")
    return re.findall(r"Device ([0-9A-F:]{17}) (.+)", out)


def find_adapter(patterns: list[str], scan_s: int = 20) -> tuple[str, str] | None:
    pats = [p.upper() for p in patterns]

    def match(devs):
        for mac, name in devs:
            if any(p in name.upper().replace(" ", "") for p in pats):
                return mac, name
        return None

    hit = match(paired_devices())
    if hit:
        return hit
    _ctl("power", "on")
    log.info("procurando adaptador OBD por %ss...", scan_s)
    _ctl("--timeout", str(scan_s), "scan", "on", timeout=scan_s + 10)
    devs = re.findall(r"Device ([0-9A-F:]{17}) (.+)", _ctl("devices"))
    log.info("dispositivos vistos: %s", devs)
    return match(devs)


def pair(mac: str) -> bool:
    _ctl("pairable", "on")
    out = _ctl("pair", mac, timeout=40)
    ok = "Pairing successful" in out or "AlreadyExists" in out or "Already" in out
    _ctl("trust", mac)
    log.info("pareamento %s: %s", mac, "ok" if ok else out.strip()[-200:])
    return ok
