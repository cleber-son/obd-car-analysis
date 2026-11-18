"""obd-web: API REST + SSE + painel estático. Lê o SQLite em modo somente leitura e /run/obd/live.json."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, config, db
from ..obd import dtc as dtcmod
from ..obd.pids import SIGNALS

CFG = config.load()
DB = CFG["storage"]["db_path"]
LIVE = Path(CFG["runtime"]["live_sock"]).parent / "live.json"
STATIC = Path(__file__).parent / "static"
FAST_COLS = {"rpm", "speed", "map", "tps", "tps_rel", "tps_b", "tps_cmd", "pedal_d", "pedal_e"}
DAY = 86400_000
TZ = time.localtime().tm_gmtoff * 1000   # dias agrupados no fuso local (America/Sao_Paulo)

app = FastAPI(title="OBD Civic", version=__version__)


def ro():
    if not Path(DB).exists():
        raise HTTPException(503, "banco ainda não criado (coletor não iniciou)")
    return db.connect(DB, readonly=True)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def now_ms() -> int:
    return int(time.time() * 1000)


def read_live() -> dict:
    try:
        d = json.loads(LIVE.read_text())
        d["age_s"] = round(time.time() - d["ts"] / 1000, 1)
        return d
    except Exception:
        return {"state": "coletor_parado", "values": {}, "alerts": [], "dtc": {}, "age_s": None}


def check_admin(token: str | None):
    want = CFG["web"].get("admin_token")
    if want and token != want:
        raise HTTPException(401, "token inválido")


# ---------------------------------------------------------------- ao vivo
@app.get("/api/live")
def live():
    return read_live()


@app.get("/api/stream")
async def stream():
    async def gen():
        last = None
        while True:
            try:
                m = LIVE.stat().st_mtime
            except FileNotFoundError:
                m = None
            if m != last:
                last = m
                yield f"data: {json.dumps(read_live(), ensure_ascii=False)}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- sinais e séries
@app.get("/api/signals")
def signals():
    con = ro()
    since = now_ms() - DAY
    out = []
    for s in rows(con.execute("SELECT id, key, name, unit, decimals FROM signal ORDER BY key")):
        r = con.execute("SELECT MIN(value) mn, MAX(value) mx, AVG(value) av, COUNT(*) n FROM sample WHERE signal_id=? AND ts>?",
                        (s["id"], since)).fetchone()
        last = con.execute("SELECT ts, value FROM sample WHERE signal_id=? ORDER BY ts DESC LIMIT 1", (s["id"],)).fetchone()
        out.append({**s, "min24h": r["mn"], "max24h": r["mx"], "avg24h": r["av"], "n24h": r["n"],
                    "last": last["value"] if last else None, "last_ts": last["ts"] if last else None})
    return out


def _bucket(points: list[tuple], max_points: int) -> list[list]:
    if len(points) <= max_points:
        return [[t, v] for t, v in points]
    step = len(points) / max_points
    out = []
    i = 0.0
    while int(i) < len(points):
        chunk = points[int(i):int(i + step) or int(i) + 1]
        vals = [v for _, v in chunk if v is not None]
        if vals:
            out.append([chunk[len(chunk) // 2][0], round(sum(vals) / len(vals), 3), min(vals), max(vals)])
        i += step
    return out


@app.get("/api/series")
def series(signals: str, frm: int | None = Query(None, alias="from"), to: int | None = None, max_points: int = 600):
    con = ro()
    to = to or now_ms()
    frm = frm or to - 3600_000
    out = {}
    for key in signals.split(","):
        key = key.strip()
        if key in FAST_COLS and to - frm <= 12 * 3600_000:
            pts = con.execute(f"SELECT ts, {key} FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (frm, to)).fetchall()
        else:
            pts = con.execute("SELECT s.ts, s.value FROM sample s JOIN signal g ON g.id=s.signal_id WHERE g.key=? AND s.ts BETWEEN ? AND ? ORDER BY s.ts",
                              (key, frm, to)).fetchall()
        out[key] = _bucket([(p[0], p[1]) for p in pts], max_points)
    return {"from": frm, "to": to, "series": out}


# ---------------------------------------------------------------- viagens
@app.get("/api/trips")
def trips(limit: int = 50, offset: int = 0):
    con = ro()
    return rows(con.execute("SELECT * FROM trip ORDER BY start_ts DESC LIMIT ? OFFSET ?", (limit, offset)))


@app.get("/api/trips/{tid}")
def trip(tid: int):
    con = ro()
    t = con.execute("SELECT * FROM trip WHERE id=?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "viagem não encontrada")
    t = dict(t)
    end = t["end_ts"] or now_ms()
    t["alerts"] = rows(con.execute("SELECT * FROM alert WHERE trip_id=? ORDER BY ts", (tid,)))
    t["dtc_events"] = rows(con.execute("SELECT * FROM dtc_event WHERE ts BETWEEN ? AND ? ORDER BY ts", (t["start_ts"], end)))
    t["series"] = series("rpm,speed,tps,tps_cmd,pedal_d,ect,ltft1,stft1,batt", t["start_ts"], end, 800)["series"]
    return t


# ---------------------------------------------------------------- alertas e DTCs
@app.get("/api/alerts")
def alerts(days: int = 30, severity: str | None = None, limit: int = 200):
    con = ro()
    q = "SELECT * FROM alert WHERE ts > ?"
    args: list = [now_ms() - days * DAY]
    if severity:
        q += " AND severity=?"
        args.append(severity)
    return rows(con.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit)))


@app.get("/api/dtc")
def dtc():
    con = ro()
    st = rows(con.execute("SELECT * FROM dtc_state ORDER BY active DESC, last_seen_ts DESC"))
    for s in st:
        s["description"] = dtcmod.describe(s["code"])
        s["throttle"] = s["code"] in dtcmod.THROTTLE_WATCH
    ev = rows(con.execute("SELECT * FROM dtc_event ORDER BY ts DESC LIMIT 200"))
    for e in ev:
        e["description"] = dtcmod.describe(e["code"])
    lv = read_live()
    return {"state": st, "events": ev, "mil": lv.get("mil"), "active": lv.get("dtc", {})}


# ---------------------------------------------------------------- corpo de borboleta
@app.get("/api/throttle")
def throttle(days: int = 30):
    con = ro()
    since = now_ms() - days * DAY
    tr = rows(con.execute("SELECT id, start_ts, tb_score, duration_s FROM trip WHERE finalized=1 AND start_ts>? ORDER BY start_ts", (since,)))
    recent = [t["tb_score"] for t in tr if t["start_ts"] > now_ms() - 7 * DAY and t["tb_score"] is not None]
    rules = rows(con.execute("""SELECT substr(rule,1,instr(rule||':',':')-1) rule, severity, COUNT(*) n, MAX(ts) last_ts, MAX(title) title
                                FROM alert WHERE source IN ('throttle','dtc_throttle') AND ts>? GROUP BY 1, 2""", (since,)))
    idle = rows(con.execute(f"""SELECT ((ts+({TZ}))/86400000)*86400000-({TZ}) day, AVG(rpm) rpm, AVG(tps) tps, COUNT(*) n,
                               AVG(rpm*rpm)-AVG(rpm)*AVG(rpm) var
                               FROM fast_sample WHERE ts>? AND speed=0 AND rpm BETWEEN 400 AND 1500
                               AND (pedal_d IS NULL OR pedal_d < (SELECT MIN(pedal_d)+2 FROM fast_sample WHERE pedal_d IS NOT NULL))
                               GROUP BY 1 ORDER BY 1""", (since,)))
    for d in idle:
        d["rpm_std"] = round((d.pop("var") or 0) ** 0.5, 1)
    last_trip = con.execute("SELECT start_ts, end_ts FROM trip ORDER BY start_ts DESC LIMIT 1").fetchone()
    scatter = []
    if last_trip:
        scatter = [list(r) for r in con.execute(
            "SELECT pedal_d, tps, tps_cmd FROM fast_sample WHERE ts BETWEEN ? AND ? AND rpm>400 AND pedal_d IS NOT NULL LIMIT 3000",
            (last_trip["start_ts"], last_trip["end_ts"] or now_ms()))]
    ev = rows(con.execute("SELECT * FROM alert WHERE source IN ('throttle','dtc_throttle') ORDER BY ts DESC LIMIT 50"))
    return {"score": round(sum(recent) / len(recent)) if recent else None, "trips": tr, "rules": rules,
            "idle": idle, "scatter": scatter, "events": ev}


# ---------------------------------------------------------------- relatórios
@app.get("/api/report")
def report(days: int = 7):
    con = ro()
    since = now_ms() - days * DAY
    t = con.execute("""SELECT COUNT(*) n, SUM(distance_km) km, SUM(duration_s) dur, SUM(idle_s) idle, MAX(max_speed) vmax,
                       MAX(max_ect) ect, MIN(min_volt) vmin, AVG(avg_ltft) ltft, AVG(tb_score) score
                       FROM trip WHERE start_ts>? AND finalized=1""", (since,)).fetchone()
    sev = rows(con.execute("SELECT severity, COUNT(*) n FROM alert WHERE ts>? GROUP BY 1", (since,)))
    top = rows(con.execute("SELECT title, severity, COUNT(*) n, MAX(ts) last_ts FROM alert WHERE ts>? GROUP BY title ORDER BY n DESC LIMIT 10", (since,)))
    daily = rows(con.execute(f"""SELECT ((start_ts+({TZ}))/86400000)*86400000-({TZ}) day, COUNT(*) trips, SUM(distance_km) km, SUM(duration_s) dur, AVG(tb_score) score
                                FROM trip WHERE start_ts>? AND finalized=1 GROUP BY 1 ORDER BY 1""", (since,)))
    return {"days": days, "totals": dict(t), "alerts_by_severity": sev, "top_alerts": top, "daily": daily}


@app.get("/api/export.csv")
def export_csv(frm: int | None = Query(None, alias="from"), to: int | None = None):
    con = ro()
    to = to or now_ms()
    frm = frm or to - DAY

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp_ms", "data_hora", "sinal", "nome", "valor", "unidade"])
        for r in con.execute("""SELECT s.ts, g.key, g.name, s.value, g.unit FROM sample s JOIN signal g ON g.id=s.signal_id
                                WHERE s.ts BETWEEN ? AND ? ORDER BY s.ts""", (frm, to)):
            w.writerow([r[0], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0] / 1000)), r[1], r[2], r[3], r[4]])
            if buf.tell() > 65536:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=obd-civic.csv"})


# ---------------------------------------------------------------- diário de manutenção
@app.get("/api/annotations")
def annotations():
    return rows(ro().execute("SELECT * FROM annotation ORDER BY ts DESC"))


@app.post("/api/annotations")
def add_annotation(body: dict, x_token: str | None = Header(None)):
    check_admin(x_token)
    con = db.connect(DB)
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)",
                (int(body.get("ts") or now_ms()), body.get("kind", "manutencao"), str(body.get("text", ""))[:500]))
    con.commit()
    return {"ok": True}


# ---------------------------------------------------------------- sistema
def _sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


@app.get("/api/system")
def system():
    temp = None
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except Exception:
        pass
    thr = _sh("vcgencmd", "get_throttled").split("=")[-1]
    flags = int(thr, 16) if thr.startswith("0x") else 0
    du = shutil.disk_usage("/data" if Path("/data").exists() else "/")
    up = float(Path("/proc/uptime").read_text().split()[0]) if Path("/proc/uptime").exists() else 0
    ips = _sh("hostname", "-I").split()
    ssid = _sh("iwgetid", "-r") or _sh("nmcli", "-t", "-f", "active,ssid", "dev", "wifi").replace("yes:", "").split("\n")[0]
    mem = {}
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines()[:3]:
            k, v = ln.split(":")
            mem[k] = int(v.split()[0]) // 1024
    except Exception:
        pass
    events = []
    try:
        events = rows(ro().execute("SELECT * FROM system_event ORDER BY ts DESC LIMIT 50"))
    except HTTPException:
        pass
    return {
        "hostname": socket.gethostname(), "version": __version__, "cpu_temp": temp,
        "throttled": thr, "undervoltage_now": bool(flags & 0x1), "undervoltage_ever": bool(flags & 0x10000),
        "throttled_now": bool(flags & 0x4), "disk_free_gb": round(du.free / 1e9, 1), "disk_total_gb": round(du.total / 1e9, 1),
        "db_mb": round(os.path.getsize(DB) / 1e6, 1) if Path(DB).exists() else 0,
        "uptime_s": int(up), "ips": ips, "ssid": ssid, "mem_mb": mem, "load": os.getloadavg(),
        "clock_synced": Path("/run/systemd/timesync/synchronized").exists(), "events": events,
        "ntfy_topic": CFG["notify"].get("ntfy_topic", ""),
    }


# ---------------------------------------------------------------- estáticos
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_exc(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def main():
    import uvicorn
    uvicorn.run(app, host=CFG["web"]["bind"], port=int(CFG["web"]["port"]), log_level="warning", workers=1)


if __name__ == "__main__":
    main()
