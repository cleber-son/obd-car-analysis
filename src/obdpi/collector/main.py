"""obd-collector: conecta no ELM327, coleta tudo, detecta viagens, gera alertas e grava no SQLite.

Ciclo: grupo rápido (borboleta) a ~1 Hz; varredura completa de todos os PIDs a cada 20 s;
DTCs (03/07) + status (0101) a cada 20 s. Commit a cada 5 s. Estado ao vivo em /run/obd/live.json.
"""
from __future__ import annotations

import json
import logging
import statistics
import os
import socket
import sys
import threading
import time
from pathlib import Path

from .. import __version__, config, db, notify
from ..analytics import SEV_ORDER, Alert
from ..analytics import fuel as fuelmod
from ..analytics import tripdiag
from ..analytics.limits import LimitsEngine
from ..analytics.throttle import ThrottleEngine, trip_score
from ..elm.elm327 import ELM327, EcuSilent, ElmError, ElmNeedsReset
from ..elm.transport import RfcommTransport, TcpTransport, TransportError
from ..obd import dtc as dtcmod
from ..obd.pids import INFO_ONLY, PID_LEN, SIGNALS, decode, decode_status, parse_supported
from . import bt

log = logging.getLogger("collector")

FAST_COLS = ("rpm", "speed", "map", "tps", "tps_rel", "tps_b", "tps_cmd", "pedal_d", "pedal_e", "fuel_lh", "load", "gear", "maf")


def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def clock_synced() -> bool:
    return Path("/run/systemd/timesync/synchronized").exists()


class Clock:
    """Timestamps monotônicos ancorados; corrigidos retroativamente quando o NTP sincroniza."""

    def __init__(self, last_db_ts: int):
        self.synced = clock_synced()
        wall = time.time() * 1000
        self.anchor_wall = wall if self.synced else max(wall, last_db_ts + 1000)
        self.anchor_mono = time.monotonic() * 1000

    def now(self) -> int:
        return int(self.anchor_wall + time.monotonic() * 1000 - self.anchor_mono)

    def check_sync(self) -> int:
        """Retorna o offset (ms) a aplicar se o relógio acabou de sincronizar, senão 0."""
        if self.synced or not clock_synced():
            return 0
        self.synced = True
        off = int(time.time() * 1000 - self.now())
        self.anchor_wall += off
        return off if abs(off) > 2000 else 0


