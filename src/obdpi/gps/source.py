"""Fontes de posição: gpsd (TCP/JSON), serial NMEA direta (reserva, sem gpsd), arquivo (replay) e nula.

Receptor ausente não é erro: read() devolve [] e info()["connected"] fica False. SourceError só quando o transporte
quebra (socket fechado, EIO na serial); quem reabre, com espera crescente, é o GpsReader.
"""
from __future__ import annotations

import json
import logging
import os
import select
import socket
import time
from datetime import datetime
from pathlib import Path

from . import Fix
from .nmea import NmeaAssembler, ubx_cfg_nav5, ubx_cfg_rate

log = logging.getLogger("gps")


class SourceError(Exception):
    def __init__(self, msg: str = "", refused: bool = False):
        super().__init__(msg)
        self.refused = refused


class GpsSource:
    name = "base"

    def open(self) -> None: ...

    def read(self, timeout: float) -> list[Fix]:
        """Bloqueia até 'timeout'; [] se não chegou nada."""
        time.sleep(timeout)
        return []

    def set_rate(self, hz: float) -> bool:
        return False

    def info(self) -> dict:
        return {"connected": False, "device": None, "driver": None, "sats_seen": None}

    def close(self) -> None: ...


class NullSource(GpsSource):
    """GPS desligado na configuração: nunca entrega nada."""
    name = "off"


def iso_ms(s: str | None) -> int | None:
    """'2026-11-22T12:34:56.200Z' → ms UTC."""
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def tpv_fix(msg: dict, sky: dict, mono: float) -> Fix | None:
    """Linha TPV do gpsd → Fix (com HDOP e satélites do último SKY). Sem posição → None."""
    mode = msg.get("mode") or 0
    lat, lon = msg.get("lat"), msg.get("lon")
    if mode < 2 or lat is None or lon is None:
        return None
    spd = msg.get("speed")
    alt = msg.get("altMSL", msg.get("alt"))
    return Fix(t_gps=iso_ms(msg.get("time")), mono=mono, lat=float(lat), lon=float(lon),
               alt=float(alt) if alt is not None else None, speed=spd * 3.6 if spd is not None else None,
               heading=msg.get("track"), climb=msg.get("climb"), hdop=sky.get("hdop"), sats=sky.get("sats"), mode=int(mode))


def sky_info(msg: dict) -> dict:
    sats = msg.get("uSat")
    sv = msg.get("satellites")
    if sats is None and isinstance(sv, list):
        sats = sum(1 for s in sv if s.get("used"))
    seen = msg.get("nSat", len(sv) if isinstance(sv, list) else None)
    return {"hdop": msg.get("hdop"), "sats": sats, "seen": seen}


class GpsdSource(GpsSource):
    name = "gpsd"
    SILENT_S = 10.0          # com receptor presente, 10 s sem nenhuma linha = gpsd travado: reconecta

    def __init__(self, host: str = "127.0.0.1", port: int = 2947):
        self.host, self.port = host, int(port)
        self.sock: socket.socket | None = None
        self.buf = b""
        self.devices: dict[str, str | None] = {}      # caminho → driver
        self.sky: dict = {}
        self.last_line = 0.0
        self.removed = 0
        self.version = None

    def open(self) -> None:
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=3)
        except ConnectionRefusedError as e:
            raise SourceError(f"gpsd recusou a conexão ({self.host}:{self.port})", refused=True) from e
        except OSError as e:
            raise SourceError(f"gpsd: {e}") from e
        self.sock, self.buf, self.devices, self.sky = s, b"", {}, {}
        self.last_line = time.monotonic()
        self._send('?WATCH={"enable":true,"json":true};')

    def _send(self, txt: str) -> None:
        try:
            self.sock.sendall(txt.encode())
        except (OSError, AttributeError) as e:
            raise SourceError(f"gpsd: escrita: {e}") from e

    def _handle(self, msg: dict, mono: float) -> Fix | None:
        c = msg.get("class")
        if c == "TPV":
            return tpv_fix(msg, self.sky, mono)
        if c == "SKY":
            self.sky = sky_info(msg)
        elif c == "DEVICES":
            self.devices = {d["path"]: d.get("driver") for d in msg.get("devices") or [] if d.get("path")}
        elif c == "DEVICE" and msg.get("path"):
            if msg.get("activated"):
                self.devices[msg["path"]] = msg.get("driver") or self.devices.get(msg["path"])
            elif msg["path"] in self.devices:            # 'activated' 0 ou ausente = receptor removido
                del self.devices[msg["path"]]
                self.removed += 1
                self.sky = {}
        elif c == "VERSION":
            self.version = msg.get("release")
        return None

    def read(self, timeout: float) -> list[Fix]:
        if not self.sock:
            raise SourceError("gpsd: não conectado")
        out: list[Fix] = []
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                r, _, _ = select.select([self.sock], [], [], left)
            except (OSError, ValueError) as e:
                raise SourceError(f"gpsd: {e}") from e
            if not r:
                break
            try:
                chunk = self.sock.recv(8192)
            except OSError as e:
                raise SourceError(f"gpsd: leitura: {e}") from e
            if not chunk:
                raise SourceError("gpsd fechou a conexão")
            self.buf += chunk
            mono = time.monotonic()
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                self.last_line = mono
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    f = self._handle(msg, mono)
                    if f:
                        out.append(f)
            if len(self.buf) > 65536:                      # lixo sem fim de linha
                self.buf = b""
            if out:
                break
        if self.devices and time.monotonic() - self.last_line > self.SILENT_S:
            raise SourceError("gpsd sem enviar nada há 10 s")
        return out

    def set_rate(self, hz: float) -> bool:
        """Pede a taxa ao gpsd (o driver u-blox dele troca a taxa do receptor). A conferência é pelo intervalo real."""
        if not self.sock or not self.devices:
            return False
        path = next(iter(self.devices))
        self._send('?DEVICE=' + json.dumps({"path": path, "cycle": round(1 / hz, 3)}) + ';')
        return True

    def info(self) -> dict:
        dev = next(iter(self.devices), None)
        return {"connected": bool(self.devices), "device": dev, "driver": self.devices.get(dev) if dev else None,
                "sats_seen": self.sky.get("seen"), "removed": self.removed}

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.devices = {}


