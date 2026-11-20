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
        last, idle = None, 0
        while True:
            try:
                m = LIVE.stat().st_mtime_ns
            except FileNotFoundError:
                m = None
            if m != last:
                last, idle = m, 0
                yield f"data: {json.dumps(read_live(), ensure_ascii=False)}\n\n"
            else:
                idle += 1
                if idle % 20 == 0:                    # mantém a conexão viva quando o coletor está parado
                    yield ": ping\n\n"
            await asyncio.sleep(0.25)                 # o coletor grava ~2x/s; o painel mostra assim que muda
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
def series(signals: str, frm: int | None = Query(None, alias="from"), to: int | None = None, max_points: int = 600,
           span: int | None = None):
    con = ro()
    to = to or now_ms()
    frm = frm or to - (span or 3600_000)
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


@app.post("/api/trips/{tid}")
def trip_edit(tid: int, body: dict, x_token: str | None = Header(None)):
    """Gerenciar viagem: {title?, note?, hidden?}"""
    check_admin(x_token)
    con = db.connect(DB)
    if not con.execute("SELECT 1 FROM trip WHERE id=?", (tid,)).fetchone():
        raise HTTPException(404, "viagem não encontrada")
    for k in ("title", "note"):
        if k in body:
            con.execute(f"UPDATE trip SET {k}=? WHERE id=?", ((str(body[k])[:300] or None), tid))
    if "hidden" in body:
        con.execute("UPDATE trip SET hidden=? WHERE id=?", (int(bool(body["hidden"])), tid))
    con.commit()
    return {"ok": True}


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
    t["series"] = series("rpm,speed,tps,tps_cmd,pedal_d,ect,ltft1,stft1,batt,maf,fuel_lh,o2c1_lambda,timing,load,gear", t["start_ts"], end, 800)["series"]
    price = con.execute("SELECT price_l FROM fuel_fill WHERE price_l IS NOT NULL AND ts <= ? ORDER BY ts DESC LIMIT 1", (end,)).fetchone()
    t["cost"] = round(t["fuel_l"] * price[0], 2) if price and t.get("fuel_l") else None
    for k in ("air_json", "eco_json", "start_snapshot", "end_snapshot"):
        t[k] = json.loads(t[k]) if t.get(k) else None
    from .. import extras
    t["habits"] = extras.habits(con, t["start_ts"], end + 1)
    return t


# ---------------------------------------------------------------- alertas e DTCs
@app.post("/api/alerts/clear")
def alerts_clear(body: dict | None = None, x_token: str | None = Header(None)):
    """Botão "Limpar alertas": tira os alertas do painel e dos relatórios. Eles vão para alert_archive (ficam
    guardados para análise futura). body opcional: {rules: ["R1", ...]} limpa só essas regras."""
    check_admin(x_token)
    con = db.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS alert_archive AS SELECT *, 0 AS cleared_ts FROM alert WHERE 0")
    cols = ", ".join(r[1] for r in con.execute("PRAGMA table_info(alert)"))
    rules = [str(r) for r in (body or {}).get("rules") or []]
    where, args = ("WHERE rule IN (%s)" % ",".join("?" * len(rules)), rules) if rules else ("", [])
    n = con.execute(f"INSERT INTO alert_archive({cols}, cleared_ts) SELECT {cols}, ? FROM alert {where}", (now_ms(), *args)).rowcount
    con.execute(f"DELETE FROM alert {where}", args)
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (now_ms(), "alertas", f"{n} alerta(s) limpos pelo painel"))
    con.commit()
    try:
        (LIVE.parent / "alerts_cmd").write_text("clear")      # o coletor também esquece os alertas recentes da tela
    except OSError:
        pass
    return {"ok": True, "cleared": n}


@app.get("/api/alerts")
def alerts(days: int = 30, severity: str | None = None, limit: int = 200):
    con = ro()
    q = "SELECT * FROM alert WHERE COALESCE(ts_end, ts) > ?"
    args: list = [now_ms() - days * DAY]
    if severity:
        q += " AND severity=?"
        args.append(severity)
    from ..analytics.advice import advice_for
    out = rows(con.execute(q + " ORDER BY COALESCE(ts_end, ts) DESC LIMIT ?", (*args, limit)))
    for a in out:
        a["has_advice"] = advice_for(a["rule"], a["source"]) is not None
    return out


