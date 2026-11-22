"""obdctl — ferramentas de diagnóstico.

  obdctl scan              procura adaptadores OBD no Bluetooth
  obdctl pair MAC          pareia e confia no adaptador
  obdctl probe [--tcp]     conecta, lista PIDs suportados, lê todos uma vez e mede a taxa
  obdctl probe-extra       (coletor parado) tenta ler o que a central guarda além do básico: ajuste longo, autotestes
                           detalhados (modo 06, falha de ignição por cilindro), quadro congelado e identificação
  obdctl status            mostra /run/obd/live.json resumido
  obdctl test-push         envia notificação de teste ao iPhone
  obdctl gps status        estado do GPS: receptor, satélites, taxa, hora, posições gravadas
  obdctl gps sim ...       viagem simulada (GPS + OBD): --scenario commute|tunnel|towed, --out arquivo.nmea|.jsonl,
                           --to-db banco --days N, --serve PORTA (finge ser o gpsd); veja obdctl gps sim --help
  obdctl gps reanalyze [--trip N | --all]   refaz o fechamento das viagens com GPS (origem/destino, paradas, trajeto…);
                           sem opção: as viagens dos últimos 7 dias
"""
from __future__ import annotations

import json
import sys
import time

from . import config, notify
from .collector import bt
from .elm.elm327 import ELM327
from .elm.transport import RfcommTransport, TcpTransport
from .obd import dtc as dtcmod
from .obd.pids import INFO_ONLY, PID_LEN, SIGNALS, decode, parse_supported


def probe(cfg, tcp: bool):
    a = cfg["adapter"]
    st = config.State(cfg["storage"]["state_path"])
    if tcp or a["transport"] == "tcp":
        t = TcpTransport(a["tcp_host"], a["tcp_port"])
    else:
        mac = a["mac"] or st.get("adapter_mac")
        if not mac:
            hit = bt.find_adapter(a["name_patterns"])
            if not hit:
                sys.exit("Nenhum adaptador OBD encontrado. Ligue a ignição e rode 'obdctl scan'.")
            mac = hit[0]
            bt.pair(mac)
        t = RfcommTransport(mac, a["rfcomm_channel"])
    print(f"Conectando {t.label} ...")
    t.open()
    e = ELM327(t, "auto")
    e.init()
    print(f"ELM: {e.version}\nProtocolo: {e.protocol_num} — {e.protocol_desc}\nMulti-PID: {e.multi_pid}")
    sup = set()
    base = 0
    while base <= 0xC0:
        r = e.request(1, [base])
        if base not in r:
            break
        got = parse_supported(base, r[base])
        sup |= got
        if base + 0x20 not in got:
            break
        base += 0x20
    print(f"\n{len(sup)} PIDs suportados: {' '.join(f'{p:02X}' for p in sorted(sup))}\n")
    pids = [p for p in sorted(sup) if p in PID_LEN and p not in INFO_ONLY and p % 0x20]
    t0 = time.monotonic()
    n0 = e.n_requests
    for p in pids:
        r = e.request(1, [p])
        for k, v in decode(p, r.get(p, b"")).items():
            s = SIGNALS.get(k)
            print(f"  {p:02X}  {s.name if s else k:42s} {v:>10g} {s.unit if s else ''}")
        if p not in r:
            print(f"  {p:02X}  (sem resposta)")
    dt = time.monotonic() - t0
    print(f"\nTaxa: {(e.n_requests - n0) / dt:.1f} requisições/s")
    print(f"Bateria: {e.voltage()} V")
    for mode, name in ((3, "confirmados"), (7, "pendentes")):
        codes = dtcmod.parse(e.raw_mode(f"{mode:02X}"), mode, e.is_can)
        print(f"DTCs {name}: {', '.join(f'{c} ({dtcmod.describe(c)})' for c in codes) or 'nenhum'}")
    t.close()


def _open(cfg):
    a = cfg["adapter"]
    st = config.State(cfg["storage"]["state_path"])
    if a["transport"] == "tcp":
        t = TcpTransport(a["tcp_host"], a["tcp_port"])
    else:
        mac = a["mac"] or st.get("adapter_mac")
        if not mac:
            sys.exit("Adaptador ainda não pareado: rode 'obdctl probe' primeiro.")
        t = RfcommTransport(mac, a["rfcomm_channel"])
    t.open()
    e = ELM327(t, "auto")
    e.init()
    return t, e