class NmeaSerialSource(GpsSource):
    """Reserva sem gpsd: lê o receptor direto pela serial (termios cru, sem pyserial). Nunca usar com o gpsd rodando."""
    name = "nmea"

    def __init__(self, device: str = "/dev/gps0"):
        self.device = device
        self.fd: int | None = None
        self.asm = NmeaAssembler()
        self.buf = b""
        self.nav5_sent = False

    def open(self) -> None:
        self.close()
        try:
            import termios
            import tty
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                tty.setraw(fd)
                a = termios.tcgetattr(fd)
                a[4] = a[5] = termios.B9600                 # no USB CDC-ACM a velocidade é ignorada
                termios.tcsetattr(fd, termios.TCSANOW, a)
            except termios.error:
                pass
        except OSError as e:
            raise SourceError(f"serial {self.device}: {e}") from e
        self.fd, self.buf, self.asm, self.nav5_sent = fd, b"", NmeaAssembler(), False

    def read(self, timeout: float) -> list[Fix]:
        if self.fd is None:
            raise SourceError("serial fechada")
        out: list[Fix] = []
        end = time.monotonic() + timeout
        while not out:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                r, _, _ = select.select([self.fd], [], [], left)
                if not r:
                    break
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                raise SourceError(f"serial {self.device}: {e}") from e
            if not chunk:
                raise SourceError(f"serial {self.device}: fim de arquivo")
            self.buf += chunk
            mono = time.monotonic()
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                f = self.asm.feed(line.decode("ascii", "replace"), mono)
                if f:
                    out.append(f)
            if len(self.buf) > 8192:
                self.buf = b""
        return out

    def set_rate(self, hz: float) -> bool:
        if self.fd is None:
            return False
        try:
            if not self.nav5_sent:
                os.write(self.fd, ubx_cfg_nav5(4))               # modelo dinâmico automotivo, uma vez por abertura
                self.nav5_sent = True
            os.write(self.fd, ubx_cfg_rate(hz))
            return True
        except OSError:
            return False

    def info(self) -> dict:
        return {"connected": self.fd is not None, "device": self.device, "driver": "nmea", "sats_seen": self.asm.sats_seen}

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None


def load_track(path: str) -> list[Fix]:
    """Arquivo .nmea (sentenças) ou .jsonl (TPV/SKY do gpsd, ou {"t": ms, "lat":…} do simulador) → posições."""
    out: list[Fix] = []
    p = Path(path)
    if p.suffix.lower() in (".nmea", ".txt", ".log"):
        asm = NmeaAssembler()
        for line in p.read_text(errors="replace").splitlines():
            f = asm.feed(line, 0.0)
            if f:
                out.append(f)
        return out
    sky: dict = {}
    for line in p.read_text(errors="replace").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if not isinstance(m, dict):
            continue
        if m.get("class") == "SKY":
            sky = sky_info(m)
        elif m.get("class") == "TPV":
            f = tpv_fix(m, sky, 0.0)
            if f:
                out.append(f)
        elif "t" in m and "lat" in m and "lon" in m:
            out.append(Fix(t_gps=int(m["t"]), mono=0.0, lat=m["lat"], lon=m["lon"], alt=m.get("alt"), speed=m.get("speed"),
                           heading=m.get("heading"), climb=m.get("climb"), hdop=m.get("hdop"), sats=m.get("sats"),
                           mode=m.get("mode", 3)))
    return out


