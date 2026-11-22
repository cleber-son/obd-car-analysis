"""Testes do GPS — Fase 2 (diário de bordo, distância híbrida, lugares, estacionamento, km sem OBD), sem hardware.
docs/GPS-ARQUITETURA.md, seções 9.1, 9.3, 9.7, 9.13, 9.16 e 10.3. Tudo determinístico: simulador com semente fixa e
nenhuma conta depende do alinhamento do relógio de parede."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, random, socket, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from obdpi import config, db, extras, notify, reset
from obdpi.gps import Fix, geo, sim, store
from obdpi.gps import places as placesmod
from obdpi.gps import trip as tripmod
from obdpi.gps.hub import GpsHub, NoObdKm
from obdpi.gps.sim import HOME, T0, _legs, simulate

DAY = 86400_000


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
    cfg["gps"].update({"source": "gpsd", "gpsd_port": free_port()}, **gps)     # porta sem ninguém: Pi sem GPS
    return cfg


def new_db():
    return db.init(str(Path(tempfile.mkdtemp()) / "x.db"))


def collector(cfg):
    from obdpi.collector.main import Collector
    col = Collector(cfg)
    if col.gps and col.gps.reader:
        col.gps.reader.stop()
    return col


class FakeReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.ring, self.pending, self.epoch = [], [], 1
    def push(self, fixes):
        self.pending += fixes
        self.ring += fixes
    def drain(self):
        out, self.pending = self.pending, []
        return out
    def latest(self):
        return self.ring[-1] if self.ring else None
    def recent(self, s):
        return []
    def status(self):
        return {"state": "ok", "src": "fake", "device": "/dev/fake", "driver": "fake", "mode": 3, "sats": 9, "hdop": 0.9,
                "hz": 5.0, "age_s": 0.1, "ttff_s": 30.0, "reconnects": 0, "rate_hz": 5}
    def stop(self): pass
    def request_rate(self, hz): pass


class Clock:
    def __init__(self):
        self.synced, self.source = True, "sistema"
    def now(self):
        return int(time.time() * 1000)


def hub_for(con, run_dir=None, **gps):
    """GpsHub com leitor falso; eventos e avisos guardados em listas."""
    ev, push = [], []
    h = GpsHub({"gps": {"source": "gpsd", **gps}}, con, Clock(), config.State(tempfile.mkdtemp() + "/s.json"),
               lambda *a: ev.append(a), lambda *a: push.append(a), run_dir or tempfile.mkdtemp())
    h.reader = FakeReader()
    h.events, h.pushes = ev, push
    return h


def fixes_at(lat, lon, n, *, speed=0.0, every_s=0.2, north_step=0.0, east_step=0.0, sats=9, hdop=0.9, end_age=0.0):
    """n posições chegadas agora (a última há end_age s), com a hora UTC verdadeira; passos em metros por posição."""
    now_m, real = time.monotonic(), time.time() * 1000
    out = []
    for i in range(n):
        mono = now_m - end_age - (n - 1 - i) * every_s
        la, lo = geo.offset(lat, lon, north_step * i, east_step * i)
        out.append(Fix(t_gps=int(real - (now_m - mono) * 1000 - 120), mono=mono, lat=la, lon=lo,
                       alt=760.0, speed=speed, heading=90.0 if speed >= 3 else None, hdop=hdop, sats=sats, mode=3))
    return out


def ctx_for(f, trip_id=None, engine_on=True, values=None):
    return SimpleNamespace(fix=f, trip_id=trip_id, engine_on=engine_on, values=values or {}, value_ts={}, ts=f.ts)


def old_distance(rows):
    """A conta do coletor antes do GPS (Collector.finalize_trip), copiada aqui de propósito."""
    dist, prev = 0.0, None
    for r in rows:
        if prev is not None:
            dt = min((r[0] - prev[0]) / 1000, 5)
            dist += (prev[1] or 0) * dt / 3600
        prev = r
    return dist


# ---------------------------------------------------------------- distância híbrida (9.3)
def test_hybrid_distance_gps_vs_obd():
    # função pura: GPS manda quando existe; sem GPS, OBD × k; a última conhecida por até 5 s; OBD 0 = parado
    gps = [(t * 1000, 36.0) for t in range(0, 60)]
    obd = [(t * 1000, 40) for t in range(0, 120)]
    d = tripmod.hybrid_distance(gps, obd, 0.9, 0, 120_000)
    assert abs(d["hybrid_km"] - (60 * 36 + 60 * 36) / 3600) < 1e-6              # 60 s × 36 (GPS) + 60 s × 40 × 0,9 (OBD)
    assert d["coverage"] == round(61 / 120, 3) and d["km"] == round(d["hybrid_km"], 2) and abs(d["gps_km"] - 0.61) < 1e-6   # a posição de 59 s vale até 60,5 s
    d = tripmod.hybrid_distance(gps, [(t * 1000, 0) for t in range(0, 60)], 1.0, 0, 60_000)
    assert d["hybrid_km"] == 0 and d["moving_s"] == 0                           # OBD diz parado: o GPS não inventa km
    d = tripmod.hybrid_distance([(0, 50.0), (10_000, 50.0)], [], 1.0, 0, 20_000)
    assert abs(d["hybrid_km"] - 50 * (2 + 5 + 3 + 5) / 3600) < 1e-3             # ±1,5 s de cada posição + até 5 s de "última conhecida"
    # cobertura baixa: volta para a conta antiga do OBD × k (com k = 1, idêntica)
    obd = [(t * 1000, 60) for t in range(0, 600)]
    d = tripmod.hybrid_distance([(0, 60.0)], obd, 1.0, 0, 600_000)
    assert d["coverage"] < 0.5 and d["km"] == round(old_distance(obd), 2)
    # viagem simulada: velocímetro 4 % otimista; com GPS a distância fica a ±1 %, a do OBD a +4 % (+3,4 % pela velocidade inteira)
    con = new_db()
    t = sim.commute(speedo=1.04)
    tid = sim.to_db(con, t, finalize=True)
    r = con.execute("SELECT distance_km, obd_km, gps_km FROM trip WHERE id=?", (tid,)).fetchone()
    true_km = t.meta["km"]
    assert abs(r["distance_km"] / true_km - 1) < 0.01, (r["distance_km"], true_km)
    assert 0.025 < r["obd_km"] / true_km - 1 < 0.05, (r["obd_km"], true_km)
    assert r["gps_km"] > 0.95 * true_km
    assert con.execute("SELECT coverage FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0] > 0.98


def test_hybrid_distance_tunnel():
    con = new_db()
    t = sim.tunnel()
    tid = sim.to_db(con, t, finalize=True)
    r = con.execute("SELECT distance_km, gps_km FROM trip WHERE id=?", (tid,)).fetchone()
    g = con.execute("SELECT coverage, track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    assert 0.5 <= g["coverage"] < 1.0
    assert abs(r["distance_km"] / t.meta["km"] - 1) < 0.02, (r["distance_km"], t.meta["km"])
    assert r["gps_km"] < r["distance_km"] - 1.0                                  # 90 s a 60 km/h ≈ 1,5 km completados pelo OBD
    trk = json.loads(g["track_json"])
    assert len(trk["gaps"]) == 1 and 85 <= trk["gaps"][0][1] - trk["gaps"][0][0] <= 95    # o buraco aparece tracejado


def test_no_gps_trip_matches_old_distance():
    """Sem GPS nada muda: a distância é exatamente a conta antiga, sem gps_km/obd_km, resumo igual ao de antes."""
    base = Path(tempfile.mkdtemp())
    col = collector(make_cfg(base))
    try:
        rnd = random.Random(7)
        t0 = col.clock.now()
        rows, ts = [], t0
        for i in range(900):                                   # velocidades e intervalos irregulares, com buracos de 8 s
            ts += rnd.choice((400, 500, 600, 700, 8000 if i % 97 == 0 else 500))
            rows.append((ts, rnd.choice((0, 0, 12, 33, 47, 61, 75))))
        tid = col.con.execute("INSERT INTO trip(start_ts, end_ts) VALUES (?,?)", (t0, ts)).lastrowid
        col.con.executemany("INSERT INTO fast_sample(ts, trip_id, rpm, speed, fuel_lh) VALUES (?,?,2000,?,5.0)",
                            [(a, tid, v) for a, v in rows])
        col.finalize_trip(tid)
        t = col.con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
        assert t["distance_km"] == round(old_distance(rows), 2) and t["gps_km"] is None and t["obd_km"] is None
        assert col.gps.summary(tid) == {"route": None, "parts": []}
        o = col.con.execute("SELECT title FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
        assert o["title"] == "Viagem encerrada"
        g = col.con.execute("SELECT n_fix, coverage, track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
        assert g["n_fix"] == 0 and g["track_json"] is None
        # posições fora do intervalo da viagem também não mexem na distância
        col.con.execute("INSERT INTO gps_fix(ts, lat, lon, speed, mode, hdop, sats) VALUES (?, -23.5, -46.6, 50, 3, 1, 9)", (t0 - 60_000,))
        assert col.gps.trip_distance(tid, t0, ts, 1.0) is None
        # e a função pura, sem posição nenhuma e k = 1, devolve a mesma conta
        assert tripmod.hybrid_distance([], rows, 1.0, t0, ts)["km"] == round(old_distance(rows), 2)
    finally:
        col.gps.stop()


# ---------------------------------------------------------------- diário de bordo (9.1)
def test_finalize_names_origin_destination():
    con = new_db()
    sim.add_places(con)
    con.execute("INSERT INTO fuel_fill(ts, fuel, liters, price_l) VALUES (?, 'gasolina', 40, 6.0)", (T0 - DAY,))
    t = sim.commute()
    tid = sim.to_db(con, t, finalize=True)
    g = con.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    tr = con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
    assert (g["start_name"], g["end_name"], g["start_src"]) == ("Casa", "Trabalho", "gps")
    assert abs(tr["distance_km"] / t.meta["km"] - 1) < 0.01
    assert g["n_stops"] == 6 and len(json.loads(g["stops_json"])) == 6
    assert g["cost"] == round(tr["fuel_l"] * 6.0, 2) and abs(g["cost_km"] - g["cost"] / tr["distance_km"]) < 0.001
    assert tripmod.summary(con, tid) == {"route": "Casa → Trabalho", "parts": [f"R$ {g['cost_km']:.2f}/km".replace(".", ",")]}
    assert tripmod.summary(con, tid, names=False)["route"] is None
    # sem lugares salvos: coordenada formatada no diário, mas nunca no resumo do celular
    con2 = new_db()
    tid2 = sim.to_db(con2, sim.commute(), finalize=True)
    g2 = con2.execute("SELECT start_name, start_place_id FROM trip_gps WHERE trip_id=?", (tid2,)).fetchone()
    assert g2["start_place_id"] is None and g2["start_name"].endswith("° O") and "° S, " in g2["start_name"]
    assert tripmod.summary(con2, tid2)["route"] is None
    assert geo.haversine_m(-23.55012, -46.63311, -23.5501, -46.6331) < 3
    from obdpi.gps.geocode import fmt_coord
    assert fmt_coord(-23.55012, -46.63311) == "23,5501° S, 46,6331° O"
    # manobra na garagem: mesma ponta
    con3 = new_db()
    placesmod.create_place(con3, "Casa", "casa", HOME[0], HOME[1], 120)
    tid3 = sim.to_db(con3, simulate(_legs(HOME, [(0, 0, 10, 15), (60, 0, 10, 10), (-60, 0, 10, 20)]), start=HOME, t0=T0), finalize=True)
    g3 = con3.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid3,)).fetchone()
    assert "manobra" in g3["flags"] and tripmod.route_label(g3["start_name"], g3["end_name"], g3["start_place_id"], g3["end_place_id"]) == "Casa (manobra)"


def test_origin_from_parked_position():
    """Partida a frio do GPS: a viagem da volta só tem posição 40 s depois de começar; a origem é onde o carro estava estacionado."""
    con = new_db()
    sim.add_places(con)
    sim.to_db(con, sim.commute(), finalize=True)
    back = sim.commute(seed=2, t0=T0 + 10 * 3600_000, reverse=True, dropouts=((0.0, 40.0),))
    tid = sim.to_db(con, back, finalize=True)
    g = con.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    assert g["start_src"] == "estacionado" and g["start_name"] == "Trabalho" and g["end_name"] == "Casa"
    assert "origem_tardia" in (g["flags"] or "")
    end = sim.commute_end()
    assert geo.haversine_m(g["start_lat"], g["start_lon"], *end) < 15
    # o Pi caiu com o carro andando (última posição em movimento): a posição de estacionamento não vale
    con2 = new_db()
    sim.add_places(con2)
    sim.to_db(con2, sim.commute(), finalize=True)
    con2.execute("INSERT INTO gps_fix(ts, lat, lon, speed, mode, hdop, sats) VALUES (?, ?, ?, 40, 3, 1, 9)",
                 (T0 + 10 * 3600_000 - 1000, end[0], end[1] + 0.01))
    tid2 = sim.to_db(con2, sim.commute(seed=2, t0=T0 + 10 * 3600_000, reverse=True, dropouts=((0.0, 40.0),)), finalize=True)
    assert con2.execute("SELECT start_src FROM trip_gps WHERE trip_id=?", (tid2,)).fetchone()[0] == "gps"


def test_stops_and_altitude():
    fx = [{"ts": i * 1000, "speed": v, "lat": -23.5, "lon": -46.6} for i, v in
          enumerate([0] * 30 + [30] * 10 + [1] * 25 + [30] * 10 + [2] * 10 + [30] * 10 + [0] * 40)]
    st = tripmod.find_stops(fx)
    assert [s["s"] for s in st] == [25]                       # 10 s não é parada; as pontas (saída e chegada) não contam
    flat = simulate(_legs(HOME, [(0, 0, 10, 10), (0, 3000, 50, 0), (3000, 0, 50, 10)]), start=HOME, t0=T0, seed=1)
    hill = simulate(_legs(HOME, [(0, 0, 10, 10), (0, 2000, 50, 0, 5.0), (0, 2000, 50, 10, -5.0)]), start=HOME, t0=T0, seed=1)
    out = {}
    for name, t in (("flat", flat), ("hill", hill)):
        con = new_db()
        tid = sim.to_db(con, t, finalize=True)
        out[name] = con.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    h = out["hill"]
    assert 85 <= h["climb_m"] <= 115 and 85 <= h["descent_m"] <= 115, (h["climb_m"], h["descent_m"])   # 2 km a ±5 % = 100 m
    assert h["alt_max"] > 840 and h["alt_max"] - h["alt_min"] > 85
    assert out["flat"]["climb_m"] < 0.7 * h["climb_m"] and out["flat"]["alt_max"] - out["flat"]["alt_min"] < 30


def test_build_track_limits_and_columns():
    moves = []
    for i in range(45):                                       # ~1 h zigue-zague: ~3.600 posições a 1 Hz
        moves.append((1000 if i % 2 else -800, 900, 55 + (i % 3) * 10, 20 if i % 5 == 0 else 0))
    t = simulate(_legs(HOME, moves), start=HOME, t0=T0, seed=4)
    con = new_db()
    tid = sim.to_db(con, t, finalize=True)
    fixes = tripmod.good_fixes(con, 0, 10 ** 14)
    assert len(fixes) >= 3000
    trk = json.loads(con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    assert trk["n"] <= 800 and len(json.dumps(trk)) <= 100_000
    n = trk["n"]
    assert len(trk["t"]) == len(trk["lat"]) == len(trk["lon"]) == len(trk["alt"]) == n
    assert all(len(v) == n for v in trk["cols"].values()) and {"speed", "rpm", "gear", "load", "stft1", "lam", "kml", "ect"} <= set(trk["cols"])
    assert sum(v is not None for v in trk["cols"]["rpm"]) > 0.9 * n and sum(v is not None for v in trk["cols"]["ect"]) > 0.9 * n
    assert trk["t"] == sorted(trk["t"]) and trk["t"][0] == 0
    # erro de posição: cada posição original fica a até a tolerância final do traçado reduzido
    eps = trk["eps_m"]
    pts = list(zip(trk["t"], trk["lat"], trk["lon"]))
    j, worst = 0, 0.0
    for f in fixes:
        tf = (f["ts"] - trk["t0"]) / 1000
        while j + 1 < len(pts) - 1 and pts[j + 1][0] <= tf:
            j += 1
        a, b = pts[j], pts[min(j + 1, len(pts) - 1)]
        worst = max(worst, geo.cross_track_m((f["lat"], f["lon"]), (a[1], a[2]), (b[1], b[2])))
    assert worst <= eps + 1.5, (worst, eps)                    # +1,5 m pelo arredondamento em 5 casas
    # no mínimo um ponto a cada 5 s em movimento (enquanto a tolerância não precisou dobrar o intervalo)
    gaps = [b - a for a, b in zip(trk["t"], trk["t"][1:])]
    assert max(gaps) <= 35


def test_auto_place_after_3_visits():
    con = new_db()
    ids = []
    for k, t0 in enumerate((T0, T0 + 3 * 3600_000, T0 + DAY, T0 + 2 * DAY)):    # 2 no mesmo dia, depois mais 2 dias
        ids.append(sim.to_db(con, sim.straight(2.0, seed=10 + k, t0=t0), finalize=True))
        auto = con.execute("SELECT * FROM place WHERE kind='auto'").fetchall()
        assert len(auto) == (1 if k == 3 else 0), (k, len(auto))
    g = con.execute("SELECT end_name, end_place_id FROM trip_gps WHERE trip_id=?", (ids[-1],)).fetchone()
    assert g["end_name"] == "Lugar frequente 1" and g["end_place_id"] == auto[0]["id"]
    # o dono apagou o lugar automático: não volta a ser criado ali
    con.execute("UPDATE place SET deleted=1 WHERE kind='auto'")
    sim.to_db(con, sim.straight(2.0, seed=20, t0=T0 + 3 * DAY), finalize=True)
    assert con.execute("SELECT COUNT(*) FROM place WHERE kind='auto'").fetchone()[0] == 1


# ---------------------------------------------------------------- cerca e estacionamento (9.7)
def test_geofence_hysteresis():
    con = new_db()
    pid = placesmod.create_place(con, "Oficina", "oficina", HOME[0], HOME[1], 80)
    eng = placesmod.GeofenceEngine(SimpleNamespace(con=con, cfg={}))
    rnd = random.Random(5)
    ts = [T0]

    def at(dist_m, n):
        for _ in range(n):
            ts[0] += 200
            d = dist_m + rnd.gauss(0, 5)                      # ruído de 5 m
            la, lo = geo.offset(HOME[0], HOME[1], 0, d)
            eng.feed(ctx_for(Fix(t_gps=ts[0], mono=0, lat=la, lon=lo, speed=10, hdop=1, sats=9, mode=3, ts=ts[0]), trip_id=3))

    at(400, 10)                                               # começa longe: estado inicial em silêncio
    for dist in range(400, 70, -10):
        at(dist, 2)
    at(80, 60)                                                # parado bem na borda, com ruído
    at(40, 20)
    at(95, 60)                                                # na faixa de histerese
    for dist in range(80, 400, 10):
        at(dist, 2)
    at(110, 60)                                               # de novo perto da borda de saída
    at(400, 20)
    kinds = [r[0] for r in con.execute("SELECT kind FROM geo_event WHERE place_id=? ORDER BY ts", (pid,))]
    assert kinds == ["entrou", "saiu"], kinds
    assert con.execute("SELECT visits FROM place WHERE id=?", (pid,)).fetchone()[0] == 1
    # boot já dentro do lugar: não "chegou"
    eng2 = placesmod.GeofenceEngine(SimpleNamespace(con=con, cfg={}))
    for i in range(20):
        f = Fix(t_gps=ts[0] + i * 200, mono=0, lat=HOME[0], lon=HOME[1], speed=0, hdop=1, sats=9, mode=3, ts=ts[0] + i * 200)
        eng2.feed(ctx_for(f))
    assert con.execute("SELECT COUNT(*) FROM geo_event").fetchone()[0] == 2
    assert eng2.private()["place"]["name"] == "Oficina" and eng2.live() == {}


def test_parked_and_moved_while_off():
    con = new_db()
    run = Path(tempfile.mkdtemp())
    placesmod.create_place(con, "Casa", "casa", HOME[0], HOME[1])
    # boot 1: chega em casa e estaciona
    h1 = hub_for(con, run)
    h1.on_trip_start(1, h1.clock.now(), False)
    h1.reader.push(fixes_at(HOME[0], HOME[1] - 0.003, 30, speed=30.0, east_step=1.7, end_age=12))
    h1.reader.push(fixes_at(HOME[0], HOME[1], 25, speed=0.0, end_age=0))
    assert h1.pump(h1.clock.now(), {}, {}, False, 1) == []
    p = store.model_get(con, "parked")
    assert p["valid"] and geo.haversine_m(p["lat"], p["lon"], *HOME) < 5
    g = json.loads((run / "gps.json").read_text())
    assert g["parked"]["name"] == "Casa" and g["place"]["name"] == "Casa"     # privado: nome e posição
    live = json.dumps(h1.live())
    assert "Casa" not in live and "parked" not in live
    # /api/gps/parked lê o mesmo modelo (link do Mapas do iPhone)
    assert placesmod.maps_url(p["lat"], p["lon"]).startswith("https://maps.apple.com/?ll=-23.5")
    con.commit()
    # boot 2: o carro liga 2 km adiante (guincho)
    far = geo.offset(HOME[0], HOME[1], 0, 2000)
    h2 = hub_for(con, run)
    h2.on_trip_start(2, h2.clock.now(), False)
    h2.reader.push(fixes_at(far[0], far[1], 10, speed=0.0))
    al = h2.pump(h2.clock.now(), {"run_time": 20}, {}, True, 2)
    assert [a.rule for a in al] == ["gps:moved_off"] and al[0].severity == "crit"
    assert "2,0 km" in al[0].detail and "-23" not in al[0].detail and "maps" not in al[0].detail   # sem coordenada no aviso
    ev = con.execute("SELECT * FROM geo_event WHERE kind='moveu_desligado'").fetchall()
    assert len(ev) == 1 and 1900 < ev[0]["value"] < 2100
    h2.reader.push(fixes_at(far[0], far[1], 10, speed=0.0))
    assert h2.pump(h2.clock.now(), {"run_time": 22}, {}, True, 2) == []            # uma vez por boot
    # boot 3 no mesmo lugar em que o boot 2 parou: nada
    con.commit()
    h3 = hub_for(con, run)
    h3.on_trip_start(3, h3.clock.now(), False)
    h3.reader.push(fixes_at(far[0], far[1], 10, speed=0.0))
    assert h3.pump(h3.clock.now(), {"run_time": 20}, {}, True, 3) == []


def test_moved_not_raised_when_explained():
    con = new_db()
    store.model_set(con, "parked", {"lat": HOME[0], "lon": HOME[1], "ts": T0, "trip_id": 1, "valid": True}, T0)
    p900 = geo.offset(HOME[0], HOME[1], 900, 0)
    # deu a partida e saiu antes de o Pi subir: PID 1F = 120 s explica 120 × 14 + 150 m
    h = hub_for(con)
    h.on_trip_start(5, h.clock.now(), False)
    h.reader.push(fixes_at(*p900, 10, speed=40.0))
    assert h.pump(h.clock.now(), {"run_time": 120}, {}, True, 5) == []
    # sem o PID 1F, o mesmo deslocamento é aviso
    h = hub_for(con)
    h.on_trip_start(5, h.clock.now(), False)
    h.reader.push(fixes_at(*p900, 10, speed=40.0))
    assert [a.rule for a in h.pump(h.clock.now(), {}, {}, True, 5)] == ["gps:moved_off"]
    # km pelo OBD desde o início da viagem também explicam
    h = hub_for(con)
    t0 = h.clock.now() - 120_000
    h.on_trip_start(6, t0, False)
    con.executemany("INSERT INTO fast_sample(ts, speed) VALUES (?, 30)", [(t0 + i * 1000, ) for i in range(121)])
    h.reader.push(fixes_at(*p900, 10, speed=30.0))
    assert h.pump(h.clock.now(), {}, {}, True, 6) == []
    # viagem retomada (Pi caiu com o motor ligado) e estacionamento inválido: não avalia
    far = geo.offset(HOME[0], HOME[1], 0, 3000)
    h = hub_for(con)
    h.on_trip_start(7, h.clock.now(), True)
    h.reader.push(fixes_at(*far, 10, speed=0.0))
    assert h.pump(h.clock.now(), {}, {}, True, 7) == []
    store.model_set(con, "parked", {"lat": HOME[0], "lon": HOME[1], "ts": T0, "trip_id": 1, "valid": False}, T0)
    h = hub_for(con)
    h.on_trip_start(8, h.clock.now(), False)
    h.reader.push(fixes_at(*far, 10, speed=0.0))
    assert h.pump(h.clock.now(), {}, {}, True, 8) == []
    # sem viagem aberta (OBD fora): não avalia, nem com posição precisa
    store.model_set(con, "parked", {"lat": HOME[0], "lon": HOME[1], "ts": T0, "trip_id": 1, "valid": True}, T0)
    h = hub_for(con)
    h.reader.push(fixes_at(*far, 10, speed=0.0))
    assert h.pump(h.clock.now(), {}, {}, True, None) == []
    assert con.execute("SELECT COUNT(*) FROM geo_event WHERE kind='moveu_desligado'").fetchone()[0] == 1


def test_no_obd_km_engine():
    con = new_db()
    eng = NoObdKm(SimpleNamespace(con=con, cfg={}))
    ts = T0
    for i in range(5 * 120):                                  # 2 min a 36 km/h, 5 Hz, sem viagem aberta
        ts += 200
        eng.feed(ctx_for(Fix(t_gps=ts, mono=0, lat=-23.5, lon=-46.6, speed=36.0, mode=3, ts=ts)))
    ev = con.execute("SELECT * FROM geo_event WHERE kind='km_sem_obd'").fetchall()
    assert len(ev) == 1 and abs(ev[0]["value"] - 0.9) < 0.02, ev[0]["value"]        # gravado aos 90 s (30 s de confirmação + 1 min)
    for i in range(50):                                       # viagem abriu: o OBD volta a contar; valor final gravado
        ts += 200
        eng.feed(ctx_for(Fix(t_gps=ts, mono=0, lat=-23.5, lon=-46.6, speed=36.0, mode=3, ts=ts), trip_id=9))
    assert con.execute("SELECT COUNT(*) FROM geo_event WHERE kind='km_sem_obd'").fetchone()[0] == 1
    v = con.execute("SELECT value FROM geo_event WHERE kind='km_sem_obd'").fetchone()[0]
    assert abs(v - 1.2) < 0.02                                # 2 min a 36 km/h, incluindo os 30 s de confirmação
    # 20 s rápido não conta (precisa de 30 s acima de 10 km/h)
    con2 = new_db()
    e2 = NoObdKm(SimpleNamespace(con=con2, cfg={}))
    ts = T0
    for i in range(100):
        ts += 200
        e2.feed(ctx_for(Fix(t_gps=ts, mono=0, lat=-23.5, lon=-46.6, speed=36.0 if i < 100 else 0, mode=3, ts=ts)))
    for i in range(50):
        ts += 200
        e2.feed(ctx_for(Fix(t_gps=ts, mono=0, lat=-23.5, lon=-46.6, speed=5.0, mode=3, ts=ts)))
    assert con2.execute("SELECT COUNT(*) FROM geo_event").fetchone()[0] == 0


# ---------------------------------------------------------------- resumo, avisos, hodômetro, reset
def test_summary_push_is_single():
    base = Path(tempfile.mkdtemp())
    col = collector(make_cfg(base))
    try:
        sim.add_places(col.con)
        t = sim.commute()
        tid = sim.to_db(col.con, t)                           # gravada como o coletor gravaria, ainda aberta
        col.finalize_trip(tid)
        col.finalize_trip(tid)                                # fechar de novo (reanálise, retomada) não repete o aviso
        rows = col.con.execute("SELECT title, body FROM outbox WHERE title LIKE 'Viagem encerrada%'").fetchall()
        assert len(rows) == 1 and rows[0]["title"] == "Viagem encerrada: Casa → Trabalho", [tuple(r) for r in rows]
        assert "°" not in rows[0]["body"] and rows[0]["body"].startswith(f"{t.meta['km']:.1f}".replace(".", ",")[:3])
        tr = col.con.execute("SELECT distance_km, gps_km, obd_km FROM trip WHERE id=?", (tid,)).fetchone()
        assert abs(tr["distance_km"] / t.meta["km"] - 1) < 0.01 and tr["gps_km"] and tr["obd_km"]
    finally:
        col.gps.stop()
    # dono preferiu sem nomes: título sem a rota
    col = collector(make_cfg(Path(tempfile.mkdtemp()), summary_names=False))
    try:
        sim.add_places(col.con)
        tid = sim.to_db(col.con, sim.commute())
        col.finalize_trip(tid)
        assert col.con.execute("SELECT title FROM outbox WHERE title LIKE 'Viagem%'").fetchone()[0] == "Viagem encerrada"
    finally:
        col.gps.stop()


def test_outbox_expiry():
    p = str(Path(tempfile.mkdtemp()) / "x.db")
    con = db.init(p)
    now = int(time.time() * 1000)
    con.execute("INSERT INTO outbox(ts, title, body, priority, tags, expire_ts) VALUES (?, 'Chegou em Casa', '', 3, '', ?)",
                (now - 40 * 60_000, now - 10 * 60_000))     # vencia em 30 min: já passou
    con.execute("INSERT INTO outbox(ts, title, body, priority, tags, expire_ts) VALUES (?, 'Saiu do Trabalho', '', 3, '', ?)",
                (now, now + 30 * 60_000))
    con.execute("INSERT INTO outbox(ts, title, body, priority, tags) VALUES (?, 'Viagem encerrada', '', 3, '')", (now,))
    con.commit()
    sent = []
    orig = notify.send
    notify.send = lambda server, topic, title, *a, **k: sent.append(title) or True
    try:
        assert notify.flush_outbox(p, "https://ntfy.invalid", "topico") == 2
    finally:
        notify.send = orig
    assert sent == ["Saiu do Trabalho", "Viagem encerrada"]
    r = dict(con.execute("SELECT title, sent_ts FROM outbox").fetchall())
    assert r["Chegou em Casa"] < 0 and r["Saiu do Trabalho"] > 0
    assert notify.flush_outbox(p, "https://ntfy.invalid", "") == 0


def test_odometer_with_gps_and_no_obd_km():
    con = new_db()
    extras.maint_set_odometer(con, 182000, T0 - 1000)
    con.commit()
    assert extras.odometer(con)["km"] == 182000 and "composicao" not in extras.odometer(con)    # sem GPS: como antes
    for k in range(2):
        sim.to_db(con, sim.straight(10.0, seed=30 + k, t0=T0 + k * DAY, speedo=1.04), finalize=True)
    store.add_event(con, T0 + 3 * DAY, "km_sem_obd", value=3.0)
    o = extras.odometer(con)
    assert abs(o["km"] - 182023.0) <= 0.2, o
    assert o["sem_obd_km"] == 3.0 and o["gps_km"] > 19.5 and "km pelo GPS" in o["composicao"] and "3 km sem OBD" in o["composicao"]
    assert o["composicao"].startswith("informado 182.000 + ")


def test_reset_keeps_places():
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base)
    con = db.init(cfg["storage"]["db_path"])
    placesmod.create_place(con, "Casa", "casa", HOME[0], HOME[1])
    con.execute("INSERT INTO segment(name, a_lat, a_lon, b_lat, b_lon) VALUES ('Subida', -23.5, -46.6, -23.51, -46.6)")
    store.model_set(con, "gears", {"1": 8.8}, T0)
    sim.to_db(con, sim.straight(1.0), finalize=True)
    con.close()
    (base / reset.MARK).write_text(str(T0))
    assert reset.apply(cfg)
    con = db.init(cfg["storage"]["db_path"])
    assert [r["name"] for r in con.execute("SELECT name FROM place")] == ["Casa"]
    assert con.execute("SELECT name FROM segment").fetchone()[0] == "Subida"
    assert store.model_get(con, "gears") == {"1": 8.8}
    assert con.execute("SELECT COUNT(*) FROM trip").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM gps_fix").fetchone()[0] == 0
    kept = list((base / "backup").glob("antes-do-reset-*/obd.db"))
    assert len(kept) == 1
    old = db.connect(str(kept[0]), readonly=True)
    assert old.execute("SELECT COUNT(*) FROM trip").fetchone()[0] == 1          # o histórico ficou na cópia guardada


def test_reanalyze_rewrites_names():
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base)
    con = db.init(cfg["storage"]["db_path"])
    sim.add_places(con)
    tid = sim.to_db(con, sim.commute(), finalize=True)
    con.execute("UPDATE place SET name='Escritório' WHERE name='Trabalho'")
    con.commit()
    assert con.execute("SELECT end_name FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0] == "Trabalho"   # histórico fica
    from obdpi import cli
    assert cli.gps_reanalyze(cfg, ["--trip", str(tid)]) == 1
    con2 = db.connect(cfg["storage"]["db_path"], readonly=True)
    assert con2.execute("SELECT end_name FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0] == "Escritório"
    assert cli.gps_reanalyze(cfg, ["--all"]) == 1


def test_api_gps_routes_smoke():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        return
    from obdpi.web import gps_api
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base)
    dbp = cfg["storage"]["db_path"]
    con = db.init(dbp)
    sim.add_places(con)
    con.execute("INSERT INTO fuel_fill(ts, fuel, liters, price_l) VALUES (?, 'gasolina', 40, 6.0)", (T0 - DAY,))
    tid = sim.to_db(con, sim.commute(), finalize=True)
    short = sim.to_db(con, simulate(_legs(HOME, [(0, 0, 10, 5), (0, 50, 10, 5)]), start=HOME, t0=T0 + DAY), finalize=True)
    store.model_set(con, "parked", {"lat": HOME[0], "lon": HOME[1], "ts": T0, "valid": True}, T0)
    store.add_event(con, int(time.time() * 1000) - 1000, "entrou", place_id=1, lat=HOME[0], lon=HOME[1])
    con.execute("INSERT INTO alert(ts, trip_id, source, rule, severity, title) VALUES (?, ?, 'x', 'r1', 'warn', 'Teste')", (T0 + 300_000, tid))
    con.commit()
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(dbp, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    lb = c.get("/api/gps/logbook?limit=10").json()
    assert [x["id"] for x in lb["items"]] == [tid] and lb["total"] == 1                # a viagem curta (oculta) não entra
    it = lb["items"][0]
    assert it["route"] == "Casa → Trabalho" and it["cost"] > 0 and it["has_track"] and it["n_stops"] == 6 and it["clock_synced"] == 1
    d = c.get(f"/api/gps/trips/{tid}").json()
    assert len(d["gps"]["stops"]) == 6 and "track_json" not in d["gps"] and d["alerts"][0]["lat"] is not None
    assert c.get(f"/api/gps/trips/{short}").json()["gps"]["n_fix"] >= 0
    trk = c.get(f"/api/gps/trips/{tid}/track").json()
    assert trk["trip"] == tid and trk["n"] <= 800
    assert c.get("/api/gps/trips/999").status_code == 404 and c.get("/api/gps/trips/999/track").status_code == 404
    pl = c.get("/api/gps/places").json()["places"]
    assert [p["name"] for p in pl] == ["Casa", "Trabalho"] and pl[0]["actions"] == [{"on": "entrou", "do": "sincronizar"}]
    r = c.post("/api/gps/places", json={"name": "Oficina do Zé", "kind": "oficina", "lat": -23.51, "lon": -46.61})
    assert r.status_code == 200
    new = r.json()["id"]
    assert json.loads((base / "gps_cmd").read_text().splitlines()[-1]) == {"cmd": "reload_places"}
    assert c.post("/api/gps/places", json={"name": "", "lat": 1, "lon": 1}).status_code == 400
    assert c.post("/api/gps/places", json={"name": "X", "kind": "lua", "lat": 1, "lon": 1}).status_code == 400
    assert c.post("/api/gps/places", json={"name": "X", "lat": 95, "lon": 1}).status_code == 400
    assert c.post(f"/api/gps/places/{new}", json={"radius_m": 5000}).status_code == 200
    pl = {p["id"]: p for p in c.get("/api/gps/places").json()["places"]}
    assert pl[new]["radius_m"] == 2000 and pl[new]["actions"][0]["do"] == "pos_servico"
    assert c.post(f"/api/gps/places/{new}", json={"delete": True}).status_code == 200
    assert new not in {p["id"] for p in c.get("/api/gps/places").json()["places"]}
    assert c.post(f"/api/gps/places/{new}", json={"name": "y"}).status_code == 404
    pk = c.get("/api/gps/parked").json()
    assert pk["known"] and pk["name"] == "Casa" and pk["maps_url"].startswith("https://maps.apple.com/?ll=")
    ev = c.get("/api/gps/events?days=7").json()["events"]
    assert ev and ev[0]["kind"] == "entrou" and ev[0]["place_name"] == "Casa"
    # rotas antigas continuam sem posição
    assert c.get("/api/gps/status").status_code == 200



# ---------------------------------------------------------------- F3: alertas no mapa, calor, GPX/KML, nuvem
def test_alerts_with_position():
    from obdpi.web.gps_api import alerts_with_position
    t = sim.commute()
    con = new_db()
    tid = sim.to_db(con, t, finalize=True)
    truth = {round(x["t"] * 10): x for x in t.truth}
    want = {}
    for i, sec in enumerate((240.0, 610.3, 905.7)):            # instantes conhecidos da viagem (andando e parado)
        ts = T0 + int(sec * 1000)
        aid = con.execute("INSERT INTO alert(ts, trip_id, source, rule, severity, title) VALUES (?, ?, 'x', ?, 'warn', ?)",
                          (ts, tid, f"r{i}", f"Teste {i}")).lastrowid
        want[aid] = truth[round(sec * 10)]
    far = con.execute("INSERT INTO alert(ts, source, rule, severity, title) VALUES (?, 'x', 'longe', 'crit', 'Sem GPS')", (T0 - 5 * DAY,)).lastrowid
    con.execute("INSERT INTO dtc_event(ts, trip_id, code, kind, event, mil) VALUES (?, ?, 'P0171', 'confirmed', 'apareceu', 1)", (T0 + 400_000, tid))
    con.commit()
    out = alerts_with_position(con, T0 - 30 * DAY)
    by = {(x["kind"], x["id"]): x for x in out["items"]}
    for aid, tr in want.items():
        it = by[("alerta", aid)]
        assert it["lat"] is not None and abs(it["fix_dt_s"]) <= 1.0
        assert geo.haversine_m(it["lat"], it["lon"], tr["lat"], tr["lon"]) <= 15, (aid, geo.haversine_m(it["lat"], it["lon"], tr["lat"], tr["lon"]))
        assert it["speed"] is not None and it["alt"] is not None and it["trip_id"] == tid
    x = by[("alerta", far)]
    assert x["lat"] is None and x["lon"] is None and x["fix_dt_s"] is None             # fora de viagem com GPS: sem posição, sem erro
    d = [v for v in out["items"] if v["kind"] == "dtc"]
    assert len(d) == 1 and d[0]["rule"] == "dtc:P0171" and d[0]["lat"] is not None and d[0]["severity"] == "crit"
    assert out["n"] == 5 and out["with_pos"] == 4 and out["hotspots"] == []
    assert [x["ts"] for x in out["items"]] == sorted((x["ts"] for x in out["items"]), reverse=True)
    assert [x["id"] for x in alerts_with_position(con, T0 - 30 * DAY, rule="r1")["items"]] == [list(want)[1]]
    # ponto repetido: a mesma regra 3 vezes a até ~100 m (o mesmo lugar em dias diferentes)
    stop_ts = T0 + 5_000                                       # parado antes de sair (20 s)
    for k in range(3):
        con.execute("INSERT INTO alert(ts, trip_id, source, rule, severity, title) VALUES (?, ?, 'x', 'sempre', 'warn', 'Aqui de novo')",
                    (stop_ts + k * 2000, tid))
    con.commit()
    out = alerts_with_position(con, T0 - 30 * DAY)
    assert len(out["hotspots"]) == 1 and out["hotspots"][0]["rule"] == "sempre" and out["hotspots"][0]["n"] == 3
    assert all(x.get("repeat") == 3 for x in out["items"] if x["rule"] == "sempre")


def test_heat_cells():
    from obdpi.web.gps_api import heat_cells
    a = {"lat": [-23.50010, -23.50012, -23.51010], "lon": [-46.60010, -46.60012, -46.61010], "cols": {"stft1": [10.0, 20.0, 5.0], "load": [1, 2, 3]}}
    b = {"lat": [-23.50011, -23.52010], "lon": [-46.60011, -46.62010], "cols": {"stft1": [60.0, None]}}
    h = heat_cells([a, b], "stft1")
    assert h["n_points"] == 4 and h["cell_deg"] == 0.0005
    cells = {(round(c[0], 4), round(c[1], 4)): c for c in h["cells"]}
    common = [c for c in h["cells"] if c[3] == 3]
    assert len(common) == 1 and common[0][2] == 30.0                          # (10 + 20 + 60) / 3: n somado, média ponderada
    assert abs(common[0][0] - -23.50025) < 1e-6 and abs(common[0][1] - -46.60025) < 1e-6   # centro da célula de 0,0005°
    assert len(cells) == 2 and h["min"] == 5.0 and h["max"] == 30.0
    assert heat_cells([a, b], "rpm")["cells"] == [] and heat_cells([], "stft1")["min"] is None
    # mais de 5.000 células: a célula dobra até caber
    many = {"lat": [-23.5 - i * 0.0006 for i in range(6000)], "lon": [-46.6] * 6000, "cols": {"speed": [50.0] * 6000}}
    hm = heat_cells([many], "speed")
    assert len(hm["cells"]) <= 5000 and hm["cell_deg"] == 0.001 and sum(c[3] for c in hm["cells"]) == 6000


def test_gpx_kml_valid():
    import xml.etree.ElementTree as ET
    from obdpi.gps import export
    con = new_db()
    sim.add_places(con)
    tid = sim.to_db(con, sim.commute(), finalize=True)
    con.execute("INSERT INTO alert(ts, trip_id, source, rule, severity, title) VALUES (?, ?, 'x', 'r', 'warn', 'Ajuste <alto> & \"x\"')", (T0 + 300_000, tid))
    con.commit()
    t = dict(con.execute("SELECT id, start_ts, end_ts, title FROM trip WHERE id=?", (tid,)).fetchone())
    g = con.execute("SELECT start_name, end_name, stops_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    t.update(start_name=g["start_name"], end_name=g["end_name"], route="Casa → Trabalho & <cia>")
    fixes = tripmod.good_fixes(con, t["start_ts"], t["end_ts"])
    txt = "".join(export.gpx([t], lambda x: tripmod.good_fixes(con, x["start_ts"], x["end_ts"])))
    root = ET.fromstring(txt.encode())
    ns = {"g": export.GPX_NS, "o": export.OBD_NS}
    pts = root.findall(".//g:trkpt", ns)
    assert len(root.findall("g:trk", ns)) == 1 and len(pts) == len(fixes) > 500
    times = [p.find("g:time", ns).text for p in pts]
    assert times == sorted(times) and len(set(times)) == len(times) and times[0].endswith("Z")
    assert times[0] == export.iso_utc(fixes[0]["ts"])
    for p, f in zip(pts, fixes):
        assert p.get("lat") == f"{f['lat']:.7f}" and p.get("lon") == f"{f['lon']:.7f}"
        assert round(float(p.get("lat")), 6) == round(f["lat"], 6) and round(float(p.get("lon")), 6) == round(f["lon"], 6)
    assert pts[10].find("o:extensions/o:speed_kmh", ns) is None and pts[10].find("g:extensions/o:speed_kmh", ns) is not None
    assert "&amp; &lt;cia&gt;" in txt
    stops = json.loads(g["stops_json"])
    al = [{"ts": T0 + 300_000, "title": 'Ajuste <alto> & "x"', "severity": "warn", "lat": -23.51, "lon": -46.59},
          {"ts": T0, "title": "sem posição", "lat": None, "lon": None}]
    k = ET.fromstring("".join(export.kml(t, fixes, al, stops)).encode())
    kn = {"k": export.KML_NS}
    assert len(k.findall(".//k:LineString", kn)) == 1
    coords = k.find(".//k:LineString/k:coordinates", kn).text.split()
    assert len(coords) == len(fixes) and coords[0].split(",")[:2] == [f"{fixes[0]['lon']:.7f}", f"{fixes[0]['lat']:.7f}"]
    names = [p.find("k:name", kn).text for p in k.findall(".//k:Placemark", kn)]
    assert names[0] == "Trajeto" and names[1].startswith("Início: Casa") and names[2].startswith("Fim: Trabalho")
    assert sum(n.startswith("Parada") for n in names) == len(stops) == 6
    assert names[-1] == 'Ajuste <alto> & "x"' and "sem posição" not in names                # alfinete do alerta; o sem posição fica de fora
    # duas viagens num arquivo
    t2 = dict(t, id=99)
    root = ET.fromstring("".join(export.gpx([t, t2], lambda x: fixes[:5])).encode())
    assert len(root.findall("g:trk", ns)) == 2 and len(root.findall(".//g:trkpt", ns)) == 10


def test_api_gps_f3_routes():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:                                    # sem FastAPI/httpx: as funções puras já foram testadas acima
        return
    from obdpi.web import gps_api
    import xml.etree.ElementTree as ET
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base)
    dbp = cfg["storage"]["db_path"]
    con = db.init(dbp)
    sim.add_places(con)
    tid = sim.to_db(con, sim.commute(t0=int(time.time() * 1000) - DAY), finalize=True)
    old = sim.to_db(con, sim.commute(t0=T0, seed=2), with_gps=False, finalize=True)       # viagem sem GPS
    a = con.execute("SELECT start_ts FROM trip WHERE id=?", (tid,)).fetchone()[0]
    con.execute("INSERT INTO alert(ts, trip_id, source, rule, severity, title) VALUES (?, ?, 'x', 'r1', 'warn', 'Teste')", (a + 300_000, tid))
    con.commit()
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(dbp, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    al = c.get("/api/gps/alerts?days=7").json()
    assert al["n"] == 1 and al["items"][0]["lat"] is not None
    h = c.get("/api/gps/heat?signal=stft1&days=7").json()
    assert h["trips"] == 1 and h["cells"] and h["min"] <= h["max"]
    for sgn in gps_api.HEAT_SIGNALS:
        assert c.get(f"/api/gps/heat?signal={sgn}&days=30").status_code == 200
    assert c.get("/api/gps/heat?signal=xyz").status_code == 400
    r = c.get(f"/api/gps/trips/{tid}/export.gpx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/gpx+xml")
    assert "attachment" in r.headers["content-disposition"] and ET.fromstring(r.content).tag.endswith("gpx")
    r = c.get(f"/api/gps/trips/{tid}/export.kml")
    assert r.status_code == 200 and "LineString" in r.text and "Teste" in r.text
    assert c.get(f"/api/gps/trips/{old}/export.gpx").status_code == 404 and c.get("/api/gps/trips/999/export.kml").status_code == 404
    r = c.get("/api/gps/export.gpx?from=0")
    assert r.status_code == 200 and r.text.count("<trk>") == 1


def test_live_json_no_position_with_places():
    """live.json (público) sem coordenada nem nome de lugar, mesmo com o carro dentro de um lugar salvo e estacionado."""
    base = Path(tempfile.mkdtemp())
    col = collector(make_cfg(base))
    placesmod.create_place(col.con, "Casa Secreta", "casa", -23.5, -46.6)
    col.con.commit()
    for e in col.gps.engines:
        if hasattr(e, "reload"):
            e.reload()
    fr = FakeReader()
    col.gps.reader = fr
    fr.push(fixes_at(-23.5, -46.6, 40, speed=0.0, end_age=0.1))
    col.trip_id = 1
    col.gps_pump()
    col.write_live()
    live = json.loads((base / "run" / "live.json").read_text())
    txt = json.dumps(live, ensure_ascii=False)
    assert live["gps"]["state"] == "ok"
    assert "Casa Secreta" not in txt and "-23.5" not in txt and "-46.6" not in txt
    assert not ({"lat", "lon", "place", "parked", "dest"} & set(live["gps"]))
    g = json.loads((base / "run" / "gps.json").read_text())
    assert g["place"] and g["place"]["name"] == "Casa Secreta" and g["lat"] is not None    # o privado tem


def test_sync_sends_location_only_when_cloud_confirms():
    """O obd-sync só envia posição depois de a nuvem confirmar a senha de localização (ping loc=true); senão, nem a
    posição ao vivo nem diário/trajetos sobem, e o que já tinha subido é apagado de lá."""
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    gps_file = base / "gps.json"
    gps_file.write_text(json.dumps({"ts": int(time.time() * 1000), "state": "ok", "lat": -23.5, "lon": -46.6, "status": {"x": 1}}))
    old_state, old_gps = syncmod.STATE, syncmod.GPS_FILE
    syncmod.STATE, syncmod.GPS_FILE = base / "sync_state.json", gps_file
    try:
        s = syncmod.Sync({"cloud": {"url": "https://nuvem.invalid", "token": "t"}, "gps": {"cloud": "auto"}})
        sent, deleted, posts = [], [], []
        ping = {"ok": True}

        def cloud(path, payload=None, method=None, timeout=60):
            if path == "/api/sync/ping":
                return dict(ping)
            if path == "/api/sync/docs":
                sent.extend(d["key"] for d in payload.get("docs", []))
                deleted.extend(payload.get("delete", []))
            return {"ok": True}
        s.cloud = cloud
        s.cloud_post = lambda path, body, gz=False: posts.append(json.loads(body))
        local = {"/api/gps/status": {"state": "ok"}, "/api/gps/logbook?limit=1000": {"items": [{"id": 4, "has_track": True}]},
                 "/api/gps/places": {"places": []}, "/api/gps/parked": {"known": False}, "/api/gps/alerts?days=90": {"items": []},
                 "/api/gps/events?days=30": {"events": []}, "/api/gps/trips/4": {"trip": {}}, "/api/gps/trips/4/track": {"lat": [-23.5]}}
        s.local = lambda path, method="GET", body=None: (200, json.dumps(local.get(path, {"p": path})).encode())

        def push_gps():
            docs = []
            s.gather_gps(docs)
            return s.push(docs)
        # 1) Worker antigo (sem 'loc') e Worker sem senha: nada de posição
        for p in ({"ok": True}, {"ok": True, "loc": False}, {"ok": True, "loc": "true"}):
            ping.clear(); ping.update(p)
            assert s.online() and not s.loc_ok()
            push_gps()
            s.live(b'{"state":"coletando"}')
            assert all(not syncmod.is_loc(k) for k in sent), sent
            assert all(not syncmod.is_loc(d["key"]) for b in posts for d in b["docs"])
        assert "/api/gps/status" in sent                                          # o que não tem posição sobe sempre
        # push() descarta posição mesmo se alguém montar a lista à mão
        assert s.push([("/api/gps/live", b'{"lat":1}'), ("/api/gps/trips/4/track", b"{}")]) == 0
        # 2) nuvem com senha: sobe diário, trajetos, calor e a posição ao vivo
        ping.clear(); ping.update({"ok": True, "loc": True})
        assert s.online() and s.loc_ok()
        sent.clear(); posts.clear()
        push_gps()
        assert {"/api/gps/logbook?limit=1000", "/api/gps/trips/4", "/api/gps/trips/4/track", "/api/gps/heat?days=30&signal=stft1"} <= set(sent)
        s.live(b'{"state":"coletando","n":2}')
        g = [d for b in posts for d in b["docs"] if d["key"] == "/api/gps/live"]
        assert len(g) == 1 and json.loads(g[0]["json"])["lat"] == -23.5 and "status" not in json.loads(g[0]["json"])
        # gps.cloud = "off" no Pi: não sobe, mesmo com a nuvem pronta
        s.cfg["gps"]["cloud"] = "off"
        assert not s.loc_ok()
        s.cfg["gps"]["cloud"] = "auto"
        # 3) a senha some da nuvem: o Pi para de enviar e apaga o que tinha subido
        ping.clear(); ping.update({"ok": True, "loc": False})
        assert s.online() and not s.loc_ok()
        n = s.purge_loc()
        assert n >= 5 and "/api/gps/live" in deleted and "/api/gps/trips/4/track" in deleted and "/api/gps/status" not in deleted
        assert not any(syncmod.is_loc(k) for k in s.state["hashes"]) and s.purge_loc() == 0
        # mesma regra do Worker
        for k in ("/api/gps/live", "/API/GPS/live", "/api//gps/places", "/api/%67ps/live", "/api/gps/heat?signal=x&days=1"):
            assert syncmod.is_loc(k), k
        for k in ("/api/gps/status", "/api/gps/gears", "/api/live", "/api/trips/3"):
            assert not syncmod.is_loc(k), k
        assert syncmod.doc_key("/api/gps/heat?signal=ect&days=30&x=1") == "/api/gps/heat?days=30&signal=ect"
    finally:
        syncmod.STATE, syncmod.GPS_FILE = old_state, old_gps

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