def probe_extra(cfg, out_path: str = "/data/obd/probe-extra.txt"):
    """Sondagem exploratória (só leitura) do que a central responde além do modo 01 básico. Ignição ligada e coletor
    parado (sudo systemctl stop obd-collector). O adaptador clone só devolve o 1º quadro de respostas longas: o que
    aparecer cortado aqui pede um adaptador melhor ou leitura direta da rede CAN."""
    t, e = _open(cfg)
    lines = [f"# probe-extra {time.strftime('%Y-%m-%d %H:%M:%S')} · ELM {e.version} · {e.protocol_desc}"]

    def ask(label: str, cmd: str, timeout: float = 3.0):
        try:
            r = e.cmd(cmd, timeout=timeout)
        except Exception as ex:                         # NO DATA, erro do adaptador etc.: registra e segue
            r = [f"<{type(ex).__name__}: {ex}>"]
        lines.append(f"{cmd:10s} {label:44s} {' | '.join(r)}")
        print(lines[-1])

    for pid, name in ((0x07, "ajuste longo banco 1"), (0x08, "ajuste curto banco 2"), (0x09, "ajuste longo banco 2"), (0x14, "sonda O2 B1S1 (tensão/ajuste)"),
                      (0x24, "sonda larga B1S1 (lambda/tensão)"), (0x2E, "purga EVAP"), (0x3C, "temperatura do catalisador B1S1"), (0x43, "carga absoluta"),
                      (0x46, "temperatura ambiente"), (0x5C, "temperatura do óleo"), (0x5E, "consumo instantâneo")):
        ask(name, f"01{pid:02X}")
    ask("quadro congelado: DTC que o gerou", "020200")
    for pid, name in ((0x04, "carga"), (0x05, "temperatura do motor"), (0x06, "ajuste curto"), (0x0B, "MAP"), (0x0C, "rotação"), (0x0D, "velocidade")):
        ask(f"quadro congelado: {name}", f"02{pid:02X}00")
    for mid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0):
        ask(f"modo 06: monitores suportados a partir de {mid:02X}", f"06{mid:02X}")
    for mid, name in ((0x01, "sonda B1S1"), (0x02, "sonda B1S2"), (0x21, "catalisador B1"), (0x31, "EGR/VVT"), (0x39, "EVAP (tampa)"), (0x3B, "EVAP (0,040\")"), (0x3C, "EVAP (0,020\")"),
                      (0xA1, "falha de ignição geral"), (0xA2, "falha de ignição cilindro 1"), (0xA3, "falha de ignição cilindro 2"), (0xA4, "falha de ignição cilindro 3"), (0xA5, "falha de ignição cilindro 4")):
        ask(f"modo 06: {name}", f"06{mid:02X}", 4.0)
    for pid, name in ((0x00, "itens de informação suportados"), (0x02, "VIN"), (0x04, "identificação da calibração"), (0x06, "CVN"), (0x08, "contadores de uso dos monitores"), (0x0A, "nome da central")):
        ask(f"modo 09: {name}", f"09{pid:02X}", 4.0)
    ask("códigos permanentes (modo 0A)", "0A")
    t.close()
    try:
        open(out_path, "w").write("\n".join(lines) + "\n")
        print(f"\nSalvo em {out_path}")
    except OSError as ex:
        print(f"\n(não consegui salvar em {out_path}: {ex})")