@app.get("/api/advice")
def advice(rule: str, source: str = ""):
    from ..analytics.advice import advice_for
    d = advice_for(rule, source)
    if not d:
        raise HTTPException(404, "sem recomendação para este alerta")
    return d


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


# ---------------------------------------------------------------- diagnósticos
@app.get("/api/diagnostics")
def diagnostics(days: int = 60):
    con = ro()
    since = now_ms() - days * DAY
    tr = rows(con.execute("""SELECT id, start_ts, duration_s, distance_km, air_verdict, air_json, warmup_s, start_ect, crank_v, rest_v,
                             eco_score, eco_json FROM trip WHERE finalized=1 AND start_ts>? ORDER BY start_ts""", (since,)))
    for t in tr:
        for k in ("air_json", "eco_json"):
            t[k] = json.loads(t[k]) if t.get(k) else None
    lv = read_live()
    from ..analytics import insight
    try:
        ins = insight.build(con, now_ms() - 30 * DAY, now_ms() + 1, lv)
    except Exception as e:
        ins = {"erro": str(e)}
    return {"trips": tr, "readiness": lv.get("readiness"), "insight": ins}


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
    from ..reports import recommendations
    return {"days": days, "totals": dict(t), "alerts_by_severity": sev, "top_alerts": top, "daily": daily,
            "recommendations": recommendations(con, since, now_ms() + 1, dict(t))}


@app.get("/api/report/period")
def report_period(kind: str = "week", offset: int = 0, day: str | None = None, trip: int | None = None):
    """kind week|month (+offset), since (+day=AAAA-MM-DD: do dia até hoje) ou trip (+trip=id: uma viagem)."""
    from .. import reports
    if (kind == "since" and not day) or (kind == "trip" and trip is None):
        raise HTTPException(400, "since precisa de day=AAAA-MM-DD; trip precisa de trip=id")
    try:
        r = reports.build(ro(), kind, offset, day, trip)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["text"] = reports.to_text(r)
    return r


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


