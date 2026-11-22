"""obdctl gps check — conferência rápida, só de leitura, do que dá para ver por software no dia do hardware
(docs/DIA-DO-GPS.md). Não muda nada: não pede taxa ao receptor, não reinicia serviço, não fala com a nuvem (o estado
da senha de localização vem do último ping do obd-sync, em /run/obd/share_cloud.json).

Cada linha sai como ok | aviso | falha | — (não se aplica), com o que conferir quando não está ok. Código de saída 1
quando há alguma falha. Sem GPS nada quebra: só aparecem as falhas.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

OK, WARN, FAIL, SKIP = "ok", "aviso", "falha", "—"
GEO_OLD_DAYS = 180


# ---------------------------------------------------------------- coleta (cada uma devolve dados, nunca levanta)
def probe_gpsd(host: str, port: int, timeout: float = 4.0) -> dict:
    """Conecta no gpsd como mais um cliente, pede o WATCH (só leitura) e junta VERSION, DEVICES e o melhor TPV."""
    out = {"reachable": False, "version": None, "devices": None, "tpv_mode": None, "error": None}
    try:
        s = socket.create_connection((host, int(port)), timeout=2)
    except OSError as e:
        out["error"] = str(e)
        return out
    out["reachable"] = True
    end = time.monotonic() + timeout
    buf = b""
    try:
        s.sendall(b'?WATCH={"enable":true,"json":true};\n')
        while time.monotonic() < end:
            s.settimeout(max(0.05, end - time.monotonic()))
            try:
                d = s.recv(4096)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                c = m.get("class")
                if c == "VERSION":
                    out["version"] = m.get("release")
                elif c == "DEVICES":
                    out["devices"] = [{"path": x.get("path"), "driver": x.get("driver"), "cycle": x.get("cycle")}
                                      for x in m.get("devices") or []]
                elif c == "TPV" and isinstance(m.get("mode"), int):
                    out["tpv_mode"] = max(out["tpv_mode"] or 0, m["mode"])
            if out["devices"] is not None and (out["tpv_mode"] == 3 or not out["devices"]):
                break
    except OSError as e:
        out["error"] = str(e)
    finally:
        try:
            s.close()
        except OSError:
            pass
    return out


def chrony_gps(run=subprocess.run) -> dict | None:
    """Linha GPS de `chronyc -n -c sources`: {'found', 'state', 'reach'}. None sem chronyc."""
    try:
        r = run(["chronyc", "-n", "-c", "sources"], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    for ln in (r.stdout or "").splitlines():
        f = ln.split(",")
        if len(f) > 5 and f[2].strip().upper() == "GPS":
            try:
                reach = int(f[5], 8)
            except ValueError:
                reach = None
            return {"found": True, "state": f[1].strip(), "reach": reach}
    return {"found": False, "state": None, "reach": None}


def throttled(run=subprocess.run) -> int | None:
    try:
        r = run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        return int(r.stdout.strip().split("=")[1], 16) if r.returncode == 0 else None
    except Exception:
        return None


def geo_info(path: str, now: float) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"exists": False}
    out = {"exists": True, "mb": round(p.stat().st_size / 1e6, 1)}
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=2)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        con.close()
        ref = meta.get("osm_ts") or meta.get("built_ts")
        out.update(cameras=meta.get("cameras"), roads=meta.get("roads"),
                   age_d=int((now * 1000 - int(ref)) / 86400_000) if ref else None)
    except (sqlite3.Error, ValueError) as e:
        out["error"] = str(e)
    return out


def _read_json(p: Path) -> tuple[dict | None, float | None]:
    try:
        return json.loads(p.read_text()), p.stat().st_mtime
    except (OSError, ValueError):
        return None, None


def gather(cfg: dict, run_dir: Path | None = None, *, gpsd=probe_gpsd, chrony=chrony_gps, power=throttled,
           synced=None, clock_src=None, now=time.time) -> dict:
    """Tudo o que `evaluate` precisa, lido do sistema. As funções entram por parâmetro (testes)."""
    from .. import timesrc
    g = cfg.get("gps") or {}
    run_dir = Path(run_dir or Path(cfg["runtime"]["live_sock"]).parent)
    t = now()
    gj, gj_mtime = _read_json(run_dir / "gps.json")
    sc, _ = _read_json(run_dir / "share_cloud.json")
    on = bool(g.get("enabled", True)) and str(g.get("source", "auto")) != "off"
    return {
        "now": t, "enabled": on,
        "gpsd": gpsd(g.get("gpsd_host", "127.0.0.1"), g.get("gpsd_port", 2947)) if on else None,
        "gps_json": gj, "gps_json_age": (t - gj_mtime) if gj_mtime else None,
        "synced": (synced or timesrc.system_synced)(), "clock_src": (clock_src or timesrc.source)(),
        "chrony": chrony(), "geo": geo_info(g.get("geo_db") or "/data/obd/geo/geo.db", t),
        "cloud_url": bool((cfg.get("cloud") or {}).get("url")), "gps_cloud": str(g.get("cloud", "auto")).lower(),
        "share_cloud": sc, "throttled": power(),
    }


# ---------------------------------------------------------------- avaliação (pura: só dados → linhas)
def evaluate(inp: dict, cfg: dict) -> list[tuple[str, str, str, str]]:
    """[(estado, item, detalhe, o que conferir)]."""
    g = cfg.get("gps") or {}
    rate = float(g.get("rate_hz") or 5)
    rows: list[tuple[str, str, str, str]] = []
    add = lambda st, name, det, hint="": rows.append((st, name, det, hint))

    if not inp["enabled"]:
        add(WARN, "GPS na configuração", "desligado ([gps] enabled = false ou source = \"off\")", "/etc/obd/obd.toml, seção [gps]")
    else:
        add(OK, "GPS na configuração", f"fonte {g.get('source', 'auto')}, {rate:g} Hz pedidos")

    d = inp.get("gpsd")
    if d is None:
        add(SKIP, "gpsd", "GPS desligado na configuração")
    elif not d["reachable"]:
        add(FAIL, "gpsd responde", f"sem conexão em {g.get('gpsd_host', '127.0.0.1')}:{g.get('gpsd_port', 2947)}",
            "systemctl status gpsd.socket gpsd; sudo systemctl restart gpsd")
    else:
        add(OK, "gpsd responde", f"versão {d.get('version') or '?'}")
        devs = d.get("devices")
        if not devs:
            add(FAIL, "receptor no gpsd", "nenhum aparelho" if devs == [] else "o gpsd não listou os aparelhos",
                "lsusb | grep -i 1546; ls -l /dev/gps0 /dev/ttyACM0; deploy/70-obd-gps.rules (idProduct)")
        else:
            dv = devs[0]
            drv = dv.get("driver") or "?"
            add(OK if "u-blox" in drv.lower() else WARN, "receptor no gpsd", f"{dv.get('path')} · driver {drv}",
                "" if "u-blox" in drv.lower() else "esperado driver u-blox (VK-172): gpspipe -w -n 5")
            cyc = dv.get("cycle")
            if isinstance(cyc, (int, float)) and cyc > 0:
                hz = 1 / cyc
                add(OK if hz >= rate * 0.8 else WARN, "taxa no receptor", f"{hz:.1f} Hz (cycle {cyc:g} s)",
                    "" if hz >= rate * 0.8 else "o coletor pede 5 Hz ao conectar; se não subir: ubxtool -p CFG-RATE,200 "
                                                "(e ExecStartPost no gpsd, item B4 do docs/DIA-DO-GPS.md)")
        m = d.get("tpv_mode")
        if devs:
            add(OK if m == 3 else WARN if m == 2 else FAIL, "posição no gpsd",
                {3: "3D", 2: "2D (poucos satélites)"}.get(m, "sem posição ainda"),
                "" if m == 3 else "céu aberto, esperar até 60 s (partida a frio pode levar alguns minutos)")

    gj, age = inp.get("gps_json"), inp.get("gps_json_age")
    if not inp["enabled"]:
        add(SKIP, "coletor e GPS", "GPS desligado na configuração")
    elif gj is None:
        add(FAIL, "coletor e GPS", "sem /run/obd/gps.json", "systemctl status obd-collector; journalctl -u obd-collector -n 50")
    elif age is not None and age > 15:
        add(FAIL, "coletor e GPS", f"/run/obd/gps.json parado há {int(age)} s", "coletor travado? journalctl -u obd-collector -n 50")
    else:
        st = gj.get("status") or {}
        state = st.get("state") or gj.get("state")
        if state == "ok":
            add(OK, "coletor e GPS", f"ok · fonte {st.get('src')} · {st.get('reconnects') or 0} reconexão(ões)"
                + (f" · 1ª posição em {st['ttff_s']} s" if st.get("ttff_s") is not None else ""))
            sats, hdop = st.get("sats"), st.get("hdop")
            if sats is not None and hdop is not None:
                good = sats >= 7 and hdop <= 1.5
                fair = sats >= int(g.get("min_sats", 5)) and hdop <= float(g.get("max_hdop", 5.0))
                add(OK if good else WARN if fair else FAIL, "satélites e HDOP", f"{sats} satélites · HDOP {hdop}",
                    "" if good else "esperado ≥ 7 e ≤ 1,5 com céu aberto; receptor no painel, longe de película metálica")
            hz = st.get("hz")
            if hz is not None:
                add(OK if hz >= rate * 0.8 else WARN, "taxa medida pelo coletor", f"{hz:g} Hz",
                    "" if hz >= rate * 0.8 else "ficou abaixo do pedido: item B4 do docs/DIA-DO-GPS.md")
        else:
            add(WARN if state == "sem_sinal" else FAIL, "coletor e GPS", {"sem_sinal": "conectado, sem posição",
                "desconectado": "GPS não conectado", "desligado": "desligado"}.get(state, str(state)),
                "céu aberto e até 60 s" if state == "sem_sinal" else "gpsd e receptor (linhas acima)")

    if inp["synced"]:
        src = inp.get("clock_src")
        add(OK, "relógio", "sincronizado pelo GPS" if src == "gps" else "sincronizado pela internet (NTP)" if src == "ntp"
            else "sincronizado")
    else:
        add(WARN, "relógio", "não sincronizado", "chronyc tracking; sem internet, o GPS acerta em até 2 min com posição")
    ch = inp.get("chrony")
    if ch is None:
        add(FAIL, "chrony com o GPS", "chronyc não encontrado ou sem resposta", "systemctl status chrony (instalado pelo deploy/install.sh)")
    elif not ch["found"]:
        add(FAIL, "chrony com o GPS", "sem fonte GPS em chronyc sources", "/etc/chrony/conf.d/obd-gps.conf (deploy/chrony-obd-gps.conf); gpsd com -n")
    elif not ch["reach"]:
        add(WARN, "chrony com o GPS", "fonte GPS sem leituras ainda (reach 0)", "esperar a posição; chronyc sources -v")
    else:
        sel = {"*": "em uso", "+": "aceita", "-": "aceita (não combinada)", "?": "sem conexão", "x": "recusada (falseticker)",
               "~": "variável demais"}.get(ch["state"], ch["state"])
        add(OK if ch["state"] in ("*", "+", "-") else WARN, "chrony com o GPS", f"fonte GPS {sel} · reach {ch['reach']:o}",
            "" if ch["state"] in ("*", "+", "-") else "comparar com NTP: chronyc sources -v; offset em deploy/chrony-obd-gps.conf")

    geo = inp.get("geo") or {}
    if not geo.get("exists"):
        add(WARN, "mapa offline (geo.db)", "ausente em /data/obd/geo/", "no PC: scripts/geo-build.py; scripts/deploy.sh envia data/geo/geo.db")
    elif geo.get("error"):
        add(FAIL, "mapa offline (geo.db)", f"ilegível: {geo['error']}", "gerar de novo no PC e reenviar pelo scripts/deploy.sh")
    else:
        old = (geo.get("age_d") or 0) > GEO_OLD_DAYS
        add(WARN if old else OK, "mapa offline (geo.db)", f"{geo.get('mb')} MB · {geo.get('cameras')} radares · "
            f"{geo.get('roads')} vias · mapa de {geo.get('age_d')} dias", "mais de 6 meses: gerar de novo" if old else "")

    if not inp.get("cloud_url"):
        add(SKIP, "localização na nuvem", "sem [cloud] configurado")
    elif inp.get("gps_cloud") == "off":
        add(WARN, "localização na nuvem", "[gps] cloud = \"off\": posição não sobe (decisão local)")
    else:
        sc = inp.get("share_cloud")
        if not sc:
            add(WARN, "localização na nuvem", "obd-sync ainda não falou com a nuvem neste boot", "systemctl status obd-sync; internet?")
        else:
            ago = int(inp["now"] - (sc.get("ts") or 0) / 1000)
            add(OK if sc.get("loc") is True else FAIL, "localização na nuvem",
                (f"LOC_PASSWORD configurada (ping há {ago} s)" if sc.get("loc") is True else f"nuvem sem LOC_PASSWORD (ping há {ago} s)"),
                "" if sc.get("loc") is True else "parte A do docs/DIA-DO-GPS.md (wrangler secret put LOC_PASSWORD + npm run deploy)")

    th = inp.get("throttled")
    if th is None:
        add(SKIP, "alimentação do Pi", "vcgencmd indisponível")
    elif th & 0x1:
        add(FAIL, "alimentação do Pi", f"subtensão AGORA (0x{th:x})", "conversor 12→5 V de 3 A reais; cabo curto e grosso")
    elif th & 0x10000:
        add(WARN, "alimentação do Pi", f"houve subtensão desde o boot (0x{th:x})", "conversor 12→5 V de 3 A reais; ver na partida")
    else:
        add(OK, "alimentação do Pi", f"sem subtensão (0x{th:x})")
    return rows


def render(rows) -> str:
    out = []
    for st, name, det, hint in rows:
        out.append(f"[{st:^5}] {name}: {det}")
        if hint and st != OK:
            out.append(f"        → {hint}")
    n = {k: sum(1 for r in rows if r[0] == k) for k in (OK, WARN, FAIL)}
    out.append(f"\n{n[OK]} ok · {n[WARN]} aviso(s) · {n[FAIL]} falha(s)")
    return "\n".join(out)


def main(cfg: dict) -> int:
    rows = evaluate(gather(cfg), cfg)
    print(render(rows))
    return 1 if any(r[0] == FAIL for r in rows) else 0
