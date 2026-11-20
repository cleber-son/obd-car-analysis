"""Testes do parser ELM327, PIDs e DTCs com um transporte falso (sem hardware)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from obdpi.elm.elm327 import ELM327, EcuSilent
from obdpi.obd import dtc
from obdpi.obd.pids import decode, parse_supported
from obdpi.analytics.throttle import ThrottleEngine
from obdpi.analytics.limits import LimitsEngine


class Fake:
    label = "fake"
    def __init__(self, table): self.table, self.last = table, ""
    def write(self, b): self.last = b.decode().strip()
    def read_until_prompt(self, t): return self.table.get(self.last, "NO DATA") + "\r\r"
    def drain(self): pass


def test_single_and_multi():
    e = ELM327(Fake({"010C": "410C1AF8", "010C0D11": "410C1AF80D3C1180", "0100": "4100BE3FA813"}))
    assert decode(0x0C, e.request(1, [0x0C])[0x0C]) == {"rpm": 1726.0}
    r = e.request(1, [0x0C, 0x0D, 0x11])
    assert decode(0x0D, r[0x0D]) == {"speed": 60.0} and round(decode(0x11, r[0x11])["tps"]) == 50
    sup = parse_supported(0, e.request(1, [0])[0])
    assert 0x0C in sup and 0x0D in sup and 0x20 in sup


def test_multiframe_vin():
    e = ELM327(Fake({"0902": "014\r0:490201314847\r1:46413136353837\r2:4C303331323334"}))
    m = e.raw_mode("0902")
    assert len(m) == 1 and m[0][:3] == bytes([0x49, 0x02, 0x01])


def test_errors():
    e = ELM327(Fake({"010C": "UNABLE TO CONNECT"}))
    try:
        e.request(1, [0x0C]); assert False
    except EcuSilent:
        pass
    assert ELM327(Fake({})).request(1, [0x0C]) == {}


def test_dtc():
    assert dtc.parse([bytes.fromhex("430221350107")], 3, True) == ["P2135", "P0107"]
    assert dtc.parse([bytes.fromhex("43000000000000")], 3, False) == []
    assert "borboleta" in dtc.describe("P2135").lower()


def test_throttle_stall_and_hunt():
    t = ThrottleEngine()
    ts, out = 0, []
    for i in range(60):   # marcha lenta oscilando ±200 rpm a cada ~3 s
        ts += 1000
        rpm = 800 + (200 if (i // 2) % 2 else -200)
        out += t.feed({"rpm": rpm, "speed": 0, "tps": 6, "tps_cmd": 7, "pedal_d": 15}, ts, 90)
    assert any(a.rule == "R4" for a in out), [a.rule for a in out]
    for rpm in (800, 790, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0):   # ECU continua respondendo com motor parado
        ts += 1000
        out += t.feed({"rpm": rpm, "speed": 0, "tps": 6, "tps_cmd": 7, "pedal_d": 15}, ts, 90)
    assert any(a.rule == "R6" and a.severity == "crit" for a in out)



def _drive(t, ts, cmd, pedal, n=6, tps=None, tps_b=None, pedal_e=None, rpm=2500, speed=50):
    """n leituras em regime com a relação real do Civic: posição = 11,7 + 0,667 × comando."""
    out = []
    for _ in range(n):
        ts += 600
        pos = 11.7 + 0.667 * cmd if tps is None else tps
        out += t.feed({"rpm": rpm, "speed": speed, "tps_cmd": cmd, "tps": pos, "tps_b": 2 * pos + 3 if tps_b is None else tps_b,
                       "pedal_d": pedal, "pedal_e": pedal / 2 if pedal_e is None else pedal_e, "tps_rel": pos - 11.7}, ts, 90)
    return ts, out


def test_throttle_no_false_alarms_and_real_faults():
    t = ThrottleEngine(); ts = 0; out = []
    for _ in range(40):                                   # aprende: quase tudo em marcha lenta, pouco em abertura grande
        ts, o = _drive(t, ts, 3, 20, n=20, rpm=780, speed=0); out += o
        for cmd in (15, 30, 45, 60, 80, 100):
            ts, o = _drive(t, ts, cmd, 20 + cmd * 0.6, n=3); out += o
    ts, o = _drive(t, ts, 100, 80, n=10); out += o        # pé no fundo: comando 100 % = posição 78 %, é normal
    assert not [a for a in out if a.rule in ("R1", "R2", "R2t", "R3", "R9:pedal_d", "R9:tps")], [(a.rule, a.detail) for a in out]
    # toque rápido no pedal (os dois sensores sobem juntos numa leitura só): não é salto de sinal nem borboleta parada
    ts, a = _drive(t, ts, 3, 20, n=3, rpm=780, speed=0); ts, b = _drive(t, ts, 3, 58, n=1, rpm=780, speed=0); ts, c = _drive(t, ts, 3, 20, n=3, rpm=780, speed=0)
    assert not [x for x in a + b + c if x.rule.startswith(("R9", "R3"))], [(x.rule, x.detail) for x in a + b + c]
    # pico só no sensor D do pedal, com o E parado: aí sim é salto de sinal
    ts, _ = _drive(t, ts, 3, 20, n=3, rpm=780, speed=0); ts, _ = _drive(t, ts, 3, 58, n=1, pedal_e=10, rpm=780, speed=0); ts, c = _drive(t, ts, 3, 20, n=2, rpm=780, speed=0)
    assert any(x.rule == "R9:pedal_d" for x in c), [(x.rule, x.detail) for x in c]
    # borboleta presa: comando 60 % e posição parada em 30 %
    ts, o = _drive(t, ts, 60, 56, n=8, tps=30.0)
    assert any(x.rule == "R1" for x in o), [(x.rule, x.detail) for x in o]
    # sensor B fugindo do A em abertura média
    t2 = ThrottleEngine(t.state()); ts2 = ts + 600000
    ts2, o = _drive(t2, ts2, 30, 38, n=8, tps_b=2 * (11.7 + 0.667 * 30) + 3 + 12)
    assert any(x.rule == "R2t" for x in o), [(x.rule, x.detail) for x in o]
    # pedal pisado de verdade por ~3 s e nada abre
    t3 = ThrottleEngine(t.state()); ts3 = ts2 + 600000
    ts3, _ = _drive(t3, ts3, 3, 20, n=3, rpm=780, speed=0); ts3, o = _drive(t3, ts3, 3, 70, n=6, rpm=780, speed=0)
    assert any(x.rule == "R3" for x in o), [(x.rule, x.detail) for x in o]


def test_keyoff_is_not_stall():
    t = ThrottleEngine(); ts = 0; out = []
    for rpm in [780] * 10 + [0]:           # chave desligada: depois disso a ECU some (sem mais leituras)
        ts += 1000
        out += t.feed({"rpm": rpm, "speed": 0, "tps": 6, "tps_cmd": 7, "pedal_d": 15}, ts, 90)
    assert not any(a.rule == "R6" for a in out)


def test_pegged_stft_still_alerts():
    # STFT cravado no limite da ECU (+46,9 % em marcha lenta) é mistura pobre de verdade: continua alertando
    l = LimitsEngine(period_s=5)
    v = {"stft1": 46.88, "ect": 90, "rpm": 750, "speed": 0, "run_time": 600}
    out = []
    for i in range(80):
        out += l.evaluate(dict(v), True, i)
    assert any(a.rule.startswith("stft1") and a.severity == "crit" for a in out)
    assert l.status(v, True)["stft1"][0] == "crit"


def test_report_since_and_trip():
    import datetime as dt
    import time
    from obdpi import db, reports
    import tempfile
    con = db.init(tempfile.mkdtemp() + "/r.db")
    now, D = int(time.time() * 1000), 86400000
    for i, st in enumerate((now - 2 * D, now - D), 1):
        con.execute("INSERT INTO trip(id,start_ts,end_ts,finalized,distance_km,duration_s) VALUES(?,?,?,1,?,1800)", (i, st, st + 1800_000, 5.0 * i))
    con.execute("INSERT INTO alert(ts,trip_id,source,rule,severity,title,count) VALUES(?,2,'limits','stft1:warm','warn','Ajuste curto alto',3)", (now - D + 60_000,))
    con.commit()
    day = dt.datetime.fromtimestamp((now - 2 * D) / 1000).strftime("%Y-%m-%d")
    r = reports.build(con, "since", day=day)
    assert r["cur"]["trips"] == 2 and [t["id"] for t in r["trips"]] == [1, 2]
    t2 = reports.build(con, "trip", trip=2)
    assert t2["cur"]["km"] == 10.0 and t2["prev"]["km"] == 5.0 and t2["trips"][0]["n_alerts"] == 3
    assert [x["title"] for x in t2["recommendations"]] == ["Ajuste curto alto"]
    assert reports.build(con, "trip", trip=1)["recommendations"] == []



def test_corrupt_db_restores_backup():
    import os
    import tempfile
    from obdpi import db
    d = tempfile.mkdtemp(); p = d + "/obd.db"; os.mkdir(d + "/backup")
    c = db.init(p); c.execute("INSERT INTO annotation(ts, kind, text) VALUES (1,'x','guardado')"); c.commit()
    c.execute(f"VACUUM INTO '{d}/backup/obd-20261119.db'"); c.close()
    with open(p, "r+b") as f:
        f.write(b"lixo" * 5000)                           # queda de energia no meio da gravação
    c = db.init(p)
    assert c.execute("SELECT text FROM annotation").fetchone()[0] == "guardado"
    assert any("corrompido" in f for f in os.listdir(d))



def _extras_db():
    """Banco de exemplo: um dia com vazamento, o conserto anotado, um dia saudável."""
    import random
    import tempfile
    import time
    from obdpi import db
    con = db.init(tempfile.mkdtemp() + "/x.db")
    now, D = int(time.time() * 1000), 86400000
    ids = {}

    def sig(k):
        if k not in ids:
            ids[k] = con.execute("INSERT INTO signal(key,name,unit) VALUES(?,?,?)", (k, k, "")).lastrowid
        return ids[k]

    def day(t0, stft_idle, maf_idle):
        random.seed(3)
        for i in range(400):
            ts = t0 + i * 5000
            v = (dict(rpm=770 + random.gauss(0, 20), speed=0, ect=92, iat=50, load=25, maf=maf_idle, map=30, stft1=stft_idle + random.gauss(0, 1), o2c1_lambda=1.0, lambda_cmd=0.99, tps_cmd=1.5, tps=12.5, timing=8)
                 if i < 200 else dict(rpm=2600, speed=60, ect=92, iat=40, load=55, maf=22, map=70, stft1=4 + random.gauss(0, 2), o2c1_lambda=1.0, lambda_cmd=0.99, tps_cmd=25, tps=28, timing=25))
            con.executemany("INSERT INTO sample(signal_id, ts, value) VALUES (?,?,?)", [(sig(k), ts, x) for k, x in v.items()])
            con.execute("INSERT OR REPLACE INTO fast_sample(ts, trip_id, rpm, speed, fuel_lh) VALUES (?,?,?,?,?)", (ts, 1, v["rpm"], v["speed"], 0.9 if i < 200 else 6.0))
    day(now - 6 * D, 45, 1.4)
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (now - 5 * D, "manutencao", "Junta do corpo de borboleta"))
    day(now - 4 * D, 6, 3.4)
    con.commit()
    return con, now, D, day


def test_impact_fingerprint_maintenance_habits():
    import json
    from obdpi import extras
    con, now, D, day = _extras_db()
    imp = extras.impact(con)[0]
    assert imp["nivel"] == "ok" and any(i["nome"].startswith("Correção de combustível parado") and i["veredito"] == "melhorou" for i in imp["indicadores"]), imp
    assert extras.fp_status(con, 4.5)["referencia"] is None
    con.execute("CREATE TABLE IF NOT EXISTS fingerprint (id INTEGER PRIMARY KEY, ts INTEGER, label TEXT, days INTEGER, profile TEXT)")
    con.execute("INSERT INTO fingerprint(ts,label,days,profile) VALUES(?,?,?,?)", (now - 3 * D, "ok", 1, json.dumps(extras.fp_profile(con, now - 4.5 * D, now - 3.5 * D))))
    day(now - 1 * D, 6, 3.4); con.commit()
    assert extras.fp_status(con, 7)["nivel"] == "ok"                      # igual à referência: nada a apontar
    day(now - 0.5 * D, 30, 1.8); con.commit()
    st = extras.fp_status(con, 0.8)
    assert st["nivel"] == "bad" and {s["key"] for s in st["sinais"] if s["status"] != "ok"} >= {"stft1", "maf"}, st
    h = extras.habits(con, now - 7 * D, now)
    assert h["parado"]["litros"] > 0 and h["parado"]["segundos"] > 0 and h["giro_alto"]["litros"] == 0
    extras.maint_set_odometer(con, 182000)
    extras.maint_done(con, "oleo", 170000, now - 30 * D)
    oil = next(i for i in extras.maintenance(con)["itens"] if i["key"] == "oleo")
    assert oil["status"] == "bad" and oil["km_restante"] == -2000, oil      # 170.000 + 10.000 < 182.000
    assert extras.kml_by_fuel([{"ts": 1, "mix_after": 0.0, "real_kml": None}, {"ts": 2, "mix_after": 1.0, "real_kml": 11.0}, {"ts": 3, "mix_after": 1.0, "real_kml": 7.7}]) == \
        {"gasolina": {"kml": 11.0, "tanques": 1}, "etanol": {"kml": 7.7, "tanques": 1}}


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)



def test_reset_all(tmp_path=None):
    """Resetar tudo: banco e backups vão para backup/antes-do-reset-*, aprendizado some, adaptador fica."""
    import json, tempfile
    from pathlib import Path
    from obdpi import db, reset
    base = Path(tmp_path or tempfile.mkdtemp())
    cfg = {"storage": {"db_path": str(base / "obd.db"), "state_path": str(base / "state.json"), "backup_dir": str(base / "backup")}}
    con = db.init(cfg["storage"]["db_path"])
    con.execute("INSERT INTO trip(start_ts) VALUES (1)")
    con.execute("INSERT INTO fuel_fill(ts, fuel) VALUES (1, 'gasolina')")
    con.commit()
    con.close()
    (base / "backup").mkdir()
    (base / "backup" / "obd-20261119.db").write_bytes(b"x")
    (base / "state.json").write_text(json.dumps({"adapter_mac": "AA", "throttle": {"n": 5}, "trip_pushed": 7}))
    assert reset.apply(cfg) is False and reset.last(cfg) is None          # sem pedido, nada acontece
    assert reset.request(cfg) == {"pending": False}                       # sem coletor rodando, aplica na hora
    con = db.init(cfg["storage"]["db_path"])
    assert con.execute("SELECT COUNT(*) FROM trip").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM fuel_fill").fetchone()[0] == 0
    con.close()
    keep = list((base / "backup").glob("antes-do-reset-*"))
    assert len(keep) == 1 and (keep[0] / "obd-20261119.db").exists() and not list((base / "backup").glob("obd-*.db"))
    old = db.connect(str(keep[0] / "obd.db"), readonly=True)
    assert old.execute("SELECT COUNT(*) FROM trip").fetchone()[0] == 1    # a cópia guardada está inteira
    old.close()
    assert json.loads((base / "state.json").read_text()) == {"adapter_mac": "AA"}
    assert reset.last(cfg) and not reset.pending(cfg) and reset.apply(cfg) is False
