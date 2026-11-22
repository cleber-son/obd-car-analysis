"""Testes do GPS — Fase 4 (rotas e trecho de referência), sem hardware. docs/GPS-ARQUITETURA.md, seções 9.3 e 9.5.
Tudo determinístico: simulador com semente fixa; nenhum resultado depende do relógio de parede (as viagens usadas
pelo extras.impact() ficam em datas relativas a "agora" só porque ele filtra os últimos 180 dias)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, socket, tempfile, time
from pathlib import Path

from obdpi import config, db, extras
from obdpi.gps import geo, hub, routes, segments, sim
from obdpi.gps.sim import HOME, T0, _legs, simulate

DAY = 86400_000


def new_db(path=None):
    return db.init(path or str(Path(tempfile.mkdtemp()) / "x.db"))


def route_of(con, tid):
    return con.execute("SELECT route_id FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()[0]


def alt_commute(seed=9, t0=T0):
    """Casa → Trabalho por outro caminho: primeiro todo o leste, depois todo o norte (mesmas pontas do commute())."""
    return simulate(_legs(HOME, [(0, 0, 10, 20), (0, 7400, 60, 30), (5200, 0, 60, 10)]), start=HOME, t0=t0, seed=seed)


# ---------------------------------------------------------------- rotas (9.3)
def test_route_clustering():
    assert hub.FINALIZERS[-2:] == [routes.assign_trip, segments.scan_trip]
    con = new_db()
    sim.add_places(con)
    ids = [sim.to_db(con, sim.commute(seed=s, t0=T0 + s * DAY), finalize=True) for s in range(1, 6)]
    rids = {route_of(con, t) for t in ids}
    assert len(rids) == 1 and None not in rids                                  # 5 viagens com ruído de GPS: uma rota
    r = con.execute("SELECT * FROM route").fetchone()
    assert (r["name"], r["start_key"], r["end_key"], r["n"]) == ("Casa → Trabalho", "p1", "p2", 5)
    # funções puras
    trk = json.loads(con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (ids[0],)).fetchone()[0])
    sig = routes.signature(trk)
    assert 40 < len(sig) < 120 and len(sig) == len(set(sig))
    assert routes.similarity(sig, sig) == 1.0 and routes.similarity(sig, []) == 0.0
    other = routes.signature(json.loads(con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (ids[1],)).fetchone()[0]))
    assert routes.similarity(sig, other) >= 0.8
    # reanalisar (finalizadores de novo) não duplica nem troca de rota
    for t in ids:
        hub.run_finalizers(con, t, hub.make_ctx(con, {}, trip_id=t))
    assert {route_of(con, t) for t in ids} == rids and con.execute("SELECT COUNT(*) FROM route").fetchone()[0] == 1
    # sem lugares salvos: as pontas viram células (com as vizinhas, contra o ruído na borda) e agrupa igual
    con2 = new_db()
    ids2 = [sim.to_db(con2, sim.commute(seed=s, t0=T0 + s * DAY), finalize=True) for s in range(1, 3)]
    assert len({route_of(con2, t) for t in ids2}) == 1
    assert con2.execute("SELECT start_key FROM route").fetchone()[0].startswith("c")
    # 3º dia no mesmo destino: vira "Lugar frequente 1" (F2) e a ponta passa a ser o lugar; reanalisar junta as três
    ids2.append(sim.to_db(con2, sim.commute(seed=3, t0=T0 + 3 * DAY), finalize=True))
    assert con2.execute("SELECT end_key FROM route WHERE id=?", (route_of(con2, ids2[-1]),)).fetchone()[0] == "p1"
    for t in ids2:
        hub.run_finalizers(con2, t, hub.make_ctx(con2, {}, trip_id=t))
    assert len({route_of(con2, t) for t in ids2}) == 1 and con2.execute("SELECT COUNT(*) FROM route").fetchone()[0] == 1
    # viagem curta (< 1 km pelo GPS) e viagem sem GPS não entram em rota
    short = sim.to_db(con, simulate(_legs(HOME, [(0, 0, 10, 5), (0, 300, 30, 5)]), start=HOME, t0=T0 + 20 * DAY), finalize=True)
    nogps = sim.to_db(con, sim.commute(seed=7, t0=T0 + 21 * DAY), with_gps=False, finalize=True)
    assert route_of(con, short) is None and route_of(con, nogps) is None


def test_route_variant_and_reverse():
    con = new_db()
    sim.add_places(con)
    main = [sim.to_db(con, sim.commute(seed=s, t0=T0 + s * DAY), finalize=True) for s in range(1, 4)]
    alt = sim.to_db(con, alt_commute(t0=T0 + 5 * DAY), finalize=True)
    back = sim.to_db(con, sim.commute(seed=11, t0=T0 + 6 * DAY, reverse=True), finalize=True)
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM route")}
    assert len(names) == 3
    assert names[route_of(con, main[0])] == "Casa → Trabalho"
    assert names[route_of(con, alt)] == "Casa → Trabalho · via 2"                  # caminho alternativo: variante
    assert names[route_of(con, back)] == "Trabalho → Casa"                         # a volta é outra rota
    # a variante não estraga a média da rota principal
    st = routes.stats(con, route_of(con, main[0]))
    assert st["n"] == 3 and abs(st["km"] - 12.0) < 0.3
    # lugar criado depois: a reanálise reagrupa (a viagem sai da rota antiga, que é apagada se ficar vazia)
    con2 = new_db()
    t1 = sim.to_db(con2, sim.commute(seed=1), finalize=True)
    old = route_of(con2, t1)
    assert con2.execute("SELECT start_key FROM route WHERE id=?", (old,)).fetchone()[0].startswith("c")
    sim.add_places(con2)
    hub.run_finalizers(con2, t1, hub.make_ctx(con2, {}, trip_id=t1))
    new = route_of(con2, t1)
    assert new != old and con2.execute("SELECT name, start_key FROM route WHERE id=?", (new,)).fetchone()[:] == ("Casa → Trabalho", "p1")
    assert con2.execute("SELECT COUNT(*) FROM route WHERE id=?", (old,)).fetchone()[0] == 0


def test_route_stats_by_fuel():
    base = Path(tempfile.mkdtemp())
    dbp = str(base / "obd.db")
    con = new_db(dbp)
    sim.add_places(con)
    con.execute("INSERT INTO fuel_fill(ts, fuel, liters, price_l, mix_after) VALUES (?, 'gasolina', 40, 6.0, 0.0)", (T0 - DAY,))
    con.execute("INSERT INTO fuel_fill(ts, fuel, liters, price_l, mix_after) VALUES (?, 'etanol', 40, 4.2, 0.9)", (T0 + 2 * DAY - 3600_000,))
    ids = [sim.to_db(con, sim.commute(seed=s, t0=T0 + s * DAY), finalize=True) for s in range(4)]     # 2 na gasolina, 2 no etanol
    # viagem com 11 min parado no meio (motor ligado): entra na rota, mas fica fora das medianas
    moves = list(sim.COMMUTE)
    moves[4] = moves[4][:3] + (660,)
    slow = sim.to_db(con, simulate(_legs(HOME, moves), start=HOME, t0=T0 + 5 * DAY, seed=5), finalize=True)
    rid = route_of(con, ids[0])
    assert {route_of(con, t) for t in ids + [slow]} == {rid}
    assert "atipica" in con.execute("SELECT flags FROM trip_gps WHERE trip_id=?", (slow,)).fetchone()[0]
    st = routes.stats(con, rid)
    assert st["n"] == 5 and st["n_tipicas"] == 4 and st["n_atipicas"] == 1
    trips = {r["id"]: r for r in con.execute("SELECT * FROM trip")}
    kmls = sorted(trips[t]["distance_km"] / trips[t]["fuel_l"] for t in ids)
    assert abs(st["km_l"] - (kmls[1] + kmls[2]) / 2) < 0.02 and abs(st["l100"] - 100 / st["km_l"]) < 0.2
    assert set(st["by_fuel"]) == {"gasolina", "etanol"} and st["by_fuel"]["gasolina"]["n"] == 2 and st["by_fuel"]["etanol"]["n"] == 2
    assert st["by_fuel"]["etanol"]["cost_km"] < st["by_fuel"]["gasolina"]["cost_km"]             # R$ 4,20 contra R$ 6,00 o litro
    assert st["cost_km"] is not None and st["best"]["km_l"] >= st["worst"]["km_l"]
    assert sum(x["n"] for x in st["by_hour"]) == 4
    prof = json.loads(con.execute("SELECT profile_json FROM route WHERE id=?", (rid,)).fetchone()[0])
    assert prof["step_m"] == 250 and 40 <= len(prof["t_s"]) <= 50 and prof["t_s"] == sorted(prof["t_s"])
    con.commit()
    # API: lista com km/L e R$/km por rota, detalhe, renomear
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        return
    from obdpi.web import gps_api
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": dbp}
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(dbp, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    lst = c.get("/api/gps/routes").json()["routes"]
    assert len(lst) == 1 and lst[0]["km_l"] == st["km_l"] and lst[0]["cost_km"] == st["cost_km"] and lst[0]["n"] == 5
    d = c.get(f"/api/gps/routes/{rid}").json()
    assert len(d["trips"]) == 5 and d["stats"]["n_tipicas"] == 4 and d["ref_trip_id"] in ids and "cells_json" not in d
    assert sum(1 for x in d["trips"] if x["atipica"]) == 1 and {x["combustivel"] for x in d["trips"]} == {"gasolina", "etanol"}
    assert c.post(f"/api/gps/routes/{rid}", json={"name": "Ida ao trabalho"}).status_code == 200
    assert c.get("/api/gps/routes").json()["routes"][0]["name"] == "Ida ao trabalho"
    assert c.post(f"/api/gps/routes/{rid}", json={"name": " "}).status_code == 400
    assert c.post("/api/gps/routes/999", json={"name": "x"}).status_code == 404 and c.get("/api/gps/routes/999").status_code == 404


# ---------------------------------------------------------------- trecho de referência (9.5)
EAST = geo.offset(*HOME, 0, 3000)


def road(seed=1, t0=T0, **kw):
    """Reta de 3 km para leste, motor já quente (a não ser que ect0 diga outra coisa)."""
    kw.setdefault("ect0", 85)
    return sim.straight(3, seed=seed, t0=t0, **kw)


def make_segment(con, tid, name="Reta da avenida"):
    """Marca o trecho como o dono faria: toca no traçado a 500 m e a 2.500 m da saída."""
    fx = segments._trip_fixes(con, tid)
    near = lambda p: min(fx, key=lambda f: geo.haversine_m(f["lat"], f["lon"], *p))["ts"]
    return segments.create(con, name, tid, near(geo.offset(*HOME, 0, 500)), near(geo.offset(*HOME, 0, 2500)))


def passes(con, sid):
    return [dict(r) for r in con.execute("SELECT * FROM segment_pass WHERE segment_id=? ORDER BY ts_start", (sid,))]


def test_segment_passes_direction_and_validity():
    con = new_db()
    ref = sim.to_db(con, road(seed=1), finalize=True)
    sid = make_segment(con, ref)
    s = segments.get(con, sid)
    assert abs(s["length_m"] - 2000) < 40 and 110 < s["ref_dur_s"] < 130 and geo.ang_diff(s["a_heading"], 90) < 5
    for k in (2, 3):
        sim.to_db(con, road(seed=k, t0=T0 + k * DAY), finalize=True)               # o finalizador já procura o trecho
    ps = passes(con, sid)
    assert len(ps) == 3 and all(p["valid"] for p in ps)
    p = ps[0]
    assert p["trip_id"] == ref and abs(p["km"] - 2.0) < 0.05 and abs(p["avg_speed"] - 60) < 3 and p["stops"] == 0
    assert p["fuel_l"] > 0 and p["l100"] > 0 and p["stft"] is not None and 0.5 < p["air_ratio"] < 1.2 and p["ect"] >= 75
    # sentido contrário: não conta
    back = sim.to_db(con, simulate(_legs(EAST, [(0, 0, 10, 10), (0, -3000, 60, 10)]), start=EAST, t0=T0 + 4 * DAY, seed=4, ect0=85), finalize=True)
    assert not [x for x in passes(con, sid) if x["trip_id"] == back]
    # parada de 40 s no meio do trecho: passagem gravada, mas inválida
    stop = sim.to_db(con, simulate(_legs(HOME, [(0, 0, 10, 10), (0, 1500, 60, 40), (0, 1500, 60, 10)]), start=HOME, t0=T0 + 5 * DAY, seed=5, ect0=85),
                     finalize=True)
    x = [x for x in passes(con, sid) if x["trip_id"] == stop]
    assert len(x) == 1 and not x[0]["valid"] and "parou" in x[0]["why"].split(",") and x[0]["stops"] == 1
    # motor frio e velocidade fora do normal do trecho (40 km/h contra a mediana de 60)
    cold = sim.to_db(con, road(seed=6, t0=T0 + 6 * DAY, ect0=20), finalize=True)
    slowv = sim.to_db(con, road(seed=7, t0=T0 + 7 * DAY, speed=40), finalize=True)
    why = {x["trip_id"]: (x["valid"], x["why"]) for x in passes(con, sid)}
    assert why[cold] == (0, "frio") and why[slowv] == (0, "vel")
    assert sum(1 for x in passes(con, sid) if x["valid"]) == 3
    h = segments.history(con, sid)
    assert h["n"] == 6 and h["n_valid"] == 3 and h["segment"]["label"] == "Trecho 1"
    assert "parou no trecho" in next(p for p in h["passes"] if p["trip_id"] == stop)["motivos"]
    # função pura: o mesmo trajeto ao contrário não tem entrada no portão A
    fx = segments._trip_fixes(con, back)
    assert segments.find_passes(s, fx) == []
    assert len(segments.find_passes(s, segments._trip_fixes(con, ref))) == 1


def test_segment_rescan_idempotent():
    con = new_db()
    trips = [sim.to_db(con, road(seed=k, t0=T0 + k * DAY), finalize=True) for k in range(1, 5)]
    sid = make_segment(con, trips[1])                                              # viagens antigas: achadas pela busca no histórico
    before = [(p["id"], p["trip_id"], p["ts_start"], p["valid"]) for p in passes(con, sid)]
    assert len(before) == 4 and {p[1] for p in before} == set(trips)
    assert segments.rescan(con, sid) == 4 and segments.rescan(con, sid) == 4
    assert [(p["id"], p["trip_id"], p["ts_start"], p["valid"]) for p in passes(con, sid)] == before
    for t in trips:                                                                # e o finalizador de novo (reanalyze)
        hub.run_finalizers(con, t, hub.make_ctx(con, {}, trip_id=t))
    assert [(p["id"], p["trip_id"], p["ts_start"], p["valid"]) for p in passes(con, sid)] == before
    # trecho desativado: o finalizador não procura nele
    con.execute("UPDATE segment SET active=0 WHERE id=?", (sid,))
    t5 = sim.to_db(con, road(seed=5, t0=T0 + 5 * DAY), finalize=True)
    assert not [p for p in passes(con, sid) if p["trip_id"] == t5]
    # apagar o trecho apaga as passagens
    segments.delete(con, sid)
    assert con.execute("SELECT COUNT(*) FROM segment_pass").fetchone()[0] == 0 and segments.get(con, sid) is None
    try:
        segments.create(con, "Curto", trips[0], T0 + DAY + 20_000, T0 + DAY + 22_000)
        assert False
    except ValueError as e:
        assert "curto" in str(e).lower()


def _leaky(con, tid, add=30.0):
    """Vazamento simulado: ajuste curto +30 pontos em toda a viagem."""
    a, b = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (tid,)).fetchone()
    con.execute("UPDATE sample SET value = value + ? WHERE ts BETWEEN ? AND ? AND signal_id = (SELECT id FROM signal WHERE key='stft1')",
                (add, a, b))


def _repair_history(con, t0):
    """6 viagens pelo trecho: 3 com vazamento (ajuste curto +30), o serviço anotado, 3 consertadas (+4)."""
    ids = []
    for k in range(6):
        tid = sim.to_db(con, road(seed=k + 1, t0=t0 + k * DAY), finalize=True)
        _leaky(con, tid, 30.0 if k < 3 else 4.0)
        ids.append(tid)
    ann = t0 + 2 * DAY + 12 * 3600_000
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?, 'manutencao', 'Troca dos bicos injetores')", (ann,))
    sid = make_segment(con, ids[0], "Subida perto de casa")                        # a busca no histórico mede as 6
    con.commit()
    return ids, ann, sid


def test_segment_before_after_repair():
    con = new_db()
    ids, ann, sid = _repair_history(con, T0)
    assert sum(p["valid"] for p in passes(con, sid)) == 6
    out = segments.impact_for(con, ann)
    assert len(out) == 1 and out[0]["trecho"] == "Trecho 1" and out[0]["veredito"] == "melhorou"
    ind = {i["key"]: i for i in out[0]["indicadores"]}
    assert ind["stft"]["veredito"] == "melhorou" and ind["stft"]["antes"] > 25 and ind["stft"]["depois"] < 10
    assert out[0]["antes"]["n"] == 3 and out[0]["depois"]["n"] == 3
    assert "Subida" not in json.dumps(out, ensure_ascii=False)                     # /api/impact é público: sem o nome
    h = segments.history(con, sid)
    assert len(h["services"]) == 1 and h["services"][0]["veredito"] == "melhorou" and h["services"][0]["text"] == "Troca dos bicos injetores"
    assert all(p["residuo_pct"] is not None for p in h["passes"])
    # longe de qualquer passagem (fora da janela de 30 dias): nada
    assert segments.impact_for(con, ann + 90 * DAY) == []
    # API: criar pelo toque no traçado, listar, histórico, editar, reprocessar, apagar
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        return
    from obdpi.web import gps_api
    base = Path(tempfile.mkdtemp())
    dbp = str(base / "obd.db")
    con2 = new_db(dbp)
    ids2 = [sim.to_db(con2, road(seed=k, t0=T0 + k * DAY), finalize=True) for k in (1, 2, 3)]
    con2.commit()
    fx = segments._trip_fixes(con2, ids2[0])
    near = lambda p: min(fx, key=lambda f: geo.haversine_m(f["lat"], f["lon"], *p))["ts"]
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": dbp}
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(dbp, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    body = {"name": "Reta", "trip_id": ids2[0], "t_a": near(geo.offset(*HOME, 0, 2500)), "t_b": near(geo.offset(*HOME, 0, 500))}
    r = c.post("/api/gps/segments", json=body)                                     # fim antes do início: troca sozinho
    assert r.status_code == 200 and r.json()["passes"] == 3
    s_id = r.json()["id"]
    lst = c.get("/api/gps/segments").json()["segments"]
    assert len(lst) == 1 and lst[0]["n"] == 3 and lst[0]["n_valid"] == 3 and lst[0]["label"] == "Trecho 1"
    h = c.get(f"/api/gps/segments/{s_id}").json()
    assert h["n"] == 3 and h["segment"]["name"] == "Reta" and len(h["passes"]) == 3
    assert c.post("/api/gps/segments", json={"name": "", "trip_id": ids2[0], "t_a": 1, "t_b": 2}).status_code == 400
    assert c.post("/api/gps/segments", json={"name": "x", "trip_id": 999, "t_a": 1, "t_b": 2}).status_code == 404
    assert c.post(f"/api/gps/segments/{s_id}", json={"name": "Reta nova", "active": False}).status_code == 200
    assert c.get("/api/gps/segments").json()["segments"][0]["name"] == "Reta nova"
    assert c.post(f"/api/gps/segments/{s_id}", json={"rescan": True}).json()["passes"] == 3
    assert c.post(f"/api/gps/segments/{s_id}", json={"delete": True}).status_code == 200
    assert c.get(f"/api/gps/segments/{s_id}").status_code == 404 and c.post(f"/api/gps/segments/{s_id}", json={}).status_code == 404


def test_impact_includes_segment():
    con = new_db()
    t0 = (int(time.time() * 1000) - 40 * DAY) // DAY * DAY                         # dentro dos 180 dias do extras.impact()
    # sem trecho nenhum: o "antes e depois" sai exatamente como antes (sem a chave "trechos")
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?, 'manutencao', 'Outro serviço')", (t0 - 10 * DAY,))
    plain = extras.impact(con)
    assert len(plain) == 1 and "trechos" not in plain[0]
    ids, ann, sid = _repair_history(con, t0)
    out = {x["texto"]: x for x in extras.impact(con)}
    tr = out["Troca dos bicos injetores"]["trechos"]
    assert len(tr) == 1 and tr[0]["trecho"] == "Trecho 1" and tr[0]["veredito"] == "melhorou"
    assert "Subida" not in json.dumps(out, ensure_ascii=False)
    assert "trechos" not in out["Outro serviço"]                                   # sem passagem perto: sem linha do trecho
    # um erro no trecho nunca derruba o "antes e depois"
    orig = segments.impact_for
    segments.impact_for = lambda *a, **k: 1 / 0
    try:
        assert "trechos" not in extras.impact(con)[0]
    finally:
        segments.impact_for = orig


def test_sync_gathers_routes_and_segments_only_with_location():
    from obdpi import sync as syncmod
    old_state = syncmod.STATE
    syncmod.STATE = Path(tempfile.mkdtemp()) / "sync_state.json"
    try:
        s = syncmod.Sync({"cloud": {"url": "https://nuvem.invalid", "token": "t"}, "gps": {"cloud": "auto"}})
        local = {"/api/gps/routes": {"routes": [{"id": 3}]}, "/api/gps/segments": {"segments": [{"id": 7}]}}
        s.local = lambda path, method="GET", body=None: (200, json.dumps(local.get(path, {})).encode())
        docs = []
        s.gather_gps(docs)                                          # sem 'loc: true' da nuvem: nada com posição
        assert [k for k, _ in docs] == ["/api/gps/status"]
        s.loc = True
        docs = []
        s.gather_gps(docs)
        keys = {k for k, _ in docs}
        assert {"/api/gps/routes", "/api/gps/routes/3", "/api/gps/segments", "/api/gps/segments/7"} <= keys
        assert all(syncmod.is_loc(k) for k in keys - {"/api/gps/status"})
    finally:
        syncmod.STATE = old_state


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
