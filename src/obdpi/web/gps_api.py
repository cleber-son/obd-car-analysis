"""Rotas /api/gps/* (docs/GPS-ARQUITETURA.md, seção 7.1). Posição só sai por aqui; na nuvem este prefixo é protegido
pela senha de localização (F3), exceto as rotas sem posição (status, gears, speedo, score, perf, dyno, terrain, roadmix).

Cada fase acrescenta um bloco próprio, com comentário de cabeçalho (# ---- F2: …).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import timesrc


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

    return r
