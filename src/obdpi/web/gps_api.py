"""Rotas /api/gps/* (docs/GPS-ARQUITETURA.md, seção 7.1). Posição só sai por aqui; na nuvem este prefixo é protegido
pela senha de localização (F3), exceto as rotas sem posição (status, gears, speedo, score, perf, dyno, terrain, roadmix).
Os radares do geo.db (/api/gps/cameras, F7) têm posição: ficam protegidos e não sobem para a nuvem.

Cada fase acrescenta um bloco próprio, com comentário de cabeçalho (# ---- F2: …).
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import config, db, timesrc


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---- F3: alertas com posição e mapa de calor (funções puras; os testes chamam direto)
HEAT_SIGNALS = ("stft1", "load", "kml", "speed", "ect", "map")     # os oferecidos no painel e sincronizados com a nuvem
HEAT_CELL_DEG = 0.0005           # ~55 m
HEAT_MAX_CELLS = 5000            # passou disso, a célula dobra
HOT_RADIUS_M = 100               # "este alerta acontece sempre aqui": mesma regra a até ~100 m…
HOT_MIN = 3                      # …3 vezes ou mais
ALERT_TOL_MS = 10_000            # posição gravada mais próxima a até 10 s


def heat_cells(tracks: list[dict], signal: str, size: float = HEAT_CELL_DEG, max_cells: int = HEAT_MAX_CELLS) -> dict:
    """Média do sinal por célula sobre os trajetos guardados (track_json). A célula comum a duas viagens soma o 'n' e
    a média é ponderada pelo número de pontos. {"cells": [[lat, lon, média, n], …], "min", "max", "cell_deg"}."""
    pts = []
    for trk in tracks:
        vals = (trk.get("cols") or {}).get(signal) or []
        for la, lo, v in zip(trk.get("lat") or [], trk.get("lon") or [], vals):
            if v is not None and la is not None and lo is not None:
                pts.append((float(la), float(lo), float(v)))
    while True:
        acc: dict[tuple[int, int], list] = {}
        for la, lo, v in pts:
            k = (math.floor(la / size), math.floor(lo / size))
            a = acc.get(k)
            if a is None:
                acc[k] = [v, 1]
            else:
                a[0] += v
                a[1] += 1
        if len(acc) <= max_cells or size >= 0.05:
            break
        size *= 2
    cells = sorted([round((i + 0.5) * size, 6), round((j + 0.5) * size, 6), round(sv / n, 3), n] for (i, j), (sv, n) in acc.items())
    means = [c[2] for c in cells]
    return {"signal": signal, "cell_deg": size, "cells": cells, "n_points": len(pts),
            "min": min(means) if means else None, "max": max(means) if means else None}


def alerts_with_position(con, since: int, rule: str | None = None, tol_ms: int = ALERT_TOL_MS, limit: int = 2000) -> dict:
    """Alertas (e códigos de falha que não viraram alerta) com a posição gravada mais próxima (até 10 s), velocidade e
    altitude naquele ponto, e os pontos repetidos (3+ alertas da mesma regra na mesma célula de ~100 m)."""
    from ..gps import geo as geomod
    from ..gps import store as gstore
    q = "SELECT id, ts, trip_id, source, rule, severity, title, detail, value FROM alert WHERE ts >= ?"
    args: list = [since]
    if rule:
        q += " AND rule = ?"
        args.append(rule)
    items = [dict(a, kind="alerta") for a in con.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit))]
    seen = {(a["rule"], a["ts"] // 5000) for a in items}
    for d in con.execute("SELECT id, ts, trip_id, code, kind, mil FROM dtc_event WHERE ts >= ? AND event = 'apareceu' ORDER BY ts DESC LIMIT ?",
                         (since, limit)):
        r = f"dtc:{d['code']}"
        if rule and rule not in (r, d["code"]):
            continue
        if (d["code"], d["ts"] // 5000) in seen:            # o código já gerou um alerta no mesmo instante
            continue
        items.append({"id": d["id"], "ts": d["ts"], "trip_id": d["trip_id"], "source": "dtc", "rule": r,
                      "severity": "crit" if d["kind"] == "confirmed" else "warn", "title": f"Código {d['code']}",
                      "detail": d["kind"], "value": None, "kind": "dtc"})
    items.sort(key=lambda x: x["ts"], reverse=True)
    groups: list[dict] = []          # agrupamento guloso: mesma regra, a até HOT_RADIUS_M do centro do grupo
    for it in items:
        f = gstore.nearest_fix(con, it["ts"], tol_ms)
        if f is None:
            it.update(lat=None, lon=None, speed=None, alt=None, slope=None, fix_dt_s=None)
            continue
        it.update(lat=round(f["lat"], 6), lon=round(f["lon"], 6), speed=f["speed"], alt=f["alt"], slope=None,
                  fix_dt_s=round((it["ts"] - f["ts"]) / 1000, 1))
        for g in groups:
            if g["rule"] == it["rule"] and geomod.haversine_m(g["lat"], g["lon"], it["lat"], it["lon"]) <= HOT_RADIUS_M:
                g["items"].append(it)
                n = len(g["items"])
                g["lat"] += (it["lat"] - g["lat"]) / n
                g["lon"] += (it["lon"] - g["lon"]) / n
                break
        else:
            groups.append({"rule": it["rule"], "lat": it["lat"], "lon": it["lon"], "items": [it]})
    hot = []
    for gr in groups:
        rl, g = gr["rule"], gr["items"]
        if len(g) >= HOT_MIN:
            for it in g:
                it["repeat"] = len(g)
            hot.append({"rule": rl, "title": g[0]["title"], "n": len(g), "lat": round(sum(x["lat"] for x in g) / len(g), 6),
                        "lon": round(sum(x["lon"] for x in g) / len(g), 6), "ids": [x["id"] for x in g]})
    hot.sort(key=lambda h: -h["n"])
    rules: dict[str, dict] = {}
    for it in items:
        x = rules.setdefault(it["rule"] or "?", {"rule": it["rule"], "title": it["title"], "n": 0, "with_pos": 0})
        x["n"] += 1
        x["with_pos"] += it["lat"] is not None
    return {"items": items, "hotspots": hot, "rules": sorted(rules.values(), key=lambda x: -x["n"]),
            "n": len(items), "with_pos": sum(1 for it in items if it["lat"] is not None)}


# ---- F7: mapa offline (geo.db) no status, sem a área coberta nem o centro das ruas locais (o status é aberto na nuvem)
GEO_PRIVATE = ("bbox", "inner")
GEO_OLD_DAYS = 180
GEO_HINT = ("Mapa de radares não instalado. No PC: python3 scripts/geo-build.py --bbox S,W,N,E (a região por onde o carro "
            "anda) e depois scripts/deploy.sh.")


def geo_status(gcfg: dict) -> dict | None:
    """Tamanho, data e contagens do geo.db; 'velho' depois de 6 meses. None sem o arquivo."""
    gp = Path(gcfg.get("geo_db") or "/data/obd/geo/geo.db")
    try:
        stt = gp.stat()
    except OSError:
        return None
    geo = {"mb": round(stt.st_size / 1e6, 1), "mtime": int(stt.st_mtime * 1000)}
    try:
        gc = sqlite3.connect(f"file:{gp}?mode=ro&immutable=1", uri=True, timeout=2)
        try:
            meta = dict(gc.execute("SELECT key, value FROM meta").fetchall())
        finally:
            gc.close()
        geo.update({k: v for k, v in meta.items() if k not in GEO_PRIVATE})
    except sqlite3.Error:
        geo["erro"] = "arquivo ilegível"
    ref = geo.get("osm_ts") or geo.get("built_ts")
    try:
        geo["age_d"] = int((time.time() * 1000 - int(ref)) / 86400_000)
        geo["velho"] = geo["age_d"] > GEO_OLD_DAYS
    except (TypeError, ValueError):
        pass
    return geo


def build(ro, check_admin, cfg, run_dir) -> APIRouter:
    r = APIRouter()
    run_dir = Path(run_dir)
    gcfg = cfg.get("gps", {})
    tz = time.localtime().tm_gmtoff * 1000

    # ---- F1: estado e posição atual
    @r.get("/api/gps/status")
    def gps_status():
        """Estado da fonte e do receptor, origem da hora, posições gravadas e o geo.db. Sem posição nem área coberta:
        esta rota fica aberta na nuvem."""
        live = _read_json(run_dir / "live.json") or {}
        g = _read_json(run_dir / "gps.json") or {}
        st = dict(g.get("status") or {})
        st.pop("lat", None)
        st.pop("lon", None)
        state = (live.get("gps") or {}).get("state") or st.get("state") or ("desligado" if not gcfg.get("enabled", True) else "desconectado")
        out = {"state": state, "enabled": bool(gcfg.get("enabled", True)) and gcfg.get("source") != "off",
               "source_cfg": gcfg.get("source", "auto"), "rate_cfg": gcfg.get("rate_hz"),
               "src": st.get("src"), "device": st.get("device"), "driver": st.get("driver"), "mode": st.get("mode"),
               "sats": st.get("sats"), "hdop": st.get("hdop"), "hz": st.get("hz"), "age_s": st.get("age_s"),
               "ttff_s": st.get("ttff_s"), "reconnects": st.get("reconnects"),
               "clock": g.get("clock"), "clock_synced": timesrc.system_synced(), "clock_source": timesrc.source(),
               "fixes_today": None, "fixes_total": None, "last_fix_ts": None, "geo": None}
        try:
            con = ro()
            now = int(time.time() * 1000)
            day0 = (now + tz) // 86400_000 * 86400_000 - tz
            out["fixes_today"] = con.execute("SELECT COUNT(*) FROM gps_fix WHERE ts >= ?", (day0,)).fetchone()[0]
            out["fixes_total"] = con.execute("SELECT COUNT(*) FROM gps_fix").fetchone()[0]
            out["last_fix_ts"] = con.execute("SELECT MAX(ts) FROM gps_fix").fetchone()[0]
        except (HTTPException, sqlite3.Error):
            pass
        out["geo"] = geo_status(gcfg)
        if out["geo"] is None:
            out["geo_hint"] = GEO_HINT
        return out

    @r.get("/api/gps/live")
    def gps_live():
        """Conteúdo de /run/obd/gps.json (posição atual; privado)."""
        g = _read_json(run_dir / "gps.json")
        if not g:
            return {"state": "coletor_parado"}
        g.pop("status", None)
        g["age_s"] = round(time.time() - g["ts"] / 1000, 1) if g.get("ts") else None
        return g

    # ---- F2: diário de bordo, viagens, lugares, estacionamento e acontecimentos
    from ..gps import places as placesmod
    from ..gps import store as gstore
    from ..gps import trip as tripmod
    from ..gps.geocode import name_of

    db_path = (cfg.get("storage") or {}).get("db_path")

    def rw():
        if not db_path:
            raise HTTPException(503, "banco não configurado")
        return db.connect(db_path)

    def reload_places():
        """Avisa o coletor (lido no próximo ciclo; ele também relê os lugares a cada 30 s)."""
        try:
            with open(run_dir / "gps_cmd", "a") as fh:
                fh.write(json.dumps({"cmd": "reload_places"}) + "\n")
        except OSError:
            pass

    def _route(d: dict) -> str | None:
        return tripmod.route_label(d.get("start_name"), d.get("end_name"), d.get("start_place_id"), d.get("end_place_id"))

    @r.get("/api/gps/logbook")
    def gps_logbook(limit: int = 50, offset: int = 0):
        """Diário de bordo: cada viagem com origem, destino, km, duração, média, km/L, custo, alertas, notas e rota.
        Viagens ocultas (curtas ou pelo dono) não entram."""
        limit, offset = max(1, min(int(limit), 2000)), max(0, int(offset))
        con = ro()
        rows = con.execute("""SELECT t.id, t.start_ts, t.end_ts, t.duration_s, t.distance_km, t.gps_km, t.obd_km, t.avg_speed, t.max_speed,
                                     t.km_l, t.fuel_l, t.n_alerts, t.title, t.note, t.end_reason, s.clock_synced,
                                     g.start_name, g.end_name, g.start_place_id, g.end_place_id, g.start_src, g.coverage, g.n_stops,
                                     g.moving_s, g.cost, g.cost_km, g.route_id, g.drive_score, g.flags, g.n_fix,
                                     (g.track_json IS NOT NULL) AS has_track
                              FROM trip t LEFT JOIN trip_gps g ON g.trip_id = t.id LEFT JOIN session s ON s.id = t.session_id
                              WHERE t.finalized = 1 AND COALESCE(t.hidden, 0) = 0
                              ORDER BY t.start_ts DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        items = []
        for x in rows:
            d = dict(x)
            d["route"] = _route(d)
            d["has_track"] = bool(d["has_track"])
            d["avg_moving"] = round(d["distance_km"] / (d["moving_s"] / 3600), 1) if d.get("moving_s") and d.get("distance_km") else None
            items.append(d)
        total = con.execute("SELECT COUNT(*) FROM trip WHERE finalized = 1 AND COALESCE(hidden, 0) = 0").fetchone()[0]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @r.get("/api/gps/trips/{tid}")
    def gps_trip(tid: int):
        """trip_gps sem o trajeto, paradas, acontecimentos, alertas com a posição mais próxima (±10 s) e arrancadas."""
        con = ro()
        t = con.execute("SELECT id, start_ts, end_ts, distance_km, gps_km, obd_km FROM trip WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "viagem não encontrada")
        a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
        g = con.execute("SELECT * FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
        gps = None
        if g:
            gps = {k: g[k] for k in g.keys() if k not in ("track_json", "stops_json")}
            gps["route"] = _route(gps)
            gps["has_track"] = g["track_json"] is not None
            try:
                gps["stops"] = json.loads(g["stops_json"] or "[]")
            except ValueError:
                gps["stops"] = []
        events = [dict(e) for e in con.execute("SELECT * FROM geo_event WHERE trip_id=? OR ts BETWEEN ? AND ? ORDER BY ts", (tid, a, b))]
        alerts = []
        for al in con.execute("SELECT id, ts, severity, title, rule, count FROM alert WHERE trip_id=? ORDER BY ts", (tid,)):
            d = dict(al)
            f = gstore.nearest_fix(con, al["ts"], 10_000)
            d.update(lat=f["lat"] if f else None, lon=f["lon"] if f else None, speed=f["speed"] if f else None)
            alerts.append(d)
        runs = [dict(x) for x in con.execute("SELECT * FROM accel_run WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b))]
        return {"trip": dict(t), "gps": gps, "events": events, "alerts": alerts, "accel": runs}

    @r.get("/api/gps/trips/{tid}/track")
    def gps_trip_track(tid: int):
        con = ro()
        g = con.execute("SELECT track_json FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
        if not g or not g["track_json"]:
            raise HTTPException(404, "viagem sem trajeto")
        return json.loads(g["track_json"])

    @r.get("/api/gps/places")
    def gps_places():
        return {"places": placesmod.load_places(ro()), "kinds": list(placesmod.KINDS)}

    @r.post("/api/gps/places")
    def gps_place_create(body: dict, x_token: str | None = Header(None)):
        """Cria um lugar: {name, kind, lat, lon, radius_m?, actions?}. Sem 'actions', os padrões do tipo."""
        check_admin(x_token)
        try:
            f = placesmod.validate(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        con = rw()
        try:
            pid = placesmod.create_place(con, f["name"], f["kind"], f["lat"], f["lon"], f["radius_m"], f.get("actions"))
            con.commit()
        finally:
            con.close()
        reload_places()
        return {"ok": True, "id": pid}

    @r.post("/api/gps/places/{pid}")
    def gps_place_edit(pid: int, body: dict, x_token: str | None = Header(None)):
        """Edita {name?, kind?, lat?, lon?, radius_m?, actions?} ou apaga com {delete: true} (fica marcado como apagado:
        o histórico do diário continua com o nome antigo)."""
        check_admin(x_token)
        con = rw()
        try:
            if not con.execute("SELECT 1 FROM place WHERE id=? AND COALESCE(deleted,0)=0", (pid,)).fetchone():
                raise HTTPException(404, "lugar não encontrado")
            if body.get("delete"):
                con.execute("UPDATE place SET deleted=1 WHERE id=?", (pid,))
            else:
                try:
                    f = placesmod.validate(body, partial=True)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
                placesmod.update_place(con, pid, f)
            con.commit()
        finally:
            con.close()
        reload_places()
        return {"ok": True}

    @r.get("/api/gps/parked")
    def gps_parked():
        """Onde o carro ficou estacionado (a última posição parada gravada pelo coletor) e o link do Mapas do iPhone."""
        con = ro()
        p = gstore.model_get(con, "parked")
        if not p or p.get("lat") is None:
            return {"known": False}
        name, pid = name_of(con, None, p["lat"], p["lon"])
        return {"known": True, "lat": p["lat"], "lon": p["lon"], "ts": p.get("ts"), "valid": bool(p.get("valid")), "name": name,
                "place_id": pid, "maps_url": placesmod.maps_url(p["lat"], p["lon"])}

    @r.get("/api/gps/events")
    def gps_events(days: int = 30):
        """Acontecimentos com posição (entrou/saiu de lugar, mudou de lugar desligado, km sem OBD…)."""
        days = max(1, min(int(days), 400))
        con = ro()
        since = int(time.time() * 1000) - days * 86400_000
        return {"events": [dict(e) for e in con.execute(
            "SELECT e.*, p.name AS place_name FROM geo_event e LEFT JOIN place p ON p.id = e.place_id WHERE e.ts >= ? ORDER BY e.ts DESC LIMIT 2000",
            (since,))]}

    # ---- F3: alertas no mapa, mapa de calor e exportação GPX/KML (só no Pi; na nuvem a exportação responde 404)
    from ..gps import export as exportmod

    heat_ok = {"speed", "kml", *tripmod.FAST_COLS, *(n for n, _ in tripmod.SLOW_COLS)}

    @r.get("/api/gps/alerts")
    def gps_alerts(days: int = 90, rule: str | None = None):
        """Cada alerta (e código de falha) com a posição mais próxima (±10 s) e os pontos repetidos."""
        days = max(1, min(int(days), 400))
        return alerts_with_position(ro(), int(time.time() * 1000) - days * 86400_000, rule or None)

    @r.get("/api/gps/heat")
    def gps_heat(signal: str = "stft1", days: int = 30):
        """Média do sinal por célula de ~55 m sobre os trajetos guardados do período."""
        if signal not in heat_ok:
            raise HTTPException(400, f"sinal desconhecido: {signal}")
        days = max(1, min(int(days), 400))
        since = int(time.time() * 1000) - days * 86400_000
        tracks, n = [], 0
        for g in ro().execute("""SELECT g.track_json FROM trip_gps g JOIN trip t ON t.id = g.trip_id
                                 WHERE t.start_ts >= ? AND g.track_json IS NOT NULL AND COALESCE(t.hidden, 0) = 0""", (since,)):
            try:
                tracks.append(json.loads(g[0]))
                n += 1
            except ValueError:
                pass
        out = heat_cells(tracks, signal)
        out.update(days=days, trips=n)
        return out

    def _trip_for_export(con, tid: int) -> dict:
        t = con.execute("""SELECT t.id, t.start_ts, t.end_ts, t.title, g.start_name, g.end_name, g.start_place_id, g.end_place_id, g.stops_json
                           FROM trip t LEFT JOIN trip_gps g ON g.trip_id = t.id WHERE t.id=?""", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "viagem não encontrada")
        d = dict(t)
        d["route"] = _route(d)
        return d

    def _fixes(con, t: dict) -> list:
        return tripmod.good_fixes(con, t["start_ts"], t["end_ts"] or t["start_ts"], gcfg)

    def _download(gen, name: str, media: str) -> StreamingResponse:
        return StreamingResponse(gen, media_type=media, headers={"Content-Disposition": f'attachment; filename="{exportmod.safe_filename(name)}"',
                                                                 "Cache-Control": "no-store"})

    @r.get("/api/gps/trips/{tid}/export.gpx")
    def gps_trip_gpx(tid: int):
        con = ro()
        t = _trip_for_export(con, tid)
        if not _fixes(con, t):
            raise HTTPException(404, "viagem sem trajeto")
        return _download(exportmod.gpx([t], lambda x: _fixes(con, x)), f"viagem-{tid}.gpx", "application/gpx+xml")

    @r.get("/api/gps/trips/{tid}/export.kml")
    def gps_trip_kml(tid: int):
        con = ro()
        t = _trip_for_export(con, tid)
        fx = _fixes(con, t)
        if not fx:
            raise HTTPException(404, "viagem sem trajeto")
        try:
            stops = json.loads(t.get("stops_json") or "[]")
        except ValueError:
            stops = []
        al = []
        for a in con.execute("SELECT id, ts, severity, title, rule FROM alert WHERE trip_id=? ORDER BY ts", (tid,)):
            f = gstore.nearest_fix(con, a["ts"], ALERT_TOL_MS)
            al.append(dict(a, lat=f["lat"] if f else None, lon=f["lon"] if f else None))
        return _download(exportmod.kml(t, fx, al, stops), f"viagem-{tid}.kml", "application/vnd.google-earth.kml+xml")

    @r.get("/api/gps/export.gpx")
    def gps_export_gpx(frm: int | None = Query(None, alias="from"), to: int | None = None):
        """Várias viagens num arquivo (padrão: últimos 30 dias; no máximo 500 viagens)."""
        con = ro()
        to = to or int(time.time() * 1000)
        frm = frm or to - 30 * 86400_000
        trips = []
        for x in con.execute("""SELECT t.id, t.start_ts, t.end_ts, t.title, g.start_name, g.end_name, g.start_place_id, g.end_place_id
                                FROM trip t JOIN trip_gps g ON g.trip_id = t.id
                                WHERE t.start_ts BETWEEN ? AND ? AND COALESCE(g.n_fix, 0) > 0 AND COALESCE(t.hidden, 0) = 0
                                ORDER BY t.start_ts LIMIT 500""", (frm, to)):
            d = dict(x)
            d["route"] = _route(d)
            trips.append(d)
        return _download(exportmod.gpx(trips, lambda x: _fixes(con, x)), "viagens.gpx", "application/gpx+xml")

    # ---- F4: rotas e trechos
    from ..gps import routes as routesmod
    from ..gps import segments as segmod

    @r.get("/api/gps/routes")
    def gps_routes():
        """Rotas com as medianas de km, tempo, km/L, L/100 km e R$/km (só viagens típicas)."""
        return {"routes": routesmod.list_routes(ro())}

    @r.get("/api/gps/routes/{rid}")
    def gps_route(rid: int):
        d = routesmod.detail(ro(), rid)
        if not d:
            raise HTTPException(404, "rota não encontrada")
        return d

    @r.post("/api/gps/routes/{rid}")
    def gps_route_edit(rid: int, body: dict, x_token: str | None = Header(None)):
        """Renomeia: {name}."""
        check_admin(x_token)
        name = str((body or {}).get("name") or "").strip()[:80]
        if not name:
            raise HTTPException(400, "Dê um nome à rota.")
        con = rw()
        try:
            if not con.execute("SELECT 1 FROM route WHERE id=?", (rid,)).fetchone():
                raise HTTPException(404, "rota não encontrada")
            con.execute("UPDATE route SET name=? WHERE id=?", (name, rid))
            con.commit()
        finally:
            con.close()
        return {"ok": True}

    @r.get("/api/gps/segments")
    def gps_segments():
        return {"segments": segmod.list_segments(ro())}

    @r.post("/api/gps/segments")
    def gps_segment_create(body: dict, x_token: str | None = Header(None)):
        """Cria um trecho de referência: {name, trip_id, t_a, t_b} (instantes em ms, tocados no traçado) e busca as
        passagens no histórico."""
        check_admin(x_token)
        try:
            f = segmod.validate(body or {})
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        con = rw()
        try:
            if not con.execute("SELECT 1 FROM trip WHERE id=?", (f["trip_id"],)).fetchone():
                raise HTTPException(404, "viagem não encontrada")
            try:
                sid = segmod.create(con, f["name"], f["trip_id"], f["t_a"], f["t_b"])
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            n = con.execute("SELECT COUNT(*) FROM segment_pass WHERE segment_id=?", (sid,)).fetchone()[0]
            con.commit()
        finally:
            con.close()
        return {"ok": True, "id": sid, "passes": n}

    @r.get("/api/gps/segments/{sid}")
    def gps_segment(sid: int):
        """Passagens, regressão do consumo, anotações de manutenção e o antes/depois de cada serviço."""
        h = segmod.history(ro(), sid)
        if not h:
            raise HTTPException(404, "trecho não encontrado")
        return h

    @r.post("/api/gps/segments/{sid}")
    def gps_segment_edit(sid: int, body: dict, x_token: str | None = Header(None)):
        """{name?, active?} edita; {rescan: true} refaz a busca no histórico; {delete: true} apaga o trecho e as passagens."""
        check_admin(x_token)
        body = body or {}
        con = rw()
        try:
            if not segmod.get(con, sid):
                raise HTTPException(404, "trecho não encontrado")
            out = {"ok": True}
            if body.get("delete"):
                segmod.delete(con, sid)
            else:
                if "name" in body:
                    name = str(body.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "Dê um nome ao trecho.")
                    con.execute("UPDATE segment SET name=? WHERE id=?", (name, sid))
                if "active" in body:
                    con.execute("UPDATE segment SET active=? WHERE id=?", (1 if body["active"] else 0, sid))
                if body.get("rescan"):
                    out["passes"] = segmod.rescan(con, sid)
            con.commit()
        finally:
            con.close()
        return out

    # ---- F5: marchas, embreagem, velocímetro e nota de condução (rotas abertas na nuvem: só números, sem posição)
    from ..gps import gears as gearsmod
    from ..gps import score as scoremod
    from ..gps import speedo as speedomod

    def gps_cmd(cmd: dict):
        try:
            with open(run_dir / "gps_cmd", "a") as fh:
                fh.write(json.dumps(cmd) + "\n")
        except OSError:
            pass

    @r.get("/api/gps/gears")
    def gps_gears(days: int = 90):
        """Relações das marchas (fábrica × aprendidas pelo GPS) e as patinações de embreagem do período."""
        return gearsmod.report(ro(), int(time.time() * 1000), max(1, min(int(days), 400)))

    @r.post("/api/gps/gears/reset")
    def gps_gears_reset(x_token: str | None = Header(None)):
        """Esquece as relações aprendidas (depois de trocar pneu ou câmbio). O coletor recomeça do zero."""
        check_admin(x_token)
        con = rw()
        try:
            con.execute("DELETE FROM gps_model WHERE key='gears'")
            con.commit()
        finally:
            con.close()
        gps_cmd({"cmd": "gears_reset"})
        return {"ok": True}

    @r.get("/api/gps/speedo")
    def gps_speedo():
        """Aferição do velocímetro da central (fator k, tabela por faixa), pneus (k da 5ª mês a mês) e o ponteiro do painel."""
        con = ro()
        m = gstore.model_get(con, "speedo") or {}
        res = speedomod.fit(speedomod._load(m))
        for b in res["bins"]:
            b["err_pct"] = round((b["obd"] / b["gps"] - 1) * 100, 1) if b["gps"] else None
        hist = gstore.model_get(con, "speedo_hist") or {}
        needle = gstore.model_get(con, "speedo_needle") or []
        pt = None
        if isinstance(needle, list) and needle:
            x = needle[-1]
            pt = {**x, "vs_real_pct": round((x["needle"] / x["real"] - 1) * 100, 1) if x.get("real") else None}
        return {**res, "texto": speedomod.text(res), "min_bins": speedomod.MIN_BINS, "min_pairs": speedomod.MIN_PAIRS,
                "n_total": m.get("n_total") if isinstance(m, dict) else None, "updated_ts": m.get("updated_ts") if isinstance(m, dict) else None,
                "pneus": speedomod.tyres(hist), "hist": hist if isinstance(hist, dict) else {}, "ponteiro": pt}

    @r.post("/api/gps/speedo")
    def gps_speedo_needle(body: dict, x_token: str | None = Header(None)):
        """O dono anota o que o ponteiro do painel marcava e a velocidade verdadeira (GPS) no mesmo instante:
        {needle, real}. Guarda as 10 últimas."""
        check_admin(x_token)
        try:
            nd, rl = float((body or {}).get("needle")), float((body or {}).get("real"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Informe o que o ponteiro marcava e a velocidade do GPS (km/h).") from None
        if not (10 <= nd <= 250 and 10 <= rl <= 250):
            raise HTTPException(400, "Velocidades entre 10 e 250 km/h.")
        con = rw()
        try:
            lst = gstore.model_get(con, "speedo_needle") or []
            lst = (lst if isinstance(lst, list) else []) + [{"needle": nd, "real": rl, "ts": int(time.time() * 1000)}]
            gstore.model_set(con, "speedo_needle", lst[-10:], int(time.time() * 1000))
            con.commit()
        finally:
            con.close()
        return {"ok": True, "vs_real_pct": round((nd / rl - 1) * 100, 1)}

    @r.get("/api/gps/score")
    def gps_score(days: int = 90):
        """Nota de condução de cada viagem do período e a tendência (sem posição: os eventos com lugar ficam no detalhe da viagem)."""
        days = max(1, min(int(days), 400))
        return {**scoremod.trend(ro(), int(time.time() * 1000) - days * 86400_000), "days": days}

    # ---- F6: desempenho — 0–100 e retomadas, dinamômetro, inclinação e altitude (rotas abertas na nuvem: só números)
    from ..gps import dyno as dynomod
    from ..gps import perf as perfmod
    from ..gps import terrain as terrainmod

    vcfg = {**config.DEFAULTS["vehicle"], **(cfg.get("vehicle") or {})}

    @r.get("/api/gps/perf")
    def gps_perf():
        """Arrancadas e retomadas (GPS e as antigas pelo OBD) e os recordes por tipo, marcha e combustível."""
        return perfmod.report(ro())

    @r.get("/api/gps/dyno")
    def gps_dyno():
        """Puxadas com a curva de potência e torque, os parâmetros do carro (massa, CdA, Crr) e a calibração por descida
        em ponto morto, com o estado do assistente (lido do live.json)."""
        out = dynomod.report(ro(), vcfg)
        live = _read_json(run_dir / "live.json") or {}
        out["coast_live"] = (live.get("gps") or {}).get("coast")
        return out

    @r.post("/api/gps/dyno/config")
    def gps_dyno_config(body: dict, x_token: str | None = Header(None)):
        """{mass_kg?, occupants?, cda?, crr?} ajusta o carro; {reset_coast: true} esquece a calibração (volta ao padrão)."""
        check_admin(x_token)
        body = body or {}
        con = rw()
        try:
            cur = gstore.model_get(con, "vehicle") or {}
            cur = cur if isinstance(cur, dict) else {}
            lim = {"mass_kg": (800, 2500), "occupants": (1, 5), "cda": (0.3, 1.2), "crr": (0.005, 0.03)}
            for k, (lo, hi) in lim.items():
                if k in body:
                    if body[k] in (None, ""):
                        cur.pop(k, None)
                        continue
                    try:
                        x = float(body[k])
                    except (TypeError, ValueError):
                        raise HTTPException(400, f"valor inválido em {k}") from None
                    if not lo <= x <= hi:
                        raise HTTPException(400, f"{k} fora de {lo}–{hi}")
                    cur[k] = int(x) if k == "occupants" else x
            now = int(time.time() * 1000)
            gstore.model_set(con, "vehicle", cur, now)
            if body.get("reset_coast"):
                con.execute("DELETE FROM gps_model WHERE key='coast'")
            con.commit()
            veh = dynomod.vehicle(con, vcfg)
        finally:
            con.close()
        return {"ok": True, "vehicle": veh}

    @r.post("/api/gps/dyno/coast")
    def gps_dyno_coast(body: dict | None = None, x_token: str | None = Header(None)):
        """Arma o assistente de descida em ponto morto ({cancel: true} desarma). O coletor mede e grava a calibração."""
        check_admin(x_token)
        gps_cmd({"cmd": "coast_cancel" if (body or {}).get("cancel") else "coast_arm"})
        return {"ok": True}

    @r.get("/api/gps/terrain")
    def gps_terrain(days: int = 90):
        """Inclinação e altitude × carga, MAP, borboleta, ajuste curto, sonda, avanço, consumo e ar; leitura automática
        (entrada de ar) e altitude × potência. Sem posição."""
        days = max(1, min(int(days), 400))
        live = _read_json(run_dir / "live.json") or {}
        g = live.get("gps") or {}
        return {**terrainmod.report(ro(), int(time.time() * 1000) - days * 86400_000), "days": days,
                "agora": {"avail_pct": g.get("avail_pct"), "slope": g.get("slope")}}

    # ---- F7: vias — cidade × estrada (aberta na nuvem: só números) e radares do geo.db (posição: protegida, só no Pi)
    from ..gps import roads as roadsmod

    @r.get("/api/gps/roadmix")
    def gps_roadmix(days: int = 30):
        """Cidade × estrada: km, tempo, km/L, L/100 km, rotação, carga, ajuste curto, temperatura e marchas por classe,
        e a tendência mensal do km/L de cada uma. Sem posição nem nome de rua."""
        days = max(1, min(int(days), 400))
        out = roadsmod.report(ro(), int(time.time() * 1000) - days * 86400_000)
        g = geo_status(gcfg)
        out.update(days=days, geo={k: g.get(k) for k in ("osm_ts", "built_ts", "age_d", "velho")} if g else None)
        return out

    @r.get("/api/gps/cameras")
    def gps_cameras(bbox: str):
        """Radares do geo.db dentro da caixa S,W,N,E (até 1° × 1°, no máximo 2.000): camada do mapa. Auxílio: o mapa
        do OpenStreetMap pode estar desatualizado."""
        try:
            s_, w, n, e = (float(x) for x in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox = S,W,N,E") from None
        if not (s_ < n and w < e) or n - s_ > 1.0 or e - w > 1.0:
            raise HTTPException(400, "caixa inválida ou maior que 1° × 1°")
        gp = Path(gcfg.get("geo_db") or "/data/obd/geo/geo.db")
        if not gp.exists():
            raise HTTPException(404, "mapa de radares não instalado")
        try:
            gc = sqlite3.connect(f"file:{gp}?mode=ro&immutable=1", uri=True, timeout=2)
            try:
                rows = gc.execute("SELECT lat, lon, maxspeed, dir FROM camera WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? LIMIT 2000",
                                  (s_, n, w, e)).fetchall()
                meta = dict(gc.execute("SELECT key, value FROM meta").fetchall())
            finally:
                gc.close()
        except sqlite3.Error:
            raise HTTPException(503, "geo.db ilegível") from None
        return {"items": [{"lat": a, "lon": b, "limit": c, "dir": d} for a, b, c, d in rows], "n": len(rows),
                "osm_ts": meta.get("osm_ts") or meta.get("built_ts"), "aviso": "auxílio: o mapa pode estar desatualizado; a placa vale sempre"}

    return r
