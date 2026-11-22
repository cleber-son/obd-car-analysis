"""Testes do GPS — Fase 6 (0–100 e retomadas, dinamômetro, inclinação e altitude), sem hardware. docs/GPS-ARQUITETURA.md,
seções 9.8, 9.9, 9.12 e 9.23. Tudo determinístico: simulador com semente fixa (física completa: inércia das partes que
giram, ar pela altitude) e motores alimentados com as horas do próprio simulador."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, math, socket, statistics, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from obdpi import config, db
from obdpi.gps import FLAG_BURST, Fix, dyno, hub, perf, sim, store, terrain
from obdpi.gps.sim import T0

FAST_LAUNCH = dict(alt0=0, mass=1178)          # 0–100 verdadeiro de 10,40 s (nível do mar, carro leve)


def new_db():
    return db.init(str(Path(tempfile.mkdtemp()) / "x.db"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_cfg(base: Path, **gps) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": str(base / "obd.db"), "state_path": str(base / "state.json"), "backup_dir": str(base / "backup")}
    cfg["runtime"]["live_sock"] = str(base / "run" / "live.sock")
    cfg["gps"].update({"source": "gpsd", "gpsd_port": free_port()}, **gps)     # porta sem ninguém: Pi sem GPS
    return cfg


class FakeHub:
    """O que os motores usam do GpsHub: banco, rajada e tabela de marchas."""
    vehicle = {"mass_kg": 1350, "occupants": 1}

    def __init__(self, con):
        self.con, self.bursts = con, []

    def start_burst(self, s):
        self.bursts.append(s)

    def gear_table(self):
        return None


class FakeReader:
    """Substitui o GpsReader (o mesmo do test_gps.py): entrega as posições que o teste mandar."""
    def __init__(self, state="ok", mono=time.monotonic):
        self.lock = threading.Lock()
        self.ring, self.pending, self.epoch, self._state, self.mono = [], [], 1, state, mono
    def push(self, fixes):
        self.pending += fixes
        self.ring += fixes
    def drain(self):
        out, self.pending = self.pending, []
        return out
    def latest(self):
        return self.ring[-1] if self.ring else None
    def recent(self, s):
        return [f for f in self.ring if f.mono >= self.mono() - s]
    def status(self):
        f = self.latest()
        return {"state": self._state, "src": "fake", "device": "/dev/fake", "driver": "fake", "mode": f.mode if f else None,
                "sats": f.sats if f else None, "hdop": f.hdop if f else None, "hz": 5.0, "age_s": 0.1, "ttff_s": 30.0,
                "reconnects": 0, "rate_hz": 5}
    def stop(self): pass
    def request_rate(self, hz): pass


def feed(engines, trip, con, tid=1):
    """Alimenta os motores como o GpsHub faria: cada posição (ts = hora do receptor) com os valores do OBD lidos até ali."""
    obd = sorted(trip.obd, key=lambda r: r["ts"])
    values, vts, i = {}, {}, 0
    for f in trip.fixes:
        g = Fix(t_gps=f.t_gps, mono=f.mono, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, heading=f.heading, climb=f.climb,
                hdop=f.hdop, sats=f.sats, mode=f.mode, ts=f.t_gps)
        while i < len(obd) and obd[i]["ts"] <= g.ts:
            for k, v in obd[i].items():
                if k not in ("ts", "slow"):
                    values[k], vts[k] = v, obd[i]["ts"]
            i += 1
        ctx = SimpleNamespace(fix=g, values=values, value_ts=vts, engine_on=True, trip_id=tid, ts=g.ts, con=con)
        for e in engines:
            e.feed(ctx)


def runs_of(trip, con=None, arm=False):
    con = con or new_db()
    h = FakeHub(con)
    d = perf.PerfDetector(h)
    if arm:
        d.arm_test(trip.t0)
    feed([d], trip, con)
    return [dict(r) for r in con.execute("SELECT * FROM accel_run ORDER BY id")], d, h, con


def truth_peak(alt):
    best = (0.0, 0)
    for r in range(1000, 7000, 10):
        p = sim.torque_nm(r) * sim.air_factor(alt) * r * 2 * math.pi / 60 / 1000
        if p > best[0]:
            best = (p, r)
    return best


def pull_db(con, trip):
    """Grava a puxada já fechada (com a massa do simulador: 1 pessoa, 1.350 kg)."""
    store.model_set(con, "vehicle", {"mass_kg": 1350, "occupants": 1}, T0)
    tid = sim.to_db(con, trip, finalize=True)
    return tid, [dict(r) for r in con.execute("SELECT * FROM dyno_run WHERE trip_id=?", (tid,))]


# ---------------------------------------------------------------- 9.8: 0–100 e retomadas
def test_launch_timing_5hz_and_1hz():
    """Aceite (1): 0–100 verdadeiro de 10,40 s → ±0,08 s a 5 Hz e ±0,25 s a 1 Hz. O ruído do Doppler do simulador
    (0,15 m/s) dá σ ≈ 0,045 s a 5 Hz: 9 de cada 10 sementes ficam dentro de ±0,08 s."""
    errs5 = []
    for seed in range(1, 11):
        t = sim.launch(seed=seed, **FAST_LAUNCH)
        assert abs(t.meta["truth_0_100"] - 10.40) <= 0.05, t.meta["truth_0_100"]
        rows, d, h, _ = runs_of(t)
        assert len(rows) == 1 and rows[0]["source"] == "gps" and rows[0]["kind"] == "0-100" and rows[0]["auto"] == 1
        assert rows[0]["hz"] == 5.0 and rows[0]["note"] is None and 45 in h.bursts            # rajada da arrancada pedida
        assert rows[0]["t60"] < rows[0]["t80"] < rows[0]["t100"] == rows[0]["t_s"]
        errs5.append(rows[0]["t_s"] - t.meta["truth_0_100"])
    assert all(abs(e) <= 0.08 for e in errs5[:4]), errs5
    assert sum(abs(e) <= 0.08 for e in errs5) >= 9 and statistics.median(abs(e) for e in errs5) <= 0.04, errs5
    for seed in range(1, 5):
        t = sim.launch(seed=seed, rate_hz=1, **FAST_LAUNCH)
        rows, *_ = runs_of(t)
        assert len(rows) == 1 and rows[0]["hz"] == 1.0
        assert abs(rows[0]["t_s"] - t.meta["truth_0_100"]) <= 0.25, (seed, rows[0]["t_s"], t.meta["truth_0_100"])
    js = json.loads(rows[0]["json"])
    assert js["curve"][0][0] <= 0 and js["label"] == "0–100" and js["valid"]


def test_gentle_acceleration_is_not_recorded():
    """Aceite (2): aceleração leve até 100 km/h em 30 s não é gravada como automática; saídas de semáforo também não."""
    rows, *_ = runs_of(sim.launch(acc=100 / 3.6 / 30))
    assert rows == []
    for trip in (sim.commute(), sim.commute(seed=4, reverse=True), sim.slip()):
        rows, *_ = runs_of(trip)
        assert rows == [], rows
    # o mesmo passeio leve com o teste armado é gravado (armado grava sempre)
    rows, d, *_ = runs_of(sim.launch(acc=100 / 3.6 / 30), arm=True)
    assert len(rows) == 1 and rows[0]["auto"] == 0 and 29 <= rows[0]["t_s"] <= 31 and d.arm["state"] == "concluido"
    # armado e parado no meio (soltou antes dos 100): gravado como incompleto
    rows, d, *_ = runs_of(sim.rollon(v_from=60, v_to=90, gear=3), arm=True)
    first = rows[0]
    assert first["auto"] == 0 and first["kind"] == "0-100" and first["t_s"] is None and d.arm["state"] == "incompleto"


def test_launch_on_slope_is_flagged_and_not_a_record():
    """Aceite (3): arrancada em rampa de 4 % sai marcada e não vira recorde (nem a descida, que é mais rápida)."""
    con = new_db()
    flat, *_ = runs_of(sim.launch(seed=1), con)
    assert json.loads(flat[0]["json"])["record"] is True
    rows, *_ = runs_of(sim.launch(seed=2, grade=4.0), con)
    up = rows[-1]
    assert up["note"] == "em subida" and 3.0 <= up["slope_pct"] <= 5.0 and json.loads(up["json"])["record"] is False
    rows, d, *_ = runs_of(sim.launch(seed=3, grade=-4.0), con)
    down = rows[-1]
    assert down["note"] == "em descida" and down["t_s"] < flat[0]["t_s"] and json.loads(down["json"])["record"] is False
    rep = perf.report(con)
    rec = [r for r in rep["records"] if r["kind"] == "0-100"]
    assert len(rec) == 1 and rec[0]["t_s"] == flat[0]["t_s"]
    assert d.last["note"] == "em descida" and d.last["record"] is False


def test_rollon_same_gear_and_shift_discarded():
    """Retomada na mesma marcha: 80–120 em 4ª (e 40–80 em 3ª). Aceite (4): com troca de marcha no meio, descartada."""
    rows, d, h, _ = runs_of(sim.rollon())
    assert [(r["kind"], r["gear"]) for r in rows] == [("80-120", 4)] and 35 in h.bursts
    assert 9 < rows[0]["t_s"] < 16 and rows[0]["auto"] == 1 and rows[0]["v0"] == 80 and rows[0]["v1"] == 120
    assert d.last["label"] == "80–120 em 4ª" and d.last["state"] == "concluido"
    rows, *_ = runs_of(sim.rollon(v_from=35, v_to=85, gear=3))
    assert [(r["kind"], r["gear"]) for r in rows] == [("40-80", 3)]
    rows, *_ = runs_of(sim.rollon(shift=True))
    assert rows == []


def test_armed_test_is_mirrored_by_collector(monkeypatch):
    """Botão ⏱ 0–100 com GPS bom: o PerfDetector mede e o coletor só espelha o estado (a tela não muda). Sem GPS (ou
    com o GPS ruim antes da largada), o caminho pelo OBD segue como antes. Relógio virtual: as posições chegam meio
    segundo por ciclo, como do USB, sem o teste esperar a corrida inteira."""
    base = Path(tempfile.mkdtemp())
    from obdpi.collector.main import Collector
    col = Collector(make_cfg(base))
    try:
        col.gps.reader.stop()
        V = {"ms": T0}
        monkeypatch.setattr(hub, "time", SimpleNamespace(monotonic=lambda: V["ms"] / 1000, time=lambda: V["ms"] / 1000))
        monkeypatch.setattr(col.clock, "now", lambda: int(V["ms"]))
        fr = FakeReader(mono=lambda: V["ms"] / 1000)
        col.gps.reader = fr
        trip = sim.launch(seed=1)
        end = next(f.t_gps for f in trip.fixes if f.speed >= 100) + 3000      # para 3 s depois dos 100 km/h
        fx = [f for f in trip.fixes if f.t_gps <= end]
        V["ms"] = fx[0].t_gps
        (base / "run" / "accel_cmd").write_text("arm")
        col.accel_poll_cmd()
        assert col.accel["state"] == "aguardando_parar"
        states = []
        for i in range(0, len(fx), 3):                   # 3 posições (0,6 s) por ciclo do coletor
            chunk = [Fix(t_gps=f.t_gps, mono=(f.t_gps + 120) / 1000, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, heading=f.heading,
                         climb=f.climb, hdop=f.hdop, sats=f.sats, mode=f.mode) for f in fx[i:i + 3]]
            V["ms"] = chunk[-1].t_gps + 170
            fr.push(chunk)
            col.gps_pump()
            col.accel_feed(col.clock.now(), 0, 800)
            states.append(col.accel["state"])
        assert [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]] == ["aguardando_parar", "pronto", "correndo", "concluido"], states
        a = col.accel
        assert a["src"] == "gps" and a["hz"] == 5.0 and set(a["splits"]) == {"60", "80", "100"} and a["elapsed"] == a["splits"]["100"]
        r = col.con.execute("SELECT * FROM accel_run").fetchall()
        assert len(r) == 1 and r[0]["source"] == "gps" and r[0]["auto"] == 0 and abs(r[0]["t_s"] - trip.meta["truth_0_100"]) <= 0.1
        n5 = col.con.execute("SELECT COUNT(*) FROM gps_fix WHERE flags & 1").fetchone()[0]
        assert n5 >= 5 * 15, n5                                   # a corrida inteira gravada a 5 Hz (rajada)
        col.write_live()
        live = json.loads((base / "run" / "live.json").read_text())
        assert live["accel"]["state"] == "concluido" and live["gps"]["perf"]["kind"] == "0-100" and live["gps"]["perf"]["t_s"] == r[0]["t_s"]
        assert not any(k in json.dumps(live["gps"]) for k in ('"lat"', '"lon"'))
        # GPS ruim (sem posição recente) antes da largada: o coletor segue pelo OBD como antes
        col.accel = {"state": "aguardando_parar", "armed_ts": col.clock.now(), "pts": [], "splits": {}}
        col.accel_obd = False
        col.gps.perf_arm()
        V["ms"] += 60_000
        t0 = col.clock.now()
        col.accel_feed(t0, 0, 800)
        assert col.accel["state"] == "pronto" and "src" not in col.accel
        for i, v in enumerate((0, 10, 30, 60, 80, 101)):
            col.accel_feed(t0 + 500 + i * 1000, v, 3000)
        assert col.accel["state"] == "concluido" and col.accel["splits"]["100"] > 0 and "src" not in col.accel
        assert col.con.execute("SELECT COUNT(*) FROM accel_run WHERE source IS NULL").fetchone()[0] == 1
    finally:
        col.gps.stop()


# ---------------------------------------------------------------- 9.9: dinamômetro
def test_dyno_pull_recovers_power_curve():
    """Aceite (1): puxada em 3ª com curva conhecida → pico de potência a ±3 % e rotação do pico a ±250 rpm.
    Aceite (2): a mesma em rampa de +4 % (com a altitude) fica a ±5 % e sai marcada. Aceite (4): com troca de marcha,
    recusada."""
    tp, rp = truth_peak(760)
    for seed in (1, 2, 3):
        con = new_db()
        tid, rows = pull_db(con, sim.pull(seed=seed))
        assert len(rows) == 1 and rows[0]["valid"] == 1 and rows[0]["gear"] == 3 and rows[0]["why"] is None, rows
        r = rows[0]
        assert abs(r["peak_kw"] / tp - 1) <= 0.03, (seed, r["peak_kw"], tp)
        assert abs(r["peak_kw_rpm"] - rp) <= 250 and r["rpm_max"] - r["rpm_min"] >= 4000
        assert 0.9 <= r["corr"] <= 1.25 and r["mass_kg"] == 1350
        c = json.loads(r["curve_json"])
        assert len(c["rpm"]) == len(c["kw"]) == len(c["nm"]) and c["k"] == 22.46
        n5 = con.execute("SELECT COUNT(*) FROM gps_fix WHERE flags & ? = ?", (FLAG_BURST, FLAG_BURST)).fetchone()[0]
        assert n5 > 100
    for seed in (1, 2):
        _, rows = pull_db(new_db(), sim.pull(seed=seed, grade=4.0))
        r = rows[0]
        assert r["valid"] == 1 and "subida" in r["why"] and 3.5 <= r["slope_pct"] <= 4.5
        assert abs(r["peak_kw"] / tp - 1) <= 0.05, (seed, r["peak_kw"], tp)
    _, rows = pull_db(new_db(), sim.pull(seed=1, shift=True))
    assert len(rows) == 1 and rows[0]["valid"] == 0 and "marcha" in rows[0]["why"] and rows[0]["peak_kw"] is None
    # reanálise é idempotente
    con = new_db()
    tid, _ = pull_db(con, sim.pull(seed=1))
    hub.run_finalizers(con, tid, hub.make_ctx(con, {}, trip_id=tid))
    assert con.execute("SELECT COUNT(*) FROM dyno_run WHERE trip_id=?", (tid,)).fetchone()[0] == 1


def test_coastdown_calibration():
    """Aceite (3): descida em ponto morto nos dois sentidos com CdA = 0,70 e Crr = 0,012 → recuperados a ±5 %; o
    dinamômetro passa a usar os valores calibrados."""
    for seed in (1, 2, 3):
        con = new_db()
        d = dyno.DynoLive(FakeHub(con))
        assert d.command({"cmd": "coast_arm"}) and d.live()["coast"]["state"] == "armado"
        feed([d], sim.coast(seed=seed), con)
        m = store.model_get(con, "coast")
        assert m and m["ok"] and len(m["segs"]) == 2, m
        assert abs(m["cda"] / 0.70 - 1) <= 0.05 and abs(m["crr"] / 0.012 - 1) <= 0.05, (seed, m["cda"], m["crr"])
        assert m["prev"] == {"cda": 0.63, "crr": 0.011, "ts": None}
        assert d.live()["coast"]["state"] == "concluido" and "Calibrado" in d.live()["coast"]["msg"]
        v = dyno.vehicle(con, FakeHub.vehicle)
        assert v["aero_src"] == "calibrado" and v["cda"] == m["cda"] and v["mass"] == 1350
    # um sentido só não calibra; sem armar, nada acontece
    con = new_db()
    d = dyno.DynoLive(FakeHub(con))
    feed([d], sim.coast(seed=1), con)
    assert store.model_get(con, "coast") is None and "coast" not in d.live()
    r = dyno.fit_coast([[(T0 + i * 200, 90 - i * 0.05) for i in range(400)]], 1350, 1.06)
    assert r["ok"] is False and "sentido" in r["motivo"]


def test_altitude_power():
    """Aceite (9.23): puxadas a 0 m e a 1.000 m com o mesmo motor → medida ~11 % menor a 1.000 m; corrigida igual a
    ±3 %. O relatório por faixa de altitude traz as duas e a linha teórica."""
    con = new_db()
    store.model_set(con, "vehicle", {"mass_kg": 1350, "occupants": 1}, T0)
    sim.to_db(con, sim.pull(seed=1, alt0=100), finalize=True)
    sim.to_db(con, sim.pull(seed=2, alt0=1100, t0=T0 + 3600_000), finalize=True)
    r0, r1 = [dict(r) for r in con.execute("SELECT * FROM dyno_run ORDER BY ts")]
    assert r0["valid"] == r1["valid"] == 1
    assert 0.85 <= r1["peak_kw"] / r0["peak_kw"] <= 0.93, (r0["peak_kw"], r1["peak_kw"])
    assert abs(r1["peak_kw"] * r1["corr"] / (r0["peak_kw"] * r0["corr"]) - 1) <= 0.03
    ap = terrain.altitude_power(con, T0 - 86400_000)
    with_p = [b for b in ap["bands"] if b["pulls"]]
    assert [b["alt"] for b in with_p] == [0, 1000] and with_p[0]["theory_pct"] > with_p[1]["theory_pct"]
    assert with_p[0]["baro"] > with_p[1]["baro"] + 9 and not any(b["restricao"] for b in ap["bands"])
    assert ap["msgs"] == []                              # potência corrigida igual: nada além do ar rarefeito
    rep = dyno.report(con, FakeHub.vehicle)
    assert rep["n_valid"] == 2 and 120 <= rep["best"]["peak_cv_corr"] <= 160 and "±8–10 %" in rep["precisao"]


# ---------------------------------------------------------------- 9.12: inclinação e altitude × motor
def _rows(trip):
    return [dict(ts=f.t_gps, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, hdop=f.hdop, sats=f.sats, mode=f.mode, climb=f.climb)
            for f in trip.fixes]


def test_hill_slope_estimate():
    """Aceite (1): cenário hill() (2 km a +6 % e 2 km a −6 %, ruído de altitude de 4 m) → +6 ± 1 % e −6 ± 1 % nos
    trechos de regime. Com a velocidade vertical do receptor: ponto a ponto; só pela altitude: na média do trecho."""
    for seed in (1, 2, 3):
        t = sim.hill(seed=seed)
        fx = _rows(t)
        grade = [t.truth[min(len(t.truth) - 1, int(round((f["ts"] - t.t0) / 100)))]["grade"] for f in fx]
        for use_climb in (True, False):
            sl = terrain.slope_series(fx, use_climb=use_climb)
            for g in (6.0, -6.0):
                xs = [s for s, gg in zip(sl, grade) if gg == g and s is not None]
                xs = xs[len(xs) // 8: len(xs) - len(xs) // 8]           # regime: sem as pontas de cada rampa
                assert abs(statistics.mean(xs) - g) <= 1.0, (seed, use_climb, g, statistics.mean(xs))
                if use_climb:
                    assert sum(abs(x - g) <= 1.0 for x in xs) >= 0.9 * len(xs)
        f1 = fx[::5]                                                     # 1 Hz
        assert all(s is None for s, f in zip(terrain.slope_series(f1), f1) if f["speed"] < 20)
    # ao vivo (últimos 150 m) também chega lá
    live = terrain.slope_series(_rows(sim.hill(seed=1)), centered=False)
    assert max(x for x in live if x is not None) > 5 and min(x for x in live if x is not None) < -5


def test_terrain_finalize_and_leak_reading():
    """Aceite (2): viagem com vazamento de ar (pesa mais com vácuo alto) produz a mensagem; a saudável não. O trajeto
    ganha a coluna de inclinação e /api/gps/terrain junta o período."""
    con = new_db()
    tid = sim.to_db(con, sim.hill(seed=1, leak="vacuo"), finalize=True)
    tj = json.loads(con.execute("SELECT terrain_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    L = tj["leitura"]
    assert L["msg"] == "A mistura empobrece quando o vácuo é alto: assinatura de entrada de ar." and L["diff"] >= 8
    steep = [b for b in tj["bands"] if b["band"] in ("+3 a +6 %", "> +6 %")]
    assert sum(b["s"] for b in steep) >= 60
    trk = json.loads(con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    sl = [x for x in trk["cols"]["slope"] if x is not None]
    assert len(trk["cols"]["slope"]) == trk["n"] and max(sl) > 4 and min(sl) < -4
    con2 = new_db()
    tid2 = sim.to_db(con2, sim.hill(seed=1), finalize=True)
    L2 = json.loads(con2.execute("SELECT terrain_json FROM trip_gps WHERE trip_id=?", (tid2,)).fetchone()[0])["leitura"]
    assert L2["msg"] is None and L2["diff"] is not None and abs(L2["diff"]) < 4
    rep = terrain.report(con, T0 - 86400_000)
    assert rep["trips"] == 1 and rep["leitura"]["msg"] == L["msg"]
    up = next(b for b in rep["bands"] if b["band"] == "+3 a +6 %")
    down = next(b for b in rep["bands"] if b["band"] == "−6 a −3 %")
    assert up["load"] > down["load"] + 15 and up["map"] > down["map"] + 10 and up["stft"] < down["stft"]
    assert terrain.avail_pct(90, 30) == round(100 * 90 / 101.3 * math.sqrt(298 / 303), 1)


def test_no_gps_trip_has_no_f6_rows():
    """Viagem sem GPS: os finalizadores da F6 não gravam nada (nem quebram)."""
    con = new_db()
    trip = sim.pull(seed=1)
    tid = sim.to_db(con, trip, with_gps=False, finalize=True)
    assert con.execute("SELECT COUNT(*) FROM dyno_run").fetchone()[0] == 0
    g = con.execute("SELECT terrain_json, flags FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    assert g["terrain_json"] is None and g["flags"] == "sem_trajeto"


# ---------------------------------------------------------------- hub, API e nuvem
def _hub_with(con, base, **gps):
    return hub.GpsHub({"gps": {"source": "gpsd", "gpsd_port": free_port(), **gps}, "vehicle": {"occupants": 3}}, con,
                      SimpleNamespace(now=lambda: T0, source="sistema", synced=True),
                      SimpleNamespace(get=lambda k: None, set=lambda k, v: None), lambda *a: None, lambda *a: None, base)


def test_hub_f6_engines_burst_and_no_gps():
    base = Path(tempfile.mkdtemp())
    con = new_db()
    h = _hub_with(con, base)
    assert {perf.PerfDetector, dyno.DynoLive} <= {type(e) for e in h.engines}
    assert hub.FINALIZERS[2:4] == [dyno.finalize, terrain.finalize]
    assert h.vehicle["occupants"] == 3 and dyno.vehicle(con, h.vehicle)["mass"] == 1350 + 2 * 75
    assert h.perf_mirror() is None                       # sem receptor: o coletor mede pelo OBD
    h.perf_arm()
    assert h.perf_mirror() is None
    h.start_burst(20)
    assert h.dec.burst_until == T0 + 20_000
    h.start_burst(5)                                     # pedido menor com a rajada aberta: não encurta
    assert h.dec.burst_until == T0 + 20_000
    (base / "gps_cmd").write_text(json.dumps({"cmd": "coast_arm"}) + "\n")
    h._commands()
    dl = next(e for e in h.engines if isinstance(e, dyno.DynoLive))
    assert dl.coast["state"] == "armado"
    off = _hub_with(con, base, enabled=False)
    off.perf_arm()
    assert off.perf_mirror() is None


def test_api_f6_routes():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        return
    from obdpi.web import gps_api
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base)
    cfg["vehicle"]["occupants"] = 1
    dbp = cfg["storage"]["db_path"]
    con = db.init(dbp)
    sim.to_db(con, sim.pull(seed=1), finalize=True)
    sim.to_db(con, sim.hill(seed=1, t0=T0 + 3600_000), finalize=True)
    runs_of(sim.launch(seed=1), con)
    con.commit()
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(dbp, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    pf = c.get("/api/gps/perf").json()
    assert pf["n"] == 1 and pf["records"][0]["kind"] == "0-100" and pf["runs"][0]["curve"]
    dy = c.get("/api/gps/dyno").json()
    assert dy["n_valid"] == 1 and dy["vehicle"]["mass"] == 1350 and dy["runs"][0]["curve"]["rpm"]
    te = c.get("/api/gps/terrain?days=400").json()
    assert te["trips"] == 2 and len(te["bands"]) == 7 and te["days"] == 400 and "power" in te
    for p in ("/api/gps/perf", "/api/gps/dyno", "/api/gps/terrain?days=400"):        # abertas na nuvem: sem posição
        txt = c.get(p).text
        assert '"lat"' not in txt and '"lon"' not in txt and "-23.5" not in txt, p
    assert c.post("/api/gps/dyno/config", json={"mass_kg": 1300, "occupants": 2}).json()["vehicle"]["mass"] == 1375
    assert c.post("/api/gps/dyno/config", json={"mass_kg": 99999}).status_code == 400
    assert c.post("/api/gps/dyno/config", json={"occupants": "x"}).status_code == 400
    assert c.post("/api/gps/dyno/coast", json={}).json() == {"ok": True}
    assert json.loads((base / "gps_cmd").read_text().splitlines()[-1]) == {"cmd": "coast_arm"}
    c.post("/api/gps/dyno/coast", json={"cancel": True})
    assert json.loads((base / "gps_cmd").read_text().splitlines()[-1]) == {"cmd": "coast_cancel"}
    acc = c.get("/api/gps/trips/1").json()["accel"]
    assert len(acc) == 1 and acc[0]["source"] == "gps" and acc[0]["t_s"] == pf["runs"][0]["t_s"]


def test_sync_sends_f6_docs_without_location():
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    old_state = syncmod.STATE
    syncmod.STATE = base / "sync_state.json"
    try:
        s = syncmod.Sync({"cloud": {"url": "https://nuvem.invalid", "token": "t"}, "gps": {"cloud": "auto"}})
        s.local = lambda path, method="GET", body=None: (200, b"{}")
        docs = []
        s.gather_gps_perf(docs)
        keys = [k for k, _ in docs]
        assert keys == ["/api/gps/perf", "/api/gps/dyno", "/api/gps/terrain?days=90"]
        assert not any(syncmod.is_loc(k) for k in keys)
    finally:
        syncmod.STATE = old_state


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
