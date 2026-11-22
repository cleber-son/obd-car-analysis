"""Rotas /api/gps/* (docs/GPS-ARQUITETURA.md, seção 7.1). Posição só sai por aqui; na nuvem este prefixo é protegido
pela senha de localização (F3), exceto as rotas sem posição (status, gears, speedo, score, perf, dyno, terrain, roadmix).

Cada fase acrescenta um bloco próprio, com comentário de cabeçalho (# ---- F2: …).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from .. import db, timesrc


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


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
        gp = Path(gcfg.get("geo_db") or "/data/obd/geo/geo.db")
        if gp.exists():
            geo = {"mb": round(gp.stat().st_size / 1e6, 1), "mtime": int(gp.stat().st_mtime * 1000)}
            try:
                gc = sqlite3.connect(f"file:{gp}?mode=ro&immutable=1", uri=True, timeout=2)
                meta = dict(gc.execute("SELECT key, value FROM meta").fetchall())
                gc.close()
                geo.update({k: v for k, v in meta.items() if k != "bbox"})       # sem a área coberta (aberta na nuvem)
            except sqlite3.Error:
                pass
            out["geo"] = geo
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

    return r
