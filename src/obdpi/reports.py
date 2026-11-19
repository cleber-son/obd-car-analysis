"""Relatórios semanal e mensal (painel + push automático via ntfy)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

DAY = 86400_000


def period_bounds(kind: str, offset: int = 0, now: dt.datetime | None = None) -> tuple[int, int, str]:
    """kind 'week' (segunda a domingo) ou 'month'. offset 0 = atual, -1 = anterior..."""
    now = now or dt.datetime.now()
    if kind == "week":
        start = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(weeks=offset)
        end = start + dt.timedelta(weeks=1)
        label = f"Semana {start:%d/%m} a {(end - dt.timedelta(days=1)):%d/%m/%Y}"
    else:
        y, m = now.year, now.month + offset
        while m < 1:
            y, m = y - 1, m + 12
        while m > 12:
            y, m = y + 1, m - 12
        start = dt.datetime(y, m, 1)
        end = dt.datetime(y + (m == 12), m % 12 + 1, 1)
        meses = "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro".split()
        label = f"{meses[m - 1].capitalize()} de {y}"
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label


def _one(con, q, args=()):
    r = con.execute(q, args).fetchone()
    return dict(r) if r else {}


def day_start(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)


def _trip_label(t) -> str:
    s = dt.datetime.fromtimestamp(t["start_ts"] / 1000)
    e = dt.datetime.fromtimestamp((t["end_ts"] or t["start_ts"]) / 1000)
    base = f"Viagem de {s:%d/%m/%Y} · {s:%H:%M}–{e:%H:%M}"
    return f"{base} · {t['title']}" if t["title"] else base


def bounds(con, kind: str, offset: int = 0, day: str | None = None, trip: int | None = None):
    """(a, b, label, pa, pb, where_cur, args_cur, where_prev, args_prev) para cada tipo de relatório.
    week/month: calendário; since: do dia escolhido até o fim de hoje; trip: uma viagem só (comparada com a anterior)."""
    f = "finalized=1 AND start_ts>=? AND start_ts<?"
    if kind in ("week", "month"):
        a, b, label = period_bounds(kind, offset)
        pa, pb, _ = period_bounds(kind, offset - 1)
        return a, b, label, pa, pb, f, (a, b), f, (pa, pb)
    if kind == "since":
        a = day_start(day)
        today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        b = int((today + dt.timedelta(days=1)).timestamp() * 1000)
        if a >= b:
            raise ValueError("dia no futuro")
        n = round((b - a) / DAY)
        label = f"Desde {dt.datetime.fromtimestamp(a / 1000):%d/%m/%Y} ({n} dia{'s' if n > 1 else ''})"
        pa, pb = a - (b - a), a
        return a, b, label, pa, pb, f, (a, b), f, (pa, pb)
    if kind == "trip":
        t = con.execute("SELECT * FROM trip WHERE id=?", (trip,)).fetchone()
        if not t:
            raise ValueError("viagem não encontrada")
        a = t["start_ts"]
        b = (t["end_ts"] or int(time.time() * 1000)) + 1
        p = con.execute("SELECT * FROM trip WHERE start_ts<? AND COALESCE(hidden,0)=0 ORDER BY start_ts DESC LIMIT 1", (a,)).fetchone()
        pa, pb = (p["start_ts"], (p["end_ts"] or p["start_ts"]) + 1) if p else (0, 0)
        return a, b, _trip_label(t), pa, pb, "id=?", (trip,), "id=?", (p["id"] if p else -1,)
    raise ValueError("kind = week, month, since ou trip")


def build(con: sqlite3.Connection, kind: str, offset: int = 0, day: str | None = None, trip: int | None = None) -> dict:
    con.row_factory = sqlite3.Row
    a, b, label, pa, pb, wc, ac, wp, ap = bounds(con, kind, offset, day, trip)

    def totals(where, args):
        return _one(con, f"""SELECT COUNT(*) trips, COALESCE(SUM(distance_km),0) km, COALESCE(SUM(duration_s),0) dur, COALESCE(SUM(idle_s),0) idle,
                            COALESCE(SUM(fuel_l),0) fuel, MAX(max_speed) vmax, MAX(max_ect) ect_max, MIN(min_volt) volt_min,
                            AVG(tb_score) tb_score, AVG(eco_score) eco_score, AVG(avg_speed) avg_speed, MIN(crank_v) crank_min,
                            AVG(warmup_s) warmup_avg FROM trip WHERE {where}""", args)
    cur, prev = totals(wc, ac), totals(wp, ap)
    for t in (cur, prev):
        t["kml"] = round(t["km"] / t["fuel"], 1) if t["fuel"] and t["fuel"] > 0.3 else None
    fills = [dict(r) for r in con.execute("SELECT * FROM fuel_fill WHERE ts>=? AND ts<? ORDER BY ts", (a, b))]
    spent = sum(f.get("total") or ((f["liters"] or 0) * (f["price_l"] or 0)) for f in fills)
    price = con.execute("SELECT price_l FROM fuel_fill WHERE price_l IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    est_cost = round(cur["fuel"] * price[0], 2) if price and cur["fuel"] else None
    real = [f["real_kml"] for f in fills if f["real_kml"]]
    alerts = [dict(r) for r in con.execute("""SELECT severity, title, rule, SUM(COALESCE(count,1)) n, MAX(COALESCE(ts_end, ts)) last_ts FROM alert
                                               WHERE ts>=? AND ts<? GROUP BY title ORDER BY CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, n DESC""", (a, b))]
    sev = {r["severity"]: r["n"] for r in con.execute("SELECT severity, SUM(COALESCE(count,1)) n FROM alert WHERE ts>=? AND ts<? GROUP BY 1", (a, b))}
    dtcs = [dict(r) for r in con.execute("SELECT code, kind, event, ts FROM dtc_event WHERE ts>=? AND ts<? ORDER BY ts", (a, b))]
    air = [dict(r) for r in con.execute(f"SELECT air_verdict v, COUNT(*) n FROM trip WHERE {wc} AND air_verdict IS NOT NULL GROUP BY 1", ac)]
    tips: dict[str, int] = {}
    for (ej,) in con.execute(f"SELECT eco_json FROM trip WHERE {wc} AND eco_json IS NOT NULL", ac):
        for t in json.loads(ej).get("dicas", []):
            tips[t] = tips.get(t, 0) + 1
    daily = [dict(r) for r in con.execute("""SELECT ((start_ts+?)/86400000)*86400000-? day, COUNT(*) trips, SUM(distance_km) km, SUM(duration_s) dur,
                                               SUM(fuel_l) fuel, AVG(eco_score) eco FROM trip WHERE finalized=1 AND start_ts>=? AND start_ts<? GROUP BY 1 ORDER BY 1""",
                                            (TZ, TZ, a, b))]
    trips = [dict(r) for r in con.execute(f"""SELECT t.id, t.start_ts, t.end_ts, t.distance_km, t.duration_s, t.title, t.km_l, t.eco_score,
        (SELECT COALESCE(SUM(COALESCE(count,1)),0) FROM alert al WHERE al.ts>=t.start_ts AND al.ts<=COALESCE(t.end_ts, t.start_ts+86400000) AND al.severity!='info') n_alerts
        FROM trip t WHERE {wc.replace('finalized=1 AND ', '').replace('start_ts', 't.start_ts').replace('id=?', 't.id=?')} AND COALESCE(t.hidden,0)=0 ORDER BY t.start_ts""", ac)]
    accel = _one(con, "SELECT MIN(t100) best FROM accel_run WHERE ts>=? AND ts<? AND t100 IS NOT NULL", (a, b))

    def delta(k):
        c, p = cur.get(k), prev.get(k)
        return None if c is None or not p else round((c - p) / p * 100, 1)
    recs = recommendations(con, a, b, cur)
    return {"kind": kind, "offset": offset, "day": day, "trip": trip, "label": label, "from": a, "to": b, "cur": cur, "prev": prev, "recommendations": recs,
            "delta": {k: delta(k) for k in ("km", "dur", "fuel", "kml", "eco_score", "tb_score")},
            "fills": fills, "spent": round(spent, 2) if spent else None, "est_cost": est_cost,
            "real_kml": round(sum(real) / len(real), 1) if real else None,
            "alerts": alerts, "alerts_by_severity": sev, "dtc_events": dtcs, "air": air,
            "tips": sorted(tips, key=lambda t: -tips[t])[:4], "daily": daily, "trips": trips, "best_0_100": accel.get("best")}


TZ = time.localtime().tm_gmtoff * 1000


SEV_RANK = {"crit": 0, "warn": 1, "info": 2}
URG_RANK = {"parar": 0, "logo": 1, "acompanhar": 2, "info": 3}


def recommendations(con: sqlite3.Connection, a: int, b: int, cur: dict) -> list[dict]:
    """Um item por problema do período (alertas, DTCs, diagnósticos), com o que fazer (base em analytics/advice.py)."""
    from .analytics.advice import advice_for
    items: dict[str, dict] = {}

    def add(key, rule, title, severity, n, evidence, source=""):
        adv = advice_for(rule, source)
        if not adv:
            return
        k = key or rule
        it = items.get(k)
        if it:
            it["n"] += n
            it["evidence"] = it["evidence"] or evidence
            if SEV_RANK[severity] < SEV_RANK[it["severity"]]:
                it["severity"] = severity
            return
        if severity != "crit" and adv.get("urgencia") == "parar":     # aviso leve: resolver logo, "pare" só no nível crítico
            adv = dict(adv, urgencia="logo", urgencia_txt="Resolver nos próximos dias (pare se piorar)")
        items[k] = {"title": title, "severity": severity, "n": n, "evidence": evidence, "rule": rule,
                    "o_que": adv.get("o_que"), "causas": adv.get("causas", [])[:4], "fazer": adv.get("fazer", []),
                    "verificar": adv.get("verificar", [])[:3], "urgencia": adv.get("urgencia"),
                    "urgencia_txt": adv.get("urgencia_txt"), "rodar": adv.get("rodar")}
    for r in con.execute("""SELECT rule, source, title, severity, SUM(COALESCE(count,1)) n, MAX(detail) detail FROM alert
                            WHERE ts>=? AND ts<? AND severity IN ('crit','warn') GROUP BY rule""", (a, b)):
        head = r["rule"].split(":")[0]
        group = "z" if head == "z" else "missing" if head == "missing" else head if head.startswith("R") or head[:1] == "P" else r["rule"]
        add(group, r["rule"], r["title"], r["severity"], r["n"], r["detail"], r["source"])
    for e in con.execute("SELECT DISTINCT code, kind FROM dtc_event WHERE ts>=? AND ts<? AND event='apareceu'", (a, b)):
        add(e["code"], e["code"], f"Código {e['code']}", "crit" if e["kind"] == "confirmed" else "warn", 1, f"{'confirmado' if e['kind'] == 'confirmed' else 'pendente'}")
    for v, n in con.execute("""SELECT air_verdict, COUNT(*) FROM trip WHERE finalized=1 AND start_ts>=? AND start_ts<?
                               AND air_verdict IN ('entrada_de_ar','suspeito','maf_baixo') GROUP BY 1""", (a, b)):
        rule = "maf_low" if v == "maf_baixo" else "air_leak"
        add(rule, rule, "MAF lendo baixo" if v == "maf_baixo" else "Entrada falsa de ar", "warn", n, f"em {n} viagem(ns)")
    eco = cur.get("eco_score")
    if eco is not None and eco < 70:
        tips = {}
        for (ej,) in con.execute("SELECT eco_json FROM trip WHERE finalized=1 AND start_ts>=? AND start_ts<? AND eco_json IS NOT NULL", (a, b)):
            for t in json.loads(ej).get("dicas", []):
                tips[t] = tips.get(t, 0) + 1
        items["eco"] = {"title": f"Direção pouco econômica ({eco:.0f}/100)", "severity": "info", "n": len(tips), "evidence": "",
                        "rule": "eco", "o_que": "A forma de dirigir aumentou o consumo neste período.", "causas": [],
                        "fazer": sorted(tips, key=lambda t: -tips[t]) or ["Suba a marcha mais cedo e evite arrancadas fortes."],
                        "verificar": [], "urgencia": "info", "urgencia_txt": "Informativo", "rodar": None}
    return sorted(items.values(), key=lambda x: (SEV_RANK[x["severity"]], URG_RANK.get(x["urgencia"] or "info", 3), -x["n"]))


def to_text(r: dict) -> tuple[str, str]:
    c, d = r["cur"], r["delta"]
    f = lambda v, n=1: "—" if v is None else f"{v:,.{n}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    arrow = lambda x: "" if x is None else f" ({'+' if x > 0 else ''}{f(x, 0)}%)"
    lines = [f"🚗 {c['trips']} viagens · {f(c['km'], 0)} km{arrow(d['km'])} · {int(c['dur'] // 3600)}h{int(c['dur'] % 3600 // 60):02d} ligado",
             f"⛽ {f(c['fuel'], 1)} L estimados · {f(c['kml'])} km/L" + (f" · real {f(r['real_kml'])} km/L" if r["real_kml"] else "")
             + (f" · ≈ R$ {f(r['est_cost'], 2)}" if r["est_cost"] else ""),
             f"🌱 Direção econômica {f(c['eco_score'], 0)}/100 · 🦋 borboleta {f(c['tb_score'], 0)}/100"]
    sev = r["alerts_by_severity"]
    lines.append(f"🚨 {sev.get('crit', 0)} crítico(s), {sev.get('warn', 0)} aviso(s)" +
                 (": " + "; ".join(a["title"] for a in r["alerts"][:3]) if r["alerts"] else ""))
    if r["dtc_events"]:
        lines.append("⚠️ Códigos: " + ", ".join(sorted({e["code"] for e in r["dtc_events"]})))
    recs = [x for x in r.get("recommendations", []) if x["severity"] != "info"][:3]
    if recs:
        lines.append("🔧 O que fazer:")
        for x in recs:
            lines.append(f"• {x['title']} ({x['urgencia_txt'].lower()}): {x['fazer'][0] if x['fazer'] else x['o_que']}")
    elif r["tips"]:
        lines.append("💡 " + r["tips"][0])
    return f"Relatório {({'week': 'semanal', 'month': 'mensal'}).get(r['kind'], '')} — {r['label']}".replace("  ", " "), "\n".join(lines)


def queue_due(db_path: str) -> list[str]:
    """Coloca na fila de push os relatórios do período que acabou de fechar (uma vez cada)."""
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    sent = []
    try:
        for kind in ("week", "month"):
            a, b, label = period_bounds(kind, -1)
            key = f"report_sent_{kind}_{a}"
            if con.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                continue
            first = con.execute("SELECT MIN(start_ts) FROM trip").fetchone()[0]
            if first is None or first > b:            # sem dados nesse período
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, '0')", (key,))
                continue
            title, body = to_text(build(con, kind, -1))
            con.execute("INSERT INTO outbox(ts, title, body, priority, tags) VALUES (?,?,?,?,?)",
                        (int(time.time() * 1000), title, body, 3, "bar_chart,car"))
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, '1')", (key,))
            sent.append(label)
        con.commit()
    finally:
        con.close()
    return sent
