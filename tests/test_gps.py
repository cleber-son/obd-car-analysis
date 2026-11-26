"""Testes do GPS — Fase 1 (fonte, gravação e relógio), sem hardware. docs/GPS-ARQUITETURA.md, seção 10.3."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, socket, sqlite3, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from obdpi import config, db, timesrc
from obdpi.gps import Fix, FLAG_STOPPED, good, valid_time
from obdpi.gps import geo, nmea, store
from obdpi.gps.hub import Decimator, GpsHub
from obdpi.gps.reader import GpsReader
from obdpi.gps.sim import FakeGpsd
from obdpi.gps.source import GpsdSource, GpsSource, ReplaySource, SourceError

FIX = Path(__file__).parent / "fixtures" / "gps"
DAY3 = 3 * 86400_000


# ---------------------------------------------------------------- utilitários
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_cfg(base: Path, **gps) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": str(base / "obd.db"), "state_path": str(base / "state.json"), "backup_dir": str(base / "backup")}
    cfg["runtime"]["live_sock"] = str(base / "run" / "live.sock")
    cfg["gps"].update({"source": "gpsd", "gpsd_port": free_port()}, **gps)     # porta sem ninguém: "receptor ausente"
    return cfg


class FakeReader:
    """Substitui o GpsReader: entrega as posições que o teste mandar."""
    def __init__(self, state="ok"):
        self.lock = threading.Lock()
        self.ring, self.pending, self.epoch, self._state = [], [], 1, state
    def push(self, fixes):
        self.pending += fixes
        self.ring += fixes
    def drain(self):
        out, self.pending = self.pending, []
        return out
    def latest(self):
        return self.ring[-1] if self.ring else None
    def recent(self, s):
        return [f for f in self.ring if f.mono >= time.monotonic() - s]
    def status(self):
        f = self.latest()
        return {"state": self._state, "src": "fake", "device": "/dev/fake", "driver": "fake", "mode": f.mode if f else None,
                "sats": f.sats if f else None, "hdop": f.hdop if f else None, "hz": 1.0, "age_s": 0.1, "ttff_s": 30.0,
                "reconnects": 0, "rate_hz": 5}
    def stop(self): pass
    def request_rate(self, hz): pass


def collector(cfg):
    from obdpi.collector.main import Collector
    col = Collector(cfg)
    if col.gps and col.gps.reader:
        col.gps.reader.stop()
    return col


def fixes_now(n, *, every_s=1.0, t_shift=0, speed=30.0, age_first=24.0, lat=-23.5, lon=-46.6):
    """n posições que chegaram de age_first s atrás até agora, com a hora UTC verdadeira (+ t_shift)."""
    now_m, real = time.monotonic(), time.time() * 1000
    out = []
    for i in range(n):
        mono = now_m - age_first + i * every_s
        out.append(Fix(t_gps=int(real - (now_m - mono) * 1000 - 120 + t_shift), mono=mono, lat=lat + i * 1e-5, lon=lon,
                       alt=760.0, speed=speed, heading=90.0, hdop=0.9, sats=9, mode=3))
    return out


def has_key(obj, keys) -> bool:
    if isinstance(obj, dict):
        return any(k in keys or has_key(v, keys) for k, v in obj.items())
    if isinstance(obj, list):
        return any(has_key(v, keys) for v in obj)
    return False


# ---------------------------------------------------------------- NMEA / UBX
def test_nmea_parse_and_checksum():
    assert nmea.checksum("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,") == "47"
    g = nmea.parse("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert g["type"] == "GGA" and abs(g["lat"] - 48.1173) < 1e-4 and abs(g["lon"] - 11.516667) < 1e-5 and g["sats"] == 8 and g["alt"] == 545.4
    r = nmea.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
    assert r["valid"] and abs(r["speed"] - 22.4 * 1.852) < 1e-6 and r["track"] == 84.4
    assert nmea.parse("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6B") is None     # checksum errado
    assert nmea.parse("$GPRMC,123519,A,4807.038,N") is None                                                # sem checksum
    lines = (FIX / "sample.nmea").read_text().splitlines()
    parsed = [nmea.parse(x) for x in lines]
    assert sum(1 for p in parsed if p and p["type"] == "RMC" and p["lat"] and p["lat"] < 0) >= 10          # S = negativo
    assert any(p and p["type"] == "GSA" and p["mode"] == 3 for p in parsed)
    assert any(x.startswith("$GN") for x in lines) and all(p is None for x, p in zip(lines, parsed) if "GSV" in x or "lixo" in x)
    v = [p for p in parsed if p and p["type"] == "RMC" and not p["valid"]]
    assert len(v) == 1 and v[0]["lat"] is None                                                             # campos vazios


def test_nmea_assembler_epochs():
    asm = nmea.NmeaAssembler()
    out = [f for i, x in enumerate((FIX / "sample.nmea").read_text().splitlines()) if (f := asm.feed(x, i * 0.01))]
    assert len(out) == 12, len(out)                       # clássico + 10 épocas a 5 Hz + volta do sinal; a época 'V' não sai
    five = out[1:11]
    assert [b.t_gps - a.t_gps for a, b in zip(five, five[1:])] == [200] * 9                    # mesmo GN, épocas certas
    assert five[0].t_gps == 1795348800000                                                       # 2026-11-22 12:00:00.00 UTC
    assert abs(five[0].speed - 30.0) < 0.01 and five[0].heading == 90.0 and five[0].alt == 762.0 and five[0].sats == 9
    assert five[-1].mode == 3 and out[-1].sats == 7
    # o simulador gera de volta e o montador entende
    f = Fix(t_gps=1795348800400, mono=0, lat=-23.51234, lon=-46.65432, alt=750.5, speed=42.0, heading=181.0, hdop=0.9, sats=9, mode=3)
    a2 = nmea.NmeaAssembler()
    got = [a2.feed(x, 0) for x in nmea.to_nmea(f)]
    g = [x for x in got if x][0]
    assert g.t_gps == f.t_gps and abs(g.lat - f.lat) < 1e-6 and abs(g.lon - f.lon) < 1e-6 and abs(g.speed - 42.0) < 0.01


def test_ubx_frames():
    assert nmea.ubx_cfg_rate(5) == bytes.fromhex("B5 62 06 08 06 00 C8 00 01 00 01 00 DE 6A")      # 200 ms
    assert nmea.ubx_cfg_rate(1) == bytes.fromhex("B5 62 06 08 06 00 E8 03 01 00 01 00 01 39")      # 1000 ms
    n5 = nmea.ubx_cfg_nav5(4)
    assert n5[:6] == bytes.fromhex("B5 62 06 24 24 00") and len(n5) == 8 + 36 and n5[6:9] == bytes([1, 0, 4])


# ---------------------------------------------------------------- fontes
def test_gpsd_source_with_fake_server():
    lines = (FIX / "gpsd-session.jsonl").read_text().splitlines()
    srv = FakeGpsd(lines, line_gap_s=0.03)
    srv.start()
    try:
        src = GpsdSource("127.0.0.1", srv.port)
        src.open()
        fixes, seen = [], []
        end = time.monotonic() + 4
        while time.monotonic() < end and len(fixes) < 5:
            fixes += src.read(0.3)
            seen.append(src.info()["connected"])
        assert len(fixes) == 5, fixes                         # mode 1 não vira posição
        a = fixes[0]
        assert a.t_gps == 1795348802000 and a.mode == 3 and a.alt == 762.1 and abs(a.speed - 8.33 * 3.6) < 1e-6 and a.hdop == 0.92 and a.sats == 9
        assert fixes[2].mode == 2 and fixes[2].alt == 762.0   # 'alt' das versões antigas
        assert fixes[3].sats == 6 and fixes[3].hdop == 1.4    # satélites contados pela lista 'used'
        assert src.info()["removed"] == 1 and src.info()["connected"] and src.info()["device"] == "/dev/ttyACM0"
        assert True in seen
        assert src.set_rate(5)
        time.sleep(0.3)
        assert any("WATCH" in r for r in srv.received)
        assert any(r.startswith("?DEVICE=") and '"cycle": 0.2' in r for r in srv.received), srv.received
        src.close()
    finally:
        srv.stop()
    # sem gpsd: recusa é SourceError(refused=True), não exceção solta
    try:
        GpsdSource("127.0.0.1", free_port()).open()
        assert False
    except SourceError as e:
        assert e.refused


def test_gpsd_partial_sky_and_repeated_tpv():
    """gpsd real (Trixie, VK-172): SKY só com DOPs não apaga a contagem; 2 TPV da mesma época viram uma posição."""
    src = GpsdSource()
    src._handle({"class": "SKY", "hdop": 1.1, "uSat": 9, "nSat": 14}, 0)
    src._handle({"class": "SKY", "hdop": 2.4}, 0)                         # parcial: sem satélites
    assert src.sky == {"hdop": 2.4, "sats": 9, "seen": 14}
    tpv = {"class": "TPV", "mode": 3, "time": "2026-11-26T12:00:00.000Z", "lat": -23.67, "lon": -46.61}
    a = src._handle({**tpv, "speed": 1.0}, 0)
    assert a and a.sats == 9 and a.hdop == 2.4
    assert src._handle({**tpv, "speed": 1.0}, 0) is None                 # mesma época: fora
    b = src._handle({**tpv, "time": "2026-11-26T12:00:00.200Z"}, 0)      # sem velocidade na 1ª…
    assert b and src._handle({**tpv, "time": "2026-11-26T12:00:00.200Z", "speed": 2.0}, 0).speed == 7.2   # …a 2ª com ela passa
    assert src._handle({**tpv, "time": "2026-11-26T12:00:00.400Z"}, 0)


def test_replay_source_rebase():
    p = Path(tempfile.mkdtemp()) / "r.jsonl"
    t0 = 1700000000000
    p.write_text("\n".join(json.dumps({"t": t0 + i * 1000, "lat": -23.5 + i * 1e-4, "lon": -46.6, "speed": 20, "hdop": 1, "sats": 8})
                           for i in range(10)) + "\n")
    src = ReplaySource(str(p), speed=40, rebase=True)
    src.open()
    got, end = [], time.monotonic() + 3
    while len(got) < 10 and time.monotonic() < end:
        got += src.read(0.2)
    assert len(got) == 10
    assert abs(got[0].t_gps - time.time() * 1000) < 3000                     # trazido para "agora"
    assert [b.t_gps - a.t_gps for a, b in zip(got, got[1:])] == [1000] * 9   # intervalos originais preservados
    assert got[-1].mono - got[0].mono < 1.0                                  # 9 s de viagem em ~0,23 s (speed 40)
    assert src.read(0.1) == [] and src.info()["connected"]


# ---------------------------------------------------------------- leitor e gravação
def test_reader_filters_and_decimation():
    r = GpsReader(dict(config.DEFAULTS["gps"]))
    base = 1795348800000
    mk = lambda i, lat=-23.5, spd=30.0, mode=3: Fix(t_gps=base + i * 200, mono=100 + i * 0.2, lat=lat, lon=-46.6 + i * 1.7e-5, speed=spd,
                                                    hdop=0.9, sats=9, mode=mode)
    assert not r._accept(mk(0, mode=1))                         # sem posição
    assert [r._accept(mk(i)) for i in range(1, 5)] == [False, False, False, True]    # 3 primeiras depois de adquirir sinal
    assert not r._accept(mk(5, lat=-23.49))                     # 1,1 km em 0,2 s: salto impossível
    assert r._accept(mk(6))
    far = Fix(t_gps=base + 1600, mono=101.6, lat=-23.5, lon=-46.6 + 7 * 1.7e-5 + 0.0015, speed=2.0, hdop=0.9, sats=9, mode=3)
    assert not r._accept(far)                                   # 150 m de salto parado
    assert len(r.drain()) == 2 and r.drain() == []
    # gravação: 5 Hz parado → 1 por segundo nos 10 s iniciais, depois 1 a cada 30 s; andando → 1 Hz
    d = Decimator(1, 30)
    stopped = [d.pick(Fix(t_gps=None, mono=0, lat=0, lon=0, speed=0.3, ts=base + i * 200)) for i in range(5 * 120)]
    kept = [x for x in stopped if x is not None]
    assert 13 <= len(kept) <= 15, len(kept)
    assert kept.count(FLAG_STOPPED) in (3, 4)
    moving = [d.pick(Fix(t_gps=None, mono=0, lat=0, lon=0, speed=40, ts=base + 120_000 + i * 200)) for i in range(5 * 60)]
    assert sum(1 for x in moving if x is not None) in (60, 61) and all(x in (None, 0) for x in moving)


class FlakySource(GpsSource):
    name = "flaky"
    def __init__(self):
        self.opens = self.reads = 0
    def open(self):
        self.opens += 1
        if self.opens <= 2:
            raise SourceError("gpsd ainda não subiu", refused=True)
    def read(self, timeout):
        self.reads += 1
        if self.reads == 2:
            raise RuntimeError("bug qualquer")
        if self.reads == 4:
            raise SourceError("conexão caiu")
        time.sleep(0.01)
        m = time.monotonic()
        return [Fix(t_gps=int(time.time() * 1000), mono=m, lat=-23.5, lon=-46.6, speed=0.0, hdop=1, sats=8, mode=3)]
    def info(self):
        return {"connected": True, "device": "x", "driver": "x", "sats_seen": 8}


def test_reader_survives_source_errors():
    src = FlakySource()
    r = GpsReader(dict(config.DEFAULTS["gps"]), source=src)
    r.BACKOFF0, r.BACKOFF_MAX = 0.02, 0.05
    r.start()
    try:
        got, end = [], time.monotonic() + 5
        while len(got) < 3 and time.monotonic() < end:
            got += r.drain()
            time.sleep(0.05)
        assert got and r.is_alive()
        assert src.opens >= 4 and r.reconnects >= 2 and r.epoch >= 2
        assert r.status()["state"] == "ok"
    finally:
        r.stop()
        r.join(2)
    assert not r.is_alive()


def test_geo_haversine_bearing_cell_simplify():
    assert abs(geo.haversine_m(0, 0, 1, 0) - 111195) < 10
    assert abs(geo.haversine_m(-23.5, -46.6, -23.5, -46.59) - 1020) < 5
    assert abs(geo.bearing_deg(-23.5, -46.6, -23.5, -46.59) - 90) < 0.01 and abs(geo.bearing_deg(-23.5, -46.6, -23.6, -46.6) - 180) < 0.01
    assert geo.ang_diff(350, 10) == 20 and geo.ang_diff(90, 270) == 180
    c = geo.cell(-23.5501, -46.6331)
    assert c == int((-23.5501 + 90) * 1000) * 1_000_000 + int((-46.6331 + 180) * 1000) == 66449133366
    nb = geo.neighbors(c)
    assert len(nb) == 9 and c in nb and geo.cell(-23.5491, -46.6321) in nb
    assert geo.cell(-23.5501, -46.6331, 0.02) == int(66.4499 * 50) * 1_000_000 + int(133.3669 * 50)
    pts = [geo.offset(-23.5, -46.6, 0, i * 10) for i in range(101)]                  # 1 km reto para leste
    assert geo.simplify(pts, 1.0) == [0, 100]
    bent = pts[:51] + [geo.offset(*pts[50], i * 10, 0) for i in range(1, 51)]        # vira para o norte
    assert geo.simplify(bent, 1.0) == [0, 50, 100]
    assert abs(geo.path_length_m(pts) - 1000) < 1
    assert abs(geo.cross_track_m(geo.offset(-23.5, -46.6, 30, 500), pts[0], pts[-1]) - 30) < 0.5


def test_schema_migration_is_additive():
    p = Path(tempfile.mkdtemp()) / "old.db"
    old = sqlite3.connect(p)
    old.executescript(db.SCHEMA.split("-- =====")[0])         # esquema de antes do GPS
    old.execute("INSERT INTO trip(id, start_ts, distance_km, finalized, duration_s) VALUES (7, 1000, 12.5, 1, 900)")
    old.execute("INSERT INTO accel_run(ts, t100) VALUES (5, 11.2)")
    old.execute("INSERT INTO fuel_fill(ts, fuel, liters) VALUES (6, 'etanol', 40)")
    old.commit()
    old.close()
    con = db.init(str(p))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gps_fix", "trip_gps", "place", "geo_event", "route", "segment", "segment_pass", "gps_model", "dyno_run", "fuel_stop"} <= tables
    cols = lambda t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    assert {"gps_km", "obd_km", "drive_score"} <= cols("trip")
    assert {"source", "kind", "v0", "v1", "t_s", "gear", "dist_m", "slope_pct", "auto", "json"} <= cols("accel_run")
    assert "place_id" in cols("fuel_fill") and "expire_ts" in cols("outbox") and "clock_source" in cols("session")
    t = con.execute("SELECT * FROM trip WHERE id=7").fetchone()
    assert t["distance_km"] == 12.5 and t["gps_km"] is None
    assert con.execute("SELECT t100, auto FROM accel_run").fetchone()[:] == (11.2, 0)
    assert con.execute("SELECT liters FROM fuel_fill").fetchone()[0] == 40
    con.close()
    con = db.init(str(p))                                     # de novo: nada muda, nada quebra
    assert con.execute("SELECT COUNT(*) FROM trip").fetchone()[0] == 1
    con.close()


# ---------------------------------------------------------------- relógio
def test_clock_gps_retro_correction():
    import obdpi.collector.main as cm
    orig = cm.clock_synced
    cm.clock_synced = lambda: False
    try:
        base = Path(tempfile.mkdtemp())
        col = collector(make_cfg(base))
        col.clock.anchor_wall -= DAY3                          # Pi sem RTC e sem internet: 3 dias atrasado
        col.boot_ts = col.clock.now() - 30_000                 # o processo subiu antes das posições chegarem
        col.session_id = col.con.execute("INSERT INTO session(started_ts, last_ts) VALUES (?,?)", (col.clock.now(),) * 2).lastrowid
        ts1 = col.clock.now()
        col.con.execute("INSERT INTO fast_sample(ts, rpm, speed) VALUES (?, 800, 0)", (ts1,))
        col.con.execute("INSERT INTO accel_run(ts, t100) VALUES (?, 11.0)", (ts1,))
        store.add_event(col.con, ts1, "teste")
        fr = FakeReader()
        col.gps.reader = fr
        fx = fixes_now(12, every_s=2.0)
        fr.push(fx)
        col.gps_pump()
        assert not col.clock.synced and col.clock.source == "nenhuma"
        col.maybe_commit(force=True)                           # check_sync() não sincroniza → hora direto do GPS
        assert col.clock.synced and col.clock.source == "gps"
        rows = col.con.execute("SELECT ts FROM gps_fix ORDER BY ts").fetchall()
        assert len(rows) == 12
        for r, f in zip(rows, fx):
            assert abs(r[0] - f.t_gps) < 1500, (r[0] - f.t_gps)           # posições na hora certa
        assert abs(col.con.execute("SELECT ts FROM fast_sample").fetchone()[0] - (ts1 + DAY3)) < 3000
        assert abs(col.con.execute("SELECT ts FROM geo_event").fetchone()[0] - (ts1 + DAY3)) < 3000
        assert abs(col.con.execute("SELECT ts FROM accel_run").fetchone()[0] - (ts1 + DAY3)) < 3000
        s = col.con.execute("SELECT clock_synced, clock_source FROM session WHERE id=?", (col.session_id,)).fetchone()
        assert tuple(s) == (1, "gps")
        assert abs(col.clock.now() - time.time() * 1000) < 2000
        # posições novas continuam na hora certa (deslocamento corrigido junto)
        more = fixes_now(3, every_s=0.2, age_first=0.6)
        fr.push(more)
        col.gps_pump()
        last = col.con.execute("SELECT MAX(ts) FROM gps_fix").fetchone()[0]
        assert abs(last - more[-1].t_gps) < 1500
    finally:
        cm.clock_synced = orig


def test_clock_noop_when_system_synced():
    import obdpi.collector.main as cm
    orig = cm.clock_synced
    cm.clock_synced = lambda: True
    try:
        col = collector(make_cfg(Path(tempfile.mkdtemp())))
        col.session_id = col.con.execute("INSERT INTO session(started_ts, last_ts) VALUES (?,?)", (col.clock.now(),) * 2).lastrowid
        ts1 = col.clock.now()
        col.con.execute("INSERT INTO fast_sample(ts, rpm, speed) VALUES (?, 800, 0)", (ts1,))
        fr = FakeReader()
        col.gps.reader = fr
        fr.push(fixes_now(12, every_s=2.0, t_shift=DAY3))     # receptor dizendo outra hora: o sistema manda
        col.gps_pump()
        assert col.gps.clock_offset() == 0
        col.maybe_commit(force=True)
        assert col.con.execute("SELECT ts FROM fast_sample").fetchone()[0] == ts1
        r = col.con.execute("SELECT MIN(ts), MAX(ts) FROM gps_fix").fetchone()
        assert abs(r[1] - time.time() * 1000) < 3000 and r[1] - r[0] > 20000   # carimbadas no relógio do coletor
        assert col.con.execute("SELECT clock_source FROM session").fetchone()[0] == "sistema"
    finally:
        cm.clock_synced = orig


def test_clock_rejects_bad_gps_date():
    assert valid_time(1795348800000) and not valid_time(1262304000000) and not valid_time(2300000000000) and not valid_time(None)
    r = GpsReader(dict(config.DEFAULTS["gps"]))
    m = time.monotonic()
    bad = 1262304000000                                         # 2010 (virada de semana do GPS no clone)
    fx = [Fix(t_gps=bad + i * 1000, mono=m - 30 + i, lat=-23.5, lon=-46.6 + i * 1e-5, speed=3.6, hdop=1, sats=8, mode=3) for i in range(30)]
    for f in fx:
        r._accept(f)
    got = r.drain()
    assert len(got) == 27 and all(f.t_gps is None for f in got)      # a posição vale, a hora não
    clock = SimpleNamespace(now=lambda: int(time.time() * 1000), synced=False, source="nenhuma",
                            sync_from_gps=lambda ms: (_ for _ in ()).throw(AssertionError("não devia acertar o relógio")))
    con = db.init(str(Path(tempfile.mkdtemp()) / "x.db"))
    hub = GpsHub({"gps": {"source": "gpsd"}}, con, clock, config.State(tempfile.mkdtemp() + "/s.json"), lambda *a: None, lambda *a: None, tempfile.mkdtemp())
    fr = FakeReader()
    hub.reader = fr
    fr.push(got)
    hub.pump(clock.now(), {}, {}, False, None)
    assert hub.clock_offset() == 0 and not clock.synced
    assert con.execute("SELECT COUNT(*) FROM gps_fix").fetchone()[0] > 0     # gravadas no relógio do coletor


def test_timesrc_never_raises():
    orig = (timesrc.TIMESYNC_FLAG, timesrc._adjtimex_state, timesrc.subprocess.run)
    try:
        timesrc.TIMESYNC_FLAG = Path("/nao/existe")
        timesrc._adjtimex_state = lambda: (_ for _ in ()).throw(OSError("sem libc"))
        assert timesrc.system_synced() is False
        timesrc._adjtimex_state = lambda: 5
        assert timesrc.system_synced() is False
        timesrc._adjtimex_state = lambda: -1
        assert timesrc.system_synced() is False
        timesrc._adjtimex_state = lambda: 0
        assert timesrc.system_synced() is True
        timesrc.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("chronyc"))
        timesrc._src_cache = None
        assert timesrc.source() == "ntp"
        timesrc.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout="47505300,GPS,1,1795348800.0,0.00001\n")
        timesrc._src_cache = None
        assert timesrc.source() == "gps"
        timesrc._adjtimex_state = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        timesrc._src_cache = None
        assert timesrc.source() == "none"
    finally:
        timesrc.TIMESYNC_FLAG, timesrc._adjtimex_state, timesrc.subprocess.run = orig
        timesrc._src_cache = None
    assert isinstance(timesrc.system_synced(), bool) and timesrc.source() in ("ntp", "gps", "none")


# ---------------------------------------------------------------- coletor sem GPS e privacidade
def test_hub_without_gps_is_silent():
    base = Path(tempfile.mkdtemp())
    from obdpi.collector.main import Collector
    col = Collector(make_cfg(base))                           # gpsd numa porta sem ninguém (= Pi sem GPS)
    try:
        time.sleep(0.3)
        for _ in range(5):
            col.gps_pump()
        assert col.gps.live() == {"state": "desconectado"}
        assert col.gps.best_speed(42) == 42 and col.gps.best_speed(None) is None
        assert col.gps.trip_distance(1, 0, 1, 3.0) is None and col.gps.summary(1) == {"route": None, "parts": []}
        col.write_live()
        live = json.loads((base / "run" / "live.json").read_text())
        assert live["gps"] == {"state": "desconectado"}
        g = json.loads((base / "run" / "gps.json").read_text())
        assert g["state"] == "desconectado" and g["lat"] is None
        # viagem fecha igual a hoje: 10 min a 60 km/h pelo OBD
        t0 = col.clock.now()
        tid = col.con.execute("INSERT INTO trip(start_ts, end_ts) VALUES (?,?)", (t0, t0 + 600_000)).lastrowid
        col.con.executemany("INSERT INTO fast_sample(ts, trip_id, rpm, speed, fuel_lh) VALUES (?,?,?,?,?)",
                            [(t0 + i * 1000, tid, 2000, 60, 6.0) for i in range(601)])
        col.finalize_trip(tid)
        t = col.con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
        assert t["distance_km"] == 10.0 and t["gps_km"] is None and t["obd_km"] is None and t["finalized"] == 1
        o = col.con.execute("SELECT title, body FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        assert o["title"] == "Viagem encerrada" and o["body"].startswith("10,0 km em 10 min")
        assert col.con.execute("SELECT COUNT(*) FROM gps_fix").fetchone()[0] == 0
        assert col.gps.reader.is_alive() and col.gps.reader.status()["state"] == "desconectado"
    finally:
        col.gps.stop()
    # GPS desligado na configuração: nenhum fio, estado "desligado"
    col2 = collector(make_cfg(Path(tempfile.mkdtemp()), source="off"))
    col2.gps_pump()
    assert col2.gps.reader is None and col2.gps.live() == {"state": "desligado"}


def test_live_json_has_no_position():
    base = Path(tempfile.mkdtemp())
    col = collector(make_cfg(base))
    fr = FakeReader()
    col.gps.reader = fr
    fr.push(fixes_now(15, every_s=0.2, age_first=3.0))
    col.trip_id = 1
    col.gps_pump()
    col.write_live()
    live = json.loads((base / "run" / "live.json").read_text())
    assert live["gps"]["state"] == "ok" and live["gps"]["sats"] == 9 and live["gps"]["speed"] == 30.0
    assert not has_key(live, {"lat", "lon", "latitude", "longitude"})
    txt = json.dumps(live)
    assert "-23.5" not in txt and "-46.6" not in txt                                   # nem o número solto
    g = json.loads((base / "run" / "gps.json").read_text())
    assert g["lat"] is not None and g["trip_id"] == 1 and g["status"]["state"] == "ok"   # a posição fica no arquivo privado
    assert col.con.execute("SELECT COUNT(*), MIN(trip_id) FROM gps_fix").fetchone()[:] in ((3, 1), (4, 1))    # ~3 s a 5 Hz → 1 Hz


def test_fix_timestamps_keep_gps_spacing():
    import random
    rnd = random.Random(3)
    con = db.init(str(Path(tempfile.mkdtemp()) / "x.db"))
    clock = SimpleNamespace(now=lambda: int(time.time() * 1000), synced=True, source="sistema")
    hub = GpsHub({"gps": {"source": "gpsd", "store_hz": 5}}, con, clock, config.State(tempfile.mkdtemp() + "/s.json"),
                 lambda *a: None, lambda *a: None, tempfile.mkdtemp())
    fr = FakeReader()
    hub.reader = fr
    m0, t0 = time.monotonic() - 10, 1795348800000
    fx = [Fix(t_gps=t0 + i * 200, mono=m0 + i * 0.2 + rnd.uniform(0, 0.09), lat=-23.5, lon=-46.6 + i * 1e-5, speed=36,
              hdop=0.9, sats=9, mode=3) for i in range(40)]           # chegada pelo USB com até 90 ms de atraso variável
    fr.push(fx)
    hub.pump(clock.now(), {}, {}, True, None)
    d = [b.ts - a.ts for a, b in zip(fx, fx[1:])]
    assert d[9:] == [200] * 30, d                                     # depois das 10 primeiras: o intervalo é o do receptor
    assert all(150 <= x <= 250 for x in d)
    expect = clock.now() - (time.monotonic() - fx[-1].mono) * 1000 - 120
    assert abs(fx[-1].ts - expect) < 150                             # e ancorado no relógio do coletor, descontada a latência
    buckets = len({f.ts // 200 for f in fx})                         # store_hz 5: uma por janela de 200 ms (o início com atraso pode juntar duas)
    assert con.execute("SELECT COUNT(*) FROM gps_fix").fetchone()[0] == buckets >= 37


def test_api_gps_status_and_live():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        return
    from obdpi.web import gps_api
    base = Path(tempfile.mkdtemp())
    con = db.init(str(base / "obd.db"))
    con.execute("INSERT INTO gps_fix(ts, lat, lon) VALUES (?, -23.5, -46.6)", (int(time.time() * 1000),))
    con.commit()
    (base / "gps.json").write_text(json.dumps({"ts": int(time.time() * 1000), "state": "ok", "lat": -23.5, "lon": -46.6,
                                                "status": {"state": "ok", "src": "gpsd", "sats": 9, "hz": 5.0}}))
    (base / "live.json").write_text(json.dumps({"gps": {"state": "ok", "sats": 9}}))
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(str(base / "obd.db"), readonly=True), lambda t: None, config.DEFAULTS, base))
    c = TestClient(app)
    s = c.get("/api/gps/status").json()
    assert s["state"] == "ok" and s["sats"] == 9 and s["fixes_today"] == 1 and not has_key(s, {"lat", "lon"})
    lv = c.get("/api/gps/live").json()
    assert lv["lat"] == -23.5 and "status" not in lv and lv["age_s"] is not None
    (base / "gps.json").unlink()
    assert c.get("/api/gps/live").json() == {"state": "coletor_parado"}


def test_gps_check_readonly():
    """obdctl gps check (F10, docs/DIA-DO-GPS.md): só leitura — não pede taxa ao gpsd — e nunca levanta sem GPS."""
    from obdpi.gps import check
    lines = (FIX / "gpsd-session.jsonl").read_text().splitlines()
    srv = FakeGpsd(lines, line_gap_s=0.01)
    srv.start()
    try:
        d = check.probe_gpsd("127.0.0.1", srv.port, timeout=3)
    finally:
        srv.stop()
    assert d["reachable"] and d["version"] == "3.25" and d["tpv_mode"] == 3, d
    assert d["devices"] == [{"path": "/dev/ttyACM0", "driver": "u-blox", "cycle": 1.0}]
    assert not any(r.startswith("?DEVICE") for r in srv.received), srv.received     # nada de mudar a taxa
    assert check.probe_gpsd("127.0.0.1", free_port())["reachable"] is False

    csv = "^,*,192.168.0.1,2,6,377,12,0.001,0.001,0.02,0.01,0.03\n#,+,GPS,0,2,17,1,0.01,0.01,0.1,0.2,0.4\n"
    ran = lambda *a, **k: SimpleNamespace(returncode=0, stdout=csv)
    assert check.chrony_gps(ran) == {"found": True, "state": "+", "reach": 0o17}
    assert check.chrony_gps(lambda *a, **k: SimpleNamespace(returncode=0, stdout=csv.splitlines()[0]))["found"] is False
    def boom(*a, **k):
        raise FileNotFoundError("chronyc")
    assert check.chrony_gps(boom) is None and check.throttled(boom) is None
    assert check.throttled(lambda *a, **k: SimpleNamespace(returncode=0, stdout="throttled=0x50005\n")) == 0x50005

    base = Path(tempfile.mkdtemp())
    now = 1795400000.0
    geo = base / "geo.db"
    con = sqlite3.connect(geo)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [("osm_ts", str(int((now - 10 * 86400) * 1000))), ("cameras", "12"), ("roads", "345")])
    con.commit(); con.close()
    cfg = make_cfg(base)
    cfg["gps"]["geo_db"] = str(geo)
    cfg["cloud"] = {"url": "https://exemplo.invalid", "token": "x"}
    (base / "gps.json").write_text(json.dumps({"state": "ok", "status": {"state": "ok", "src": "gpsd", "sats": 9, "hdop": 0.9,
                                               "hz": 5.0, "reconnects": 0, "ttff_s": 31}}))
    (base / "share_cloud.json").write_text(json.dumps({"ts": int((now - 20) * 1000), "loc": True, "links": []}))
    good = {"reachable": True, "version": "3.25", "devices": [{"path": "/dev/ttyACM0", "driver": "u-blox", "cycle": 0.2}], "tpv_mode": 3}
    inp = check.gather(cfg, base, gpsd=lambda h, p: good, chrony=lambda: {"found": True, "state": "*", "reach": 0o377},
                       power=lambda: 0, synced=lambda: True, clock_src=lambda: "gps", now=lambda: now)
    inp["gps_json_age"] = 1.0                                   # o arquivo acabou de ser escrito (relógio do teste é fixo)
    rows = check.evaluate(inp, cfg)
    assert all(r[0] == check.OK for r in rows), rows
    assert any("12 radares" in r[2] and "10 dias" in r[2] for r in rows)
    assert "0 falha(s)" in check.render(rows)

    # nada instalado: sem gps.json, gpsd recusando, sem chrony/vcgencmd/geo.db, nuvem sem senha → só falhas e avisos
    empty = base / "vazio"
    empty.mkdir()
    (empty / "share_cloud.json").write_text(json.dumps({"ts": int(now * 1000), "loc": False}))
    cfg["gps"]["geo_db"] = str(empty / "nao-existe.db")
    inp = check.gather(cfg, empty, gpsd=lambda h, p: {"reachable": False, "error": "recusado"}, chrony=lambda: None,
                       power=lambda: 0x50005, synced=lambda: False, clock_src=lambda: "none", now=lambda: now)
    st = {r[1]: r[0] for r in check.evaluate(inp, cfg)}
    assert st["gpsd responde"] == check.FAIL and st["coletor e GPS"] == check.FAIL and st["chrony com o GPS"] == check.FAIL
    assert st["mapa offline (geo.db)"] == check.WARN and st["relógio"] == check.WARN
    assert st["localização na nuvem"] == check.FAIL and st["alimentação do Pi"] == check.FAIL
    # GPS desligado na configuração: não conecta no gpsd
    cfg["gps"]["enabled"] = False
    called = []
    inp = check.gather(cfg, empty, gpsd=lambda h, p: called.append(1), chrony=lambda: None, power=lambda: None,
                       synced=lambda: True, clock_src=lambda: "ntp", now=lambda: now)
    assert not called and inp["gpsd"] is None
    assert {r[1]: r[0] for r in check.evaluate(inp, cfg)}["GPS na configuração"] == check.WARN


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