class ReplaySource(GpsSource):
    """Reproduz um arquivo gravado respeitando os intervalos originais ÷ speed. rebase=True traz as horas para "agora"."""
    name = "replay"

    def __init__(self, path: str, speed: float = 1.0, loop: bool = False, rebase: bool = True):
        self.path, self.speed, self.loop, self.rebase = path, max(0.01, float(speed)), loop, rebase
        self.items: list[Fix] = []
        self.i = 0
        self.t0 = 0
        self.start = 0.0
        self.offset = 0

    def open(self) -> None:
        try:
            items = load_track(self.path)
        except OSError as e:
            raise SourceError(f"replay {self.path}: {e}") from e
        last = None
        for f in items:                      # posições sem hora seguem a anterior (200 ms) para o ritmo não quebrar
            if f.t_gps is None:
                f.t_gps = (last + 200) if last is not None else 0
            last = f.t_gps
        self.items = items
        self._restart()

    def _restart(self) -> None:
        self.i = 0
        self.start = time.monotonic()
        self.t0 = self.items[0].t_gps if self.items else 0
        self.offset = int(time.time() * 1000) - self.t0 if self.rebase else 0

    def read(self, timeout: float) -> list[Fix]:
        if not self.items:
            time.sleep(timeout)
            return []
        out: list[Fix] = []
        end = time.monotonic() + timeout
        while not out:
            now = time.monotonic()
            el = (now - self.start) * self.speed * 1000
            while self.i < len(self.items) and self.items[self.i].t_gps - self.t0 <= el:
                s = self.items[self.i]
                out.append(Fix(t_gps=s.t_gps + self.offset if s.t_gps else None, mono=now, lat=s.lat, lon=s.lon, alt=s.alt,
                               speed=s.speed, heading=s.heading, climb=s.climb, hdop=s.hdop, sats=s.sats, mode=s.mode))
                self.i += 1
            if out:
                break
            if self.i >= len(self.items):
                if self.loop:
                    self._restart()
                    continue
                time.sleep(max(0.0, end - now))
                break
            wait = (self.items[self.i].t_gps - self.t0 - el) / 1000 / self.speed
            if now + wait > end:
                time.sleep(max(0.0, end - now))
                break
            time.sleep(max(0.0, wait))
        return out

    def info(self) -> dict:
        return {"connected": bool(self.items), "device": self.path, "driver": "replay", "sats_seen": None}


class AutoSource(GpsSource):
    """gps.source = "auto": usa o gpsd. Se a conexão for recusada 3 vezes seguidas e existir gps.device, lê a serial
    direto. Se o gpsd responde mas não lista receptor, fica nele (estado "desconectado") esperando o hotplug."""

    def __init__(self, host: str, port: int, device: str | None):
        self.gpsd = GpsdSource(host, port)
        self.device = device
        self.cur: GpsSource = self.gpsd
        self.refused = 0
        self.name = "gpsd"

    def open(self) -> None:
        if self.cur is self.gpsd:
            try:
                self.gpsd.open()
                self.refused = 0
                return
            except SourceError as e:
                self.refused = self.refused + 1 if e.refused else 0
                if not (self.refused >= 3 and self.device and Path(self.device).exists()):
                    raise
            log.warning("gpsd fora do ar (3 recusas): lendo %s direto", self.device)
            self.cur, self.name = NmeaSerialSource(self.device), "nmea"
        try:
            self.cur.open()
        except SourceError:
            self.cur, self.name, self.refused = self.gpsd, "gpsd", 0      # serial falhou: volta a tentar o gpsd
            raise

    def read(self, timeout: float) -> list[Fix]:
        try:
            return self.cur.read(timeout)
        except SourceError:
            if self.cur is not self.gpsd:
                self.cur.close()
                self.cur, self.name, self.refused = self.gpsd, "gpsd", 0
            raise

    def set_rate(self, hz: float) -> bool:
        return self.cur.set_rate(hz)

    def info(self) -> dict:
        return self.cur.info()

    def close(self) -> None:
        self.cur.close()


def open_source(cfg: dict) -> GpsSource:
    """Cria (sem abrir) a fonte pedida em cfg (seção [gps])."""
    src = str(cfg.get("source", "auto")).lower()
    if not cfg.get("enabled", True) or src == "off":
        return NullSource()
    if src == "gpsd":
        return GpsdSource(cfg.get("gpsd_host", "127.0.0.1"), cfg.get("gpsd_port", 2947))
    if src == "nmea":
        return NmeaSerialSource(cfg.get("device", "/dev/gps0"))
    if src == "replay":
        return ReplaySource(cfg.get("replay_file", ""), cfg.get("replay_speed", 1.0), cfg.get("replay_loop", False))
    return AutoSource(cfg.get("gpsd_host", "127.0.0.1"), cfg.get("gpsd_port", 2947), cfg.get("device"))
