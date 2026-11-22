"""Testes do GPS — Fase 5 (marchas, embreagem, velocímetro e nota de condução), sem hardware. docs/GPS-ARQUITETURA.md,
seções 9.6, 9.10 e 9.11. Tudo determinístico: simulador com semente fixa e motores alimentados com as horas do próprio
simulador; nenhum resultado depende do relógio de parede (as consultas por período usam instantes fixos)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, math, socket, tempfile
from pathlib import Path
from types import SimpleNamespace

from obdpi import config, db
from obdpi.analytics import advice
from obdpi.gps import Fix, gears, hub, score, sim, speedo, store
from obdpi.gps import trip as tripmod
from obdpi.gps.geo import haversine_m, offset
from obdpi.gps.sim import HOME, T0, Leg, _legs, simulate

DAY = 86400_000
TYRE = 0.97                       # pneu 3 % menor que o nominal: as relações verdadeiras não são as de fábrica


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


def collector(cfg):
    from obdpi.collector.main import Collector
    col = Collector(cfg)
    if col.gps and col.gps.reader:
        col.gps.reader.stop()
    return col


def feed(engines, trip, con, tid=1):
    """Alimenta os motores como o GpsHub faria: cada posição (com ts = hora do receptor) com os valores do OBD lidos até
    ali e a hora de cada leitura (value_ts). Devolve os alertas."""
    obd = sorted(trip.obd, key=lambda r: r["ts"])
    values, vts, i, out = {}, {}, 0, []
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
            out += e.feed(ctx) or []
    return out


def gear_tour(seed=3, tyre=TYRE, t0=T0):
    """~20 min de cidade e estrada passando pelas 5 marchas (1ª a 20 km/h, 2ª a 35, 3ª a 50, 4ª a 70, 5ª a 100)."""
    moves = []
    for _ in range(2):
        moves += [(0, 0, 10, 8), (0, 250, 20, 0), (0, 300, 20, 6), (0, 300, 20, 6), (0, 500, 35, 6), (0, 800, 50, 6),
                  (0, 1500, 70, 6), (0, 2500, 100, 6), (0, 600, 45, 0), (0, 900, 75, 8)]
    return simulate(_legs(HOME, moves), start=HOME, t0=t0, seed=seed, tyre=tyre)


def speed_ladder(seed=1, speedo_f=1.04, lows=(20, 30, 40, 50, 60, 70, 80, 90)):
    """Retas em velocidade constante: 4 velocidades (verdadeiras) por faixa de 10 km/h da central, 25 s cada."""
    moves = [(0, 0, 10, 10)]
    for lo in lows:
        for d in (1.5, 3.75, 6.0, 8.25):
            v = (lo + d) / speedo_f
            moves.append((0, v / 3.6 * 25, v, 0))
    moves.append((0, 300, 40, 10))
    return simulate(_legs(HOME, moves), start=HOME, t0=T0, seed=seed, speedo=speedo_f)


def eng_hub(con):
    return SimpleNamespace(con=con, cfg=hub.gps_cfg({}))


# ---------------------------------------------------------------- marchas (9.6)
def test_gear_learning():
    assert gears.GearEngine in hub.LIVE_ENGINES and speedo.SpeedoEngine in hub.LIVE_ENGINES
    assert gears.FACTORY == {1: 8.83, 2: 14.84, 3: 22.46, 4: 29.26, 5: 38.16}
    con = new_db()
    t = gear_tour()
    assert 18 <= t.truth[-1]["t"] / 60 <= 22                                    # viagem de ~20 min
    e = gears.GearEngine(eng_hub(con))
    assert e.table() is None                                                     # nada aprendido: fábrica
    assert feed([e], t, con) == []                                               # sem patinação
    assert e.n >= gears.MIN_SAMPLES
    m = e.model()                                                                # o que vai para gps_model['gears']
    tab = e.table()
    assert sorted(tab) == [1, 2, 3, 4, 5]
    for g in range(1, 6):
        true = sim.GEAR_KMH_PER_KRPM[g] * TYRE
        assert abs(tab[g] / true - 1) < 0.015, (g, tab[g], true)
        assert abs(tab[g] / gears.FACTORY[g] - 1) > 0.015                       # aprendeu de fato (não é a de fábrica)
    # persistência: o modelo gravado devolve a mesma tabela num motor novo (o próximo boot)
    store.model_set(con, "gears", m["gears"], T0)
    assert "speedo_hist" in m and list(m["speedo_hist"]) == ["2026-11"]            # resumo mensal de k_5ª (pneus)
    e2 = gears.GearEngine(eng_hub(con))
    assert all(abs(e2.table()[g] - tab[g]) <= 0.002 for g in tab)
    rep = gears.report(con, T0 + DAY)
    assert rep["status"] == "aprendido" and [g["learned"] for g in rep["gears"]] == [tab[g] for g in range(1, 6)]
    assert all(g["diff_pct"] < -1.5 for g in rep["gears"]) and rep["slips"] == []
    assert "\"lat\"" not in json.dumps(rep) and "\"lon\"" not in json.dumps(rep)          # rota aberta na nuvem
    # sanidade: centro a mais de 10 % da fábrica é descartado e registrado
    hist = {round(math.log(gears.FACTORY[3]) / gears.BIN): 300.0, round(math.log(gears.FACTORY[5] * 1.12) / gears.BIN): 300.0}
    r = gears.cluster(hist)
    assert r["learned"] == [3] and r["table"][5] == gears.FACTORY[5] and r["rejected"][0]["gear"] == 5
    # funções puras
    assert gears.match(29.26 * 1.02, gears.FACTORY) == 4 and gears.match(29.26 * 1.05, gears.FACTORY) is None
    assert abs(gears.slip_ratio(3240, 88.0, 29.26) - (3240 / (88 / 29.26 * 1000) - 1)) < 1e-9
    # modelo em formato estranho (de antes da F5) não derruba o motor
    store.model_set(con, "gears", {"1": 8.8}, T0)
    assert gears.GearEngine(eng_hub(con)).table() is None
    # reset pelo comando do painel
    e.command({"cmd": "gears_reset"})
    assert e.table() is None and e.n == 0


def test_gear_fallback_without_gps():
    """Sem GPS, gear() devolve exatamente o mesmo de antes (tabela de fábrica, velocidade da central)."""
    def old_gear(speed, rpm):
        if not speed or not rpm or speed < 5 or rpm < 700:
            return None
        r = speed / (rpm / 1000)
        g, ref = min(gears.FACTORY.items(), key=lambda kv: abs(kv[1] - r))
        return g if abs(r - ref) / ref < 0.08 else 0
    grid = [(s, r) for s in (None, 0, 3, 5, 12, 20, 33, 47, 60, 75, 90, 118, 140) for r in (None, 0, 650, 700, 900, 1500, 2100, 2800, 3500, 4800, 6200)]
    for kw in ({}, {"source": "off"}):
        col = collector(make_cfg(Path(tempfile.mkdtemp()), **kw))
        try:
            assert col.gps.gear_table() is None and col.gps.speedo_k() == 1.0 and col.gps.true_speed(57) == 57
            assert [col.gear(s, r) for s, r in grid] == [old_gear(s, r) for s, r in grid]
        finally:
            col.gps.stop()
    # GPS desligado na configuração com um modelo aprendido no banco: continua a conta antiga
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base, source="off")
    con = db.init(cfg["storage"]["db_path"])
    hist = {round(math.log(k * 0.92) / gears.BIN): 200.0 for k in gears.FACTORY.values()}
    store.model_set(con, "gears", {"hist": {str(b): w for b, w in hist.items()}, "n": 5000}, T0)
    con.commit()
    col = collector(cfg)
    assert col.gps.gear_table() is None and [col.gear(s, r) for s, r in grid] == [old_gear(s, r) for s, r in grid]
    col.gps.stop()
    # com o GPS ligado, a marcha exibida usa as relações aprendidas (aqui 8 % abaixo da fábrica)
    cfg["gps"]["source"] = "gpsd"
    col = collector(cfg)
    try:
        tab = col.gps.gear_table()
        assert tab and abs(tab[4] / (gears.FACTORY[4] * 0.92) - 1) < 0.01
        assert col.gear(4 * tab[4] * 1.0, 4000) == 4 and old_gear(4 * tab[4], 4000) == 0      # a conta antiga diria "ponto morto"
    finally:
        col.gps.stop()


def test_clutch_slip_detected():
    con = new_db()
    t = sim.slip()
    t_on, dur, frac = t.meta["slip"]
    e = gears.GearEngine(eng_hub(con))
    al = feed([e], t, con, tid=7)
    assert len(al) == 1
    a = al[0]
    assert (a.source, a.rule, a.severity, a.title) == ("gps", "gps:clutch_slip", "warn", "Embreagem patinando")
    assert "4ª" in a.detail and 6 <= a.value <= 10 and "lat" not in a.detail
    ev = con.execute("SELECT * FROM geo_event WHERE kind='patinacao'").fetchall()
    assert len(ev) == 1 and ev[0]["trip_id"] == 7 and 0.06 <= ev[0]["value"] <= 0.10 and ev[0]["lat"] is not None
    assert abs((ev[0]["ts"] - T0) / 1000 - t_on) <= 1.0
    d = json.loads(ev[0]["detail"])
    assert d["gear"] == 4 and abs(d["dur_s"] - dur) <= 0.6
    # aparece no relatório de marchas (sem posição) e o terceiro em 7 dias vira crítico
    rep = gears.report(con, T0 + DAY)
    assert rep["slips_7d"] == 1 and rep["slips"][0]["gear"] == 4 and "lat" not in rep["slips"][0]
    for k in (1, 2):
        store.add_event(con, T0 - k * DAY, "patinacao", value=0.07, detail="{}")
    al = feed([gears.GearEngine(eng_hub(con))], sim.slip(seed=2), con)
    assert len(al) == 1 and al[0].severity == "crit" and "4 vezes em 7 dias" in al[0].detail
    # orientação para os dois alertas do GPS
    for rule in ("gps:clutch_slip", "gps:moved_off"):
        adv = advice.advice_for(rule, "gps")
        assert adv and adv["o_que"] and adv["causas"] and adv["fazer"] and adv["urgencia_txt"]
    # pelo GpsHub: o alerta sai do pump e o live leva a marcha (sem posição)
    live = e.live()
    assert set(live) == {"gear", "slip"} and live["slip"] is False


def test_no_slip_on_shifts():
    """Trocas normais, reduções com pé embaixo e arrancadas fortes não são patinação."""
    con = new_db()
    p = offset(HOME[0], HOME[1], 0, 1200)
    hard = [Leg(to=HOME, speed=10, stop_s=10), Leg(to=p, speed=120, pedal=1.0)]          # pé no fundo de 0 a 120 (trocas a 6.300)
    for i, (hold, kmh) in enumerate(((5, 80), (4, 110), (5, 85), (3, 95), (None, 60))):
        p = offset(p[0], p[1], 0, 900)
        hard.append(Leg(to=p, speed=kmh, hold_gear=hold) if i % 2 == 0 else Leg(to=p, speed=kmh, pedal=0.9, hold_gear=hold))  # reduções a fundo
    p = offset(p[0], p[1], 0, 300)
    hard.append(Leg(to=p, speed=30, stop_s=10))
    trips = [sim.slip(frac=0), sim.slip(frac=0, seed=5), sim.commute(), gear_tour(seed=4, tyre=1.0),
             simulate(hard, start=HOME, t0=T0, seed=2), simulate(hard, start=HOME, t0=T0, seed=3, tyre=TYRE)]
    pedal_s = sum(1 for s in trips[4].truth if s.get("pedal")) / 10
    assert pedal_s > 30                                                          # houve bastante pé embaixo
    for t in trips:
        assert feed([gears.GearEngine(eng_hub(con))], t, con) == []
    assert con.execute("SELECT COUNT(*) FROM geo_event WHERE kind='patinacao'").fetchone()[0] == 0
    # patinação fora da faixa (40 %: rotação disparou = embreagem pisada) e curta demais (1 s) também não
    assert feed([gears.GearEngine(eng_hub(con))], sim.slip(frac=0.40), con) == []
    assert feed([gears.GearEngine(eng_hub(con))], sim.slip(dur_s=0.8), con) == []


# ---------------------------------------------------------------- velocímetro (9.11)
def test_speedo_factor():
    con = new_db()
    t = speed_ladder()                                                          # central 4 % otimista
    e = speedo.SpeedoEngine(eng_hub(con))
    assert e.k == 1.0 and e.live() == {}
    feed([e], t, con)
    m = e.model()["speedo"]
    assert m["ok"] and abs(m["k"] - 0.960) <= 0.005 and abs(m["k"] - 1 / 1.04) <= 0.003, m["k"]
    assert m["n_bins"] >= 5 and e.live()["speedo_err"] == round((1 / m["k"] - 1) * 100, 1)
    txt = speedo.text(m)
    assert txt.startswith("A central informa 100 km/h quando o carro está a 96,") and "−3," in txt
    # gravado onde trip.speedo_k() e o GpsHub leem (gps_model['speedo']['k'])
    store.model_set(con, "speedo", m, T0)
    con.commit()
    assert tripmod.speedo_k(con) == m["k"]
    e2 = speedo.SpeedoEngine(eng_hub(con))
    assert e2.k == m["k"] and abs(sum(len(v) for v in e2.pairs.values()) - sum(len(v) for v in e.pairs.values())) == 0
    # a distância de uma viagem sem GPS usa k (aceite 3 da 9.11); a velocidade sem GPS também
    base = Path(tempfile.mkdtemp())
    cfg = make_cfg(base)
    c2 = db.init(cfg["storage"]["db_path"])
    store.model_set(c2, "speedo", m, T0)
    c2.commit()
    c2.close()
    col = collector(cfg)
    try:
        assert col.gps.speedo_k() == m["k"] and abs(col.gps.best_speed(100) - 100 * m["k"]) < 1e-9 and col.gps.best_speed(0) == 0
        t0 = col.clock.now()
        tid = col.con.execute("INSERT INTO trip(start_ts, end_ts) VALUES (?,?)", (t0, t0 + 600_000)).lastrowid
        col.con.executemany("INSERT INTO fast_sample(ts, trip_id, rpm, speed, fuel_lh) VALUES (?,?,?,?,?)",
                            [(t0 + i * 1000, tid, 2000, 60, 6.0) for i in range(601)])
        col.finalize_trip(tid)
        r = col.con.execute("SELECT distance_km, obd_km, gps_km FROM trip WHERE id=?", (tid,)).fetchone()
        assert r["obd_km"] == 10.0 and r["gps_km"] is None and abs(r["distance_km"] - round(10.0 * m["k"], 2)) < 1e-9
    finally:
        col.gps.stop()
    # pneus: queda de 1 % ou mais em k_5ª desde o primeiro mês vira informação
    ty = speedo.tyres({"2026-11": 37.0, "2026-12": 36.9, "2027-01": 36.5})
    assert ty["pct"] == -1.4 and "Pneus mais gastos" in ty["texto"]
    assert speedo.tyres({"2026-11": 37.0, "2026-12": 37.1})["texto"] is None and speedo.tyres({"2026-11": 37.0}) is None


def test_speedo_needs_bins():
    con = new_db()
    e = speedo.SpeedoEngine(eng_hub(con))
    feed([e], speed_ladder(lows=(20, 30, 40, 50)), con)                        # só 4 faixas
    m = e.model()["speedo"]
    assert not m["ok"] and m["k"] == 1.0 and m["n_bins"] == 4 and e.k == 1.0 and e.live() == {}
    assert speedo.text(m).startswith("Ainda aprendendo")
    store.model_set(con, "speedo", m, T0)
    assert tripmod.speedo_k(con) == 1.0
    # faixa com poucos pares não conta; fator absurdo (fora de 0,85–1,15) é recusado
    few = {b: [(b * 10 + 5, b * 10 + 5.0)] * 59 for b in range(2, 10)}
    assert not speedo.fit(few)["ok"]
    odd = {b: [(b * 10 + 5, (b * 10 + 5) * 0.7)] * 60 for b in range(2, 8)}
    r = speedo.fit(odd)
    assert not r["ok"] and r["k"] == 1.0 and r["fora"] < 0.85
    # sem nenhum par: o motor responde sem quebrar e o modelo ruim do banco é ignorado
    store.model_set(con, "speedo", {"pairs": "lixo", "k": 7}, T0)
    assert speedo.SpeedoEngine(eng_hub(con)).k == 1.0 and tripmod.speedo_k(con) == 1.0


# ---------------------------------------------------------------- nota de condução (9.10)
def _arc(p, hdg, turn, lat_acc, kmh):
    """Curva de 'turn' graus com aceleração lateral constante: pedaços de 4 passos do simulador (o carro não corta)."""
    v = kmh / 3.6
    radius = v * v / lat_acc
    step = 4 * v * sim.DT
    n = max(1, int(round(abs(math.radians(turn)) * radius / step)))
    legs = []
    for _ in range(n):
        hdg += turn / n
        p = offset(p[0], p[1], step * math.cos(math.radians(hdg)), step * math.sin(math.radians(hdg)))
        legs.append(Leg(to=p, speed=kmh))
    return legs, p, hdg


def drive(hard: bool, seed=1, t0=T0):
    """~9,5 km a 50 km/h: 4 paradas e 3 curvas de 90°. hard=True: freadas de −14 km/h/s e curvas a 4,5 m/s²."""
    legs = [Leg(to=HOME, speed=10, stop_s=15)]
    st = {"p": HOME, "h": 90.0}

    def straight(m, kmh, stop=0, brake=None):
        st["p"] = offset(st["p"][0], st["p"][1], m * math.cos(math.radians(st["h"])), m * math.sin(math.radians(st["h"])))
        legs.append(Leg(to=st["p"], speed=kmh, stop_s=stop, brake=brake))
    for i in range(4):
        straight(1600, 50)
        straight(300, 50, stop=20, brake=14 / 3.6 if hard else None)
        if i < 3:
            straight(400, 50)
            a, st["p"], st["h"] = _arc(st["p"], st["h"], 90 if i % 2 == 0 else -90, 4.5 if hard else 1.5, 50)
            legs.extend(a)
    straight(500, 50, stop=10)
    return simulate(legs, start=HOME, t0=t0, seed=seed, ect0=70)


def _zones(t):
    """Onde, na verdade da viagem, houve freada forte (< −3 m/s² por 1 s) e curva forte (v²/R > 3 m/s²)."""
    brakes, curves = [], []
    tr = t.truth
    for i in range(10, len(tr) - 10):
        s = tr[i]
        if sum(x["a"] for x in tr[i - 5:i + 5]) / 10 < -3.0:                 # 1 s inteiro (não o último passo antes de parar)
            brakes.append((s["lat"], s["lon"]))
        dh = abs((tr[i + 5]["hdg"] - tr[i - 5]["hdg"] + 540) % 360 - 180)    # graus em 1 s
        if s["v"] >= 20 and s["v"] / 3.6 * math.radians(dh) > 3.0:
            curves.append((s["lat"], s["lon"]))
    return brakes, curves


def test_drive_score_calm_vs_aggressive():
    assert hub.FINALIZERS[:2] == [tripmod.finalize, score.finalize]
    con = new_db()
    calm = sim.to_db(con, drive(False), finalize=True)
    com = sim.to_db(con, sim.commute(seed=2, t0=T0 + DAY), finalize=True)
    for tid in (calm, com):
        sc = con.execute("SELECT drive_score FROM trip WHERE id=?", (tid,)).fetchone()[0]
        assert sc is not None and sc >= 90, (tid, sc)
    t = drive(True, t0=T0 + 2 * DAY)
    hard = sim.to_db(con, t, finalize=True)
    r = con.execute("SELECT t.drive_score, t.distance_km, g.drive_score AS gs, g.drive_json FROM trip t JOIN trip_gps g ON g.trip_id=t.id WHERE t.id=?",
                    (hard,)).fetchone()
    assert 55 <= r["drive_score"] <= 70 and r["gs"] == r["drive_score"] and 9.0 <= r["distance_km"] <= 10.0, (r["drive_score"], r["distance_km"])
    d = json.loads(r["drive_json"])
    assert (d["freadas"], d["curvas"], d["arrancadas"]) == (4, 3, 0) and d["penalidades"]["freada"] > 15 and d["dicas"]
    ev = con.execute("SELECT kind, lat, lon, value FROM geo_event WHERE trip_id=? AND kind IN ('freada','curva','arrancada') ORDER BY ts", (hard,)).fetchall()
    assert len(ev) == 7
    brakes, curves = _zones(t)
    for e in ev:
        zone = brakes if e["kind"] == "freada" else curves
        assert min(haversine_m(e["lat"], e["lon"], la, lo) for la, lo in zone) <= 30, dict(e)
        assert (e["value"] <= -11) if e["kind"] == "freada" else (e["value"] > 3.5)
    # cada freada e cada curva verdadeira ganhou exatamente um evento
    for zone, kind, n in ((brakes, "freada", 4), (curves, "curva", 3)):
        groups = []
        for la, lo in zone:
            if not any(haversine_m(la, lo, g[0], g[1]) < 150 for g in groups):
                groups.append((la, lo))
        assert len(groups) == n
        for g in groups:
            assert sum(1 for e in ev if e["kind"] == kind and haversine_m(e["lat"], e["lon"], *g) < 150) == 1
    # reanálise é idempotente
    hub.run_finalizers(con, hard, hub.make_ctx(con, {}, trip_id=hard))
    assert con.execute("SELECT COUNT(*) FROM geo_event WHERE trip_id=? AND kind IN ('freada','curva','arrancada')", (hard,)).fetchone()[0] == 7
    # resumo do ntfy: "nota N"
    assert f"nota {r['drive_score']}" in tripmod.summary(con, hard)["parts"] + [score.summary_part(con, hard)]
    h = SimpleNamespace(con=con, cfg={"summary_names": True}, _warn=lambda *a: None)
    assert f"nota {r['drive_score']}" in hub.GpsHub.summary(h, hard)["parts"]
    # tendência (rota aberta na nuvem: sem posição)
    tr = score.trend(con, 0)
    assert tr["n"] == 3 and [x["trip_id"] for x in tr["items"]] == [calm, com, hard] and tr["por_100km"]["freadas"] > 0
    assert "\"lat\"" not in json.dumps(tr) and "\"lon\"" not in json.dumps(tr)
    # funções puras: arrancada forte e excesso de velocidade com limite (F7)
    fx = [{"ts": T0 + i * 1000, "lat": HOME[0], "lon": HOME[1] + i * 1e-4, "speed": min(10.0 * i, 120.0), "heading": 90.0} for i in range(240)]
    s1, d1 = score.drive_score(fx, [], km=5.0, coverage=1.0)
    assert d1["arrancadas"] == 1 and d1["acima_pct"] > 90 and d1["penalidades"]["velocidade"] == 20
    s2, d2 = score.drive_score(fx, [], limits=[200] * len(fx), km=5.0, coverage=1.0)
    assert d2["acima_pct"] == 0 and s2 > s1


def test_drive_score_short_trip_none():
    con = new_db()
    tid = sim.to_db(con, sim.straight(1.0), finalize=True)
    r = con.execute("SELECT t.drive_score, g.drive_score AS gs, g.drive_json FROM trip t JOIN trip_gps g ON g.trip_id=t.id WHERE t.id=?", (tid,)).fetchone()
    assert r["drive_score"] is None and r["gs"] is None and json.loads(r["drive_json"])["motivo"] == "viagem curta"
    assert score.summary_part(con, tid) is None
    # pouco GPS na viagem (cobertura < 70 %) e viagem sem GPS também ficam sem nota
    fx = [{"ts": T0 + i * 1000, "lat": HOME[0], "lon": HOME[1] + i * 2e-4, "speed": 60.0, "heading": 90.0} for i in range(400)]
    assert score.drive_score(fx, [], km=6.0, coverage=0.5) == (None, {"motivo": "pouco GPS na viagem", "km": 6.0})
    assert score.drive_score(fx, [], km=6.0, dur_s=120, coverage=1.0)[0] is None
    nog = sim.to_db(con, sim.commute(t0=T0 + DAY), with_gps=False, finalize=True)
    assert con.execute("SELECT drive_score FROM trip WHERE id=?", (nog,)).fetchone()[0] is None


# ---------------------------------------------------------------- painel e nuvem
def test_api_f5_routes():
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
    tid = sim.to_db(con, drive(True), finalize=True)
    e = speedo.SpeedoEngine(eng_hub(con))
    feed([e], speed_ladder(), con)
    for k, v in e.model().items():
        store.model_set(con, k, v, T0)
    g = gears.GearEngine(eng_hub(con))
    feed([g], gear_tour(), con)
    for k, v in g.model().items():
        store.model_set(con, k, v, T0)
    con.commit()
    app = FastAPI()
    app.include_router(gps_api.build(lambda: db.connect(dbp, readonly=True), lambda t: None, cfg, base))
    c = TestClient(app)
    ge = c.get("/api/gps/gears").json()
    assert ge["status"] == "aprendido" and len(ge["gears"]) == 5
    sp = c.get("/api/gps/speedo").json()
    assert sp["ok"] and abs(sp["k"] - 0.96) <= 0.005 and sp["texto"].startswith("A central informa") and sp["bins"][0]["err_pct"] > 0
    assert list(sp["hist"]) == ["2026-11"]
    sc = c.get("/api/gps/score?days=400").json()
    assert set(sc) >= {"items", "n", "media", "tendencia", "days"}
    for p in ("/api/gps/gears", "/api/gps/speedo", "/api/gps/score?days=400"):          # abertas na nuvem: sem posição
        txt = c.get(p).text
        assert '"lat"' not in txt and '"lon"' not in txt, p
    d = c.get(f"/api/gps/trips/{tid}").json()
    assert d["gps"]["drive_score"] is not None and json.loads(d["gps"]["drive_json"])["freadas"] == 4
    assert sum(1 for x in d["events"] if x["kind"] in ("freada", "curva")) == 7
    lb = c.get("/api/gps/logbook?limit=5").json()
    assert lb["items"][0]["drive_score"] == d["gps"]["drive_score"]
    # ponteiro do painel e reset das marchas (escrita no Pi)
    assert c.post("/api/gps/speedo", json={"needle": 104, "real": 100}).json() == {"ok": True, "vs_real_pct": 4.0}
    assert c.post("/api/gps/speedo", json={"needle": "x"}).status_code == 400
    assert c.get("/api/gps/speedo").json()["ponteiro"]["vs_real_pct"] == 4.0
    assert c.post("/api/gps/gears/reset").json() == {"ok": True}
    assert c.get("/api/gps/gears").json()["status"] == "aprendendo"
    assert json.loads((base / "gps_cmd").read_text().splitlines()[-1]) == {"cmd": "gears_reset"}


def _hub_with(con, base):
    h = hub.GpsHub({"gps": {"source": "gpsd", "gpsd_port": free_port()}}, con, SimpleNamespace(now=lambda: T0, source="sistema", synced=True),
                   SimpleNamespace(get=lambda k: None, set=lambda k, v: None), lambda *a: None, lambda *a: None, base)
    return h


def test_hub_f5_engines_and_reset_command():
    base = Path(tempfile.mkdtemp())
    con = new_db()
    h = _hub_with(con, base)
    kinds = {type(e) for e in h.engines}
    assert {gears.GearEngine, speedo.SpeedoEngine} <= kinds
    assert h.gear_table() is None and h.speedo_k() == 1.0
    ge = next(e for e in h.engines if isinstance(e, gears.GearEngine))
    feed([ge], gear_tour(), con)
    assert h.gear_table() == ge.table() and h.gear_table()
    (base / "gps_cmd").write_text(json.dumps({"cmd": "gears_reset"}) + "\n")
    h._commands()
    assert h.gear_table() is None and not (base / "gps_cmd").exists()


def test_sync_sends_f5_docs_without_location():
    """Marchas, velocímetro e nota sobem sempre (só números); não entram em gather_gps (que é o da localização)."""
    from obdpi import sync as syncmod
    base = Path(tempfile.mkdtemp())
    old_state = syncmod.STATE
    syncmod.STATE = base / "sync_state.json"
    try:
        s = syncmod.Sync({"cloud": {"url": "https://nuvem.invalid", "token": "t"}, "gps": {"cloud": "auto"}})
        s.local = lambda path, method="GET", body=None: (200, b"{}")
        docs = []
        s.gather_gps_open(docs)
        keys = [k for k, _ in docs]
        assert keys == ["/api/gps/gears", "/api/gps/speedo", "/api/gps/score?days=90"]
        assert not any(syncmod.is_loc(k) for k in keys)
    finally:
        syncmod.STATE = old_state


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
