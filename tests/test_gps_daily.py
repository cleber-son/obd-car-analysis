"""Testes do GPS — Fase 8 (rotina: postos, ações dos lugares, chegada, autonomia e trânsito), sem hardware.
docs/GPS-ARQUITETURA.md, seções 9.14, 9.16, 9.18 e 9.19. Tudo determinístico: simulador com semente fixa; os horários
de saída são montados no fuso local (time.mktime), então o resultado não depende do fuso em que o teste roda."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

from obdpi import config, db, notify
from obdpi.gps import Fix, eta, fuelstop, good, hub, places as placesmod, routes, segments, sim, traffic
from obdpi.gps.sim import HOME, T0

DAY = 86400_000


def new_db(path=None):
    return db.init(path or str(Path(tempfile.mkdtemp()) / "x.db"))


def local_ms(y, mo, d, h, mi):
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)) * 1000)


def weekdays(n, h, mi, start=(2026, 11, 2)):
    """n dias úteis seguidos (hora local), a partir de start."""
    out, d = [], 0
    while len(out) < n:
        t = local_ms(start[0], start[1], start[2] + d, h, mi)
        d += 1
        if time.localtime(t / 1000).tm_wday < 5:
            out.append(t)
    return out


def as_fix(f):
    return Fix(t_gps=f.t_gps, mono=f.mono, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, heading=f.heading, climb=f.climb,
               hdop=f.hdop, sats=f.sats, mode=f.mode, ts=f.t_gps)


def outbox(con):
    return [dict(r) for r in con.execute("SELECT title, body, expire_ts, ts FROM outbox ORDER BY id")]


# ---------------------------------------------------------------- 9.14 parada em posto
def refuel_db(gap_s=360, at_station=True, seed=3, notify_on=False, synced=True):
    con = new_db()
    a, b = sim.refuel(seed=seed, gap_s=gap_s, at_station=at_station)
    placesmod.create_place(con, "Posto da Esquina", "posto", *a.meta["station"])
    ta = sim.to_db(con, a, finalize=True)
    tb = sim.to_db(con, b, finalize=True)
    if not synced:
        con.execute("UPDATE session SET clock_synced=0 WHERE id=(SELECT session_id FROM trip WHERE id=?)", (tb,))
    ctx = hub.make_ctx(con, {"gps": {"notify_routine": notify_on}}, trip_id=tb, geo=None)
    return con, ta, tb, fuelstop.detect(con, tb, ctx)


def test_fuel_stop_detected():
    con, ta, tb, r = refuel_db(notify_on=True)
    assert r and r["status"] == "suspeito" and r["name"] == "Posto da Esquina" and r["min"] == 6
    rows = [dict(x) for x in con.execute("SELECT * FROM fuel_stop")]
    assert len(rows) == 1 and rows[0]["trip_before"] == ta and rows[0]["trip_after"] == tb and rows[0]["notified"] == 1
    ob = outbox(con)
    assert len(ob) == 1 and ob[0]["title"] == "Parou em Posto da Esquina por 6 min" and "Consumo" in ob[0]["body"]
    assert ob[0]["expire_ts"] - ob[0]["ts"] == 6 * 3600_000                         # vence em 6 h
    # reanálise (ou nova chamada) não duplica
    assert fuelstop.detect(con, tb, hub.make_ctx(con, {"gps": {"notify_routine": True}}, trip_id=tb, geo=None))["id"] == r["id"]
    assert con.execute("SELECT COUNT(*) FROM fuel_stop").fetchone()[0] == 1 and len(outbox(con)) == 1
    # padrão: aviso no celular desligado; a parada é gravada do mesmo jeito (aparece em Consumo)
    con2, _, _, r2 = refuel_db()
    assert r2 and con2.execute("SELECT notified FROM fuel_stop").fetchone()[0] == 0 and outbox(con2) == []
    # o posto do geo.db (sem lugar salvo) também vale: nome do OpenStreetMap
    con3 = new_db()
    a, b = sim.refuel(seed=4)
    sim.to_db(con3, a, finalize=True)
    tb3 = sim.to_db(con3, b, finalize=True)
    geo = new_db(str(Path(tempfile.mkdtemp()) / "geo.db"))
    geo.execute("CREATE TABLE poi (id INTEGER PRIMARY KEY, cell INTEGER, lat REAL, lon REAL, kind TEXT, name TEXT, brand TEXT)")
    from obdpi.gps.geo import cell, offset
    la, lo = offset(*a.meta["station"], 20, 15)
    geo.execute("INSERT INTO poi VALUES (1, ?, ?, ?, 'posto', 'Auto Posto Central', 'Ipiranga')", (cell(la, lo), la, lo))
    r3 = fuelstop.detect(con3, tb3, hub.make_ctx(con3, {}, trip_id=tb3, geo=geo))
    assert r3 and r3["place_id"] is None and r3["name"] == "Auto Posto Central (Ipiranga)"
    assert fuelstop.recent_stops(con3, geo, now=r3["ts_end"] + 60_000)[0]["place_name"] == "Auto Posto Central (Ipiranga)"


def test_fuel_stop_rejected_cases():
    for kw in ({"gap_s": 40 * 60}, {"gap_s": 60}, {"at_station": False}, {"synced": False}):
        con, _, _, r = refuel_db(notify_on=True, **kw)
        assert r is None, kw
        assert con.execute("SELECT COUNT(*) FROM fuel_stop").fetchone()[0] == 0 and outbox(con) == [], kw
    # viagem sem a anterior terminando no mesmo lugar (começou 2 km adiante: guincho, não posto)
    con = new_db()
    a, b = sim.towed(seed=2)
    b = sim.simulate(sim._legs(b.meta["start"], [(0, 0, 10, 15), (-1500, 0, 50, 20)]), start=b.meta["start"],
                     t0=a.obd[-1]["ts"] + 300_000, seed=5)
    placesmod.create_place(con, "Posto", "posto", *a.meta["end"])
    sim.to_db(con, a, finalize=True)
    tb = sim.to_db(con, b, finalize=True)
    assert fuelstop.detect(con, tb, hub.make_ctx(con, {}, trip_id=tb, geo=None)) is None
    # funções puras
    assert fuelstop.judge((0, 0), (0, 0), 300, True, {"x": 1}) is None
    assert fuelstop.judge((0, 0), (0, 0), 300, True, None) == "sem_posto"
    assert fuelstop.judge(None, (0, 0), 300, True, {}) == "sem_posicao"


def test_fuel_stop_engine_waits_for_first_fix():
    """Ao vivo: no início da viagem a posição ainda não chegou (partida a frio); a detecção roda na 1ª posição boa."""
    con = new_db()
    a, b = sim.refuel(seed=6)
    placesmod.create_place(con, "Posto", "posto", *a.meta["station"])
    sim.to_db(con, a, finalize=True)
    tb = sim.to_db(con, b, with_gps=False)                     # as posições desta viagem ainda não estão no banco
    h = SimpleNamespace(con=con, cfg={"notify_routine": True}, clock=SimpleNamespace(synced=True), _geo=lambda: None)
    e = fuelstop.FuelStopEngine(h)
    e.on_trip_start(tb, b.obd[0]["ts"], False)
    assert e.pending and con.execute("SELECT COUNT(*) FROM fuel_stop").fetchone()[0] == 0
    f = as_fix(next(x for x in b.fixes if good(x)))
    e.feed(SimpleNamespace(fix=f, trip_id=tb, values={}, ts=f.ts))
    assert e.pending is None and con.execute("SELECT COUNT(*) FROM fuel_stop").fetchone()[0] == 1
    assert outbox(con)[0]["title"].startswith("Parou em Posto")
    # viagem retomada (o Pi caiu andando) não é parada
    e.on_trip_start(tb, b.obd[0]["ts"], True)
    assert e.pending is None


def _app(base):
    """obdpi.web.app apontando para um banco de teste (as funções são chamadas direto)."""
    from obdpi.web import app as A
    old = (A.DB, A.CFG)
    A.DB = str(base / "obd.db")
    A.CFG = copy.deepcopy(A.CFG)
    A.CFG["gps"]["geo_db"] = str(base / "sem-geo.db")
    return A, old


def test_fill_links_station():
    base = Path(tempfile.mkdtemp())
    con = new_db(str(base / "obd.db"))
    a, b = sim.refuel(seed=3)
    pid = placesmod.create_place(con, "Posto da Esquina", "posto", *a.meta["station"])
    sim.to_db(con, a, finalize=True)
    tb = sim.to_db(con, b, finalize=True)
    st = fuelstop.detect(con, tb, hub.make_ctx(con, {}, trip_id=tb, geo=None))
    con.commit()
    A, old = _app(base)
    try:
        # cartão "Abastecimento detectado": só o valor, a hora da parada e a parada
        r = A.fuel_fill({"total": 200, "price_l": 6.0, "ts": st["ts_end"] - 60_000, "stop_id": st["id"]})
        assert r["ok"] and r["posto"] == {"place_id": pid, "name": "Posto da Esquina", "stop_id": st["id"]}
        c = db.connect(str(base / "obd.db"), readonly=True)
        assert c.execute("SELECT place_id FROM fuel_fill").fetchone()[0] == pid
        s = c.execute("SELECT status, fill_id FROM fuel_stop WHERE id=?", (st["id"],)).fetchone()
        assert s["status"] == "confirmado" and s["fill_id"] == c.execute("SELECT id FROM fuel_fill").fetchone()[0]
        # parada inexistente: erro e nada gravado
        try:
            A.fuel_fill({"total": 100, "stop_id": 999})
            raise AssertionError("devia recusar")
        except A.HTTPException as e:
            assert e.status_code == 404
        assert c.execute("SELECT COUNT(*) FROM fuel_fill").fetchone()[0] == 1
        # posto escolhido à mão
        r = A.fuel_fill({"total": 150, "ts": st["ts_end"] + 5 * DAY, "place_id": pid})
        assert r["posto"]["place_id"] == pid
        # botão normal (sem stop_id): procura a parada suspeita a até 30 min…
        con.execute("UPDATE fuel_stop SET status='suspeito', fill_id=NULL")
        con.commit()
        r = A.fuel_fill({"total": 180, "ts": st["ts_end"] + 20 * 60_000})
        assert r["posto"]["stop_id"] == st["id"]
        # …ou a posição daquele instante (parado no posto, sem parada suspeita): associa pelo lugar
        fix = c.execute("SELECT ts FROM gps_fix WHERE trip_id=? ORDER BY ts DESC LIMIT 1", (tb - 1,)).fetchone()[0]
        r = A.fuel_fill({"total": 120, "ts": fix + 30_000})
        assert r["posto"] == {"place_id": pid, "name": "Posto da Esquina", "stop_id": None}
    finally:
        A.DB, A.CFG = old


def test_fill_without_gps_is_unchanged():
    """Sem GPS (nem parada nem posição), o botão "Tanque cheio" responde e grava exatamente como antes."""
    base = Path(tempfile.mkdtemp())
    new_db(str(base / "obd.db")).close()
    A, old = _app(base)
    try:
        r1 = A.fuel_fill({"total": 200, "price_l": 6.25, "ts": T0})
        r2 = A.fuel_fill({"total": 150, "ts": T0 + DAY})
        keys = {"ok", "real_kml", "km_since_prev", "mix_after", "liters", "price_l", "cost_km"}
        assert set(r1) == keys and set(r2) == keys and r2["liters"] == 24.0 and r2["price_l"] == 6.25
        c = db.connect(str(base / "obd.db"), readonly=True)
        assert [x[0] for x in c.execute("SELECT place_id FROM fuel_fill")] == [None, None]
        assert c.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM annotation WHERE kind='abastecimento'").fetchone()[0] == 2
    finally:
        A.DB, A.CFG = old


def test_station_ranking():
    con = new_db()
    ids = {n: placesmod.create_place(con, n, "posto", HOME[0] + i * 0.01, HOME[1]) for i, n in enumerate(("Posto A", "Posto B", "Posto C"))}
    seq = [("Posto A", None, 6.10), ("Posto B", 12.0, 6.30), ("Posto C", 10.0, 5.90), ("Posto A", 9.0, 6.20),
           ("Posto B", 12.4, 6.40), ("Posto C", 10.2, 5.95), ("Posto A", 9.2, 6.15)]
    for i, (n, kml, price) in enumerate(seq):          # o km/L medido num abastecimento é do tanque anterior
        con.execute("INSERT INTO fuel_fill(ts, fuel, liters, price_l, full, mix_after, real_kml, place_id, total) VALUES (?,?,?,?,?,?,?,?,?)",
                    (T0 + i * 7 * DAY, "gasolina", 40, price, 1, 0.0, kml, ids[n], 40 * price))
    st = {s["name"]: s for s in fuelstop.stations(con)}
    assert [s["name"] for s in fuelstop.stations(con)] == ["Posto A", "Posto B", "Posto C"]
    assert st["Posto A"]["kml"]["n"] == 2 and st["Posto A"]["kml"]["avg"] == 12.2       # tanques de A medidos em B
    assert st["Posto B"]["kml"]["avg"] == 10.1 and st["Posto C"]["kml"]["avg"] == 9.1
    avg = (12 + 10 + 9 + 12.4 + 10.2 + 9.2) / 6
    assert abs(st["Posto A"]["kml"]["vs_media_pct"] - (12.2 / avg - 1) * 100) < 0.1 and st["Posto C"]["kml"]["vs_media_pct"] < -10
    assert st["Posto A"]["n"] == 3 and st["Posto A"]["fuels"]["gasolina"]["last_price"] == 6.15
    assert "a mais" in st["Posto A"]["opiniao"] and "a menos" in st["Posto C"]["opiniao"]
    # um tanque só: não opina
    con.execute("DELETE FROM fuel_fill WHERE place_id=? AND ts > ?", (ids["Posto C"], T0 + 3 * DAY))
    c = {s["name"]: s for s in fuelstop.stations(con)}["Posto C"]
    assert c["kml"]["vs_media_pct"] is None and "Poucos dados" in c["opiniao"]


# ---------------------------------------------------------------- 9.16 ações dos lugares
def geo_engine(con, run_dir, **cfg):
    return placesmod.GeofenceEngine(SimpleNamespace(con=con, cfg=cfg, run_dir=run_dir, clock=SimpleNamespace(synced=True)))


def walk(eng, ts, pts, trip_id=1):
    for la, lo in pts:
        ts[0] += 1000
        eng.feed(SimpleNamespace(fix=Fix(t_gps=ts[0], mono=0, lat=la, lon=lo, speed=20, hdop=1, sats=9, mode=3, ts=ts[0]),
                                 trip_id=trip_id, engine_on=True, values={}, value_ts={}, ts=ts[0]))


def test_place_actions():
    from obdpi.gps.geo import offset
    con = new_db()
    run = Path(tempfile.mkdtemp())
    casa = placesmod.create_place(con, "Casa", "casa", *HOME)                       # padrão: sincronizar ao entrar
    trab = sim.commute_end()
    placesmod.create_place(con, "Trabalho", "trabalho", *trab, actions=[
        {"on": "entrou", "do": "avisar"}, {"on": "entrou", "do": "falar"}, {"on": "saiu", "do": "nota", "arg": "Saí do trabalho"}])
    assert placesmod.load_places(con)[0]["actions"] == [{"on": "entrou", "do": "sincronizar"}]
    eng = geo_engine(con, run)
    ts = [T0]
    far = offset(*HOME, 0, 600)
    walk(eng, ts, [far] * 8)                                   # começa fora: estado inicial em silêncio
    walk(eng, ts, [HOME] * 5)                                  # entrou em Casa
    assert (run / "sync_cmd").exists() and json.loads((run / "sync_cmd").read_text())["why"] == f"entrou:{casa}"
    (run / "sync_cmd").unlink()
    walk(eng, ts, [HOME] * 20)                                 # continua dentro: não cria de novo
    assert not (run / "sync_cmd").exists()
    walk(eng, ts, [far] * 8 + [HOME] * 5)                      # saiu e voltou: de novo
    assert (run / "sync_cmd").exists()
    assert outbox(con) == []                                   # sincronizar não manda aviso
    # Trabalho: aviso de chegada (vence em 30 min), fala no gps.json e nota ao sair
    tfar = offset(*trab, 0, 600)
    walk(eng, ts, [tfar] * 8 + [trab] * 5)
    ob = outbox(con)
    assert len(ob) == 1 and ob[0]["title"] == "Chegou em Trabalho" and ob[0]["expire_ts"] - ob[0]["ts"] == 30 * 60_000
    say = eng.private()["say"]
    assert say["text"] == "Chegando em Trabalho." and eng.private()["place"]["name"] == "Trabalho"
    walk(eng, ts, [trab] * 130)                                # a fala some depois de 2 min
    assert eng.private()["say"] is None
    walk(eng, ts, [tfar] * 8)
    assert [tuple(x) for x in con.execute("SELECT kind, text FROM annotation")] == [("nota", "Saí do trabalho")]
    assert eng.live() == {}                                    # nada de nome de lugar no live.json
    # aviso de chegada com mais de 30 min não é enviado (flush_outbox marca como vencido)
    p = str(run / "o.db")
    c2 = db.init(p)
    placesmod.push(c2, "Chegou em Casa", "Às 18:00.", ts=int(time.time() * 1000) - 31 * 60_000, expire_ms=30 * 60_000)
    placesmod.push(c2, "Chegou em Trabalho", "Às 08:00.", ts=int(time.time() * 1000), expire_ms=30 * 60_000)
    c2.commit()
    sent, orig = [], notify.send
    notify.send = lambda server, topic, title, *a, **k: sent.append(title) or True
    try:
        assert notify.flush_outbox(p, "https://ntfy.invalid", "topico") == 1
    finally:
        notify.send = orig
    assert sent == ["Chegou em Trabalho"] and c2.execute("SELECT sent_ts FROM outbox WHERE title='Chegou em Casa'").fetchone()[0] < 0


def _oficina_db(gap_min, synced=True):
    con = new_db()
    ofi = sim.offset(*HOME, 300, 0)
    placesmod.create_place(con, "Oficina do Zé", "oficina", *ofi)                  # padrão: pós-serviço ao partir depois de 20 min
    s1 = con.execute("INSERT INTO session(started_ts, last_ts, clock_synced) VALUES (?,?,?)", (T0 - DAY, T0, 1)).lastrowid
    prev_end = T0
    con.execute("INSERT INTO trip(session_id, start_ts, end_ts, finalized) VALUES (?,?,?,1)", (s1, T0 - 900_000, prev_end))
    for i in range(3):                                          # o carro ficou parado na oficina
        con.execute("INSERT INTO gps_fix(ts, lat, lon, speed, hdop, sats, mode) VALUES (?,?,?,?,?,?,3)",
                    (prev_end - 60_000 + i * 20_000, ofi[0], ofi[1], 0.0, 0.9, 9))
    start = prev_end + gap_min * 60_000
    s2 = con.execute("INSERT INTO session(started_ts, last_ts, clock_synced) VALUES (?,?,?)", (start, start, int(synced))).lastrowid
    tid = con.execute("INSERT INTO trip(session_id, start_ts, end_ts) VALUES (?,?,?)", (s2, start, start)).lastrowid
    return con, ofi, tid, start


def test_left_after_action():
    for gap, notify_on, want_ann, want_push in ((190, True, True, True), (190, False, True, False), (5, True, False, False)):
        con, ofi, tid, start = _oficina_db(gap)
        eng = placesmod.GeofenceEngine(SimpleNamespace(con=con, cfg={"notify_routine": notify_on}, run_dir=None, clock=SimpleNamespace(synced=None)))
        eng.on_trip_start(tid, start, False)
        ts = [start + 40_000]
        walk(eng, ts, [ofi] * 3, trip_id=tid)
        ann = con.execute("SELECT ts, kind, text FROM annotation").fetchall()
        if want_ann:
            assert [tuple(a) for a in ann] == [(T0, "oficina", "Visita à oficina, 3 h 10")], (gap, ann)
        else:
            assert ann == [], gap
        ob = outbox(con)
        assert (len(ob) == 1 and ob[0]["title"] == "Saiu da oficina: anote o serviço e rode o teste") if want_push else ob == [], (gap, ob)
        walk(eng, ts, [ofi] * 3, trip_id=tid)                  # uma vez por viagem
        assert len(con.execute("SELECT * FROM annotation").fetchall()) == (1 if want_ann else 0)
    # relógio sem sincronizar numa das sessões: não dá para saber quanto tempo ficou parado → nada
    con, ofi, tid, start = _oficina_db(190, synced=False)
    eng = placesmod.GeofenceEngine(SimpleNamespace(con=con, cfg={"notify_routine": True}, run_dir=None, clock=SimpleNamespace(synced=None)))
    eng.on_trip_start(tid, start, False)
    walk(eng, [start], [ofi] * 3, trip_id=tid)
    assert con.execute("SELECT COUNT(*) FROM annotation").fetchone()[0] == 0 and outbox(con) == []
    assert placesmod.due_actions({"actions": [{"on": "partiu_apos", "do": "nota", "arg": 20}]}, "partiu_apos", 19 * 60) == []
    assert placesmod.fmt_dur(45 * 60) == "45 min" and placesmod.fmt_dur(3 * 3600 + 600) == "3 h 10"


# ---------------------------------------------------------------- 9.18 chegada e autonomia
_ETA_DB = {}


def eta_db():
    """8 viagens casa → trabalho em dias úteis às 7h10 (hora local) e um tanque cheio registrado."""
    if "con" not in _ETA_DB:
        con = new_db()
        sim.add_places(con)
        for i, t in enumerate(weekdays(8, 7, 10)):
            sim.to_db(con, sim.commute(seed=i + 1, t0=t), finalize=True)
        con.execute("INSERT INTO fuel_fill(ts, fuel, liters, price_l, full, mix_after, real_kml) VALUES (?,?,?,?,?,?,?)",
                    (weekdays(1, 6, 0)[0], "gasolina", 40, 6.0, 1, 0.0, 11.0))
        con.commit()
        _ETA_DB["con"] = con
    return _ETA_DB["con"]


def drive(con, trip):
    """Passa a viagem simulada pelo EtaEngine como o hub passaria. Devolve [(km rodados, ts, dest)] a cada avaliação."""
    tid = con.execute("INSERT INTO trip(start_ts, end_ts) VALUES (?,?)", (trip.t0, trip.t0)).lastrowid
    e = eta.EtaEngine(SimpleNamespace(con=con, cfg={}, vehicle={"tank_l": 50}))
    e.on_trip_start(tid, trip.t0, False)
    out = []
    for f in trip.fixes:
        g = as_fix(f)
        if not good(g):
            continue
        prev = e.last_eval
        e.feed(SimpleNamespace(fix=g, trip_id=tid, values={"fuel_lh": 3.0}, ts=g.ts))
        if e.last_eval != prev:
            out.append((e.traveled / 1000, g.ts, dict(e.dest) if e.dest else None, e.range))
    con.execute("DELETE FROM trip WHERE id=?", (tid,))
    return out, e


def test_eta_prediction():
    con = eta_db()
    t9 = weekdays(9, 7, 12)[-1]
    trip = sim.commute(seed=30, t0=t9)
    evs, e = drive(con, trip)
    km = trip.meta["km"]
    first = next(x for x in evs if x[2])
    assert first[0] < 0.3 * km and first[2]["name"] == "Trabalho" and first[2]["conf"] > 0.6
    end = trip.obd[-1]["ts"]
    half = next(x for x in evs if x[0] >= km / 2 and x[2])
    left_true = (end - half[1]) / 1000
    left_pred = (half[2]["eta_ts"] - half[1]) / 1000
    assert abs(left_pred - left_true) / left_true < 0.10, (left_pred, left_true)
    assert abs(half[2]["left_km"] - km / 2) < 0.7
    assert e.private()["dest"]["name"] == "Trabalho" and "range_km" in e.live()
    assert set(e.live()) == {"range_km"}                       # no live.json (público) só o número da autonomia


def test_eta_unknown_route():
    con = eta_db()
    trip = sim.commute_alt(seed=31, t0=weekdays(10, 7, 10)[-1], detour=True)      # mesmas pontas, outro caminho
    evs, e = drive(con, trip)
    assert len(evs) > 20 and all(x[2] is None for x in evs)
    assert e.private() == {"dest": None}
    # funções puras: confiança e dominância
    a, b = {"freq": 8.0, "cells": {1, 2, 3, 4}}, {"freq": 1.0, "cells": {1, 2, 3, 4}}
    sc = eta.score([a, b], {1, 2, 3, 4})
    assert eta.pick(sc)[1] is a and abs(sc[0][0] - 8 / 9) < 1e-9
    assert eta.pick(eta.score([a, {"freq": 6.0, "cells": {1, 2, 3, 4}}], {1, 2, 3, 4})) is None     # sem o dobro da segunda
    assert eta.score([a], {1, 2, 9, 10}) == []                 # metade fora da rota


def test_range():
    assert eta.range_km(50, 30, 11) == 220.0
    assert eta.range_km(50, 30, 11, left_km=8, kml_route=8) == 8 + (20 - 1) * 11       # o que falta da rota a 8 km/L
    assert eta.range_km(50, 49.5, 11, left_km=8, kml_route=8) == 4.0                    # não chega ao destino
    assert eta.range_km(50, 30, None) is None
    con = eta_db()
    fs = eta.fuel_state(con)
    assert fs["kml"] == 11.0 and 8 < fs["used_l"] < 20                                  # 8 viagens de ~1,5 L desde o tanque cheio
    assert new_db() and eta.fuel_state(new_db()) is None                               # sem tanque cheio: "—"
    # baixo: aviso uma vez por viagem só com notify_routine; o posto mais bem colocado perto entra no texto
    c = new_db()
    sim.add_places(c)
    placesmod.create_place(c, "Posto Bom", "posto", *sim.offset(*HOME, 0, 400))
    c.execute("INSERT INTO fuel_fill(ts, fuel, liters, full, real_kml) VALUES (?, 'gasolina', 40, 1, 11.0)", (T0 - DAY,))
    c.execute("INSERT INTO trip(start_ts, end_ts, finalized, fuel_l, distance_km) VALUES (?,?,1,45.5,500)", (T0 - DAY + 1000, T0 - 1000))
    for notify_on, want in ((True, 1), (False, 0)):
        c.execute("DELETE FROM outbox")
        trip = sim.straight(3.0, seed=40, t0=T0)
        tid = c.execute("INSERT INTO trip(start_ts, end_ts) VALUES (?,?)", (trip.t0, trip.t0)).lastrowid
        e = eta.EtaEngine(SimpleNamespace(con=c, cfg={"notify_routine": notify_on}, vehicle={"tank_l": 50}))
        e.on_trip_start(tid, trip.t0, False)
        for f in trip.fixes:
            g = as_fix(f)
            if good(g):
                e.feed(SimpleNamespace(fix=g, trip_id=tid, values={}, ts=g.ts))
        assert e.range is not None and e.range < 60 and e.live()["range_km"] == round(e.range)
        ob = outbox(c)
        assert len(ob) == want and (not want or ("Autonomia baixa" == ob[0]["title"] and "Posto Bom" in ob[0]["body"]))


# ---------------------------------------------------------------- 9.19 trânsito
def test_traffic_best_departure():
    assert hub.FINALIZERS.index(traffic.finalize) < hub.FINALIZERS.index(routes.assign_trip)
    assert hub.FINALIZERS[-2:] == [routes.assign_trip, segments.scan_trip]
    con = new_db()
    sim.add_places(con)
    days = weekdays(12, 7, 10)
    for i, t in enumerate(days):
        slow = i % 2 == 1                                      # metade sai às 7h40 e pega 10 min a mais de semáforo
        sim.to_db(con, sim.commute_alt(seed=i + 1, t0=t + (30 * 60_000 if slow else 0), extra_stop_s=600 if slow else 0), finalize=True)
    rep = traffic.report(con, days[0] - DAY)
    r = rep["routes"][0]
    assert r["name"] == "Casa → Trabalho" and r["n"] == 12 and r["status"] == "ok"
    best = r["melhor"]
    assert best["melhor"] == "7h10" and best["habitual"] == "7h40" and best["ganha"]
    assert abs(best["ganho_s"] - 600) <= 120 and best["ganho_l"] > 0
    assert r["texto"].startswith("Saindo às 7h10 em vez de 7h40 você ganha 10 min")
    assert 0 < r["delay_s"] <= 700 and r["stopped_s"] > 200
    tj = json.loads(con.execute("SELECT traffic_json FROM trip_gps WHERE trip_id=2").fetchone()[0])
    tj1 = json.loads(con.execute("SELECT traffic_json FROM trip_gps WHERE trip_id=1").fetchone()[0])
    assert tj["stopped_s"] - tj1["stopped_s"] > 450 and tj["stopped_l"] > tj1["stopped_l"]      # os 10 min a mais aparecem parados
    assert rep["hotspots"] and all(len(h) == 5 for h in rep["hotspots"])                    # os semáforos: "onde eu fico parado"
    # rota com 4 viagens: poucos dados
    c2 = new_db()
    sim.add_places(c2)
    for i, t in enumerate(weekdays(4, 7, 10)):
        sim.to_db(c2, sim.commute(seed=i + 1, t0=t), finalize=True)
    r2 = traffic.report(c2, 0)["routes"][0]
    assert r2["status"] == "poucos dados" and r2["n"] == 4 and "Poucos dados" in r2["texto"]


def test_trip_traffic_states():
    """Função pura: parado com o motor ligado, arrastando (3–15 km/h por 20 s ou mais) e os litros de cada estado."""
    fixes, fast = [], []
    t = 0
    for s, v in ((30, 0.3), (40, 8.0), (10, 9.0), (60, 40.0), (12, 6.0), (30, 40.0)):     # parado, arrastando, andando, 12 s devagar (não conta)
        for _ in range(s):
            fixes.append({"ts": t, "speed": v})
            fast.append((t, 800 if v < 1 else 2000, 1.0 if v < 1 else 3.6))
            t += 1000
    d = traffic.trip_traffic(fixes, fast)
    assert d["stopped_s"] == 30 and d["crawl_s"] == 50 and d["moving_s"] == 101
    assert abs(d["stopped_l"] - 30 / 3600) < 1e-3 and abs(d["crawl_l"] - 50 * 3.6 / 3600) < 2e-3
    # motor desligado parado (rpm 0) não conta como parado no trânsito
    d2 = traffic.trip_traffic([{"ts": i * 1000, "speed": 0.0} for i in range(30)], [(i * 1000, 0, 0.0) for i in range(30)])
    assert d2["stopped_s"] == 0
    assert traffic.best_time([{"n": 5, "dur_s": 900, "fuel_l": 1, "saida": "7h10", "saida_min": 430}])["ganha"] is False
    assert traffic.percentile([1, 2, 3, 4, 5], 0.1) == 1.4


# ---------------------------------------------------------------- hub, API e nuvem
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
    synced, source = True, "sistema"
    def now(self):
        return int(time.time() * 1000)


def test_hub_f8_private_and_public():
    base = Path(tempfile.mkdtemp())
    con = eta_db()
    h = hub.GpsHub({"gps": {"source": "gpsd", "geo_db": str(base / "nada.db")}}, con, Clock(), config.State(str(base / "s.json")),
                   lambda *a: None, lambda *a: None, base)
    kinds = {type(e) for e in h.engines}
    assert {fuelstop.FuelStopEngine, eta.EtaEngine, placesmod.GeofenceEngine} <= kinds
    h.reader = FakeReader()
    trip = sim.commute(seed=33, t0=weekdays(11, 7, 10)[-1])
    start = Clock().now()                                     # o hub carimba as posições no relógio de agora
    tid = con.execute("INSERT INTO trip(start_ts, end_ts) VALUES (?,?)", (start, start)).lastrowid
    h.on_trip_start(tid, start, False)
    now_m = time.monotonic()
    seen, dests = [], []
    for k in range(0, len(trip.fixes), 25):
        fs = [Fix(t_gps=f.t_gps, mono=now_m, lat=f.lat, lon=f.lon, alt=f.alt, speed=f.speed, heading=f.heading, hdop=f.hdop,
                  sats=f.sats, mode=f.mode) for f in trip.fixes[k:k + 25]]
        h.reader.push(fs)
        h.pump(fs[-1].t_gps, {"fuel_lh": 3.0}, {}, True, tid)
        seen.append(h.live())
        dests.append(json.loads((base / "gps.json").read_text()).get("dest"))
    assert any(d and d["name"] == "Trabalho" and d["eta_ts"] for d in dests)            # destino e chegada no gps.json (privado)
    txt = json.dumps(seen, ensure_ascii=False)
    for bad in ("Trabalho", "Casa", '"dest"', '"say"', '"lat"', '"lon"', "eta_ts"):
        assert bad not in txt, bad
    assert any("range_km" in x for x in seen)                 # autonomia (número) no live.json
    con.execute("DELETE FROM trip WHERE id=?", (tid,))
    con.execute("DELETE FROM gps_fix WHERE ts >= ?", (start - 60_000,))
    con.commit()


def test_api_f8_routes():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        return
    from obdpi.web import gps_api
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    p = str(base / "obd.db")
    con = new_db(p)
    a, b = sim.refuel(seed=3, t0=int(time.time() * 1000) - 3600_000)           # há 1 h: o cartão mostra os últimos 3 dias
    placesmod.create_place(con, "Posto da Esquina", "posto", *a.meta["station"])
    sim.to_db(con, a, finalize=True)
    tb = sim.to_db(con, b, finalize=True)
    st = fuelstop.detect(con, tb, hub.make_ctx(con, {}, trip_id=tb, geo=None))
    con.commit()
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": p}
    cfg["gps"]["geo_db"] = str(base / "nada.db")
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(p, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    j = c.get("/api/gps/stations").json()
    assert len(j["stops"]) == 1 and j["stops"][0]["id"] == st["id"] and j["stops"][0]["place_name"] == "Posto da Esquina" and j["stops"][0]["min"] == 6
    assert j["stations"][0]["name"] == "Posto da Esquina" and "Poucos dados" in j["stations"][0]["opiniao"]
    assert c.post(f"/api/gps/fuelstop/{st['id']}", json={"status": "descartado"}).json() == {"ok": True}
    assert con.execute("SELECT status FROM fuel_stop").fetchone()[0] == "descartado"
    assert c.post(f"/api/gps/fuelstop/{st['id']}", json={"status": "bobagem"}).status_code == 400
    # confirmar pelo painel online (fila de localização): registra o tanque cheio na hora da parada, com a parada
    got, old_hook = [], gps_api.FILL_HOOK
    gps_api.FILL_HOOK = lambda body, tok: got.append(body) or {"ok": True}
    try:
        assert c.post(f"/api/gps/fuelstop/{st['id']}", json={"status": "confirmado"}).status_code == 400       # sem o valor
        assert c.post(f"/api/gps/fuelstop/{st['id']}", json={"status": "confirmado", "total": "180", "x": 1}).json() == {"ok": True}
        assert got == [{"total": "180", "stop_id": st["id"], "full": True, "ts": st["ts_end"] - 60_000}]
    finally:
        gps_api.FILL_HOOK = old_hook
    assert c.post("/api/gps/fuelstop/999", json={"status": "descartado"}).status_code == 404
    tr = c.get("/api/gps/traffic?days=60").json()
    assert tr["days"] == 60 and "routes" in tr and "hotspots" in tr
    for k in ("/api/gps/stations", "/api/gps/traffic?days=60", "/api/gps/fuelstop/1"):
        assert syncmod.is_loc(k), k                           # nomes e posições: só com a senha de localização
    assert syncmod.doc_key("/api/gps/traffic?days=60&x=1") == "/api/gps/traffic?days=60"


def test_sync_f8_docs_and_sync_now():
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    old = (syncmod.STATE, syncmod.SYNC_CMD)
    syncmod.STATE, syncmod.SYNC_CMD = base / "sync_state.json", base / "sync_cmd"
    try:
        s = syncmod.Sync({"cloud": {"url": "https://nuvem.invalid", "token": "t"}, "gps": {"cloud": "auto"}})
        s.local = lambda path, method="GET", body=None: (200, b"{}")
        docs = []
        s.gather_gps(docs)
        assert [k for k, _ in docs] == ["/api/gps/status"]   # sem senha de localização na nuvem: nada com posição
        s.loc = True
        docs = []
        s.gather_gps(docs)
        keys = [k for k, _ in docs]
        assert "/api/gps/stations" in keys and "/api/gps/traffic?days=60" in keys
        assert not s.sync_requested()
        syncmod.SYNC_CMD.write_text('{"ts": 1}')
        assert s.sync_requested() and not syncmod.SYNC_CMD.exists() and not s.sync_requested()
    finally:
        syncmod.STATE, syncmod.SYNC_CMD = old


# ---------------------------------------------------------------- F9: compartilhar a viagem ao vivo (9.21) e modo sempre ligado (9.7)
from obdpi.gps import share as sharemod
import hashlib, random


def test_share_token_and_frame():
    toks = set()
    for _ in range(50):
        tok, h = sharemod.new_token()
        assert sharemod.TOKEN_RE.match(tok) and len(tok) == 22 and h == hashlib.sha256(tok.encode()).hexdigest()
        toks.add(tok)
    assert len(toks) == 50
    assert sharemod.parse_request({"mode": "1h"})["minutes"] == 60 and sharemod.parse_request({"mode": "4h"})["minutes"] == 240
    assert sharemod.parse_request({"mode": "24h"}) == {"mode": "tempo", "minutes": 1440, "label": "", "dest": False}
    assert sharemod.parse_request({"mode": "sempre"})["mode"] == "sempre" and sharemod.parse_request({"mode": "sempre"})["minutes"] is None
    v = sharemod.parse_request({"mode": "viagem", "label": "<b>Para a Ana</b>\n" + "x" * 80, "dest": "sim"})
    assert v["mode"] == "viagem" and v["minutes"] == 1440 and "<" not in v["label"] and len(v["label"]) == 40 and v["dest"] is False
    for bad in ({}, {"minutes": 2}, {"minutes": 2000}, {"minutes": "x"}, {"mode": "nunca"}):
        try:
            sharemod.parse_request(bad)
            assert False, bad
        except ValueError:
            pass
    ts = 1_800_000_000_000
    live = {"ts": ts, "trip_id": 31, "engine_on": True, "values": {"speed": 62, "rpm": 2400, "ect": 91, "kml_inst": 13.4, "fuel_lh": 4.6, "batt": 14},
            "value_ts": {"speed": ts - 500, "rpm": ts - 500, "ect": ts - 10_000, "kml_inst": ts - 500, "fuel_lh": ts - 60_000},
            "status": {"ect": ["ok", 91]}, "trip": {"elapsed_s": 900, "km": 7.42, "kml": 11.8},
            "alerts": [{"ts": ts - 60_000, "sev": "warn", "title": "Temperatura subindo", "detail": "detalhe técnico"}, {"ts": ts, "sev": "info", "title": "x"}],
            "gps": {"state": "ok", "sats": 9}}
    gps = {"state": "ok", "lat": -23.550123, "lon": -46.633111, "speed": 61.0, "heading": 181, "fix_ts": ts - 300, "trip_id": 31,
           "place": {"id": 1, "name": "Casa"}, "parked": {"lat": 1, "lon": 2}, "say": {"text": "Chegando em Casa."},
           "dest": {"name": "Trabalho", "route": "Casa → Trabalho", "eta_ts": ts + 600_000, "left_km": 7.4, "conf": 0.8}}
    f = sharemod.frame(live, gps)
    assert f["lat"] == -23.55012 and f["lon"] == -46.63311 and f["hdg"] == 181 and f["spd"] == 62 and f["rpm"] == 2400 and f["ect"] == 91
    assert f["kml"] == 13.4 and f["lh"] is None and f["trip_s"] == 900 and f["trip_km"] == 7.4 and f["alert"] == "Temperatura subindo"
    assert f["eta"] == ts + 600_000 and f["left_km"] == 7.4 and "dest" not in f and f["ect_st"] == "ok"
    txt = json.dumps(f, ensure_ascii=False)
    for bad in ("Casa", "Trabalho", "place", "parked", "say", "route", "detalhe", "batt"):
        assert bad not in txt, bad
    assert sharemod.frame(live, gps, want_dest=True)["dest"] == "Trabalho"                 # só se o dono marcou
    assert sharemod.frame({**live, "trip_id": None}, gps) == {"ts": ts, "trip": None}        # sem viagem: nenhuma posição
    old = sharemod.frame(live, {**gps, "fix_ts": ts - 30_000})
    assert "lat" not in old and old["spd"] == 62                                             # posição velha não vai
    assert "lat" not in sharemod.frame(live, {**gps, "trip_id": 30})                         # posição de outra viagem não vai


def _share_app(base, cloud=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from obdpi.web import gps_api
    p = str(base / "obd.db")
    con = new_db(p)
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": p}
    cfg["gps"]["geo_db"] = str(base / "nada.db")
    if cloud:
        cfg["cloud"] = {"url": "https://obd.exemplo/", "token": "t"}
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(p, readonly=True), lambda t: None, cfg, base))
    return con, TestClient(app)


def test_share_requires_cloud():
    try:
        import fastapi  # noqa: F401
    except ImportError:
        return
    base = Path(tempfile.mkdtemp())
    _, c = _share_app(base, cloud=False)
    r = c.post("/api/gps/share", json={"mode": "1h"})
    assert r.status_code == 400 and "painel online" in r.json()["detail"]
    assert not (base / "share.json").exists()
    assert c.get("/api/gps/share").json()["cloud"] is False
    # com nuvem: o link sai uma vez; no Pi fica só o hash, pendente até o obd-sync enviar
    base = Path(tempfile.mkdtemp())
    _, c = _share_app(base)
    (base / "live.json").write_text(json.dumps({"trip_id": 12}))
    r = c.post("/api/gps/share", json={"mode": "viagem", "dest": True, "label": "Ana"})
    assert r.status_code == 200
    j = r.json()
    assert j["url"].startswith("https://obd.exemplo/s/") and j["pending"] is True
    tok = j["url"].rsplit("/", 1)[1]
    assert sharemod.TOKEN_RE.match(tok)
    raw = (base / "share.json").read_text()
    assert tok not in raw and hashlib.sha256(tok.encode()).hexdigest() in raw
    rec = json.loads(raw)["links"][0]
    assert rec["id"] == j["id"] and rec["mode"] == "viagem" and rec["trip"] == 12 and rec["state"] == "pendente" and rec["dest"] is True
    assert rec["expires_ts"] - rec["created_ts"] == 24 * 3600_000
    assert c.post("/api/gps/share", json={"mode": "nunca"}).status_code == 400
    lst = c.get("/api/gps/share").json()
    assert lst["cloud"] is True and lst["links"][0]["state"] == "pendente" and "hash" not in json.dumps(lst)
    assert lst["modes"] == ["24h", "sempre"]                                    # a tela oferece só estas duas (decisão do dono)
    # "até eu cancelar": sem prazo (data-sentinela no arquivo; a API mostra None)
    r = c.post("/api/gps/share", json={"mode": "sempre"})
    assert r.status_code == 200 and r.json()["expires_ts"] is None and r.json()["mode"] == "sempre"
    rec2 = [x for x in json.loads((base / "share.json").read_text())["links"] if x["id"] == r.json()["id"]][0]
    assert rec2["expires_ts"] == sharemod.FOREVER_TS
    it = [x for x in c.get("/api/gps/share").json()["links"] if x["id"] == r.json()["id"]][0]
    assert it["expires_ts"] is None and it["mode"] == "sempre"
    # no máximo 5 ativos
    for _ in range(3):
        assert c.post("/api/gps/share", json={"mode": "24h"}).status_code == 200
    assert c.post("/api/gps/share", json={"mode": "24h"}).status_code == 429
    # encerrar um pendente: nunca chega à nuvem
    ids = [x["id"] for x in c.get("/api/gps/share").json()["links"]]
    assert c.post(f"/api/gps/share/{ids[0]}", json={"revoke": True}).json() == {"ok": True}
    assert c.post("/api/gps/share/xyz", json={"revoke": True}).status_code == 400
    # a nuvem sem senha de localização (último ping): recusa criar
    (base / "share_cloud.json").write_text(json.dumps({"ts": int(time.time() * 1000), "loc": False, "links": []}))
    r = c.post("/api/gps/share", json={"mode": "1h"})
    assert r.status_code == 400 and "LOC_PASSWORD" in r.json()["detail"]
    # obd-sync: com a senha de localização entra na fila de envio (só hashes); sem ela, nada sai
    from obdpi import sync as syncmod
    old = (syncmod.STATE, syncmod.SHARE_FILE, syncmod.SHARE_CLOUD)
    syncmod.STATE, syncmod.SHARE_FILE, syncmod.SHARE_CLOUD = base / "st.json", base / "share.json", base / "share_cloud.json"
    try:
        s = syncmod.Sync({"cloud": {"url": "https://obd.exemplo", "token": "t"}, "gps": {"cloud": "auto"}})
        sent = []
        def fake_cloud(path, payload=None, method=None, timeout=60):
            sent.append((path, payload))
            return {"ok": True, "created": [x["hash"][:8] for x in payload.get("create", [])], "links": [{"id": "abcd0123", "state": "ativo", "dest": True}]}
        s.cloud = fake_cloud
        assert s.share_sync() == 0 and sent == []                     # sem a senha de localização confirmada
        s.loc = True
        assert s.share_sync() == 4                                     # o encerrado antes de enviar nunca sai do Pi
        path, body = sent[0]
        assert path == "/api/sync/share" and len(body["create"]) == 4 and body["revoke"] == []
        assert [x["mode"] for x in body["create"]].count("sempre") == 1
        assert tok not in json.dumps(body) and all(len(x["hash"]) == 64 for x in body["create"])
        states = {x["id"]: x["state"] for x in json.loads((base / "share.json").read_text())["links"]}
        assert states[ids[0]] == "encerrado" and list(states.values()).count("enviado") == 4
        assert s.share_links[0]["id"] == "abcd0123"
        assert json.loads((base / "share_cloud.json").read_text())["links"][0]["id"] == "abcd0123"
        assert s.share_sync() == 0                                     # nada pendente
        # encerrar um enviado: vai na próxima sincronização
        sid = [k for k, v in states.items() if v == "enviado"][0]
        assert c.post(f"/api/gps/share/{sid}", json={"revoke": True}).json() == {"ok": True}
        assert c.get("/api/gps/share").json()["links"][0]["state"] in ("encerrando", "ativo", "pendente", "vencido", "encerrado")
        sent.clear()
        assert s.share_sync() == 1 and sent[0][1]["revoke"] == [sid]
    finally:
        syncmod.STATE, syncmod.SHARE_FILE, syncmod.SHARE_CLOUD = old


def test_share_live_channel_and_trip_end():
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    old = (syncmod.STATE, syncmod.GPS_FILE)
    syncmod.STATE, syncmod.GPS_FILE = base / "st.json", base / "gps.json"
    try:
        ts = int(time.time() * 1000)
        syncmod.GPS_FILE.write_text(json.dumps({"state": "ok", "lat": -23.5, "lon": -46.6, "fix_ts": ts, "trip_id": 7, "place": {"name": "Casa"},
                                                "dest": {"name": "Casa", "eta_ts": ts + 60_000, "left_km": 1.0}}))
        d = {"ts": ts, "trip_id": 7, "engine_on": True, "state": "coletando", "values": {}, "value_ts": {}, "gps": {"state": "ok"}}
        raw = json.dumps(d).encode()
        s = syncmod.Sync({"cloud": {"url": "https://obd.exemplo", "token": "t"}, "gps": {"cloud": "auto"}})
        assert s.live_body(raw, d) is raw                                # sem link ativo: o mesmo de sempre
        s.share_on = True
        assert s.live_body(raw, d) is raw                                # link ativo mas sem a senha de localização: nada
        s.loc = True
        out = json.loads(s.live_body(raw, d))
        assert out["_share"]["lat"] == -23.5 and out["_share"]["trip"] == 7 and "dest" not in out["_share"] and out["trip_id"] == 7
        assert "Casa" not in json.dumps(out["_share"], ensure_ascii=False)
        s.share_links = [{"id": "abcd0123", "dest": True}]
        assert json.loads(s.live_body(raw, d))["_share"]["dest"] == "Casa"
        s.share_on = False
        assert s.live_body(raw, d) is raw
        s.loc = False
        s.share_on = True
        assert s.live_body(raw, d) is raw
        s.loc = True
        # trajeto da viagem (uma vez por viagem) e aviso de fim de viagem
        calls = []
        s.cloud = lambda path, payload=None, method=None, timeout=60: calls.append((path, payload)) or {"ok": True}
        s.local = lambda path, method="GET", body=None: (200, json.dumps({"trip": 7, "pts": [[-23.5, -46.6, ts]]}).encode())
        s.share_track(d)
        s.share_track(d)
        assert calls == [("/api/sync/share", {"track": {"trip": 7, "pts": [[-23.5, -46.6, ts]]}})]
        calls.clear()
        s.share_trip_watch(d)
        s.share_trip_watch(d)
        s.share_trip_watch({**d, "trip_id": None, "state": "ecu_silenciosa"})
        assert calls == [("/api/sync/share", {"trip_end": 7})] and s.share_track_trip is None
        calls.clear()
        s.share_on, s.share_links = False, []
        s.share_trip_watch(d)
        s.share_trip_watch({**d, "trip_id": 8})                          # sem link nenhum: não incomoda a nuvem
        assert calls == []
        # vigia do estacionamento: a posição sobe a cada 5 s enquanto dura o alarme
        syncmod.GPS_FILE.write_text(json.dumps({"ts": ts, "state": "ok", "guard": {"state": "alarme", "since": ts}}))
        s.gps_live_raw()
        assert s.guard_alarm is True
        syncmod.GPS_FILE.write_text(json.dumps({"ts": ts, "state": "ok"}))
        s.gps_live_raw()
        assert s.guard_alarm is False
    finally:
        syncmod.STATE, syncmod.GPS_FILE = old


def test_share_track_only_current_trip():
    try:
        import fastapi  # noqa: F401
    except ImportError:
        return
    base = Path(tempfile.mkdtemp())
    con, c = _share_app(base)
    t1 = sim.commute(seed=5)
    t2 = sim.commute(seed=6, t0=t1.obd[-1]["ts"] + 3600_000)
    a = sim.to_db(con, t1, finalize=True)
    b = sim.to_db(con, t2, finalize=False)
    con.execute("UPDATE trip SET end_ts=NULL WHERE id=?", (b,))
    con.commit()
    j = c.get(f"/api/gps/share/track?trip={b}").json()
    pts = j["pts"]
    b0 = con.execute("SELECT start_ts FROM trip WHERE id=?", (b,)).fetchone()[0]
    assert j["trip"] == b and len(pts) > 50 and all(p[2] >= b0 for p in pts)                # nada da viagem anterior
    from obdpi.gps.geo import haversine_m
    assert all(haversine_m(p[0], p[1], q[0], q[1]) >= sharemod.TRACK_STEP_M - 2 for p, q in zip(pts, pts[1:]))   # 5 casas ≈ 1 m
    few = sharemod.track_points(db.connect(str(base / "obd.db"), readonly=True), b, max_n=20)["pts"]
    assert len(few) <= 21 and few[-1] == pts[-1]                                             # a ponta atual fica
    assert c.get("/api/gps/share/track?trip=999").json() == {"trip": 999, "pts": []}
    assert a != b
    # partida a frio (2026-11-26, VK-172): posição com HDOP alto a 1 km do carro não entra no trajeto
    con.execute("UPDATE gps_fix SET lat=lat+0.01, hdop=12 WHERE ts=(SELECT MIN(ts) FROM gps_fix WHERE ts>=?)", (b0,))
    con.commit()
    bad = sharemod.track_points(db.connect(str(base / "obd.db"), readonly=True), b)["pts"]
    assert bad and haversine_m(bad[0][0], bad[0][1], pts[0][0], pts[0][1]) < 500


def test_current_track_open_trip():
    """Ao vivo: /api/gps/current_track devolve o trajeto da viagem aberta do live.json, com a hora da saída; sem viagem, vazio."""
    try:
        import fastapi  # noqa: F401
    except ImportError:
        return
    base = Path(tempfile.mkdtemp())
    con, c = _share_app(base)
    assert c.get("/api/gps/current_track").json() == {"trip": None, "start_ts": None, "pts": []}
    t1 = sim.commute(seed=7)
    b = sim.to_db(con, t1, finalize=False)
    con.execute("UPDATE trip SET end_ts=NULL WHERE id=?", (b,))
    con.commit()
    b0 = con.execute("SELECT start_ts FROM trip WHERE id=?", (b,)).fetchone()[0]
    (base / "live.json").write_text(json.dumps({"engine_on": True, "trip_id": b}))
    j = c.get("/api/gps/current_track").json()
    assert j["trip"] == b and j["start_ts"] == b0 and len(j["pts"]) > 50 and all(p[2] >= b0 for p in j["pts"])
    (base / "live.json").write_text(json.dumps({"engine_on": False, "trip_id": None}))
    (base / "gps.json").write_text(json.dumps({"engine_on": False, "trip_id": b}))      # motor desligado: nada
    assert c.get("/api/gps/current_track").json()["pts"] == []


def test_sync_trip_track_only_with_loc_and_incremental():
    """obd-sync: o trajeto da viagem aberta sobe completo uma vez, depois só os pontos novos (≥ 30 s), apaga no fim, e
    nunca sobe sem a senha de localização confirmada pela nuvem."""
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    old = syncmod.STATE
    syncmod.STATE = base / "st.json"
    try:
        ts = 1_800_000_000_000
        pts = [[-23.5 + i * 1e-3, -46.6, ts + i * 1000] for i in range(5000)]
        trk = {"trip": 7, "start_ts": ts - 5000, "pts": pts[:10]}
        s = syncmod.Sync({"cloud": {"url": "https://obd.exemplo", "token": "t"}, "gps": {"cloud": "auto"}})
        calls, local = [], []
        s.cloud = lambda path, payload=None, method=None, timeout=60: calls.append((path, payload)) or {"ok": True}
        s.local = lambda path, method="GET", body=None: local.append(path) or (200, json.dumps(trk).encode())
        d = {"trip_id": 7, "engine_on": True, "state": "coletando"}
        assert s.trip_track(d, now=100.0) is None and calls == [] and local == []      # sem a senha: nem lê o trajeto
        s.cfg["gps"]["cloud"] = "off"; s.loc = True
        assert s.trip_track(d, now=100.0) is None and calls == []                      # gps.cloud = off: também não
        s.cfg["gps"]["cloud"] = "auto"
        assert s.trip_track(d, now=100.0) == "full"
        assert calls == [("/api/sync/track", {"trip": 7, "start_ts": ts - 5000, "pts": pts[:10]})] and local == ["/api/gps/share/track?trip=7"]
        assert s.state["track_sent"] is True
        calls.clear()
        assert s.trip_track(d, now=110.0) is None and calls == []                      # antes de 30 s: nada
        trk["pts"] = pts[:12]
        assert s.trip_track(d, now=131.0) == "append"
        assert calls == [("/api/sync/track", {"trip": 7, "append": pts[10:12]})]       # só o que é novo
        calls.clear()
        assert s.trip_track(d, now=162.0) is None and calls == []                      # nada novo: nem chama a nuvem
        trk["pts"] = pts[:2000]
        assert s.trip_track(d, now=193.0) == "append" and len(calls[0][1]["append"]) == syncmod.TRACK_CHUNK
        assert calls[0][1]["append"][0] == pts[12]
        calls.clear()
        # a nuvem perdeu o documento: manda tudo de novo (no máximo TRACK_FULL_MAX pontos, a ponta atual fica)
        trk["pts"] = pts
        s.cloud = lambda path, payload=None, method=None, timeout=60: calls.append((path, payload)) or ({"ok": False, "reset": True} if "append" in payload else {"ok": True})
        assert s.trip_track(d, now=224.0) is None and s.tt is None
        assert s.trip_track(d, now=225.0) == "full"
        full = calls[-1][1]["pts"]
        assert len(full) <= syncmod.TRACK_FULL_MAX and full[0] == pts[0] and full[-1] == pts[-1]
        calls.clear()
        # fim da viagem: apaga lá; viagem nova: completo de novo
        assert s.trip_track({**d, "trip_id": None}, now=300.0) == "end" and calls == [("/api/sync/track", {"end": 7})]
        assert s.trip_track({**d, "trip_id": None}, now=400.0) is None
        trk.update(trip=8, pts=pts[:3])
        assert s.trip_track({**d, "trip_id": 8}, now=500.0) == "full"
        # a nuvem tira a senha de localização: purge_loc apaga o documento de lá
        calls.clear()
        s.state["hashes"] = {}
        assert s.purge_loc() >= 1
        assert calls and syncmod.TRACK_KEY in calls[0][1]["delete"] and s.tt is None and s.state["track_sent"] is False
    finally:
        syncmod.STATE = old


def _guard(always_on, **kw):
    con = new_db()
    calls = []
    hubx = SimpleNamespace(con=con, power={"always_on": always_on, "min_batt_v": 12.0}, poweroff=lambda: calls.append(1))
    return con, placesmod.ParkGuard(hubx), calls


def _gfix(ts, la, lo, spd=0.0):
    return Fix(t_gps=ts, mono=0, lat=la, lon=lo, speed=spd, heading=0, hdop=0.9, sats=9, mode=3, ts=ts)


def _gctx(ts, fix=None, engine_on=False, trip_id=None, batt=12.6):
    return SimpleNamespace(fix=fix, ts=ts, engine_on=engine_on, trip_id=trip_id, values={"batt": batt}, value_ts={"batt": ts})


def test_park_guard_always_on():
    from obdpi.gps import store as gstore
    from obdpi.gps.geo import offset
    rnd = random.Random(9)
    # desligado (padrão): nada acontece, nem com o carro sendo levado
    con, g, calls = _guard(False)
    ts = T0
    for i in range(400):
        ts += 1000
        la, lo = offset(*HOME, i * 4.0, 0)
        assert g.tick(_gctx(ts, batt=11.0)) == [] and g.feed(_gctx(ts, _gfix(ts, la, lo, 14.0))) == []
    assert g.private() == {} and calls == []
    # ligado: estacionado em casa (posição de estacionamento válida)
    con, g, calls = _guard(True)
    gstore.model_set(con, "parked", {"lat": HOME[0], "lon": HOME[1], "ts": T0, "valid": True}, T0)
    ts = T0
    alerts = []
    for _ in range(30):                                   # ainda não armou (1 min depois de desligar)
        ts += 1000
        alerts += g.tick(_gctx(ts)) + g.feed(_gctx(ts, _gfix(ts, *offset(*HOME, 200, 0), 20.0)))
    assert alerts == [] and g.state == "aguardando"
    for _ in range(40):
        ts += 1000
        alerts += g.tick(_gctx(ts))
    assert g.state == "armado" and g.anchor == {"lat": HOME[0], "lon": HOME[1]}
    for _ in range(300):                                  # ruído do GPS parado (±8 m) e picos isolados: nada
        ts += 1000
        la, lo = offset(*HOME, rnd.uniform(-8, 8), rnd.uniform(-8, 8))
        alerts += g.tick(_gctx(ts)) + g.feed(_gctx(ts, _gfix(ts, la, lo, rnd.uniform(0, 2))))
    ts += 1000
    alerts += g.feed(_gctx(ts, _gfix(ts, *offset(*HOME, 90, 0), 0.5)))          # um salto de 90 m sozinho
    assert alerts == [] and g.private()["guard"]["state"] == "armado"
    # guincho: 10 km/h por mais de 10 s, saindo do lugar → 1 alerta crítico, sem coordenada no texto
    d = 0.0
    for _ in range(40):
        ts += 1000
        d += 10 / 3.6
        alerts += g.tick(_gctx(ts)) + g.feed(_gctx(ts, _gfix(ts, *offset(*HOME, d, 0), 10.0)))
    assert len(alerts) == 1 and alerts[0].rule == "gps:park_guard" and alerts[0].severity == "crit"
    assert str(HOME[0])[:6] not in alerts[0].detail and "-23" not in alerts[0].detail
    assert g.private()["guard"]["state"] == "alarme" and con.execute("SELECT COUNT(*) FROM geo_event WHERE kind='vigia'").fetchone()[0] == 1
    for _ in range(200):                                  # 500 m adiante: avisa de novo (só uma vez)
        ts += 1000
        d += 10 / 3.6
        alerts += g.feed(_gctx(ts, _gfix(ts, *offset(*HOME, d, 0), 10.0)))
    assert len(alerts) == 2 and alerts[1].title.startswith("Carro desligado continua")
    # o dono liga o carro: desarma
    g.tick(_gctx(ts, engine_on=True, trip_id=5))
    assert g.state == "aguardando" and g.alarm is None
    assert g.feed(_gctx(ts, _gfix(ts, *offset(*HOME, d + 100, 0), 40.0), engine_on=True, trip_id=5)) == []
    # sem posição de estacionamento válida: a primeira posição precisa parada vira a âncora; deslocamento de 60 m+ dispara
    con, g, calls = _guard(True)
    ts = T0
    for _ in range(61):
        ts += 1000
        g.tick(_gctx(ts))
    assert g.state == "armado" and g.anchor is None
    g.feed(_gctx(ts, _gfix(ts, *HOME, 0.0)))
    out = []
    for k in range(4):
        ts += 1000
        out += g.feed(_gctx(ts, _gfix(ts, *offset(*HOME, 70 + k, 0), 2.0)))     # empurrado devagar (2 km/h)
    assert len(out) == 1 and out[0].value >= 70
    # bateria: abaixo de 12 V por 10 min com o carro desligado → aviso; 1 min depois desliga o Pi (uma vez)
    con, g, calls = _guard(True)
    ts, al = T0, []
    for i in range(9 * 60):
        ts += 1000
        al += g.tick(_gctx(ts, batt=11.7 if i % 97 else 12.2))   # um pico acima zera a contagem só se ficar acima
    assert al == []
    for _ in range(10 * 60 + 1):
        ts += 1000
        al += g.tick(_gctx(ts, batt=11.7))
    assert len(al) == 1 and al[0].rule == "power:low_batt" and calls == []
    for _ in range(61):
        ts += 1000
        al += g.tick(_gctx(ts, batt=11.7))
    assert calls == [1] and len(al) == 1
    for _ in range(100):
        ts += 1000
        g.tick(_gctx(ts, batt=11.6))
    assert calls == [1]
    # leitura velha da bateria não conta; carro ligado também não
    con, g, calls = _guard(True)
    ts = T0
    for _ in range(15 * 60):
        ts += 1000
        assert g.tick(SimpleNamespace(fix=None, ts=ts, engine_on=False, trip_id=None, values={"batt": 11.0}, value_ts={"batt": T0})) == []
    for _ in range(15 * 60):
        ts += 1000
        assert g.tick(_gctx(ts, engine_on=True, trip_id=3, batt=11.0)) == []
    assert calls == []


def test_park_guard_in_hub():
    base = Path(tempfile.mkdtemp())
    con = new_db()
    mk = lambda pw: hub.GpsHub({"gps": {"source": "gpsd", "geo_db": str(base / "nada.db")}, "power": pw}, con, Clock(),
                               config.State(str(base / "s.json")), lambda *a: None, lambda *a: None, base)
    h = mk({})
    g = h._engine(placesmod.ParkGuard)
    assert g is not None and g.on is False and h.power["always_on"] is False
    assert h.pump(Clock().now(), {"batt": 10.0}, {}, False, None) == []
    assert "guard" not in json.loads((base / "gps.json").read_text())                 # padrão: o gps.json não muda
    h2 = mk({"always_on": True})
    g2 = h2._engine(placesmod.ParkGuard)
    assert g2.on is True
    h2.pump(Clock().now(), {"batt": 12.5}, {}, False, None)                        # sem receptor: a vigia segue (bateria)
    assert g2.off_since is not None
    h2.reader = FakeReader()
    h2.pump(Clock().now(), {}, {}, False, None)
    assert h2.dec.stopped_ms == 60_000                                              # sempre ligado e parado: 1 por minuto
    h2.pump(Clock().now(), {}, {}, True, 4)
    assert h2.dec.stopped_ms == 30_000
    assert json.loads((base / "gps.json").read_text())["guard"]["state"] == "aguardando"


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
