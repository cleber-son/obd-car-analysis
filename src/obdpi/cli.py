"""obdctl — ferramentas de diagnóstico.

  obdctl scan              procura adaptadores OBD no Bluetooth
  obdctl pair MAC          pareia e confia no adaptador
  obdctl probe [--tcp]     conecta, lista PIDs suportados, lê todos uma vez e mede a taxa
  obdctl status            mostra /run/obd/live.json resumido
  obdctl test-push         envia notificação de teste ao iPhone
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
    elif args[0] == "status":
        try:
            d = json.load(open("/run/obd/live.json"))
        except FileNotFoundError:
            sys.exit("coletor não está rodando")
        print(f"estado={d['state']} {d.get('detail', '')}\nmotor={'ligado' if d['engine_on'] else 'desligado'} viagem={d['trip_id']}")
        print(f"taxa={d['rates']} DTC={d['dtc']} MIL={d['mil']}")
        for k, v in sorted(d["values"].items()):
            print(f"  {k:16s} {v}")
    elif args[0] == "test-push":
        n = cfg["notify"]
        ok = notify.send(n["ntfy_server"], n["ntfy_topic"], "Teste do Civic OBD ✅", "Se você recebeu isto, as notificações estão funcionando.", 3, "white_check_mark")
        print("enviado" if ok else "falhou (sem internet ou sem tópico)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