class Collector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        c = cfg["collector"]
        self.full_period = c["full_period_s"]
        self.fast_period = c["fast_period_s"]
        self.dtc_period = c["dtc_period_s"]
        self.commit_every = c["commit_interval_s"]
        self.engine_rpm = c["engine_on_rpm"]
        self.trip_end_s = c["trip_end_s"]
        self.fast_pids_cfg = [int(p, 16) for p in c["fast_pids"]]
        st = cfg["storage"]
        self.db_path = st["db_path"]
        self.state = config.State(st["state_path"])
        self.con = db.init(self.db_path)
        self._restored = list(db.RESTORED)
        self.run_dir = Path(cfg["runtime"]["live_sock"]).parent
        self.run_dir.mkdir(parents=True, exist_ok=True)
        last = self.con.execute("SELECT MAX(ts) FROM sample").fetchone()[0] or 0
        self.clock = Clock(last)
        self.boot_ts = self.clock.now()            # tudo gravado por este processo é >= boot_ts
        self.sig_ids = {r["key"]: r["id"] for r in self.con.execute("SELECT id, key FROM signal")}
        self.limits = LimitsEngine([dict(r) for r in self.con.execute("SELECT * FROM baseline")], period_s=c["full_period_s"])
        if self._restored:
            self.event("warn", "banco_restaurado", f"banco corrompido (queda de energia?); restaurado do backup {self._restored[0]}")
            self.queue_push("Banco de dados restaurado", f"O banco estava corrompido e voltou do backup {self._restored[0]}. Os dados depois dele ficaram no arquivo .corrompido.", "warn")
            self.con.commit()
        self.throttle = ThrottleEngine(self.state.get("throttle"))
        self.session_id = None
        self.session_start = 0
        self.elm: ELM327 | None = None
        self.supported: set[int] = set()
        self.sweep_pids: list[int] = []
        self.fast_pids: list[int] = []
        self.values: dict[str, float] = {}
        self.value_ts: dict[str, int] = {}
        self.state_name = "iniciando"
        self.state_detail = ""
        self.engine_on = False
        self.rpm_streak = 0
        self.last_engine_ts = 0
        self.trip_id = None
        self.trip_start = 0
        self.trip_alerts: list[Alert] = []
        self.notified: dict[str, int] = {}
        self.dtc_active: dict[str, set] = {"confirmed": set(), "pending": set()}
        self.mil = False
        self.recent_alerts: list[dict] = []
        self.fast_times: list[float] = []
        self.last_commit = time.monotonic()
        self.last_full = 0.0
        self.last_dtc = 0.0
        self.last_batt_store = 0.0
        self.ecu_info: dict = {}
        self.fuel_mix, self.fuel_calib = 0.0, 1.0
        self.trip_live = {"km": 0.0, "fuel_l": 0.0, "moving_s": 0.0, "max_speed": 0, "last_ts": None}
        self.accel: dict | None = None
        self.readiness: dict | None = None
        self.volt_hist: list[tuple[int, float]] = []      # ATRV recentes (para medir a queda na partida)
        self.pending_crank: dict | None = None
        self.load_fuel()
        self.finalize_open_trips()
        self._load_dtc_state()
        threading.Thread(target=self._notifier_loop, daemon=True).start()

    # ------------------------------------------------------------------ util
    def event(self, level: str, kind: str, detail: str = "") -> None:
        log.log(logging.WARNING if level != "info" else logging.INFO, "%s: %s", kind, detail)
        self.con.execute("INSERT INTO system_event(ts, level, kind, detail) VALUES (?,?,?,?)",
                         (self.clock.now(), level, kind, detail))

    def set_state(self, name: str, detail: str = "") -> None:
        if name != self.state_name:
            self.event("info", "estado", f"{self.state_name} → {name} {detail}")
        self.state_name, self.state_detail = name, detail
        self.write_live()

    def sig_id(self, key: str) -> int:
        sid = self.sig_ids.get(key)
        if sid is None:
            s = SIGNALS.get(key)
            cur = self.con.execute("INSERT OR IGNORE INTO signal(key, pid, name, unit, decimals) VALUES (?,?,?,?,?)",
                                   (key, s.pid if s else None, s.name if s else key, s.unit if s else "", s.decimals if s else 0))
            sid = cur.lastrowid or self.con.execute("SELECT id FROM signal WHERE key=?", (key,)).fetchone()[0]
            self.sig_ids[key] = sid
        return sid

    def maybe_commit(self, force: bool = False) -> None:
        if force or time.monotonic() - self.last_commit >= self.commit_every:
            off = self.clock.check_sync()
            if off:
                self.apply_clock_offset(off)
            if self.trip_id:
                self.con.execute("UPDATE trip SET end_ts=? WHERE id=?", (self.clock.now(), self.trip_id))
            if self.session_id:
                self.con.execute("UPDATE session SET last_ts=?, clock_synced=? WHERE id=?",
                                 (self.clock.now(), int(self.clock.synced), self.session_id))
            self.con.commit()
            self.last_commit = time.monotonic()

    def apply_clock_offset(self, off: int) -> None:
        start = self.boot_ts
        log.warning("relógio sincronizou: corrigindo %d ms desde %d", off, start)
        c = self.con
        c.execute("UPDATE sample SET ts = -(ts + ?) WHERE ts >= ?", (off, start))
        c.execute("UPDATE sample SET ts = -ts WHERE ts < 0")
        c.execute("UPDATE fast_sample SET ts = -(ts + ?) WHERE ts >= ?", (off, start))
        c.execute("UPDATE fast_sample SET ts = -ts WHERE ts < 0")
        for t, col in (("alert", "ts"), ("dtc_event", "ts"), ("system_event", "ts")):
            c.execute(f"UPDATE {t} SET {col} = {col} + ? WHERE {col} >= ?", (off, start))
        c.execute("UPDATE trip SET start_ts = start_ts + ?, end_ts = end_ts + ? WHERE start_ts >= ?", (off, off, start))
        c.execute("UPDATE session SET started_ts = started_ts + ? WHERE id=?", (off, self.session_id))
        self.session_start += off if self.session_start else 0
        self.boot_ts += off
        self.trip_start += off if self.trip_start else 0
        if self.last_engine_ts:
            self.last_engine_ts += off
        if self.trip_live.get("last_ts"):
            self.trip_live["last_ts"] += off
        if self.pending_crank:
            self.pending_crank["ts"] += off
        self.value_ts = {k: v + off for k, v in self.value_ts.items()}
        self.volt_hist = [(t + off, v) for t, v in self.volt_hist]

    # ------------------------------------------------------------------ conexão
    def make_transport(self):
        a = self.cfg["adapter"]
        if a["transport"] == "tcp":
            return TcpTransport(a["tcp_host"], a["tcp_port"])
        mac = a["mac"] or self.state.get("adapter_mac")
        if not mac:
            self.set_state("procurando_adaptador", "varrendo Bluetooth")
            hit = bt.find_adapter(a["name_patterns"])
            if not hit:
                raise TransportError("nenhum adaptador OBD encontrado no Bluetooth")
            mac, name = hit
            self.event("info", "adaptador_encontrado", f"{name} {mac}")
            bt.pair(mac)
            self.state.set("adapter_mac", mac)
            self.state.set("adapter_name", name)
        if not getattr(self, "_bt_reset_done", False):
            # ao iniciar, derruba qualquer enlace antigo (o clone ELM327 aceita só uma conexão e às vezes fica preso)
            bt._ctl("disconnect", mac, timeout=10)
            time.sleep(1)
            self._bt_reset_done = True
        return RfcommTransport(mac, self.state.get("rfcomm_channel", a["rfcomm_channel"]))

    def start_session(self) -> None:
        e = self.elm
        assert e
        self.session_start = self.clock.now()
        cur = self.con.execute(
            "INSERT INTO session(started_ts, last_ts, clock_synced, adapter, elm_version, protocol, multi_pid, supported_pids, app_version)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (self.session_start, self.session_start, int(self.clock.synced), e.t.label, e.version,
             f"{e.protocol_num} {e.protocol_desc}", int(e.multi_pid), "", __version__))
        self.session_id = cur.lastrowid
        self.con.commit()

    def discover(self) -> None:
        e = self.elm
        sup: set[int] = set()
        base = 0x00
        while base <= 0xC0:
            r = e.request(0x01, [base])
            if base not in r:
                break
            got = parse_supported(base, r[base])
            sup |= got
            if (base + 0x20) not in got:
                break
            base += 0x20
        if not sup:
            raise EcuSilent("ECU não informou PIDs (ignição desligada / acordando)")
        prev = {int(x, 16) for x in (self.state.get("last_probe") or {}).get("supported", [])}
        if prev and len(sup) < len(prev):
            log.warning("descoberta parcial (%d < %d PIDs): usando a união com a última", len(sup), len(prev))
            sup |= prev
        if not e.multi_pid and e.is_can:
            e.multi_pid = e._probe_multi()
        self.supported = sup
        self.sweep_pids = sorted(p for p in sup if p in PID_LEN and p not in INFO_ONLY and p % 0x20 != 0)
        self.fast_pids = [p for p in self.fast_pids_cfg if p in sup]
        # PIDs do modo direção (ajuste curto, avanço, sonda): um grupo por ciclo rápido, só para o painel ao vivo.
        # Continuam sendo gravados pela varredura completa; aqui só deixam de esperar 5 s para aparecer na tela.
        self.live_rot = [g for g in ([p for p in grp if p in sup and p not in self.fast_pids] for grp in ([0x06, 0x0E], [0x34])) if g]
        info = {"supported": [f"{p:02X}" for p in sorted(sup)], "multi_pid": e.multi_pid,
                "elm": e.version, "protocol": e.protocol_desc}
        for pid in (0x1C, 0x51, 0x13):
            if pid in sup:
                r = e.request(0x01, [pid])
                if pid in r:
                    info[f"pid_{pid:02X}"] = r[pid].hex()
        try:
            vin = e.raw_mode("0902")
            if vin:
                raw = vin[0][3:] if len(vin[0]) > 3 else b""
                info["vin"] = "".join(chr(b) for b in raw if 32 < b < 127)[-17:]
        except ElmError:
            pass
        self.ecu_info = info
        self.con.execute("UPDATE session SET supported_pids=?, vin=? WHERE id=?",
                         (json.dumps(info["supported"]), info.get("vin"), self.session_id))
        self.state.set("last_probe", info)
        self.event("info", "pids_descobertos", f"{len(sup)} PIDs: {' '.join(info['supported'])}")

    @staticmethod
    def pack_single_frame(pids: list[int], cap: int = 6) -> list[list[int]]:
        """Agrupa PIDs para a resposta caber num único quadro CAN (7 bytes: 41 + pares pid/dados).
        O clone ELM327 deste carro (CAN 29 bits) só devolve o 1º quadro de respostas multi-frame."""
        groups: list[list[int]] = []
        sizes: list[int] = []
        for p in sorted(pids, key=lambda x: -PID_LEN.get(x, 4)):
            need = 1 + PID_LEN.get(p, 4)
            for i, used in enumerate(sizes):
                if used + need <= cap and len(groups[i]) < 6:
                    groups[i].append(p)
                    sizes[i] += need
                    break
            else:
                groups.append([p])
                sizes.append(need)
        return groups

    def read_pids(self, pids: list[int]) -> dict[int, bytes]:
        e = self.elm
        out: dict[int, bytes] = {}
        if e.multi_pid:
            for chunk in self.pack_single_frame(pids):
                r = e.request(0x01, chunk)
                out.update(r)
                if len(chunk) > 1:
                    for p in chunk:
                        if p not in r:          # se algum faltou, tenta individual
                            out.update(e.request(0x01, [p]))
        else:
            for p in pids:
                out.update(e.request(0x01, [p]))
        return out

    # ------------------------------------------------------------------ viagens
    def update_engine(self, rpm: float | None, ts: int) -> None:
        if rpm is not None and rpm >= self.engine_rpm:
            self.rpm_streak += 1
            self.last_engine_ts = ts
            if self.rpm_streak >= 3:
                if not self.engine_on:
                    self.engine_on = True
                    self.event("info", "motor_ligado")
                if not self.trip_id:
                    self.start_trip(ts)
        else:
            self.rpm_streak = 0
            if self.engine_on and ts - self.last_engine_ts > 5000:
                self.engine_on = False
                self.event("info", "motor_desligado")
        if self.trip_id and not self.engine_on and ts - self.last_engine_ts > self.trip_end_s * 1000:
            self.end_trip("motor_desligado")

    # relação km/h por 1000 rpm de cada marcha — Civic 8ª geração 1.8 manual (5 marchas), pneu 205/55 R16
    GEAR_KMH_PER_KRPM = {1: 8.83, 2: 14.84, 3: 22.46, 4: 29.26, 5: 38.16}

    def gear(self, speed, rpm) -> int | None:
        if not speed or not rpm or speed < 5 or rpm < 700:
            return None
        r = speed / (rpm / 1000)
        g, ref = min(self.GEAR_KMH_PER_KRPM.items(), key=lambda kv: abs(kv[1] - r))
        return g if abs(r - ref) / ref < 0.08 else 0     # 0 = embreagem pressionada / ponto morto

    def trip_live_update(self, vals: dict, ts: int) -> None:
        t = self.trip_live
        if not self.trip_id:
            return
        last = t.get("last_ts")
        dt = min((ts - last) / 1000, 5) if last else 0
        spd = vals.get("speed") or 0
        t["km"] += spd * dt / 3600
        t["fuel_l"] += (vals.get("fuel_lh") or 0) * dt / 3600
        if spd > 2:
            t["moving_s"] += dt
        t["max_speed"] = max(t["max_speed"], spd)
        t["last_ts"] = ts

    def start_trip(self, ts: int) -> None:
        self.trip_live = {"km": 0.0, "fuel_l": 0.0, "moving_s": 0.0, "max_speed": 0, "last_ts": None}
        cold = self.values.get("ect")
        # o Pi caiu (energia) e voltou em < 3 min com o motor ligado: continua a mesma viagem
        last = self.con.execute("SELECT id, start_ts, end_ts, end_reason, last_run_time FROM trip ORDER BY start_ts DESC LIMIT 1").fetchone()
        run_time = self.values.get("run_time")
        # O Pi é alimentado pelo carro e liga DEPOIS da partida, então "motor ligado antes do Pi" não distingue nada.
        # Critério: o contador da ECU de tempo desde a partida (PID 1F) zera a cada partida. Se agora ele é maior que o último
        # valor visto na viagem anterior, é o mesmo funcionamento do motor (o Pi caiu no meio) → continua a viagem.
        same_engine_run = (last is not None and run_time is not None and last["last_run_time"] is not None
                           and run_time >= last["last_run_time"])
        if same_engine_run and self.clock.synced and last["end_ts"]:
            same_engine_run = run_time >= (ts - last["end_ts"]) / 1000 - 60     # com relógio confiável, confere o intervalo também
        self.trip_merged = False
        if last and last["end_reason"] in ("corte_de_energia", "sem_conexao") and same_engine_run:
            self.trip_merged = True
            self.con.execute("UPDATE trip SET finalized=0, end_reason=NULL WHERE id=?", (last["id"],))
            if not self.con.execute("SELECT start_snapshot FROM trip WHERE id=?", (last["id"],)).fetchone()[0]:
                self.snapshot_pending = True    # viagem sem retrato: tira agora
            self.trip_id, self.trip_start = last["id"], last["start_ts"]
            self.event("info", "viagem_continuada", f"viagem {last['id']} retomada após queda de {int((ts - last['end_ts']) / 1000)} s")
        else:
            cur = self.con.execute("INSERT INTO trip(session_id, start_ts, end_ts, cold_start, start_ect) VALUES (?,?,?,?,?)",
                                   (self.session_id, ts, ts, int(cold is not None and cold < 50), cold))
            self.trip_id, self.trip_start = cur.lastrowid, ts
            self.snapshot_pending = True        # retrato completo logo após a partida
        # bateria: repouso (antes) e mínimo durante a partida
        before = [v for t, v in self.volt_hist if ts - 15000 <= t <= ts - 1500]
        self.pending_crank = {"ts": ts, "rest": statistics.median(before) if before else None}
        self.trip_alerts = []
        self.notified = {}
        self.con.commit()

    def snapshot(self, ts: int) -> dict:
        """Retrato de tudo que o carro informa neste instante (para análises futuras)."""
        rest = [v for t, v in self.volt_hist if ts - 15000 <= t <= ts - 1500]
        return {"ts": ts, "values": dict(self.values), "dtc": {k: sorted(v) for k, v in self.dtc_active.items()}, "mil": self.mil,
                "readiness": self.readiness, "supported": sorted(f"{p:02X}" for p in self.supported),
                "batt_rest": statistics.median(rest) if rest else None, "clock_synced": self.clock.synced,
                "elm": self.elm.version if self.elm else None, "protocol": self.elm.protocol_desc if self.elm else None}

    def take_start_snapshot(self) -> None:
        """Logo após a partida: varredura completa + DTCs e grava o retrato na viagem."""
        self.snapshot_pending = False
        try:
            self.full_cycle()
            self.poll_dtc()
        except ElmError:
            raise
        except Exception as e:
            log.warning("retrato de partida incompleto: %s", e)
        ts = self.clock.now()
        self.con.execute("UPDATE trip SET start_snapshot=? WHERE id=? AND start_snapshot IS NULL",
                         (json.dumps(self.snapshot(ts), ensure_ascii=False), self.trip_id))
        self.event("info", "retrato_partida", f"viagem {self.trip_id}: {len(self.values)} sinais")
        self.con.commit()

    def end_trip(self, reason: str) -> None:
        if not self.trip_id:
            return
        self.con.execute("UPDATE trip SET end_snapshot=? WHERE id=?",
                         (json.dumps(self.snapshot(self.last_engine_ts or self.clock.now()), ensure_ascii=False), self.trip_id))
        self.con.execute("UPDATE trip SET end_ts=?, end_reason=? WHERE id=?", (self.last_engine_ts or self.clock.now(), reason, self.trip_id))
        self.finalize_trip(self.trip_id, None if getattr(self, "trip_merged", False) else self.trip_alerts)
        s = self.con.execute("SELECT * FROM trip WHERE id=?", (self.trip_id,)).fetchone()
        crit = sum(1 for a in self.trip_alerts if a.severity == "crit")
        warn = sum(1 for a in self.trip_alerts if a.severity == "warn")
        if self.trip_alerts:
            self.queue_push(f"Resumo da viagem: {crit} crítico(s), {warn} aviso(s)",
                            f"{(s['duration_s'] or 0) // 60} min, {s['distance_km'] or 0:.1f} km. Nota da borboleta {s['tb_score']}/100.",
                            "warn" if crit == 0 else "crit")
        self.trip_id = None
        self.con.commit()

    def finalize_trip(self, tid: int, alerts: list[Alert] | None = None) -> None:
        c = self.con
        t = c.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
        if not t:
            return
        a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
        f = c.execute("SELECT ts, speed, rpm, fuel_lh FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
        dist, idle, fuel_l, prev = 0.0, 0.0, 0.0, None
        for r in f:
            if prev is not None:
                dt = min((r["ts"] - prev["ts"]) / 1000, 5)
                dist += (prev["speed"] or 0) * dt / 3600
                fuel_l += (prev["fuel_lh"] or 0) * dt / 3600
                if (prev["speed"] or 0) == 0 and (prev["rpm"] or 0) > 400:
                    idle += dt
            prev = r
        agg = c.execute("SELECT AVG(speed) av, MAX(speed) ms, MAX(rpm) mr FROM fast_sample WHERE ts BETWEEN ? AND ? AND rpm > 400", (a, b)).fetchone()

        def sagg(key, fn):
            sid = self.sig_ids.get(key)
            if not sid:
                return None
            return c.execute(f"SELECT {fn}(value) FROM sample WHERE signal_id=? AND ts BETWEEN ? AND ?", (sid, a, b)).fetchone()[0]

        if alerts is None:
            rows = c.execute("SELECT source, rule, severity, title FROM alert WHERE trip_id=?", (tid,)).fetchall()
            alerts = [Alert(r["source"], r["rule"], r["severity"], r["title"]) for r in rows]
        score = trip_score(alerts, self.throttle.idle_std_recent())
        c.execute("""UPDATE trip SET finalized=1, duration_s=?, distance_km=?, idle_s=?, avg_speed=?, max_speed=?, max_rpm=?,
                     max_ect=?, min_volt=?, max_volt=?, avg_ltft=?, tb_score=?, n_alerts=? WHERE id=?""",
                  (int((b - a) / 1000), round(dist, 2), int(idle), agg["av"], agg["ms"], agg["mr"], sagg("ect", "MAX"),
                   sagg("batt", "MIN"), sagg("batt", "MAX"), sagg("ltft1", "AVG"), score, len(alerts), tid))
        c.execute("UPDATE trip SET fuel_l=?, km_l=? WHERE id=?",
                  (round(fuel_l, 3) if fuel_l else None, round(dist / fuel_l, 1) if fuel_l > 0.05 else None, tid))
        self.trip_diagnostics(tid, a, b)
        self.trip_summary_push(tid)

    def trip_summary_push(self, tid: int) -> None:
        """Resumo curto no celular quando a viagem termina (uma vez por viagem; só viagens de verdade)."""
        try:
            t = self.con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
            if not t or self.state.get("trip_pushed") == tid or ((t["distance_km"] or 0) < 1 and (t["duration_s"] or 0) < 300):
                return
            price = self.con.execute("SELECT price_l FROM fuel_fill WHERE price_l IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
            n = self.con.execute("SELECT COUNT(*) FROM alert WHERE trip_id=? AND severity!='info'", (tid,)).fetchone()[0]
            crit = self.con.execute("SELECT COUNT(*) FROM alert WHERE trip_id=? AND severity='crit'", (tid,)).fetchone()[0]
            br = lambda v, d=1: f"{v:.{d}f}".replace(".", ",")
            parts = [f"{br(t['distance_km'] or 0)} km em {int((t['duration_s'] or 0) // 60)} min"]
            if t["km_l"]:
                parts.append(f"{br(t['km_l'])} km/L")
            if price and t["fuel_l"]:
                parts.append(f"R$ {br(t['fuel_l'] * price[0], 2)}")
            if (t["duration_s"] or 0) > 0 and (t["idle_s"] or 0) / t["duration_s"] > 0.35:
                parts.append(f"{int(t['idle_s'] / t['duration_s'] * 100)}% parado ligado")
            parts.append(f"{n} alerta(s)" if n else "sem alertas")
            self.queue_push("Viagem encerrada", " · ".join(parts), "warn" if crit else "info")
            self.state.set("trip_pushed", tid)
        except Exception as e:                                # o resumo nunca atrapalha o fechamento da viagem
            log.debug("resumo da viagem: %s", e)

    def trip_diagnostics(self, tid: int, a: int, b: int) -> None:
        c = self.con
        try:
            rows = c.execute("SELECT ts, speed, rpm, load, gear, maf, map FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
            sid_ect, sid_iat = self.sig_ids.get("ect"), self.sig_ids.get("iat")
            iat = c.execute("SELECT AVG(value) FROM sample WHERE signal_id=? AND ts BETWEEN ? AND ?", (sid_iat, a, b)).fetchone()[0] if sid_iat else None
            warm = c.execute("SELECT MIN(ts) FROM sample WHERE signal_id=? AND ts BETWEEN ? AND ? AND value >= 80", (sid_ect, a, b)).fetchone()[0] if sid_ect else None
            t = dict(c.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone())
            warmup = int((warm - a) / 1000) if warm and (t.get("start_ect") or 99) < 50 else None
            buckets, verdict, txt = tripdiag.air_check(rows, iat)
            eco, eco_d = tripdiag.eco_score(rows)
            c.execute("UPDATE trip SET warmup_s=?, air_json=?, air_verdict=?, eco_score=?, eco_json=? WHERE id=?",
                      (warmup, json.dumps({"buckets": buckets, "txt": txt}, ensure_ascii=False), verdict, eco,
                       json.dumps(eco_d, ensure_ascii=False) if eco_d else None, tid))
            t.update(warmup_s=warmup, air_verdict=verdict, air_txt=txt)
            if t.get("duration_s", 0) and t["duration_s"] > 300:
                self.handle_alerts(tripdiag.trip_alerts(t, b))
        except Exception as e:                      # diagnóstico nunca derruba o coletor
            log.warning("diagnóstico da viagem %s falhou: %s", tid, e)

    def end_snapshot_from_db(self, tid: int) -> None:
        t = self.con.execute("SELECT start_ts, end_ts, end_snapshot FROM trip WHERE id=?", (tid,)).fetchone()
        if not t or t["end_snapshot"]:
            return
        rows = self.con.execute("""SELECT g.key, s.value FROM sample s JOIN signal g ON g.id = s.signal_id
                                   WHERE s.ts = (SELECT MAX(ts) FROM sample s2 WHERE s2.signal_id = s.signal_id AND s2.ts BETWEEN ? AND ?)""",
                                (t["start_ts"], t["end_ts"] or t["start_ts"])).fetchall()
        snap = {"ts": t["end_ts"], "values": {r["key"]: r["value"] for r in rows}, "origem": "últimas leituras antes da queda de energia"}
        self.con.execute("UPDATE trip SET end_snapshot=? WHERE id=?", (json.dumps(snap, ensure_ascii=False), tid))

    def finalize_open_trips(self) -> None:
        for r in self.con.execute("SELECT id FROM trip WHERE finalized=0").fetchall():
            self.end_snapshot_from_db(r["id"])
            self.con.execute("UPDATE trip SET end_reason=COALESCE(end_reason,'corte_de_energia') WHERE id=?", (r["id"],))
            self.finalize_trip(r["id"])
        self.con.commit()

    # ------------------------------------------------------------------ combustível
    def load_fuel(self) -> None:
        fills = [dict(r) for r in self.con.execute("SELECT * FROM fuel_fill ORDER BY ts")]
        if fills:
            self.fuel_mix = fills[-1]["mix_after"] if fills[-1]["mix_after"] is not None else 0.0
        self.fuel_calib = fuelmod.calib_from(fills)

    # ------------------------------------------------------------------ alertas
    def handle_alerts(self, alerts: list[Alert]) -> None:
        for al in alerts:
            al.ts = al.ts or self.clock.now()
            # mesma regra nos últimos 30 min → atualiza a linha existente ("continua"), sem novo alerta
            prev = self.con.execute("SELECT id, count, severity FROM alert WHERE rule=? AND trip_id IS ? AND COALESCE(ts_end, ts) > ? ORDER BY id DESC LIMIT 1",
                                    (al.rule, self.trip_id, al.ts - 30 * 60 * 1000)).fetchone()
            if prev:
                escalated = SEV_ORDER[al.severity] > SEV_ORDER[prev["severity"]]
                sev = al.severity if escalated else prev["severity"]
                if escalated:
                    self.queue_push(f"{al.title} (piorou)", al.detail, sev)
                    if self.trip_id:
                        self.trip_alerts.append(al)
                n = (prev["count"] or 1) + 1
                self.con.execute("UPDATE alert SET count=?, ts_end=?, detail=?, value=?, severity=? WHERE id=?",
                                 (n, al.ts, al.detail, al.value, sev, prev["id"]))
                for r in self.recent_alerts:
                    if r["rule"] == al.rule:
                        r.update(count=n, ts_end=al.ts, detail=al.detail, sev=sev)
                if n == 3 and SEV_ORDER[sev] >= SEV_ORDER[self.cfg["notify"]["min_severity"]]:
                    self.queue_push(f"{al.title} (continua)", f"Repetiu 3× nos últimos minutos. {al.detail}", sev)
                continue
            self.con.execute("INSERT INTO alert(ts, trip_id, source, rule, signal, severity, title, detail, value, count)"
                             " VALUES (?,?,?,?,?,?,?,?,?,1)",
                             (al.ts, self.trip_id, al.source, al.rule, al.signal, al.severity, al.title, al.detail, al.value))
            if self.trip_id:
                self.trip_alerts.append(al)
            self.recent_alerts = ([{"ts": al.ts, "sev": al.severity, "title": al.title, "detail": al.detail,
                                    "rule": al.rule, "source": al.source, "count": 1}] + self.recent_alerts)[:20]
            # 1 push por regra por viagem; crítico pode repetir a cada 15 min
            minsev = SEV_ORDER[self.cfg["notify"]["min_severity"]]
            if SEV_ORDER[al.severity] < minsev:
                continue
            last = self.notified.get(al.rule)
            if last is None or (al.severity == "crit" and al.ts - last > 15 * 60 * 1000):
                self.notified[al.rule] = al.ts
                self.queue_push(al.title, al.detail, al.severity)

    def queue_push(self, title: str, body: str, sev: str) -> None:
        self.con.execute("INSERT INTO outbox(ts, title, body, priority, tags) VALUES (?,?,?,?,?)",
                         (self.clock.now(), title, body, notify.PRIO[sev], notify.TAGS[sev] + ",car"))

    def _notifier_loop(self) -> None:
        n = self.cfg["notify"]
        while True:
            time.sleep(15)
            try:
                notify.flush_outbox(self.db_path, n["ntfy_server"], n["ntfy_topic"], "http://obd.local")
            except Exception as e:
                log.debug("notifier: %s", e)

    # ------------------------------------------------------------------ DTCs
    def _load_dtc_state(self) -> None:
        for r in self.con.execute("SELECT code, kind FROM dtc_state WHERE active=1"):
            self.dtc_active.setdefault(r["kind"], set()).add(r["code"])

    def poll_dtc(self) -> None:
        e = self.elm
        ts = self.clock.now()
        st = e.request(0x01, [0x01])
        if 0x01 in st:
            s = decode_status(st[0x01])
            self.readiness = {"ready": s["ready"], "total": s["total"], "monitors": s["monitors"]}
            if s["mil"] != self.mil:
                self.mil = s["mil"]
                self.event("warn" if self.mil else "info", "luz_injecao", "acesa" if self.mil else "apagada")
                if self.mil:
                    self.handle_alerts([Alert("dtc", "MIL", "crit", "Luz de injeção acendeu", "A ECU acendeu a luz de anomalia (MIL)", ts=ts)])
        for kind, mode in (("confirmed", 0x03), ("pending", 0x07)):
            msgs = e.raw_mode(f"{mode:02X}")
            if not any(m and m[0] == mode + 0x40 for m in msgs):
                continue                        # NO DATA / falha momentânea: não conclui que os códigos sumiram
            codes = set(dtcmod.parse(msgs, mode, e.is_can))
            old = self.dtc_active.get(kind, set())
            for code in codes - old:
                freeze = self.read_freeze() if kind == "confirmed" else None
                self.con.execute("INSERT INTO dtc_event(ts, trip_id, code, kind, event, mil, freeze_json) VALUES (?,?,?,?,?,?,?)",
                                 (ts, self.trip_id, code, kind, "apareceu", int(self.mil), json.dumps(freeze) if freeze else None))
                self.con.execute("""INSERT INTO dtc_state(code, kind, first_seen_ts, last_seen_ts, active, count) VALUES (?,?,?,?,1,1)
                                    ON CONFLICT(code, kind) DO UPDATE SET last_seen_ts=excluded.last_seen_ts, active=1, count=count+1""",
                                 (code, kind, ts, ts))
                tb = code in dtcmod.THROTTLE_WATCH
                sev = "crit" if (kind == "confirmed" or tb) else "warn"
                self.handle_alerts([Alert("dtc_throttle" if tb else "dtc", code, sev,
                                          f"Código {code}{' (pendente)' if kind == 'pending' else ''}",
                                          dtcmod.describe(code) + (" — corpo de borboleta/marcha lenta" if tb else ""), ts=ts)])
            for code in old - codes:
                self.con.execute("INSERT INTO dtc_event(ts, trip_id, code, kind, event, mil) VALUES (?,?,?,?,?,?)",
                                 (ts, self.trip_id, code, kind, "sumiu", int(self.mil)))
                self.con.execute("UPDATE dtc_state SET active=0, last_seen_ts=? WHERE code=? AND kind=?", (ts, code, kind))
            for code in codes & old:
                self.con.execute("UPDATE dtc_state SET last_seen_ts=? WHERE code=? AND kind=?", (ts, code, kind))
            self.dtc_active[kind] = codes

    def read_freeze(self) -> dict | None:
        e = self.elm
        out = {}
        try:
            for pid in (0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x11):
                r = e.request(0x02, [pid])
                if pid in r:
                    out.update(decode(pid, r[pid]))
        except ElmError:
            pass
        return out or None

    # ------------------------------------------------------------------ live
    def write_live(self) -> None:
        now = time.time()
        self.fast_times = [t for t in self.fast_times if now - t < 10]
        live = {
            "ts": self.clock.now(), "state": self.state_name, "detail": self.state_detail,
            "engine_on": self.engine_on, "trip_id": self.trip_id, "trip_start": self.trip_start or None,
            "values": self.values, "value_ts": self.value_ts,
            "pedal_norm": self.throttle.pedal_norm(self.values.get("pedal_d")),
            "status": self.limits.status(dict(self.values, pedal_d_norm=self.throttle.pedal_norm(self.values.get("pedal_d")) or 0), self.engine_on),
            "dtc": {k: sorted(v) for k, v in self.dtc_active.items()}, "mil": self.mil,
            "alerts": self.recent_alerts[:10],
            "rates": {"fast_hz": round(len(self.fast_times) / 10, 2), "req": self.elm.n_requests if self.elm else 0},
            "adapter": {"label": self.elm.t.label if self.elm else None, "elm": self.elm.version if self.elm else None,
                        "protocol": self.elm.protocol_desc if self.elm else None,
                        "multi_pid": self.elm.multi_pid if self.elm else None,
                        "name": self.state.get("adapter_name")},
            "trip": self._trip_live_view(),
            "readiness": self.readiness,
            "accel": {k: v for k, v in self.accel.items() if k != "pts"} if self.accel else None,
            "supported": len(self.supported), "clock_synced": self.clock.synced, "version": __version__,
        }
        p = self.run_dir / "live.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(live, ensure_ascii=False))
        os.replace(tmp, p)

    def _trip_live_view(self) -> dict | None:
        if not self.trip_id:
            return None
        t = self.trip_live
        el = (self.clock.now() - self.trip_start) / 1000 if self.trip_start else 0
        price = self.con.execute("SELECT price_l FROM fuel_fill WHERE price_l IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        return {"km": round(t["km"], 2), "elapsed_s": int(el), "moving_s": int(t["moving_s"]),
                "avg_speed": round(t["km"] / (el / 3600), 1) if el > 30 else None,
                "avg_moving": round(t["km"] / (t["moving_s"] / 3600), 1) if t["moving_s"] > 30 else None,
                "max_speed": t["max_speed"], "fuel_l": round(t["fuel_l"], 3),
                "kml": round(t["km"] / t["fuel_l"], 1) if t["fuel_l"] > 0.05 else None,
                "cost": round(t["fuel_l"] * price[0], 2) if price else None}

    # ------------------------------------------------------------------ ciclos
    # ------------------------------------------------------------------ teste 0–100
    def accel_poll_cmd(self) -> None:
        c = self.run_dir / "alerts_cmd"
        if c.exists():                                 # botão "Limpar alertas" do painel
            c.unlink(missing_ok=True)
            self.recent_alerts = []
        f = self.run_dir / "accel_cmd"
        if not f.exists():
            return
        cmd = f.read_text().strip()
        f.unlink(missing_ok=True)
        if cmd == "arm":
            self.accel = {"state": "aguardando_parar", "armed_ts": self.clock.now(), "pts": [], "splits": {}}
        elif cmd == "cancel":
            self.accel = None

    def accel_feed(self, ts: int, spd: float | None, rpm: float | None) -> None:
        a = self.accel
        if not a or spd is None or a["state"] in ("concluido", "cancelado"):
            return
        if a["state"] == "aguardando_parar":
            if spd == 0:
                a["state"] = "pronto"
                a["ready_ts"] = ts
            elif ts - a["armed_ts"] > 180_000:
                a["state"] = "cancelado"
            a["last0"] = ts
            return
        if a["state"] == "pronto":
            if spd == 0:
                a["last0"] = ts
                if ts - a.get("ready_ts", ts) > 300_000:
                    a["state"] = "cancelado"      # armado e esquecido: libera a coleta normal
                return
            a["state"] = "correndo"
            a["t0"] = (a["last0"] + ts) / 2          # largada entre a última leitura parada e a primeira em movimento
            a["pts"] = [(a["last0"], 0.0)]
        a["pts"].append((ts, spd))
        a["max_rpm"] = max(a.get("max_rpm", 0), rpm or 0)
        (t1, v1), (t2, v2) = a["pts"][-2], a["pts"][-1]
        for target in (60, 80, 100):
            if str(target) not in a["splits"] and v1 < target <= v2 and v2 > v1:
                tc = t1 + (t2 - t1) * (target - v1) / (v2 - v1)
                a["splits"][str(target)] = round((tc - a["t0"]) / 1000, 2)
        a["elapsed"] = round((ts - a["t0"]) / 1000, 2)
        if "100" in a["splits"] or spd < v1 - 5 or a["elapsed"] > 40:
            a["state"] = "concluido" if "100" in a["splits"] else "incompleto"
            dts = [b[0] - x[0] for x, b in zip(a["pts"], a["pts"][1:])]
            hz = round(1000 / (sum(dts) / len(dts)), 1) if dts else None
            self.con.execute("INSERT INTO accel_run(ts, trip_id, t60, t80, t100, max_rpm, samples, hz, fuel_mix) VALUES (?,?,?,?,?,?,?,?,?)",
                             (int(a["t0"]), self.trip_id, a["splits"].get("60"), a["splits"].get("80"), a["splits"].get("100"),
                              a.get("max_rpm"), len(a["pts"]), hz, self.fuel_mix))
            a["hz"] = hz
            self.con.commit()

    def fast_cycle(self) -> None:
        ts = self.clock.now()
        self.accel_poll_cmd()
        if self.accel and self.accel["state"] in ("aguardando_parar", "pronto", "correndo"):
            raw = self.read_pids([0x0D, 0x0C])       # só velocidade e rpm: máxima taxa durante o teste
            vals = {}
            for pid, data in raw.items():
                vals.update(decode(pid, data))
            self.values.update(vals)
            for k in vals:
                self.value_ts[k] = ts
            self.fast_times.append(time.time())
            self.accel_feed(ts, vals.get("speed"), vals.get("rpm"))
            self.update_engine(vals.get("rpm"), ts)
            self.trip_live_update(vals, ts)
            if self.trip_id:
                self.con.execute("INSERT OR REPLACE INTO fast_sample(ts, trip_id, rpm, speed) VALUES (?,?,?,?)",
                                 (ts, self.trip_id, vals.get("rpm"), vals.get("speed")))
            return
        raw = self.read_pids(self.fast_pids or [0x0C])
        rot = getattr(self, "live_rot", None)
        if rot and self.engine_on and raw:
            self.live_i = (getattr(self, "live_i", -1) + 1) % len(rot)
            raw.update(self.read_pids(rot[self.live_i]))
        if not self.engine_on or (self.pending_crank and ts - self.pending_crank["ts"] < 4000):
            bv = self.elm.voltage()
            if bv is not None:
                self.volt_hist = [(t, v) for t, v in self.volt_hist if ts - t < 20000] + [(ts, bv)]
                self.values["batt"] = bv
                self.value_ts["batt"] = ts
        if self.pending_crank and ts - self.pending_crank["ts"] >= 4000 and self.trip_id:
            pc = self.pending_crank
            dip = [v for t, v in self.volt_hist if pc["ts"] - 6000 <= t <= pc["ts"] + 4000]
            if pc["rest"] is not None and dip and min(dip) < pc["rest"] - 0.5:
                self.con.execute("UPDATE trip SET rest_v=COALESCE(rest_v, ?), crank_v=COALESCE(crank_v, ?) WHERE id=?",
                                 (pc["rest"], min(dip), self.trip_id))
            self.pending_crank = None
        vals: dict[str, float] = {}
        for pid, data in raw.items():
            vals.update(decode(pid, data))
        if not vals:
            self.empty_fast = getattr(self, "empty_fast", 0) + 1
            if self.empty_fast >= 5:
                self.empty_fast = 0
                raise EcuSilent("ECU parou de responder (NO DATA)")
            return
        self.empty_fast = 0
        self.values.update(vals)
        for k in vals:
            self.value_ts[k] = ts
        self.fast_times.append(time.time())
        lh = fuelmod.fuel_lh(vals.get("maf", self.values.get("maf")), vals.get("lambda_cmd", self.values.get("lambda_cmd")),
                             self.fuel_mix, self.fuel_calib, rpm=vals.get("rpm"), load=vals.get("load"),
                             closed_loop=bool(int(self.values.get("fuel_status", 2)) & 0x02))
        if lh is not None:
            vals["fuel_lh"] = round(lh, 2)
            spd = vals.get("speed") or 0
            vals["kml_inst"] = round(min(spd / lh, 99.0), 1) if lh > 0.2 and spd > 3 else None
            self.values.update({k: vals[k] for k in ("fuel_lh", "kml_inst")})
            for k in ("fuel_lh", "kml_inst"):
                self.value_ts[k] = ts
        g = self.gear(vals.get("speed"), vals.get("rpm"))
        vals["gear"] = g
        self.values["gear"] = g
        self.value_ts["gear"] = ts
        self.update_engine(vals.get("rpm"), ts)
        self.trip_live_update(vals, ts)
        if self.engine_on or self.trip_id:
            self.con.execute(f"INSERT OR REPLACE INTO fast_sample(ts, trip_id, {', '.join(FAST_COLS)}) VALUES (?,?,{','.join('?' * len(FAST_COLS))})",
                             (ts, self.trip_id, *[vals.get(c) for c in FAST_COLS]))
            # as regras da borboleta usam janelas calibradas para ~1 amostra/s
            if ts - getattr(self, "_last_tb_feed", 0) >= 900:
                self._last_tb_feed = ts
                self.handle_alerts(self.throttle.feed(vals, ts, self.values.get("ect")))

    def full_cycle(self) -> None:
        ts = self.clock.now()
        raw = self.read_pids([p for p in self.sweep_pids if p not in self.fast_pids])
        vals: dict[str, float] = {}
        for pid, data in raw.items():
            vals.update(decode(pid, data))
        v = self.elm.voltage()
        if v is not None:
            vals["batt"] = v
        self.values.update(vals)
        for k in vals:
            self.value_ts[k] = ts
        allv = dict(self.values)
        allv["pedal_d_norm"] = self.throttle.pedal_norm(allv.get("pedal_d")) or 0
        fresh = ts - 2 * self.full_period * 1000
        rows = [(self.sig_id(k), ts, x) for k, x in allv.items()
                if k != "pedal_d_norm" and x is not None and self.value_ts.get(k, 0) >= fresh]
        self.con.executemany("INSERT OR REPLACE INTO sample(signal_id, ts, value) VALUES (?,?,?)", rows)
        expected = {k for p in self.sweep_pids if p not in self.fast_pids for k in decode(p, b"\x80" * 8)}
        if self.trip_id and vals.get("run_time") is not None:
            self.con.execute("UPDATE trip SET last_run_time=? WHERE id=?", (vals["run_time"], self.trip_id))
        self.handle_alerts(self.limits.evaluate(allv, self.engine_on, ts) +
                           self.limits.check_missing(expected, vals, ts, self.engine_on))
        self.load_fuel()
        if int(ts / 1000) % 300 < self.full_period:      # a cada ~5 min persiste aprendizado
            self.con.executemany("INSERT OR REPLACE INTO baseline(key, n, mean, m2, updated_ts) VALUES (?,?,?,?,?)",
                                 [(*r, ts) for r in self.limits.baseline_rows()])
            self.state.set("throttle", self.throttle.state())

    def run_connected(self) -> None:
        self.elm.init()
        if self.elm.protocol_num and self.state.get("protocol") in (None, "auto"):
            self.state.set("protocol", self.elm.protocol_num)   # fixa o protocolo descoberto (conecta mais rápido)
        self.start_session()
        self.set_state("descobrindo", "lendo PIDs suportados")
        self.discover()
        self.set_state("coletando")
        self.last_full = self.last_dtc = 0.0
        while True:
            t0 = time.monotonic()
            sd_notify("WATCHDOG=1")
            self.fast_cycle()
            if getattr(self, "snapshot_pending", False) and self.trip_id:
                self.take_start_snapshot()
                self.last_full = self.last_dtc = time.monotonic()
            testing = self.accel and self.accel["state"] in ("pronto", "correndo")
            if t0 - self.last_full >= self.full_period and not testing:
                self.last_full = t0
                self.full_cycle()
            if t0 - self.last_dtc >= self.dtc_period and not testing:
                self.last_dtc = t0
                self.poll_dtc()
            self.write_live()
            self.maybe_commit()
            dt = self.fast_period - (time.monotonic() - t0)
            if dt > 0 and not testing:
                time.sleep(dt)

    def ecu_silent_loop(self, why: str) -> None:
        """ELM ok mas ECU calada (ignição desligada): grava tensão da bateria e espera a ECU voltar."""
        self.set_state("ecu_silenciosa", why)
        if self.engine_on:
            self.engine_on = False
            self.event("info", "motor_desligado", "ECU parou de responder")
        while True:
            sd_notify("WATCHDOG=1")
            ts = self.clock.now()
            self.update_engine(None, ts)
            v = self.elm.voltage()
            if v is not None:
                self.values["batt"] = v
                self.value_ts["batt"] = ts
                if time.monotonic() - self.last_batt_store > 60:
                    self.last_batt_store = time.monotonic()
                    self.con.execute("INSERT OR REPLACE INTO sample(signal_id, ts, value) VALUES (?,?,?)", (self.sig_id("batt"), ts, v))
                    self.handle_alerts(self.limits.evaluate({"batt": v}, False, ts, learn=False))
            self.write_live()
            self.maybe_commit()
            try:
                if self.elm.request(0x01, [0x00]):
                    return
            except EcuSilent:
                pass
            time.sleep(5)

    def run(self) -> None:
        sd_notify("READY=1")
        backoff = 2
        fails = 0
        while True:
            sd_notify("WATCHDOG=1")
            try:
                t = self.make_transport()
                self.set_state("conectando", t.label)
                t.open()
                self.elm = ELM327(t, self.state.get("protocol", self.cfg["adapter"]["protocol"]),
                                  "/data/obd/raw.jsonl" if self.cfg["adapter"]["record_raw"] else None)
                backoff, fails = 2, 0
                while True:
                    try:
                        self.run_connected()
                    except EcuSilent as e:
                        self.maybe_commit(force=True)
                        self.ecu_silent_loop(str(e)[:80])
                    except ElmNeedsReset as e:
                        self.event("warn", "elm_reset", str(e)[:80])
            except (TransportError, TimeoutError, OSError, ElmError) as e:
                fails += 1
                if self.cfg["adapter"]["transport"] == "rfcomm" and fails >= 2 and self.state.get("adapter_mac"):
                    bt._ctl("disconnect", self.state.get("adapter_mac"), timeout=10)
                self.elm = None
                self.set_state("desconectado", str(e)[:120])
                self.maybe_commit(force=True)
                if self.trip_id and self.clock.now() - self.last_engine_ts > self.trip_end_s * 1000:
                    self.end_trip("sem_conexao")
                if fails % 10 == 0 and self.cfg["adapter"]["transport"] == "rfcomm" and not self.cfg["adapter"]["mac"]:
                    # depois de muitas falhas, esquece o MAC salvo e volta a procurar
                    if fails >= 30:
                        self.state.set("adapter_mac", None)
                end = time.monotonic() + backoff
                while time.monotonic() < end:
                    sd_notify("WATCHDOG=1")
                    time.sleep(1)
                backoff = min(backoff * 2, 15)


def main() -> None:
    logging.basicConfig(level=os.environ.get("OBD_LOG", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    if len(sys.argv) > 1 and sys.argv[1] == "--tcp":
        cfg["adapter"]["transport"] = "tcp"
    col = Collector(cfg)

    def stop(*_):
        # fecha o RFCOMM e grava antes de sair: evita o adaptador ficar "preso" na conexão antiga
        try:
            col.maybe_commit(force=True)
            if col.elm:
                col.elm.t.close()
        finally:
            os._exit(0)
    import signal
    signal.signal(signal.SIGTERM, stop)
    col.run()


if __name__ == "__main__":
    main()
