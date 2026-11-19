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


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)

