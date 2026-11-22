"""O fio "gps" dentro do obd-collector: abre a fonte, lê, filtra e enfileira. Nunca propaga exceção.

O laço principal do coletor (único que grava no banco) tira as posições com drain() a cada ciclo. A fila guarda 5 min
a 5 Hz: o laço pode ficar preso num connect do Bluetooth por dezenas de segundos sem perder nada.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from . import Fix, valid_time
from .geo import haversine_m
from .source import GpsSource, NullSource, SourceError, open_source

log = logging.getLogger("gps")

SKIP_AFTER_ACQUIRE = 3       # descarta as 3 primeiras posições depois de adquirir sinal
LOST_S = 5.0                 # sem posição por mais que isso = "sem_sinal" (e a próxima é uma nova aquisição)
MAX_KMH = 250                # velocidade implícita acima disso entre duas posições = salto impossível
JUMP_SLOW_M = 100            # salto maior que isso com velocidade informada < 5 km/h = salto impossível
MAX_REJECTS = 5              # rejeitou 5 seguidas: a referência é que estava errada, recomeça dela


class GpsReader(threading.Thread):
    BACKOFF0, BACKOFF_MAX = 2.0, 30.0

    def __init__(self, cfg: dict, source: GpsSource | None = None):
        super().__init__(daemon=True, name="gps")
        self.cfg = cfg
        self.rate_hz = float(cfg.get("rate_hz", 5) or 5)
        self._given = source
        self.src: GpsSource | None = None
        self.lock = threading.Lock()
        self.queue: deque[Fix] = deque(maxlen=1500)
        self.ring: deque[Fix] = deque(maxlen=200)          # 30 s a 5 Hz (com folga)
        self._latest: Fix | None = None
        self._halt = threading.Event()
        self._rate_req: float | None = None
        self.disabled = False
        self.opened = False            # a fonte está aberta (socket/serial)
        self.connected = False         # receptor presente
        self.info_cache: dict = {}
        self.reconnects = 0
        self.epoch = 0                 # sobe a cada (re)abertura: o GpsHub recalcula o deslocamento da hora
        self.present_mono: float | None = None
        self.ttff_s: float | None = None
        self.first_fix_epoch = 0       # época (abertura) em que a primeira posição chegou
        self.last_seen: float | None = None
        self.skip = SKIP_AFTER_ACQUIRE
        self.prev: Fix | None = None
        self.rejects = 0
        self.rate_sent = False
        self.last_error = ""
        self.n_in = self.n_ok = 0

    # ------------------------------------------------------------ fio
    def run(self) -> None:
        backoff = self.BACKOFF0
        while not self._halt.is_set():
            try:
                if self.src is None:
                    self.src = self._given or open_source(self.cfg)
                    self._given = None
                    if isinstance(self.src, NullSource):
                        self.disabled = True
                        return
                if not self.opened:
                    self.src.open()
                    self.opened = True
                    self.epoch += 1
                    self.rate_sent = False
                    backoff = self.BACKOFF0
                fixes = self.src.read(0.5)
                self._on_info(self.src.info())
                for f in fixes:
                    self._accept(f)
                req = self._rate_req
                if self.connected and (req is not None or not self.rate_sent):
                    self._rate_req = None
                    if req is not None:
                        self.rate_hz = req
                    self.rate_sent = bool(self.src.set_rate(self.rate_hz)) or req is None
            except Exception as e:                 # SourceError ou qualquer outra coisa: o fio não morre
                if not isinstance(e, SourceError):
                    log.warning("gps: erro inesperado: %r", e)
                else:
                    log.info("gps: %s", e)
                bad = self.src                       # fecha e reabre a mesma fonte (o "auto" guarda as recusas do gpsd)
                self.last_error = str(e)[:200]
                try:
                    if bad:
                        bad.close()
                except Exception:
                    pass
                if self.opened:
                    self.reconnects += 1
                self.opened = False
                self._on_info({"connected": False})
                self._halt.wait(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
        try:
            if self.src:
                self.src.close()
        except Exception:
            pass

    def _on_info(self, info: dict) -> None:
        was = self.connected
        self.info_cache = info
        self.connected = bool(info.get("connected"))
        if self.connected and not was:
            self.present_mono = time.monotonic()
            self.ttff_s = None
            self.rate_sent = False
            self.skip = SKIP_AFTER_ACQUIRE
            self.prev = None

    # ------------------------------------------------------------ filtros
    def _jump(self, a: Fix, b: Fix) -> bool:
        d = haversine_m(a.lat, a.lon, b.lat, b.lon)
        if a.t_gps is not None and b.t_gps is not None and b.t_gps != a.t_gps:
            dt = (b.t_gps - a.t_gps) / 1000
        else:
            dt = b.mono - a.mono
        if dt > 0 and d / dt * 3.6 > MAX_KMH:
            return True
        return d > JUMP_SLOW_M and (b.speed or 0) < 5

    def _accept(self, f: Fix) -> bool:
        """Filtro antes de enfileirar. Devolve True se a posição entrou."""
        self.n_in += 1
        if f.mode < 2:
            return False
        if not valid_time(f.t_gps):
            f.t_gps = None                         # hora absurda (virada de semana do GPS): a posição vale, a hora não
        if self.last_seen is None or f.mono - self.last_seen > LOST_S:
            self.skip = SKIP_AFTER_ACQUIRE         # (re)aquisição: as primeiras costumam vir ruins
            self.prev = None
        self.last_seen = f.mono
        if self.skip > 0:
            self.skip -= 1
            return False
        if self.prev is not None and self._jump(self.prev, f):
            self.rejects += 1
            if self.rejects < MAX_REJECTS:
                return False
        self.rejects = 0
        self.prev = f
        with self.lock:
            self.queue.append(f)
            self.ring.append(f)
            self._latest = f
        self.n_ok += 1
        if self.ttff_s is None and self.present_mono is not None:
            self.ttff_s = round(f.mono - self.present_mono, 1)
            self.first_fix_epoch = self.epoch
        return True

    # ------------------------------------------------------------ interface para o GpsHub
    def latest(self) -> Fix | None:
        return self._latest

    def drain(self) -> list[Fix]:
        with self.lock:
            out = list(self.queue)
            self.queue.clear()
        return out

    def recent(self, seconds: float) -> list[Fix]:
        lim = time.monotonic() - seconds
        with self.lock:
            return [f for f in self.ring if f.mono >= lim]

    def hz(self) -> float | None:
        pts = self.recent(5)
        if len(pts) < 2:
            return None
        a, b = pts[0], pts[-1]
        span = (b.t_gps - a.t_gps) / 1000 if a.t_gps and b.t_gps else b.mono - a.mono
        return round((len(pts) - 1) / span, 1) if span > 0 else None

    def status(self) -> dict:
        now = time.monotonic()
        f = self._latest
        age = round(now - f.mono, 1) if f else None
        if self.disabled:
            state = "desligado"
        elif not self.connected:
            state = "desconectado"
        elif f is None or age > LOST_S:
            state = "sem_sinal"
        else:
            state = "ok"
        info = self.info_cache or {}
        return {"state": state, "src": getattr(self.src, "name", None), "device": info.get("device"), "driver": info.get("driver"),
                "mode": f.mode if f and state == "ok" else None, "sats": f.sats if f and state == "ok" else info.get("sats_seen"),
                "hdop": f.hdop if f and state == "ok" else None, "hz": self.hz() if state == "ok" else None,
                "age_s": age, "ttff_s": self.ttff_s, "reconnects": self.reconnects, "rate_hz": self.rate_hz}

    def request_rate(self, hz: float) -> None:
        self._rate_req = float(hz)

    def stop(self) -> None:
        self._halt.set()
