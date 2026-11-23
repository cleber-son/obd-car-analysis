"""❄️ automático: o compressor deixa a carga da lenta em degraus (gravação real de 23/11, ar ligado, lenta quente)."""
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from obdpi.analytics.acdetect import AcDetector

FIX = Path(__file__).parent / "fixtures" / "ac_idle_20261123.csv"
T0 = 1_795_464_000_000


def real():
    out = []
    for r in csv.DictReader(FIX.open()):
        h, m, s = map(int, r["hms"].split(":"))
        out.append((T0 + ((h * 60 + m) * 60 + s) * 1000, float(r["load"]), float(r["rpm"])))
    return out


def idle(load, rpm=750.0, **kw):
    return dict(load=load, rpm=rpm, speed=0.0, tps_rel=2.0, ect=92.0, **kw)


def run(d, pts):
    ch = []
    for ts, load, rpm in pts:
        c = d.feed(ts, idle(load, rpm))
        if c is not None:
            ch.append((ts, c))
    return ch


def quiet(t0, secs, load=33.0):
    return [(t0 + i * 1000, load + (i % 3) * 0.8, 750.0) for i in range(secs)]


def test_real_cycling_turns_on_fast():
    d = AcDetector()
    pts = real()
    ch = run(d, pts)
    assert ch and ch[0][1] is True
    assert ch[0][0] - pts[0][0] <= 60_000          # liga em menos de 1 min
    assert d.on and all(c for _, c in ch)          # e não fica piscando
    assert 30 <= d.base <= 36                      # aprendeu o nível sem ar (~33 %)


def test_ac_off_idle_never_turns_on():
    d = AcDetector()
    assert run(d, quiet(T0, 600)) == [] and not d.on


def test_single_load_bump_is_not_ac():
    """Um salto só (ventoinha, direção, farol alto) não liga o ❄️."""
    d = AcDetector()
    pts = quiet(T0, 60) + [(T0 + 60_000 + i * 1000, 55.0, 750.0) for i in range(8)] + quiet(T0 + 68_000, 300)
    assert run(d, pts) == [] and not d.on


def test_turns_off_after_quiet_idle():
    d = AcDetector()
    pts = real()
    run(d, pts)
    assert d.on
    ch = run(d, quiet(pts[-1][0] + 1000, 120))
    assert ch and ch[-1][1] is False and not d.on


def test_sustained_compressor_without_cycling():
    """Cabine quente: o compressor acopla e não solta. Carga alta direto por 60 s liga."""
    d = AcDetector()
    pts = quiet(T0, 30) + [(T0 + 30_000 + i * 1000, 58.0, 750.0) for i in range(70)]
    ch = run(d, pts)
    assert ch == [(ch[0][0], True)] and ch[0][0] - (T0 + 30_000) <= 61_000


def test_driving_keeps_state():
    d = AcDetector()
    run(d, real())
    assert d.on
    for i in range(300):                           # rodando: carga varia sem parar e nada muda
        assert d.feed(T0 + 10**6 + i * 1000, dict(load=20 + (i % 7) * 10, rpm=2200, speed=45, tps_rel=15, ect=92)) is None
    assert d.on


def test_cold_engine_ignored():
    d = AcDetector()
    for ts, load, rpm in real():
        assert d.feed(ts, dict(idle(load, rpm), ect=45.0)) is None
    assert not d.on


def test_manual_wins_then_auto_follows():
    d = AcDetector()
    d.manual(True, T0)                             # dono marcou ligado
    assert d.on
    assert run(d, quiet(T0 + 1000, 150)) == []     # segura 3 min antes de discordar
    assert d.on
    ch = run(d, quiet(T0 + 151_000, 150))
    assert ch and ch[-1][1] is False               # depois, ar claramente desligado: desmarca


def test_reset_on_engine_off():
    d = AcDetector()
    run(d, real())
    d.reset()
    assert not d.on


def test_collector_writes_flag_and_diary(tmp_path):
    """Coletor de verdade (sem ELM): o automático grava o arquivo `ac` e a nota no diário; o ❄️ do dono continua valendo."""
    import copy, time
    from obdpi import config
    from obdpi.collector.main import Collector
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["storage"] = {"db_path": str(tmp_path / "obd.db"), "state_path": str(tmp_path / "state.json"), "backup_dir": str(tmp_path / "bk")}
    cfg["runtime"]["live_sock"] = str(tmp_path / "run" / "live.sock")
    cfg["gps"]["source"] = "off"
    col = Collector(cfg)
    col.engine_on = True
    for ts, load, rpm in real():
        col.values.update(idle(load, rpm))
        col.ac_poll_manual()
        col.ac_feed(ts)
    flag = tmp_path / "run" / "ac"
    assert col.ac_on and col.limits.ac_on and flag.read_text() == "1"
    notes = [tuple(r) for r in col.con.execute("SELECT text FROM annotation WHERE kind='ar'")]
    assert notes == [("Ar-condicionado ligado (detectado)",)]
    col.ac_poll_manual()                           # o arquivo escrito pelo próprio coletor não conta como toque
    assert col.acdet.hold_until == 0
    time.sleep(0.01)
    flag.write_text("0")                           # dono toca no ❄️ (o painel grava o arquivo)
    col.ac_poll_manual()
    assert not col.ac_on and col.acdet.hold_until > 0
    col.acdet.manual(True, 0); col.acdet.hold_until = 0; col._ac_set(True)
    col.engine_on = False                          # desligou o carro: o ar some junto, sem nota
    col.ac_feed(T0 + 10**7)
    assert not col.ac_on and flag.read_text() == "0"
    assert col.con.execute("SELECT COUNT(*) FROM annotation WHERE kind='ar'").fetchone()[0] == 1
