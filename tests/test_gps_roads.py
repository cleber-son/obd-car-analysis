"""Testes do GPS — Fase 7 (vias: radares, limites, cidade × estrada e nomes de rua), sem hardware e sem rede.
docs/GPS-ARQUITETURA.md, seções 4.3, 9.17, 9.20 e 9.1 (passo 4). O geo.db de cada teste é montado pelo próprio
scripts/geo-build.py a partir de respostas do Overpass escritas à mão (tests/fixtures/gps/overpass-*.json) ou geradas
aqui com a geometria do simulador. Tudo determinístico: simulador com semente fixa e horas do próprio simulador."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, importlib.util, json, socket, sqlite3, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from obdpi import config, db
from obdpi.gps import Fix, geocode, hub, places, roads, routes, score, segments, sim, terrain
from obdpi.gps import trip as tripmod
from obdpi.gps.geo import haversine_m, offset
from obdpi.gps.sim import HOME, T0, Leg, _legs, simulate

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "gps"
BBOX = (-23.6, -46.7, -23.3, -46.2)


def builder():
    spec = importlib.util.spec_from_file_location("geo_build", ROOT / "scripts" / "geo-build.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


GB = builder()


def new_db():
    return db.init(str(Path(tempfile.mkdtemp()) / "x.db"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def P(n, e, base=HOME):
    return offset(base[0], base[1], n, e)


def way(wid, tags, pts, nids=None):
    return {"type": "way", "id": wid, "nodes": nids or [wid * 100 + k for k in range(len(pts))],
            "geometry": [{"lat": a, "lon": b} for a, b in pts], "tags": tags}


def node(nid, lat, lon, tags):
    return {"type": "node", "id": nid, "lat": lat, "lon": lon, "tags": tags}


def geo_from(elements, bbox=BBOX, inner=None):
    """geo.db (pelo geo-build.py) a partir de elementos do Overpass; devolve (conexão só leitura, caminho)."""
    out = Path(tempfile.mkdtemp()) / "geo.db"
    GB.build([{"osm3s": {"timestamp_osm_base": "2026-10-01T12:00:00Z"}, "elements": elements}], out, bbox, inner, now_ms=T0)
    return sqlite3.connect(f"file:{out}?mode=ro&immutable=1", uri=True), out


def fixture_geo():
    resp = [json.loads((FIX / f"overpass-{n}.json").read_text(encoding="utf-8")) for n in ("roads", "cameras")]
    out = Path(tempfile.mkdtemp()) / "geo.db"
    GB.build(resp, out, (-23.53, -46.62, -23.40, -46.40), (-23.5, -46.6, 3.0), now_ms=T0)
    return sqlite3.connect(f"file:{out}?mode=ro&immutable=1", uri=True), out


def as_fix(f):
    return Fix(t_gps=f.t_gps, mono=f.mono, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, heading=f.heading, climb=f.climb,
               hdop=f.hdop, sats=f.sats, mode=f.mode, ts=f.t_gps)


def drive(geo, trip, eng=None):
    """Alimenta o RoadEngine como o GpsHub (ctx.geo = geo.db); devolve [(Fix, live())] de cada posição."""
    eng = eng or roads.RoadEngine(SimpleNamespace(cfg={}))
    out = []
    for f in trip.fixes:
        g = as_fix(f)
        eng.feed(SimpleNamespace(fix=g, geo=geo))
        out.append((g, eng.live()))
    return out, eng


# ---------------------------------------------------------------- cenários
# A (Avenida das Flores, mão dupla, 60 km/h) e B (Rua Paralela, 40 m ao norte, sem limite) de leste a oeste por 6 km;
# radar em A a 3 km, com sentido leste (pela relação de fiscalização).
def parallel_geo():
    a = [P(0, e) for e in range(-500, 6600, 500)]
    b = [P(40, e) for e in range(-500, 6600, 500)]
    cam = P(0, 3000)
    els = [way(1, {"highway": "secondary", "name": "Avenida das Flores", "maxspeed": "60"}, a),
           way(2, {"highway": "residential", "name": "Rua Paralela"}, b),
           {"type": "relation", "id": 9, "tags": {"type": "enforcement", "enforcement": "maxspeed", "maxspeed": "60"},
            "members": [{"type": "node", "ref": 50, "role": "device", "lat": cam[0], "lon": cam[1]},
                        {"type": "node", "ref": 51, "role": "from", "lat": P(0, 2900)[0], "lon": P(0, 2900)[1]}]}]
    return geo_from(els)


def along(n_m, e0, e1, speed, seed=3):
    """Reta de leste a oeste (ou o contrário) a n_m metros ao norte de HOME."""
    start = P(n_m, e0)
    return simulate([Leg(to=start, speed=10, stop_s=3), Leg(to=P(n_m, e1), speed=speed, stop_s=5)], start=start, t0=T0, seed=seed)


URBAN = [(0, 0, 10, 15)] + [(0, 1000, 40, 20)] * 5             # 5 km de cidade a 40 km/h com semáforo a cada km
HIGHWAY = [(0, 5000, 100, 0)] * 6                                # 30 km de rodovia a 100 km/h
URBAN2 = [(0, 1000, 40, 20)] * 5


def highway_trip(seed=1):
    """10 km urbanos e 30 km de rodovia (5 km de cidade, 30 de estrada, 5 de cidade), em linha reta para o leste."""
    return simulate(_legs(HOME, URBAN + HIGHWAY + URBAN2), start=HOME, t0=T0, seed=seed, ect0=85)


def highway_geo():
    e1, e2 = 5000, 35000
    city1 = [P(0, e) for e in range(-200, e1 + 1, 1000)]
    road = [P(0, e) for e in range(e1, e2 + 1, 5000)]
    city2 = [P(0, e) for e in range(e2, 40000 + 201, 1000)] + [P(0, 40200)]
    els = [way(1, {"highway": "secondary", "name": "Avenida da Saída", "maxspeed": "50"}, city1),
           way(2, {"highway": "trunk", "ref": "BR-999", "maxspeed": "100"}, road),
           way(3, {"highway": "secondary", "name": "Avenida da Chegada", "maxspeed": "50"}, city2)]
    return geo_from(els, bbox=(-23.6, -46.7, -23.4, -46.1))


def truth_kml(trip):
    """km/L de cada classe pelo próprio simulador (verdade): leituras do OBD separadas pela posição (cidade até 5 km e
    depois de 35 km a leste de HOME)."""
    acc = {"cidade": [0.0, 0.0], "estrada": [0.0, 0.0]}
    rows = [r for r in trip.obd if not r.get("slow")]
    tt = trip.truth
    for p, r in zip(rows, rows[1:]):
        dt = (r["ts"] - p["ts"]) / 1000
        s = tt[min(len(tt) - 1, int(round((p["ts"] - trip.t0) / 100)))]
        e = haversine_m(HOME[0], HOME[1], HOME[0], s["lon"])
        c = "estrada" if 5000 <= e <= 35000 else "cidade"
        acc[c][0] += s["v"] * dt / 3600
        acc[c][1] += (p["fuel_lh"] or 0) * dt / 3600
    return {c: km / l for c, (km, l) in acc.items()}


def reanalyze(con, tid, geo):
    hub.run_finalizers(con, tid, hub.make_ctx(con, {}, trip_id=tid, geo=geo))
    con.commit()


# ---------------------------------------------------------------- 9.17: o script
def test_geo_build_from_fixtures():
    geo, path = fixture_geo()
    rd = {r[0]: r for r in geo.execute("SELECT * FROM road")}
    assert set(rd) == {10, 11, 12, 13}                           # calçada fora; "Rua Longe" fora do raio --inner
    assert rd[10][1:] == ("Avenida das Flores", "secondary", 60, 0)
    assert rd[11][4] == 1 and rd[11][3] is None                   # mão única; sem limite
    assert rd[12][1] == "BR-999" and rd[12][3] == 100              # nome pela ref; 62 mph = 100 km/h
    assert rd[13][3] is None                                      # "BR:urban" não é número
    # pontos densificados (≤ 40 m) e célula coerente com geo.cell()
    pts = geo.execute("SELECT cell, way_id, seq, lat, lon FROM road_pt ORDER BY way_id, seq").fetchall()
    for p, q in zip(pts, pts[1:]):
        if p[1] == q[1]:
            assert q[2] == p[2] + 1 and haversine_m(p[3], p[4], q[3], q[4]) <= roads.DENSIFY_M + 0.5
    assert all(c == roads.cell(la, lo) for c, _, _, la, lo in pts)
    cams = {r[0]: r for r in geo.execute("SELECT id, maxspeed, dir, way_id FROM camera")}
    assert set(cams) == {1003, 1102, 2001, 2003}                  # fora da área e o de semáforo ficam de fora
    assert cams[1003][1:] == (60, 90.0, 10)                       # "forward" = sentido da via
    assert cams[2001][1:] == (50, 90.0, 10)                       # relação de fiscalização: de → até; perto da via, associado
    assert cams[1102][2] == 90.0 and cams[1102][3] == 11           # sem sentido na etiqueta: o da via de mão única
    assert cams[2003][1:] == (100, 270.0, 12)                      # "W"; limite da via
    poi = {r[0]: r for r in geo.execute("SELECT id, kind, name, brand FROM poi")}
    assert sorted(x[1] for x in poi.values()) == ["oficina", "posto", "posto"]
    assert poi[30010][2:] == ("Posto Teste", "Marca X") and poi[30021][2] == "Posto Quadrado"
    loc = {r[0]: r[1] for r in geo.execute("SELECT name, kind FROM locality")}
    assert loc == {"Centro": "bairro", "Cidade Teste": "cidade", "Outra Cidade": "cidade", "Vila Estrada": "vila"}
    meta = dict(geo.execute("SELECT key, value FROM meta"))
    assert meta["roads"] == "4" and meta["cameras"] == "4" and meta["skipped_inner"] == "1"
    assert "ODbL" in meta["atribuicao"] and meta["osm_ts"] == str(1790856000000) and meta["bbox"] and meta["inner"]
    assert not path.with_name("geo.db.tmp").exists()
    # pedidos ao Overpass: um bloco por 0,25°, área inteira no bbox global, ruas locais só no raio
    assert len(GB.chunks((-23.6, -46.8, -23.0, -46.3))) == 3 * 2
    q = GB.query((-23.6, -46.8, -23.35, -46.55), (-23.5, -46.6, 8))
    assert "[bbox:-23.6,-46.8,-23.35,-46.55]" in q and "around:8000,-23.5,-46.6" in q and "speed_camera" in q
    assert [roads.parse_maxspeed(x) for x in ("60", "40 km/h", "30 mph", "60;40", "BR:urban", "none", None, "999")] == [60, 40, 48, 60, None, None, None, None]
    # linha de comando sem rede (--from-json) e com número negativo depois de --bbox
    out = Path(tempfile.mkdtemp()) / "cli.db"
    rc = GB.main(["--from-json", str(FIX / "overpass-roads.json"), str(FIX / "overpass-cameras.json"), "--bbox", "-23.53,-46.62,-23.40,-46.40",
                  "--out", str(out)])
    assert rc == 0 and sqlite3.connect(out).execute("SELECT COUNT(*) FROM road").fetchone()[0] == 5      # sem --inner: a rua longe entra


# ---------------------------------------------------------------- 9.17: via atual
def test_road_match_parallel_streets():
    geo, _ = parallel_geo()
    for n_m, want in ((0, 1), (40, 2)):
        trip = along(n_m, 0, 5000, 50)
        m = roads.RoadMatcher()
        got = []
        for f in tripmod_one_hz(trip.fixes):
            r = m.step(geo, f.lat, f.lon, f.heading, f.speed)
            got.append(r["way"] if r else None)
        moving = [w for w in got if w is not None]
        assert moving and set(moving) == {want}, (n_m, set(moving))
        assert m.changes == 1
    # uma posição solta perto da outra rua não troca a via (3 leituras seguidas)
    m = roads.RoadMatcher()
    for k in range(10):
        m.step(geo, *P(40, 1000 + k * 14), 90.0, 50)
    assert m.cur == 2
    m.step(geo, *P(5, 1150), 90.0, 50)
    m.step(geo, *P(40, 1164), 90.0, 50)
    assert m.cur == 2
    for k in range(3):
        m.step(geo, *P(0, 1200 + k * 14), 90.0, 50)
    assert m.cur == 1 and m.changes == 2
    # mão única respeitada: na contramão de uma via de mão única não casa
    g2, _ = geo_from([way(7, {"highway": "primary", "name": "Mão Única", "oneway": "yes"}, [P(0, 0), P(0, 2000)])])
    assert roads.candidates(g2, *P(2, 500), 90.0) and not roads.candidates(g2, *P(2, 500), 270.0)


def tripmod_one_hz(fixes):
    return roads._one_hz([as_fix(f) for f in fixes])


# ---------------------------------------------------------------- 9.17: radar à frente
def test_camera_ahead_direction():
    geo, _ = parallel_geo()
    cam = P(0, 3000)
    # leste pela Avenida (sentido do radar): aviso entre 600 e 450 m antes, some depois de passar
    trip = along(0, 0, 6000, 80)
    res, eng = drive(geo, trip)
    warns = [(f, lv["warn"]) for f, lv in res if lv.get("warn")]
    assert warns
    f0, w0 = warns[0]
    d0 = haversine_m(f0.lat, f0.lon, *cam)
    assert 450 <= d0 <= 600 and 450 <= w0["dist_m"] <= 610, (d0, w0)
    assert w0["kind"] == "radar" and w0["limit"] == 60 and w0["n"] == 1
    assert set(w0) == {"kind", "dist_m", "limit", "n"}             # nada que revele a posição (nem o id do OSM)
    for f, w in warns:
        assert abs(haversine_m(f.lat, f.lon, *cam) - w["dist_m"]) <= 30 and f.lon < cam[1]
    passed = [lv for f, lv in res if haversine_m(f.lat, f.lon, *cam) > 60 and f.lon > cam[1]]
    assert passed and not any(lv.get("warn") for lv in passed)
    assert all(lv.get("limit") == 60 and lv.get("limit_src") == "osm" for f, lv in res[200:300])
    # oeste pela mesma Avenida (sentido contrário): nenhum aviso
    res, _ = drive(geo, along(0, 6000, 0, 80))
    assert not any(lv.get("warn") for _, lv in res)
    # leste pela rua paralela (40 m ao norte): nenhum aviso
    res, _ = drive(geo, along(40, 0, 6000, 80))
    assert not any(lv.get("warn") for _, lv in res)
    assert all(lv.get("limit") == 30 and lv.get("limit_src") == "presumido" for f, lv in res[200:300])


def test_presumed_limit_is_silent():
    geo, _ = parallel_geo()
    # 80 km/h na Rua Paralela (limite presumido 30): nunca "acima do limite"
    res, _ = drive(geo, along(40, 0, 6000, 80))
    lv = [x for _, x in res if x.get("limit_src")]
    assert lv and all(x["limit_src"] == "presumido" and x["over"] is False for x in lv)
    # 80 km/h na Avenida (60 do OSM): "acima" só depois de 5 s acima de 60 × 1,07 + 2
    res, _ = drive(geo, along(0, 0, 6000, 80))
    over = [(f, x) for f, x in res if x.get("over")]
    assert over
    first = over[0][0]
    fast_since = next(f for f, x in res if (f.speed or 0) > 60 * roads.OVER_K + roads.OVER_ADD and x.get("limit_src") == "osm")
    assert 5000 <= first.ts - fast_since.ts <= 7000
    # a nota de condução também não usa o limite presumido
    fx = tripmod_one_hz(along(40, 0, 6000, 80).fixes)
    assert roads.osm_limits(geo, fx) is None and roads.track_limits(geo, fx)


# ---------------------------------------------------------------- 9.20: cidade × estrada
def test_road_class_with_and_without_geodb():
    trip = highway_trip()
    con = new_db()
    tid = sim.to_db(con, trip, finalize=True)                     # sem geo.db: pelo perfil de velocidade
    prof = json.loads(con.execute("SELECT road_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    assert prof["fonte"] == "perfil"
    geo, _ = highway_geo()
    reanalyze(con, tid, geo)                                      # com geo.db: pela via casada
    mapa = json.loads(con.execute("SELECT road_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    assert mapa["fonte"] in ("mapa", "mapa+perfil") and mapa["mapa"] == "2026-10"
    truth = truth_kml(trip)
    for d in (prof, mapa):
        c = roads.means(d["classes"]["cidade"])
        e = roads.means(d["classes"]["estrada"])
        tot = sum(roads.means(x)["km"] for x in d["classes"].values())
        assert 39 <= tot <= 41, tot
        assert abs(c["km"] / tot * 100 - 25) <= 5 and abs(e["km"] / tot * 100 - 75) <= 5, (d["fonte"], c["km"], e["km"])
    c, e = roads.means(mapa["classes"]["cidade"]), roads.means(mapa["classes"]["estrada"])
    assert abs(c["km"] - 10) <= 0.5 and abs(e["km"] - 30) <= 1.5              # pelo mapa: ±5 % de cada um
    assert abs(c["kml"] / truth["cidade"] - 1) <= 0.05 and abs(e["kml"] / truth["estrada"] - 1) <= 0.05, (c["kml"], e["kml"], truth)
    assert e["kml"] > c["kml"] and e["avg_kmh"] > 90 and c["avg_kmh"] < 40
    assert e["gears"].get("5", 0) > 80 and c["rpm"] and c["load"] and c["ect"]
    # função pura sem geo.db
    fx = tripmod_one_hz(trip.fixes)
    cls, src = roads.road_class(fx)
    assert src == "perfil" and set(cls) <= {"cidade", "estrada", "misto"}
    # período (/api/gps/roadmix): soma das viagens e tendência mensal, sem posição
    rep = roads.report(con, T0 - 86400_000)
    assert rep["trips"] == 1 and [x["cls"] for x in rep["classes"]] == ["cidade", "estrada", "misto"]
    assert rep["monthly"] and rep["monthly"][0]["estrada"] > rep["monthly"][0]["cidade"] and "cidade" in rep["texto"]
    txt = json.dumps(rep)
    assert '"lat"' not in txt and "-23.5" not in txt and "Avenida" not in txt and "BR-999" not in txt


# ---------------------------------------------------------------- 9.1: nomes
def test_reverse_geocode_from_geodb():
    geo, _ = fixture_geo()
    con = new_db()
    p = P(-5, 1010, (-23.5, -46.6))
    assert geocode.name_of(con, geo, *p) == ("Avenida das Flores, Centro, Cidade Teste", None)     # sem "Casa": com a cidade
    places.create_place(con, "Casa", "casa", -23.5, -46.6)
    con.commit()
    assert geocode.name_of(con, geo, *p) == ("Avenida das Flores, Centro", None)                    # mesma cidade de "Casa"
    assert geocode.name_of(con, geo, -23.5, -46.6)[0] == "Casa"                                     # lugar salvo vem primeiro
    q = P(-3000, 1200, (-23.5, -46.6))                                                              # na BR, longe do bairro
    assert geocode.name_of(con, geo, *q)[0] == "BR-999, Vila Estrada"
    far = P(0, 19900, (-23.5, -46.6))                                                               # sem rua nem bairro perto
    assert geocode.name_of(con, geo, *far)[0] == geocode.fmt_coord(*far)
    assert geocode.name_of(con, None, *p)[0] == geocode.fmt_coord(*p)                               # sem geo.db: como antes
    # no fechamento da viagem (origem e destino com rua e bairro)
    con2 = new_db()
    g2, _ = parallel_geo()
    els = [node(70, *P(60, 200), {"place": "neighbourhood", "name": "Jardim Teste"}), node(71, *P(80, 5000), {"place": "suburb", "name": "Vila Final"}),
           way(1, {"highway": "secondary", "name": "Avenida das Flores", "maxspeed": "60"}, [P(0, e) for e in range(-500, 6600, 500)])]
    g3, _ = geo_from(els)
    tid = sim.to_db(con2, along(0, 100, 5200, 50), finalize=True)
    t = con2.execute("SELECT start_name, end_name FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    assert t[0].endswith("° O")                                    # sem geo.db: coordenada
    reanalyze(con2, tid, g3)
    t = con2.execute("SELECT start_name, end_name FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    assert tuple(t) == ("Avenida das Flores, Jardim Teste", "Avenida das Flores, Vila Final")
    assert tripmod.summary(con2, tid)["route"] is None              # ntfy: só nomes de lugares salvos, nunca a rua


# ---------------------------------------------------------------- 9.10 com os limites (F7)
def test_score_uses_limits():
    geo, _ = parallel_geo()
    con = new_db()
    tid = sim.to_db(con, along(0, 0, 6000, 80, seed=5), finalize=True)
    d0 = json.loads(con.execute("SELECT drive_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    assert d0["penalidades"]["velocidade"] == 0 and d0["limites"].startswith("sem limites")      # sem geo.db: só acima de 115
    trk0 = json.loads(con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    assert "limit" not in trk0["cols"]
    reanalyze(con, tid, geo)
    d1 = json.loads(con.execute("SELECT drive_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    assert d1["limites"] == "da via" and d1["acima_pct"] > 80 and d1["penalidades"]["velocidade"] == 20
    assert any("acima do limite da via" in x for x in d1["dicas"])
    assert con.execute("SELECT drive_score FROM trip WHERE id=?", (tid,)).fetchone()[0] <= 80
    trk = json.loads(con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0])
    lims = [x for x in trk["cols"]["limit"] if x is not None]
    assert lims and set(lims) == {60}
    # 80 km/h na rua de limite presumido (30): a nota não desconta
    con2 = new_db()
    t2 = sim.to_db(con2, along(40, 0, 6000, 80, seed=5), finalize=True)
    reanalyze(con2, t2, geo)
    d2 = json.loads(con2.execute("SELECT drive_json FROM trip_gps WHERE trip_id=?", (t2,)).fetchone()[0])
    assert d2["penalidades"]["velocidade"] == 0 and d2["limites"].startswith("sem limites")
    trk2 = json.loads(con2.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (t2,)).fetchone()[0])
    assert 30 in trk2["cols"]["limit"]                             # o mapa mostra o presumido (esmaecido na tela)


# ---------------------------------------------------------------- hub, API e nuvem
class FakeReader:
    """Substitui o GpsReader: entrega as posições que o teste mandar."""
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


def _hub(con, base, geo_path):
    return hub.GpsHub({"gps": {"source": "gpsd", "gpsd_port": free_port(), "geo_db": str(geo_path)}}, con,
                      SimpleNamespace(now=lambda: T0, source="sistema", synced=True),
                      SimpleNamespace(get=lambda k: None, set=lambda k, v: None), lambda *a: None, lambda *a: None, base)


def test_hub_f7_engine_and_no_geodb():
    assert hub.FINALIZERS.index(roads.finalize) == hub.FINALIZERS.index(terrain.finalize) + 1
    assert hub.FINALIZERS[-2:] == [routes.assign_trip, segments.scan_trip]
    base = Path(tempfile.mkdtemp())
    con = new_db()
    h = _hub(con, base, base / "nao-existe.db")                    # sem geo.db: nada muda
    assert roads.RoadEngine in {type(e) for e in h.engines} and h._geo() is None
    trip = along(0, 0, 4000, 80)
    fr = FakeReader()
    h.reader = fr
    for k in range(0, 400, 5):
        fr.push([Fix(t_gps=f.t_gps, mono=time.monotonic(), lat=f.lat, lon=f.lon, speed=f.speed, heading=f.heading, hdop=f.hdop,
                     sats=f.sats, mode=f.mode, alt=f.alt) for f in trip.fixes[k:k + 5]])
        h.pump(T0, {}, {}, True, 1)
    lv = h.live()
    assert lv["state"] == "ok" and not ({"limit", "warn", "over", "geo_m"} & set(lv))
    # com geo.db: limite, aviso e nada de posição nem de nome de rua no live.json (público)
    _, gp = parallel_geo()
    h2 = _hub(con, base, gp)
    fr2 = FakeReader()
    h2.reader = fr2
    seen = []
    for k in range(0, len(trip.fixes), 5):
        fr2.push([Fix(t_gps=f.t_gps, mono=time.monotonic(), lat=f.lat, lon=f.lon, speed=f.speed, heading=f.heading, hdop=f.hdop,
                      sats=f.sats, mode=f.mode, alt=f.alt) for f in trip.fixes[k:k + 5]])
        h2.pump(T0, {}, {}, True, 1)
        seen.append(h2.live())
    assert any(x.get("limit") == 60 and x.get("limit_src") == "osm" for x in seen)
    assert any((x.get("warn") or {}).get("kind") == "radar" for x in seen)
    txt = json.dumps(seen, ensure_ascii=False)
    for bad in ('"lat"', '"lon"', "Avenida", "Paralela", "-23.", "-46.", '"way"', '"name"'):
        assert bad not in txt, bad
    g = json.loads((base / "gps.json").read_text())
    assert g["lat"] is not None                                    # a posição continua só no gps.json (privado)


def test_api_f7_routes():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        return
    from obdpi.web import gps_api
    base = Path(tempfile.mkdtemp())
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": str(base / "obd.db"), "state_path": str(base / "state.json"), "backup_dir": str(base / "backup")}
    _, gp = fixture_geo()
    cfg["gps"]["geo_db"] = str(gp)
    con = db.init(cfg["storage"]["db_path"])
    tid = sim.to_db(con, highway_trip(), finalize=True)
    con.execute("UPDATE trip SET start_ts=?, end_ts=? WHERE id=?", (int(time.time() * 1000) - 3600_000, int(time.time() * 1000) - 600_000, tid))
    con.commit()
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(cfg["storage"]["db_path"], readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    rm = c.get("/api/gps/roadmix?days=30").json()
    assert rm["trips"] == 1 and rm["days"] == 30 and rm["geo"]["osm_ts"] == "1790856000000"
    st = c.get("/api/gps/status").json()
    assert st["geo"]["cameras"] == "4" and "bbox" not in st["geo"] and "inner" not in st["geo"] and st["geo"]["age_d"] >= 0
    for p in ("/api/gps/roadmix?days=30", "/api/gps/status"):        # abertas na nuvem: sem posição
        txt = c.get(p).text
        assert '"lat"' not in txt and '"lon"' not in txt and "-23.5" not in txt and "-46.6" not in txt, p
    cams = c.get("/api/gps/cameras?bbox=-23.53,-46.62,-23.40,-46.40").json()
    assert cams["n"] == 4 and {x["limit"] for x in cams["items"]} == {60, 50, 100, None} and "auxílio" in cams["aviso"]
    assert c.get("/api/gps/cameras?bbox=-25,-48,-22,-45").status_code == 400
    cfg2 = copy.deepcopy(cfg)
    cfg2["gps"]["geo_db"] = str(base / "nada.db")
    app2 = FastAPI()
    app2.include_router(gps_api.build(lambda: db.connect(cfg["storage"]["db_path"], readonly=True), lambda t: None, cfg2, base))
    c2 = TestClient(app2)
    st2 = c2.get("/api/gps/status").json()
    assert st2["geo"] is None and "geo-build.py" in st2["geo_hint"]
    assert c2.get("/api/gps/cameras?bbox=-23.53,-46.62,-23.40,-46.40").status_code == 404
    from obdpi import sync as syncmod
    assert syncmod.is_loc("/api/gps/cameras?bbox=1,2,3,4") and not syncmod.is_loc("/api/gps/roadmix?days=30")


def test_sync_sends_roadmix_without_location():
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    old_state = syncmod.STATE
    syncmod.STATE = base / "sync_state.json"
    try:
        s = syncmod.Sync({"cloud": {"url": "https://nuvem.invalid", "token": "t"}, "gps": {"cloud": "auto"}})
        s.local = lambda path, method="GET", body=None: (200, b"{}")
        docs = []
        s.gather_gps_roads(docs)
        keys = [k for k, _ in docs]
        assert keys == ["/api/gps/roadmix?days=30"] and not syncmod.is_loc(keys[0])
        assert syncmod.doc_key(keys[0]) == keys[0] if hasattr(syncmod, "doc_key") else True
    finally:
        syncmod.STATE = old_state


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
