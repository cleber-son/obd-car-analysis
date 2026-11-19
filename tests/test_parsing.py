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


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