# ---------------------------------------------------------------- resumo: motor, km, histórico
def _day_start(ts: int) -> int:
    return ((ts + TZ) // DAY) * DAY - TZ


@app.get("/api/summary")
def summary(days: int = 14):
    con = ro()
    now = now_ms()
    today = _day_start(now)
    lv = read_live()

    def agg(since):
        r = con.execute("""SELECT COUNT(*) n, COALESCE(SUM(distance_km),0) km, COALESCE(SUM(duration_s),0) dur,
                           COALESCE(SUM(fuel_l),0) fuel FROM trip WHERE start_ts >= ? AND finalized=1""", (since,)).fetchone()
        return dict(r)
    cur = con.execute("SELECT * FROM trip WHERE finalized=0 ORDER BY start_ts DESC LIMIT 1").fetchone()
    cur_km = 0.0
    if cur:
        f = con.execute("SELECT ts, speed FROM fast_sample WHERE ts >= ? ORDER BY ts", (cur["start_ts"],)).fetchall()
        for a, b in zip(f, f[1:]):
            cur_km += (a["speed"] or 0) * min((b["ts"] - a["ts"]) / 1000, 5) / 3600
    last = con.execute("SELECT end_ts FROM trip WHERE finalized=1 ORDER BY end_ts DESC LIMIT 1").fetchone()
    t_today, t_all = agg(today), agg(0)
    hist = rows(con.execute("""SELECT id, start_ts, end_ts, duration_s, distance_km, finalized FROM trip
                               WHERE start_ts > ? ORDER BY start_ts""", (now - days * DAY,)))
    per_day = rows(con.execute(f"""SELECT ((start_ts+({TZ}))/86400000)*86400000-({TZ}) day, COUNT(*) n,
                                    SUM(COALESCE(duration_s,(end_ts-start_ts)/1000)) dur, SUM(distance_km) km
                                    FROM trip WHERE start_ts > ? GROUP BY 1 ORDER BY 1""", (now - days * DAY,)))
    return {
        "engine_on": bool(lv.get("engine_on")), "trip_start": lv.get("trip_start"),
        "engine_since_s": int((now - lv["trip_start"]) / 1000) if lv.get("engine_on") and lv.get("trip_start") else None,
        "off_since_ts": None if lv.get("engine_on") else (last["end_ts"] if last else None),
        "current_trip_km": round(cur_km, 2),
        "today": {**t_today, "km": t_today["km"] + cur_km, "dur": t_today["dur"] + (int((now - cur["start_ts"]) / 1000) if cur else 0)},
        "total": {**t_all, "km": t_all["km"] + cur_km},
        "history": hist, "per_day": per_day,
    }


# ---------------------------------------------------------------- combustível
def _cost_km(fills: list) -> float | None:
    """R$ por km medido: o valor pago num tanque cheio repõe o que foi gasto nos km desde o tanque cheio anterior."""
    ok = [f for f in fills if f.get("total") and f["full"] and (f["km_since_prev"] or 0) > 20]
    return round(sum(f["total"] for f in ok[-5:]) / sum(f["km_since_prev"] for f in ok[-5:]), 3) if ok else None


@app.get("/api/fuel")
def fuel_info():
    from ..analytics import fuel as fm
    con = ro()
    fills = rows(con.execute("SELECT * FROM fuel_fill ORDER BY ts"))
    last = fills[-1] if fills else None
    since = last["ts"] if last else 0
    t = con.execute("SELECT COALESCE(SUM(distance_km),0) km, COALESCE(SUM(fuel_l),0) l FROM trip WHERE start_ts >= ? AND finalized=1", (since,)).fetchone()
    trips = rows(con.execute("SELECT id, start_ts, distance_km, fuel_l, km_l FROM trip WHERE finalized=1 AND km_l IS NOT NULL ORDER BY start_ts DESC LIMIT 60"))
    real = [f for f in fills if f["real_kml"]]
    prices = [f["price_l"] for f in fills if f["price_l"]]
    kml_est = t["km"] / t["l"] if t["l"] > 0.1 else None
    return {
        "fills": fills[::-1], "mix": last["mix_after"] if last else None, "calib": fm.calib_from(fills),
        "since_fill": {"ts": since or None, "km": round(t["km"], 1), "liters_est": round(t["l"], 2), "kml_est": round(kml_est, 1) if kml_est else None,
                       "cost_est": round(t["l"] * last["price_l"], 2) if last and last["price_l"] else None},
        "avg_real_kml": round(sum(f["real_kml"] for f in real[-5:]) / len(real[-5:]), 1) if real else None,
        "cost_per_km": _cost_km(fills) or (round(prices[-1] / (real[-1]["real_kml"] if real else kml_est), 3) if prices and (real or kml_est) else None),
        "spent_30d": round(sum(f.get("total") or ((f["liters"] or 0) * (f["price_l"] or 0)) for f in fills if f["ts"] > now_ms() - 30 * DAY), 2),
        "trips": trips, "tank_l": fm.TANK_L, "kml_by_fuel": __import__("obdpi.extras", fromlist=["x"]).kml_by_fuel(fills),
    }


def _num(v, lo, hi, nome):
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        raise HTTPException(400, f"{nome} inválido")
    if not lo <= x <= hi:
        raise HTTPException(400, f"{nome} fora do esperado")
    return x


@app.post("/api/fuel/fill/{fid}")
def fuel_fill_edit(fid: int, body: dict, x_token: str | None = Header(None)):
    """Corrigir ou apagar um abastecimento: {delete: true} ou {fuel?, total?, price_l?, liters?}."""
    check_admin(x_token)
    con = db.connect(DB)
    f = con.execute("SELECT * FROM fuel_fill WHERE id=?", (fid,)).fetchone()
    if not f:
        raise HTTPException(404, "abastecimento não encontrado")
    if body.get("delete"):
        con.execute("DELETE FROM fuel_fill WHERE id=?", (fid,))
        con.commit()
        return {"ok": True, "deleted": True}
    fuel = body.get("fuel") or f["fuel"]
    if fuel not in ("gasolina", "etanol"):
        raise HTTPException(400, "combustível deve ser gasolina ou etanol")
    total = _num(body.get("total"), 1, 5000, "valor") if "total" in body else f["total"]
    price = _num(body.get("price_l"), 0.5, 30, "preço por litro") if "price_l" in body else f["price_l"]
    liters = _num(body.get("liters"), 0.5, 80, "litros") if body.get("liters") not in (None, "") else (round(total / price, 2) if total and price else f["liters"])
    kml = round(f["km_since_prev"] / liters, 2) if liters and f["full"] and (f["km_since_prev"] or 0) > 20 else None
    con.execute("UPDATE fuel_fill SET fuel=?, total=?, price_l=?, liters=?, real_kml=? WHERE id=?", (fuel, total, price, liters, kml, fid))
    con.commit()
    return {"ok": True, "real_kml": kml}


@app.post("/api/fuel/fill")
def fuel_fill(body: dict, x_token: str | None = Header(None)):
    """Botão 'Tanque cheio'. Basta {total: valor pago}. Opcionais: fuel (gasolina|etanol), price_l, liters, full, ts.
    O que não vier é herdado do abastecimento anterior (combustível e preço por litro); litros = valor ÷ preço."""
    from ..analytics import fuel as fm
    check_admin(x_token)
    ts = int(body.get("ts") or now_ms())          # abastecimento registrado no painel online chega depois, com a hora do clique
    con0 = ro()
    last = con0.execute("SELECT * FROM fuel_fill WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    fuel = body.get("fuel") or (last["fuel"] if last else "gasolina")
    if fuel not in ("gasolina", "etanol"):
        raise HTTPException(400, "combustível deve ser gasolina ou etanol")
    total = _num(body.get("total"), 1, 5000, "valor")
    price = _num(body.get("price_l"), 0.5, 30, "preço por litro")
    liters = _num(body.get("liters"), 0.5, 80, "litros")
    if price is None and total and liters:
        price = round(total / liters, 3)
    if price is None and last and last["price_l"] and last["fuel"] == fuel:
        price = last["price_l"]                   # mesmo combustível: assume o preço do último até ser informado outro
    if liters is None and total and price:
        liters = round(total / price, 2)
    if total is None and liters and price:
        total = round(liters * price, 2)
    full = bool(body.get("full", True))
    exact = body.get("liters") not in (None, "") or body.get("price_l") not in (None, "")   # litros medidos, não deduzidos do preço antigo
    con = db.connect(DB)
    prev = con.execute("SELECT * FROM fuel_fill WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    km = est = kml = calib = None
    if prev:
        r = con.execute("SELECT COALESCE(SUM(distance_km),0) km, COALESCE(SUM(fuel_l),0) l FROM trip WHERE start_ts >= ? AND start_ts < ? AND finalized=1",
                        (prev["ts"], ts)).fetchone()
        km, est = round(r["km"], 1), round(r["l"], 2)
        if full and prev["full"] and liters and km > 20:   # método tanque a tanque
            kml = round(km / liters, 2)
            if est and est > 1 and exact:
                old = fm.calib_from([dict(x) for x in con.execute("SELECT calib FROM fuel_fill")])
                calib = round(liters / (est / old), 3)       # estimativa bruta (sem calibração) × fator = real
    mix = fm.mix_after_fill(prev["mix_after"] if prev else None, fuel, liters)
    con.execute("""INSERT INTO fuel_fill(ts, fuel, liters, price_l, full, mix_after, km_since_prev, est_l_since_prev, real_kml, calib, note, total)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (ts, fuel, liters, price, int(full), round(mix, 3), km, est, kml, calib, body.get("note"), total))
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)",
                (ts, "abastecimento", f"Tanque cheio: {fuel}" + (f", R$ {total:.2f}" if total else "") + (f", {liters:g} L" if liters else "") + (f" a R$ {price:.2f}/L" if price else "")))
    con.commit()
    return {"ok": True, "real_kml": kml, "km_since_prev": km, "mix_after": mix, "liters": liters, "price_l": price,
            "cost_km": round(total / km, 2) if total and km and km > 20 and prev and prev["full"] else None}


# ---------------------------------------------------------------- análise por IA
@app.post("/api/ai/analyze")
def ai_analyze(body: dict | None = None, x_token: str | None = Header(None)):
    check_admin(x_token)
    from .. import ai
    trip = (body or {}).get("trip")
    return ai.start(DB, read_live(), trip=int(trip) if trip is not None else None)


@app.get("/api/ai/settings")
def ai_settings():
    from .. import ai
    return ai.public_settings()


@app.post("/api/ai/settings")
def ai_settings_save(body: dict, x_token: str | None = Header(None)):
    """Salva provedor/chaves. Campo vazio mantém a chave atual; "-" apaga. As chaves nunca são devolvidas."""
    check_admin(x_token)
    from .. import ai
    if body.get("provider") not in (None, "", "anthropic", "openai"):
        raise HTTPException(400, "provedor deve ser anthropic ou openai")
    for k in ("anthropic_key", "openai_key"):
        v = (body.get(k) or "").strip()
        if v and v != "-" and (len(v) < 20 or len(v) > 300 or any(c.isspace() for c in v)):
            raise HTTPException(400, f"chave {k} parece inválida")
    return ai.save_settings(body)


@app.get("/api/ai/analysis")
def ai_analysis(trip: int | None = None):
    """Sem trip: laudo geral (ou o que está rodando). Com trip: o laudo daquela viagem, se existir."""
    from .. import ai
    j = ai.job()
    if trip is not None:
        if j.get("status") == "running" and j.get("trip") == trip:
            return {"status": "running", "elapsed_s": int(time.time() - j["started"])}
        last = ai.last(DB, trip)
        return {"status": "done", **last} if last else {"status": "idle"}
    if j.get("status") in ("idle", None) or j.get("trip") is not None:
        last = ai.last(DB)
        return {"status": "done", **last} if last else {"status": "idle"}
    if j.get("status") == "running":
        j["elapsed_s"] = int(time.time() - j["started"])
    return j


@app.post("/api/ai/auto")
def ai_auto(x_token: str | None = Header(None)):
    """Chamado pelo obd-sync quando há internet (no fim da viagem ou na próxima vez que o Pi ligar): faz o laudo da
    viagem mais recente que terminou e ainda não tem laudo.
    Só viagens de verdade (≥ 5 min ou ≥ 1 km) e só se houver chave de IA configurada."""
    from .. import ai
    check_admin(x_token)
    if not ai.configured() or ai.job().get("status") == "running":
        return {"started": False}
    lv = read_live()
    con = ro()
    done = ai.trips_done(DB)
    # terminou há mais de 3 min (uma parada curta continua a mesma viagem) e há menos de 3 dias
    for t in con.execute("""SELECT id FROM trip WHERE finalized=1 AND COALESCE(hidden,0)=0 AND end_ts > ? AND end_ts < ?
                            AND (duration_s >= 300 OR distance_km >= 1) ORDER BY start_ts DESC LIMIT 3""", (now_ms() - 3 * DAY, now_ms() - 180_000)):
        if t["id"] not in done:
            ai.start(DB, lv, trip=t["id"], auto=True)
            return {"started": True, "trip": t["id"]}
    return {"started": False}


# ---------------------------------------------------------------- teste 0–100
@app.post("/api/accel/{cmd}")
def accel_cmd(cmd: str, x_token: str | None = Header(None)):
    check_admin(x_token)
    if cmd not in ("arm", "cancel"):
        raise HTTPException(400, "use arm ou cancel")
    (LIVE.parent / "accel_cmd").write_text(cmd)
    return {"ok": True}


@app.get("/api/accel")
def accel_runs():
    return rows(ro().execute("SELECT * FROM accel_run ORDER BY ts DESC LIMIT 50"))


# ---------------------------------------------------------------- impressão digital, antes/depois, manutenção
@app.get("/api/fingerprint")
def fingerprint(days: int = 7):
    from .. import extras
    return extras.fp_status(ro(), days)


@app.post("/api/fingerprint")
def fingerprint_save(body: dict | None = None, x_token: str | None = Header(None)):
    """Grava a referência do motor saudável com as leituras dos últimos dias. {label?, days?}"""
    from .. import extras
    check_admin(x_token)
    try:
        return extras.fp_save(db.connect(DB), str((body or {}).get("label") or "Carro saudável"), int((body or {}).get("days") or 7))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/impact")
def impact():
    """Antes e depois de cada serviço anotado no diário de manutenção."""
    from .. import extras
    return extras.impact(ro())


@app.get("/api/maintenance")
def maintenance():
    from .. import extras
    return extras.maintenance(ro())


@app.post("/api/maintenance/odometer")
def maintenance_odo(body: dict, x_token: str | None = Header(None)):
    from .. import extras
    check_admin(x_token)
    km = _num(body.get("km"), 1, 999999, "hodômetro")
    if km is None:
        raise HTTPException(400, "informe os km do hodômetro")
    extras.maint_set_odometer(db.connect(DB), km, int(body.get("ts") or now_ms()))
    return {"ok": True}


@app.post("/api/maintenance/done")
def maintenance_done(body: dict, x_token: str | None = Header(None)):
    """Marca um item como feito: {key, odo?, ts?, note?, km_int?, month_int?}"""
    from .. import extras
    check_admin(x_token)
    try:
        extras.maint_done(db.connect(DB), str(body.get("key")), _num(body.get("odo"), 0, 999999, "hodômetro"), int(body["ts"]) if body.get("ts") else None,
                          str(body.get("note") or "") or None, _num(body.get("km_int"), 500, 300000, "intervalo em km"), _num(body.get("month_int"), 1, 240, "intervalo em meses"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/ai/chat")
def ai_chat():
    from .. import ai
    return ai.chat_list(DB)


@app.post("/api/ai/ask")
def ai_ask(body: dict, x_token: str | None = Header(None)):
    from .. import ai
    check_admin(x_token)
    q = str(body.get("question") or "").strip()
    if len(q) < 3:
        raise HTTPException(400, "escreva a pergunta")
    return ai.ask(DB, read_live(), q[:600])


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


@app.get("/api/reset")
def reset_status():
    from .. import reset
    return {"pending": reset.pending(CFG), "last_ts": reset.last(CFG)}


@app.post("/api/reset")
def reset_all(body: dict | None = None, x_token: str | None = Header(None)):
    """Botão "Resetar tudo": histórico, aprendizado e anotações recomeçam do zero (ver obdpi/reset.py).
    Exige {confirm: "RESETAR"}. O banco anterior fica guardado em backup/antes-do-reset-*."""
    import sys
    from .. import reset
    check_admin(x_token)
    if (body or {}).get("confirm") != "RESETAR":
        raise HTTPException(400, 'para confirmar, envie {"confirm": "RESETAR"}')
    ai = sys.modules.get("obdpi.ai")
    if ai and ai.job().get("status") != "running":
        ai._job.clear()
        ai._job.update(status="idle")             # o último laudo em memória também some
    return {"ok": True, **reset.request(CFG)}


@app.get("/api/repairs")
def repairs():
    try:
        return rows(ro().execute("SELECT * FROM repair_log ORDER BY ts DESC LIMIT 100"))
    except Exception:
        return []


# ---------------------------------------------------------------- estáticos
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/oficina")
def oficina():
    return FileResponse(STATIC / "oficina.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/dirigir")
def drive():
    return FileResponse(STATIC / "drive.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_exc(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def main():
    import uvicorn
    uvicorn.run(app, host=CFG["web"]["bind"], port=int(CFG["web"]["port"]), log_level="warning", workers=1,
                timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
