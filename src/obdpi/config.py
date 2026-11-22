"""Configuração: /etc/obd/obd.toml (+ secrets.toml) sobre os padrões abaixo."""
from __future__ import annotations

import copy
import json
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "adapter": {
        "transport": "rfcomm",        # rfcomm | tcp
        "mac": "",                    # vazio = descobrir automaticamente
        "rfcomm_channel": 1,
        "tcp_host": "127.0.0.1",
        "tcp_port": 35000,
        "protocol": "auto",           # auto | 1..9 (ATSP)
        "name_patterns": ["OBD", "ELM", "V-LINK", "VLINK", "VGATE", "KONNWEI", "CHX", "IOS-VLINK", "OBDII"],
        "pins": ["1234", "0000", "6789"],
        "record_raw": False,
    },
    "collector": {
        "full_period_s": 5,
        "fast_period_s": 0.5,
        "fast_pids": ["0C", "11", "49", "4C", "0D", "0B", "45", "4A", "47", "10", "44", "04"],
        "dtc_period_s": 20,
        "commit_interval_s": 5,
        "engine_on_rpm": 400,
        "trip_end_s": 120,
    },
    "storage": {"db_path": "/data/obd/obd.db", "state_path": "/data/obd/state.json", "backup_dir": "/data/obd/backup"},
    "web": {"bind": "0.0.0.0", "port": 80, "admin_token": ""},
    "notify": {"ntfy_server": "https://ntfy.sh", "ntfy_topic": "", "min_severity": "warn"},
    "runtime": {"live_sock": "/run/obd/live.sock", "control_sock": "/run/obd/control.sock"},
    # GPS (docs/GPS-ARQUITETURA.md). Sem receptor nada muda: o painel mostra "GPS não conectado".
    "gps": {
        "enabled": True, "source": "auto",            # auto | gpsd | nmea | replay | off
        "gpsd_host": "127.0.0.1", "gpsd_port": 2947, "device": "/dev/gps0",
        "rate_hz": 5, "store_hz": 1, "stopped_every_s": 30, "latency_ms": 120,
        "min_sats": 5, "max_hdop": 5.0,
        "replay_file": "", "replay_speed": 1.0, "replay_loop": False,
        "geo_db": "/data/obd/geo/geo.db",
        "moved_m": 300,                               # distância que caracteriza "mudou de lugar desligado"
        "cloud": "auto",                              # auto (envia posição só se a nuvem tiver senha de localização) | off
        "voice_radar": True, "voice_limit": True,
        "summary_names": True,                        # nomes dos lugares no resumo do ntfy ("Casa → Trabalho"); False = só números
    },
    "vehicle": {"mass_kg": 1350, "cda": 0.63, "crr": 0.011, "drivetrain_eff": 0.88, "tank_l": 50,
                "tyre": "205/55 R16", "occupants": 2},       # decisões do dono (2026-11-22); ocupantes entram na massa do dinamômetro (F6)
    "power": {"always_on": False, "min_batt_v": 12.0},
}

CONFIG_PATH = Path(os.environ.get("OBD_CONFIG", "/etc/obd/obd.toml"))
SECRETS_PATH = Path(os.environ.get("OBD_SECRETS", "/etc/obd/secrets.toml"))


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    for p in (CONFIG_PATH, SECRETS_PATH):
        if p.exists():
            with p.open("rb") as f:
                _merge(cfg, tomllib.load(f))
    return cfg


class State:
    """Pequeno JSON persistente (MAC descoberto, protocolo, etc.)."""

    def __init__(self, path: str):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, k, v):
        self.data[k] = v
        tmp = self.path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)