def gps_status(cfg):
    from pathlib import Path
    run = Path(cfg["runtime"]["live_sock"]).parent
    try:
        g = json.loads((run / "gps.json").read_text())
    except (OSError, ValueError):
        sys.exit("coletor não está rodando (sem /run/obd/gps.json)")
    st = g.get("status") or {}
    print(f"estado={st.get('state') or g.get('state')} fonte={st.get('src')} receptor={st.get('device') or '—'} driver={st.get('driver') or '—'}")
    print(f"modo={st.get('mode')} satélites={st.get('sats')} hdop={st.get('hdop')} taxa={st.get('hz')} Hz (pedida {st.get('rate_hz')})")
    print(f"idade={st.get('age_s')} s  1ª posição={st.get('ttff_s')} s  reconexões={st.get('reconnects')}  relógio={g.get('clock')}")
    if g.get("lat") is not None:
        print(f"posição={g['lat']:.6f}, {g['lon']:.6f}  alt={g.get('alt')} m  vel={g.get('speed')} km/h")
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{cfg['storage']['db_path']}?mode=ro", uri=True, timeout=5)
        n, last = con.execute("SELECT COUNT(*), MAX(ts) FROM gps_fix").fetchone()
        print(f"posições gravadas={n}" + (f" (última {time.strftime('%d/%m %H:%M:%S', time.localtime(last / 1000))})" if last else ""))
    except Exception as e:
        print(f"banco: {e}")


def gps_reanalyze(cfg, args: list[str]) -> int:
    """Roda de novo a lista FINALIZERS (a mesma do fechamento) nas viagens pedidas. Devolve quantas foram refeitas."""
    from . import db
    from .gps.hub import make_ctx, run_finalizers
    con = db.connect(cfg["storage"]["db_path"])
    try:
        if "--trip" in args:
            i = args.index("--trip")
            try:
                ids = [int(args[i + 1])]
            except (IndexError, ValueError):
                sys.exit("uso: obdctl gps reanalyze --trip N")
        elif "--all" in args:
            ids = [r[0] for r in con.execute("SELECT id FROM trip WHERE finalized=1 ORDER BY start_ts")]
        else:
            since = int(time.time() * 1000) - 7 * 86400_000
            ids = [r[0] for r in con.execute("SELECT id FROM trip WHERE finalized=1 AND start_ts >= ? ORDER BY start_ts", (since,))]
        n = 0
        for tid in ids:
            t = con.execute("SELECT end_ts FROM trip WHERE id=?", (tid,)).fetchone()
            if not t:
                print(f"viagem {tid}: não existe")
                continue
            run_finalizers(con, tid, make_ctx(con, cfg, trip_id=tid, ts=t[0]))
            con.commit()                                   # uma por vez: o coletor (outro escritor) não fica esperando
            n += 1
        print(f"{n} viagem(ns) refeita(s)")
        return n
    finally:
        con.close()


def main():
    cfg = config.load()
    args = sys.argv[1:] or ["help"]
    if args[0] == "scan":
        hit = bt.find_adapter(cfg["adapter"]["name_patterns"], 25)
        print("Encontrado:", hit) if hit else print("Nenhum adaptador com nome de OBD. Dispositivos vistos:\n" + bt._ctl("devices"))
    elif args[0] == "pair" and len(args) > 1:
        print("OK" if bt.pair(args[1]) else "Falhou (veja journalctl -u obd-btagent)")
    elif args[0] == "probe":
        probe(cfg, "--tcp" in args)
    elif args[0] == "probe-extra":
        probe_extra(cfg)
    elif args[0] == "status":
        try:
            d = json.load(open("/run/obd/live.json"))
        except FileNotFoundError:
            sys.exit("coletor não está rodando")
        print(f"estado={d['state']} {d.get('detail', '')}\nmotor={'ligado' if d['engine_on'] else 'desligado'} viagem={d['trip_id']}")
        print(f"taxa={d['rates']} DTC={d['dtc']} MIL={d['mil']}")
        for k, v in sorted(d["values"].items()):
            print(f"  {k:16s} {v}")
    elif args[0] == "gps" and len(args) > 1 and args[1] == "status":
        gps_status(cfg)
    elif args[0] == "gps" and len(args) > 1 and args[1] == "sim":
        from .gps import sim
        sim.main(args[2:])
    elif args[0] == "gps" and len(args) > 1 and args[1] == "reanalyze":
        gps_reanalyze(cfg, args[2:])
    elif args[0] == "test-push":
        n = cfg["notify"]
        ok = notify.send(n["ntfy_server"], n["ntfy_topic"], "Teste do Civic OBD ✅", "Se você recebeu isto, as notificações estão funcionando.", 3, "white_check_mark")
        print("enviado" if ok else "falhou (sem internet ou sem tópico)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
