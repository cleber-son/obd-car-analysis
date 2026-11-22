"""GpsHub: a única ponte entre o coletor e o GPS. Engole qualquer erro: o GPS nunca derruba o coletor.

O laço principal chama pump() a cada ciclo: carimba a hora de cada posição no relógio do coletor, escolhe o que gravar
(1 Hz andando, 1 a cada 30 s parado, tudo nas rajadas), alimenta os motores ao vivo e escreve /run/obd/gps.json.
Cada fase acrescenta uma linha em LIVE_ENGINES / FINALIZERS (docs/GPS-ARQUITETURA.md, seção 5.3).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from .. import config
from . import FLAG_BURST, FLAG_STOPPED, Fix, good
from . import places, routes, segments, store, trip
from .reader import GpsReader

log = logging.getLogger("gps")

# classes com feed(ctx) -> list[Alert], live() -> dict, on_trip_start(tid, ts, merged), shift(off); uma linha por fase
LIVE_ENGINES: list = []
# funções f(con, tid, ctx) chamadas em ordem por finalize_trip(), cada uma no seu try/except; uma linha por fase
FINALIZERS: list = []

STOPPED_KMH = 1.5            # abaixo disso por 10 s = parado
STOPPED_AFTER_MS = 10_000
SPEED_FRESH_S = 1.5          # best_speed só usa o GPS com posição deste tempo para cá


def gps_cfg(cfg: dict) -> dict:
    """Seção [gps] com os padrões (aceita a configuração inteira ou só a seção)."""
    sec = cfg.get("gps", cfg) if isinstance(cfg, dict) else {}
    return {**config.DEFAULTS["gps"], **(sec or {})}


class Decimator:
    """O que gravar: a primeira posição de cada segundo cheio andando; 1 a cada 30 s parado (flags bit 1);
    tudo durante uma rajada (flags bit 0)."""

    def __init__(self, store_hz: float = 1, stopped_every_s: float = 30):
        self.period = max(1, int(1000 / (store_hz or 1)))
        self.stopped_ms = int(stopped_every_s * 1000)
        self.last_ts: int | None = None
        self.slow_since: int | None = None
        self.burst_until = 0

    def pick(self, f: Fix) -> int | None:
        """Flags com que a posição deve ser gravada, ou None para descartar."""
        if f.ts <= self.burst_until:
            self.last_ts = f.ts
            return FLAG_BURST
        if f.speed is not None and f.speed < STOPPED_KMH:
            if self.slow_since is None:
                self.slow_since = f.ts
        else:
            self.slow_since = None
        if self.slow_since is not None and f.ts - self.slow_since >= STOPPED_AFTER_MS:
            if self.last_ts is None or f.ts - self.last_ts >= self.stopped_ms:
                self.last_ts = f.ts
                return FLAG_STOPPED
            return None
        if self.last_ts is None or f.ts // self.period != self.last_ts // self.period:
            self.last_ts = f.ts
            return 0
        return None

    def shift(self, off: int) -> None:
        if self.last_ts is not None:
            self.last_ts += off
        if self.slow_since is not None:
            self.slow_since += off
        if self.burst_until:
            self.burst_until += off


class NoObdKm:
    """Km sem OBD (seção 9.13): carro andando pelo GPS (> 10 km/h por 30 s) sem viagem aberta (adaptador fora, Bluetooth
    caído). Um geo_event 'km_sem_obd' por sessão, com o valor em km atualizado a cada minuto; extras.odometer() soma."""
    MIN_KMH, QUALIFY_MS, SAVE_MS = 10.0, 30_000, 60_000

    def __init__(self, hub):
        self.con = hub.con
        self.ev_id: int | None = None
        self.km = self.pend = 0.0
        self.fast_since: int | None = None
        self.active = False
        self.last: int | None = None
        self.last_save: int | None = None

    def on_trip_start(self, tid, ts, merged) -> None:
        pass

    def shift(self, off: int) -> None:
        for k in ("fast_since", "last", "last_save"):
            if getattr(self, k) is not None:
                setattr(self, k, getattr(self, k) + off)

    def _save(self, ts: int) -> None:
        if self.ev_id is None:
            self.ev_id = store.add_event(self.con, self.fast_since or ts, "km_sem_obd", value=round(self.km, 3),
                                         detail="carro andando pelo GPS sem viagem do OBD (adaptador fora ou Bluetooth caído)")
        else:
            self.con.execute("UPDATE geo_event SET value=? WHERE id=?", (round(self.km, 3), self.ev_id))
        self.last_save = ts

    def feed(self, ctx) -> list:
        f = ctx.fix
        prev, self.last = self.last, f.ts
        if ctx.trip_id is not None:                   # viagem aberta: quem conta é o OBD
            if self.active:
                self._save(f.ts)
            self.active, self.fast_since, self.pend = False, None, 0.0
            return []
        spd = f.speed or 0.0
        dt = (f.ts - prev) / 1000 if prev is not None else 0.0
        step = spd * dt / 3600 if 0 < dt <= 2 and spd >= 1.5 else 0.0
        if self.active:
            self.km += step
        else:
            if spd >= self.MIN_KMH:
                if self.fast_since is None:
                    self.fast_since, self.pend = f.ts, 0.0
                self.pend += step
                if f.ts - self.fast_since >= self.QUALIFY_MS:
                    self.active, self.km = True, self.km + self.pend
                    self.pend = 0.0
            else:
                self.fast_since, self.pend = None, 0.0
        if self.active and (self.ev_id is None or self.last_save is None or f.ts - self.last_save >= self.SAVE_MS):
            self._save(f.ts)
        return []

    def live(self) -> dict:
        return {}


# ---- F2: diário de bordo, lugares e estacionamento
FINALIZERS += [trip.finalize]
LIVE_ENGINES += [places.ParkEngine, places.GeofenceEngine, NoObdKm]

# ---- F4: rotas e trechos de referência
FINALIZERS += [routes.assign_trip, segments.scan_trip]


def make_ctx(con, cfg: dict, **kw) -> SimpleNamespace:
    """ctx fora do coletor (sim.to_db, obdctl gps reanalyze): mesmos campos de GpsHub.ctx()."""
    base = dict(fix=None, values={}, value_ts={}, engine_on=False, trip_id=None, ts=int(time.time() * 1000), reader=None,
                cfg=gps_cfg(cfg), con=con, geo=None, models={})
    base.update(kw)
    return SimpleNamespace(**base)


def run_finalizers(con, tid: int, ctx) -> None:
    for fn in FINALIZERS:
        try:
            fn(con, tid, ctx)
        except Exception as e:                       # uma análise com problema não impede as outras
            log.warning("gps: finalizador %s da viagem %s falhou: %r", getattr(fn, "__name__", fn), tid, e)


class GpsHub:
    def __init__(self, cfg, con, clock, state, event, queue_push, run_dir):
        self.cfg = gps_cfg(cfg)
        self.con, self.clock, self.state = con, clock, state
        self.event, self.queue_push = event, queue_push
        self.run_dir = Path(run_dir)
        self.enabled = bool(self.cfg.get("enabled")) and str(self.cfg.get("source", "auto")).lower() != "off"
        self.latency = int(self.cfg.get("latency_ms", 120))
        self.max_hdop, self.min_sats = float(self.cfg.get("max_hdop", 5.0)), int(self.cfg.get("min_sats", 5))
        self.reader: GpsReader | None = None
        self.dec = Decimator(self.cfg.get("store_hz", 1), self.cfg.get("stopped_every_s", 30))
        self.desloc: float | None = None
        self._dsamples: list[float] = []
        self._epoch = -1
        self.time_fixes = 0
        self.first_time_mono: float | None = None
        self.prev_state: str | None = None
        self._ttff_logged = False
        self._live: dict = {"state": "desligado" if not self.enabled else "desconectado"}
        self._warned: dict[str, float] = {}
        self._last_model = time.monotonic()
        self._json_written = False
        self.engines = []
        for E in LIVE_ENGINES:
            try:
                self.engines.append(E(self))
            except Exception as e:
                self._warn("engine", e)

    # ------------------------------------------------------------ util
    def _warn(self, key: str, e: Exception) -> None:
        now = time.monotonic()
        if now - self._warned.get(key, -1e9) > 60:
            self._warned[key] = now
            log.warning("gps: %s falhou: %r", key, e)

    def ctx(self, **kw) -> SimpleNamespace:
        base = dict(fix=None, values={}, value_ts={}, engine_on=False, trip_id=None, ts=self.clock.now(), reader=self.reader,
                    cfg=self.cfg, con=self.con, geo=None, models={})
        base.update(kw)
        return SimpleNamespace(**base)

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.reader = GpsReader(self.cfg)
            self.reader.start()
        except Exception as e:
            self.reader = None
            self._warn("start", e)

    def stop(self) -> None:
        try:
            if self.reader:
                self.reader.stop()
        except Exception as e:
            self._warn("stop", e)

    # ------------------------------------------------------------ hora de cada posição (seção 3.4)
    def _stamp(self, f: Fix, now_clock: int, now_mono: float) -> None:
        arrival = now_clock - (now_mono - f.mono) * 1000          # relógio do coletor no instante da chegada
        if f.t_gps is not None:
            if len(self._dsamples) < 10:
                self._dsamples.append(arrival - f.t_gps)
                self.desloc = statistics.median(self._dsamples) - self.latency
            f.ts = int(round(f.t_gps + self.desloc))
        else:
            f.ts = int(round(arrival - self.latency))

    # ------------------------------------------------------------ ciclo
    def _commands(self) -> None:
        p = self.run_dir / "gps_cmd"
        if not p.exists():
            return
        try:
            txt = p.read_text()
        finally:
            p.unlink(missing_ok=True)
        for line in txt.splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            if c.get("cmd") == "rate" and self.reader:
                self.reader.request_rate(float(c.get("hz", 5)))
            elif c.get("cmd") == "burst":
                self.start_burst(float(c.get("s", 30)))
            elif c.get("cmd") == "reload_places":
                for e in self.engines:
                    if hasattr(e, "reload"):
                        e.reload()
            else:
                log.debug("gps: comando ignorado nesta versão: %s", c)

    def start_burst(self, seconds: float) -> None:
        """Janela de desempenho: grava a taxa cheia por 'seconds', incluindo os 5 s anteriores (tirados do anel)."""
        self.dec.burst_until = self.clock.now() + int(seconds * 1000)
        if self.reader:
            prev = [f for f in self.reader.recent(5) if f.ts]
            store.insert_fixes(self.con, prev, None, FLAG_BURST)

    def pump(self, ts: int, values: dict, value_ts: dict, engine_on: bool, trip_id: int | None) -> list:
        if not self.enabled:
            if not self._json_written:
                self._write_json({"state": "desligado"}, engine_on, trip_id)
            return []
        alerts: list = []
        try:
            self._commands()
        except Exception as e:
            self._warn("comandos", e)
        try:
            r = self.reader
            if r is None:
                self._live = {"state": "desconectado"}
                self._write_json({"state": "desconectado"}, engine_on, trip_id)
                return []
            if r.epoch != self._epoch:                     # reconexão da fonte: recalcula o deslocamento da hora
                self._epoch, self._dsamples, self.desloc = r.epoch, [], None
            fixes = r.drain()
            now_clock, now_mono = self.clock.now(), time.monotonic()
            groups: dict[int, list[Fix]] = {}
            for f in fixes:
                self._stamp(f, now_clock, now_mono)
                if f.t_gps is not None:
                    self.time_fixes += 1
                    if self.first_time_mono is None:
                        self.first_time_mono = f.mono
                fl = self.dec.pick(f)
                if fl is not None:
                    groups.setdefault(fl, []).append(f)
            for fl, fs in groups.items():
                store.insert_fixes(self.con, fs, trip_id, fl)
            if self.engines and fixes:
                for f in fixes:
                    if not good(f, self.max_hdop, self.min_sats):
                        continue
                    c = self.ctx(fix=f, values=values, value_ts=value_ts, engine_on=engine_on, trip_id=trip_id, ts=f.ts)
                    for e in self.engines:
                        try:
                            alerts += e.feed(c) or []
                        except Exception as ex:
                            self._warn(type(e).__name__, ex)
            if self.engines and time.monotonic() - self._last_model > 60:
                self._last_model = time.monotonic()
                for e in self.engines:
                    try:
                        m = getattr(e, "model", None)
                        if m:
                            for key, obj in (m() or {}).items():
                                store.model_set(self.con, key, obj, ts)
                    except Exception as ex:
                        self._warn("modelo", ex)
            st = r.status()
            self._transitions(st)
            self._live = self._make_live(st)
            self._write_json(st, engine_on, trip_id)
        except Exception as e:
            self._warn("pump", e)
        return alerts

    def _transitions(self, st: dict) -> None:
        state, prev = st["state"], self.prev_state
        self.prev_state = state
        on_now, on_before = state in ("ok", "sem_sinal"), prev in ("ok", "sem_sinal")
        if on_now and not on_before:
            self._ttff_logged = False
            self.event("info", "gps_conectado", f"{st.get('src') or ''} {st.get('device') or ''}".strip())
        elif on_before and not on_now:
            self.event("warn", "gps_removido", f"receptor sumiu ({st.get('src') or ''})")
        if state == "ok" and not self._ttff_logged and st.get("ttff_s") is not None:
            self._ttff_logged = True
            self.event("info", "gps_primeiro_sinal", f"{st['ttff_s']:.0f} s até a primeira posição")
            if not self.state.get("gps_first_fix_ts"):            # primeiro sinal da vida do sistema
                self.state.set("gps_first_fix_ts", self.clock.now())
                self.queue_push("GPS funcionando ✅", f"Primeira posição recebida em {st['ttff_s']:.0f} s. O mapa começa a ser preenchido sozinho.", "info")

    def _make_live(self, st: dict) -> dict:
        state = st["state"]
        if state in ("desligado", "desconectado"):
            return {"state": state}
        d = {"state": state, "src": st.get("src"), "sats": st.get("sats"), "clock": getattr(self.clock, "source", None)}
        f = self.reader.latest() if self.reader else None
        if state == "ok" and f is not None:
            spd = f.speed
            d.update(mode=f.mode, hdop=f.hdop, hz=st.get("hz"), age_s=st.get("age_s"),
                     speed=round(spd, 1) if spd is not None else None,
                     heading=round(f.heading) if f.heading is not None and (spd or 0) >= store.HEADING_MIN_KMH else None,
                     alt=round(f.alt) if f.alt is not None else None)
        for e in self.engines:
            try:
                d.update(e.live() or {})
            except Exception as ex:
                self._warn(type(e).__name__, ex)
        for k in ("lat", "lon"):                         # live.json é público: nunca leva posição
            d.pop(k, None)
        return d

    def _write_json(self, st: dict, engine_on: bool, trip_id) -> None:
        """/run/obd/gps.json (privado; vira /api/gps/live). Arquivo temporário + os.replace, como o live.json."""
        try:
            f = self.reader.latest() if self.reader else None
            r = lambda v, n: round(v, n) if v is not None else None
            d = {"ts": self.clock.now(), "state": st.get("state"), "lat": r(f.lat, 6) if f else None, "lon": r(f.lon, 6) if f else None,
                 "alt": r(f.alt, 1) if f else None, "speed": r(f.speed, 1) if f else None,
                 "heading": r(f.heading, 0) if f and (f.speed or 0) >= store.HEADING_MIN_KMH else None,
                 "sats": f.sats if f else None, "hdop": f.hdop if f else None, "fix_ts": f.ts if f else None,
                 "engine_on": engine_on, "trip_id": trip_id, "place": None, "parked": None,
                 "status": {k: st.get(k) for k in ("state", "src", "device", "driver", "mode", "sats", "hdop", "hz", "age_s",
                                                   "ttff_s", "reconnects", "rate_hz")},
                 "clock": getattr(self.clock, "source", None)}
            for e in self.engines:                   # lugar atual e estacionamento (privados: levam posição e nome)
                fn = getattr(e, "private", None)
                if fn:
                    try:
                        d.update({k: v for k, v in (fn() or {}).items() if k in ("place", "parked")})
                    except Exception as ex:
                        self._warn(type(e).__name__, ex)
            p = self.run_dir / "gps.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False))
            os.replace(tmp, p)
            self._json_written = True
        except Exception as e:
            self._warn("gps.json", e)

    # ------------------------------------------------------------ consultas do coletor
    def live(self) -> dict:
        """Campo "gps" do live.json (público, sem posição)."""
        return dict(self._live)

    def best_speed(self, obd_speed: float | None) -> float | None:
        """Velocidade do GPS se a posição é recente (< 1,5 s) e boa; senão a do OBD (× fator do velocímetro, F5)."""
        if not self.enabled or self.reader is None:
            return obd_speed
        try:
            f = self.reader.latest()
            if f is None or f.speed is None or time.monotonic() - f.mono > SPEED_FRESH_S or not good(f, self.max_hdop, self.min_sats):
                return obd_speed
            if obd_speed == 0:                      # OBD diz parado: é parado (o ruído do GPS não inventa km)
                return obd_speed
            return 0.0 if f.speed < STOPPED_KMH else f.speed
        except Exception as e:
            self._warn("best_speed", e)
            return obd_speed

    def clock_offset(self) -> int:
        """Correção direta pela hora do GPS (reserva, seção 3.3): só com 10 posições com hora válida, 20 s depois da
        primeira, e com o relógio ainda não sincronizado. Devolve o offset a aplicar (0 = nada)."""
        try:
            if not self.enabled or self.reader is None or self.clock.synced:
                return 0
            now = time.monotonic()
            if self.time_fixes < 10 or self.first_time_mono is None or now - self.first_time_mono < 20:
                return 0
            f = self.reader.latest()
            if f is None or f.t_gps is None or now - f.mono > 5:
                return 0
            utc = int(f.t_gps + self.latency + (now - f.mono) * 1000)
            off = self.clock.sync_from_gps(utc)
            log.warning("gps: relógio acertado pela hora do GPS (offset %d ms)", off)
            return off
        except Exception as e:
            self._warn("clock_offset", e)
            return 0

    def shift_clock(self, off: int, start: int) -> None:
        """apply_clock_offset do coletor: mesma transação para as tabelas do GPS; em memória, deslocamento e motores."""
        store.shift(self.con, off, start)
        if self.desloc is not None:
            self.desloc += off
            self._dsamples = [x + off for x in self._dsamples]
        if self.reader:
            with self.reader.lock:
                for f in self.reader.ring:
                    if f.ts:
                        f.ts += off
        self.dec.shift(off)
        for e in self.engines:
            try:
                e.shift(off)
            except Exception as ex:
                self._warn(type(e).__name__, ex)

    def on_trip_start(self, trip_id: int, ts: int, merged: bool) -> None:
        for e in self.engines:
            try:
                e.on_trip_start(trip_id, ts, merged)
            except Exception as ex:
                self._warn(type(e).__name__, ex)

    def trip_distance(self, tid: int, a: int, b: int, obd_km: float) -> dict | None:
        """Distância híbrida GPS + OBD (seção 9.3). None sem nenhuma posição boa na viagem: a distância continua
        sendo a do OBD, exatamente como antes do GPS."""
        if not self.enabled:
            return None
        try:
            return trip.trip_distance(self.con, a, b, obd_km, self.cfg)
        except Exception as e:
            self._warn("trip_distance", e)
            return None

    def finalize_trip(self, tid: int) -> None:
        run_finalizers(self.con, tid, self.ctx(trip_id=tid))

    def summary(self, tid: int) -> dict:
        """Complemento do resumo da viagem no celular: {"route": "Casa → Trabalho" | None, "parts": [...]}.
        Os nomes dos lugares vão no título se gps.summary_names (padrão: sim; decisão do dono)."""
        try:
            return trip.summary(self.con, tid, bool(self.cfg.get("summary_names", True)))
        except Exception as e:
            self._warn("summary", e)
            return {"route": None, "parts": []}
