"""Parada em posto e ranking de postos (docs/GPS-ARQUITETURA.md, seção 9.14).

O PID 2F (nível do tanque) não existe neste carro: a parada é reconhecida pelo comportamento. A viagem anterior
terminou a menos de 80 m de onde esta começou, o intervalo ficou entre 2 e 25 min (relógio sincronizado nas duas
sessões) e o ponto está a até 60 m de um posto (lugar salvo do tipo "posto" ou posto do geo.db). Vira uma linha de
`fuel_stop` ("suspeito"); o registro continua sendo o botão "Tanque cheio" (só o valor pago), que confirma a parada.

- `FuelStopEngine` (hub.LIVE_ENGINES): chama `detect` no início de cada viagem e, se a posição ainda não chegou
  (partida a frio do GPS), na primeira posição boa da viagem.
- `link_fill`: depois de um abastecimento registrado (app.fuel_fill), associa o posto: pela parada informada
  (stop_id), pelo lugar informado (place_id), pela parada suspeita a até 30 min ou pela posição daquele instante.
- `stations`: ranking — vezes, preço por combustível e o rendimento do combustível comprado em cada posto.

Aviso no celular só com `gps.notify_routine = true` (padrão: desligado; a parada aparece em Consumo do mesmo jeito).
"""
from __future__ import annotations

import logging
import statistics
import time

from . import places as placesmod
from .geo import cell, haversine_m, neighbors

log = logging.getLogger("gps")

SAME_SPOT_M = 80             # fim da viagem anterior × começo desta
STATION_M = 60               # até 60 m de um posto
GAP_MIN_S, GAP_MAX_S = 120, 25 * 60
START_WAIT_MS = 10 * 60_000  # posição da partida: até 10 min depois do início da viagem
EXPIRE_MS = 6 * 3600_000     # o aviso perde o sentido em 6 h
LINK_MS = 30 * 60_000        # abastecimento registrado a até 30 min de uma parada suspeita
POS_TOL_MS = 10 * 60_000     # …ou a posição gravada a até 10 min do registro
STOP_SHOW_MS = 3 * 86400_000  # o cartão em Consumo mostra as paradas suspeitas dos últimos 3 dias
STFT_KM = 20.0               # ajuste curto nos 20 km depois × 20 km antes de abastecer
MIN_TANKS = 2                # tanques cheios medidos por posto para opinar
MIX_GAS, MIX_ETH = 0.25, 0.75


# ---------------------------------------------------------------- postos perto de um ponto
def poi_near(geo, lat: float, lon: float, radius_m: float = STATION_M) -> dict | None:
    """Posto do geo.db (OpenStreetMap) mais perto, a até radius_m. None sem geo.db."""
    if geo is None:
        return None
    best = None
    try:
        cells = neighbors(cell(lat, lon))
        rows = geo.execute(f"SELECT id, lat, lon, name, brand FROM poi WHERE kind='posto' AND cell IN ({','.join('?' * len(cells))})",
                           cells).fetchall()
    except Exception:
        return None
    for r in rows:
        d = haversine_m(lat, lon, r[1], r[2])
        if d <= radius_m and (best is None or d < best["dist_m"]):
            best = {"osm": r[0], "lat": r[1], "lon": r[2], "name": poi_name(r[3], r[4]), "dist_m": round(d)}
    return best


def poi_name(name, brand) -> str | None:
    name, brand = (name or "").strip(), (brand or "").strip()
    if name and brand and brand.lower() not in name.lower():
        return f"{name} ({brand})"
    return name or (f"Posto {brand}" if brand else None)


def station_place(plist: list[dict], lat: float, lon: float, radius_m: float = STATION_M) -> dict | None:
    """Lugar salvo do tipo "posto" que contém o ponto (raio do lugar, no mínimo 60 m)."""
    best, bd = None, None
    for p in plist:
        if p.get("kind") != "posto":
            continue
        d = haversine_m(lat, lon, p["lat"], p["lon"])
        if d <= max(radius_m, p.get("radius_m") or 0) and (bd is None or d < bd):
            best, bd = p, d
    return best


def station_at(con, geo, lat: float, lon: float) -> dict | None:
    """{"place_id", "name", "lat", "lon", "src": "lugar" | "mapa"} do posto no ponto, ou None."""
    p = station_place(placesmod.load_places(con), lat, lon)
    if p:
        return {"place_id": p["id"], "name": p["name"], "lat": p["lat"], "lon": p["lon"], "src": "lugar"}
    q = poi_near(geo, lat, lon)
    if q:
        return {"place_id": None, "name": q["name"], "lat": q["lat"], "lon": q["lon"], "src": "mapa"}
    return None


def ensure_place(con, geo, lat: float, lon: float) -> int | None:
    """Id do lugar "posto" no ponto; cria o lugar (nome do geo.db ou "Posto N") quando ainda não existe."""
    st = station_at(con, geo, lat, lon)
    if st and st["place_id"]:
        return st["place_id"]
    name = st["name"] if st and st.get("name") else None
    la, lo = (st["lat"], st["lon"]) if st else (lat, lon)
    if not name:
        n = con.execute("SELECT COUNT(*) FROM place WHERE kind='posto'").fetchone()[0] + 1
        name = f"Posto {n}"
    return placesmod.create_place(con, name[:60], "posto", round(la, 6), round(lo, 6), STATION_M, [])


# ---------------------------------------------------------------- detecção
def _synced(con, session_id) -> bool:
    if not session_id:
        return False
    r = con.execute("SELECT clock_synced FROM session WHERE id=?", (session_id,)).fetchone()
    return bool(r and r[0])


def _good(r, cfg) -> bool:
    return (r["mode"] or 0) >= 2 and (r["hdop"] is None or r["hdop"] <= float(cfg.get("max_hdop", 5.0))) \
        and (r["sats"] is None or r["sats"] >= int(cfg.get("min_sats", 5)))


def _last_fix(con, a: int, b: int, cfg) -> tuple | None:
    for r in con.execute("SELECT ts, lat, lon, mode, hdop, sats FROM gps_fix WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 30", (a, b)):
        if _good(r, cfg):
            return r["lat"], r["lon"]
    return None


def _first_fix(con, a: int, b: int, cfg) -> tuple | None:
    for r in con.execute("SELECT ts, lat, lon, mode, hdop, sats FROM gps_fix WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 30", (a, b)):
        if _good(r, cfg):
            return r["lat"], r["lon"]
    return None


def judge(prev_end: tuple | None, start: tuple | None, gap_s: float | None, synced: bool, station: dict | None) -> str | None:
    """Função pura: None = é parada em posto; senão o motivo de não ser."""
    if prev_end is None or start is None:
        return "sem_posicao"
    if not synced:
        return "relogio"
    if gap_s is None or not GAP_MIN_S <= gap_s <= GAP_MAX_S:
        return "intervalo"
    if haversine_m(prev_end[0], prev_end[1], start[0], start[1]) > SAME_SPOT_M:
        return "longe"
    if station is None:
        return "sem_posto"
    return None


def detect(con, tid: int, ctx=None) -> dict | None:
    """Início da viagem tid (ou reanálise): grava a parada suspeita e, se configurado, o aviso. Devolve a parada (dict),
    {"pending": True} quando a posição da partida ainda não chegou, ou None."""
    cfg = getattr(ctx, "cfg", None) or {}
    geo = getattr(ctx, "geo", None)
    t = con.execute("SELECT id, start_ts, session_id FROM trip WHERE id=?", (tid,)).fetchone()
    if not t:
        return None
    old = con.execute("SELECT * FROM fuel_stop WHERE trip_after=?", (tid,)).fetchone()
    if old:
        return dict(old)
    prev = con.execute("SELECT id, end_ts, session_id FROM trip WHERE id != ? AND end_ts IS NOT NULL AND end_ts <= ?"
                       " ORDER BY end_ts DESC LIMIT 1", (tid, t["start_ts"])).fetchone()
    if not prev:
        return None
    gap_s = (t["start_ts"] - prev["end_ts"]) / 1000
    if gap_s > GAP_MAX_S or gap_s < GAP_MIN_S:          # nem precisa da posição
        return None
    f = getattr(ctx, "fix", None)
    start = (f.lat, f.lon) if f is not None and getattr(ctx, "trip_id", None) == tid else \
        _first_fix(con, t["start_ts"], t["start_ts"] + START_WAIT_MS, cfg)
    if start is None:
        return {"pending": True}
    prev_end = _last_fix(con, prev["end_ts"] - 15 * 60_000, prev["end_ts"] + 60_000, cfg)
    clock = getattr(ctx, "clock_synced", None)            # ao vivo: o relógio do coletor agora (a sessão grava a cada commit)
    now_ok = bool(clock) if clock is not None else _synced(con, t["session_id"])
    synced = now_ok and _synced(con, prev["session_id"])
    st = station_at(con, geo, *(prev_end or start)) if prev_end else None
    why = judge(prev_end, start, gap_s, synced, st)
    if why:
        return None
    lat, lon = (prev_end[0] + start[0]) / 2, (prev_end[1] + start[1]) / 2
    sid = con.execute("INSERT INTO fuel_stop(ts_start, ts_end, lat, lon, place_id, trip_before, trip_after, status, notified)"
                      " VALUES (?,?,?,?,?,?,?,'suspeito',0)",
                      (prev["end_ts"], t["start_ts"], round(lat, 6), round(lon, 6), st["place_id"], prev["id"], tid)).lastrowid
    mins = max(1, round(gap_s / 60))
    out = {"id": sid, "ts_start": prev["end_ts"], "ts_end": t["start_ts"], "place_id": st["place_id"], "name": st["name"],
           "min": mins, "status": "suspeito"}
    if cfg.get("notify_routine"):
        # no ntfy só nomes de lugares salvos (seção 7.4), e só se gps.summary_names
        name = st["name"] if st["place_id"] and cfg.get("summary_names", True) else "um posto"
        placesmod.push(con, f"Parou em {name} por {mins} min", "Abasteceu? Abra o painel → Consumo e registre só o valor pago.",
                       "info", ts=t["start_ts"], expire_ms=EXPIRE_MS)
        con.execute("UPDATE fuel_stop SET notified=1 WHERE id=?", (sid,))
    # Se um dia houver PID 2F (outro carro), a subida do nível confirma aqui: ramo guardado por "fuel_level" in values.
    if "fuel_level" in (getattr(ctx, "values", None) or {}):
        pass
    return out


class FuelStopEngine:
    """Ao vivo: a detecção da parada em posto no início da viagem (e na primeira posição boa, se ainda faltava)."""

    def __init__(self, hub):
        self.con = hub.con
        self.hub = hub
        self.pending: tuple | None = None            # (tid, ts do início)

    def _ctx(self, **kw):
        base = dict(cfg=getattr(self.hub, "cfg", {}) or {}, geo=None, fix=None, trip_id=None, values={},
                    clock_synced=getattr(getattr(self.hub, "clock", None), "synced", None))
        try:
            base["geo"] = self.hub._geo() if hasattr(self.hub, "_geo") else None
        except Exception:
            pass
        base.update(kw)
        from types import SimpleNamespace
        return SimpleNamespace(**base)

    def on_trip_start(self, tid, ts, merged) -> None:
        self.pending = None
        if merged:
            return
        r = detect(self.con, tid, self._ctx())
        if r and r.get("pending"):
            self.pending = (tid, ts)

    def shift(self, off: int) -> None:
        if self.pending:
            self.pending = (self.pending[0], self.pending[1] + off)

    def feed(self, ctx) -> list:
        if not self.pending:
            return []
        tid, ts = self.pending
        self.pending = None
        if ctx.trip_id != tid or ctx.fix.ts - ts > START_WAIT_MS:
            return []
        detect(self.con, tid, self._ctx(fix=ctx.fix, trip_id=tid, values=ctx.values or {}))
        return []

    def live(self) -> dict:
        return {}


# ---------------------------------------------------------------- ligação com o abastecimento
def link_fill(con, fill_id: int, ts: int, stop_id=None, place_id=None, geo=None) -> dict | None:
    """Associa o posto ao abastecimento recém-gravado. Sem parada nem posição por perto, não mexe em nada (None)."""
    stop = None
    if stop_id is not None:
        stop = con.execute("SELECT * FROM fuel_stop WHERE id=?", (int(stop_id),)).fetchone()
        if stop is None:
            raise ValueError("parada não encontrada")
    elif place_id is not None:
        p = con.execute("SELECT id, name FROM place WHERE id=? AND COALESCE(deleted,0)=0", (int(place_id),)).fetchone()
        if p is None:
            raise ValueError("lugar não encontrado")
        con.execute("UPDATE fuel_fill SET place_id=? WHERE id=?", (p["id"], fill_id))
        return {"place_id": p["id"], "name": p["name"], "stop_id": None}
    else:
        stop = con.execute("""SELECT * FROM fuel_stop WHERE status='suspeito' AND ts_start - ? <= ? AND ? <= ts_end + ?
                              ORDER BY ABS(ts_end - ?) LIMIT 1""", (LINK_MS, ts, ts, LINK_MS, ts)).fetchone()
    if stop is not None:
        pid = stop["place_id"] or ensure_place(con, geo, stop["lat"], stop["lon"])
        con.execute("UPDATE fuel_stop SET status='confirmado', fill_id=?, place_id=? WHERE id=?", (fill_id, pid, stop["id"]))
        con.execute("UPDATE fuel_fill SET place_id=? WHERE id=?", (pid, fill_id))
        name = con.execute("SELECT name FROM place WHERE id=?", (pid,)).fetchone()
        return {"place_id": pid, "name": name[0] if name else None, "stop_id": stop["id"]}
    # pela posição gravada mais perto do registro (o dono registrou ainda no posto)
    from .store import nearest_fix
    f = nearest_fix(con, ts, POS_TOL_MS)
    if f is None or (f["mode"] or 0) < 2 or (f["speed"] or 0) > 5:
        return None
    if station_at(con, geo, f["lat"], f["lon"]) is None:
        return None
    pid = ensure_place(con, geo, f["lat"], f["lon"])
    con.execute("UPDATE fuel_fill SET place_id=? WHERE id=?", (pid, fill_id))
    name = con.execute("SELECT name FROM place WHERE id=?", (pid,)).fetchone()
    return {"place_id": pid, "name": name[0] if name else None, "stop_id": None}


def set_status(con, stop_id: int, status: str) -> None:
    if status not in ("descartado", "suspeito"):
        raise ValueError("status deve ser descartado ou suspeito (para confirmar, registre o abastecimento)")
    r = con.execute("SELECT status FROM fuel_stop WHERE id=?", (stop_id,)).fetchone()
    if r is None:
        raise LookupError("parada não encontrada")
    if r["status"] == "confirmado":
        raise ValueError("parada já confirmada por um abastecimento")
    con.execute("UPDATE fuel_stop SET status=? WHERE id=?", (status, stop_id))


# ---------------------------------------------------------------- ranking
def fuel_kind(mix) -> str | None:
    if mix is None:
        return None
    return "gasolina" if mix <= MIX_GAS else "etanol" if mix >= MIX_ETH else "mistura"


def _stft_window(con, ts: int, after: bool, km_goal: float = STFT_KM) -> tuple[float | None, int]:
    """Ajuste curto médio nas viagens que somam os 20 km depois (ou antes) de ts."""
    q = ("SELECT start_ts, end_ts, distance_km FROM trip WHERE finalized=1 AND start_ts >= ? ORDER BY start_ts" if after else
         "SELECT start_ts, end_ts, distance_km FROM trip WHERE finalized=1 AND end_ts <= ? ORDER BY start_ts DESC")
    km, vals = 0.0, []
    for r in con.execute(q + " LIMIT 40", (ts,)):
        vals += [x[0] for x in con.execute("SELECT s.value FROM sample s JOIN signal g ON g.id = s.signal_id"
                                            " WHERE g.key='stft1' AND s.ts BETWEEN ? AND ?", (r["start_ts"], r["end_ts"] or r["start_ts"]))]
        km += r["distance_km"] or 0
        if km >= km_goal:
            break
    if km < km_goal * 0.5 or len(vals) < 3:
        return None, len(vals)
    return statistics.fmean(vals), len(vals)


def stations(con, with_stft: bool = True) -> list[dict]:
    """Por posto: vezes, preço médio e último por combustível, e o rendimento do combustível comprado ali (o km/L real
    medido no abastecimento seguinte pertence ao tanque anterior, mesma regra de extras.kml_by_fuel), comparado com a
    média do dono para o mesmo combustível; e a variação do ajuste curto nos 20 km depois × 20 km antes."""
    fills = [dict(r) for r in con.execute("SELECT * FROM fuel_fill ORDER BY ts")]
    # rendimento de cada tanque: o real_kml do abastecimento seguinte (os dois tanques cheios)
    tank: list[tuple[dict, float, str | None]] = []
    for f, nxt in zip(fills, fills[1:]):
        if f.get("full") and nxt.get("full") and nxt.get("real_kml"):
            tank.append((f, nxt["real_kml"], fuel_kind(f.get("mix_after"))))
    avg: dict[str, float] = {}
    for k in ("gasolina", "etanol", "mistura"):
        v = [kml for _, kml, fk in tank if fk == k]
        if v:
            avg[k] = statistics.fmean(v)
    names = {r["id"]: dict(r) for r in con.execute("SELECT id, name, kind, lat, lon, deleted FROM place")}
    ids = sorted({f["place_id"] for f in fills if f.get("place_id")} | {i for i, p in names.items() if p["kind"] == "posto" and not p["deleted"]})
    out = []
    for pid in ids:
        p = names.get(pid)
        if not p:
            continue
        mine = [f for f in fills if f.get("place_id") == pid]
        by_fuel: dict[str, dict] = {}
        for f in mine:
            d = by_fuel.setdefault(f["fuel"] or "gasolina", {"n": 0, "prices": []})
            d["n"] += 1
            if f.get("price_l"):
                d["prices"].append(f["price_l"])
        fuels = {k: {"n": v["n"], "avg_price": round(statistics.fmean(v["prices"]), 3) if v["prices"] else None,
                     "last_price": v["prices"][-1] if v["prices"] else None} for k, v in by_fuel.items()}
        tk = [(f, kml, fk) for f, kml, fk in tank if f.get("place_id") == pid]
        diffs = [(kml / avg[fk] - 1) * 100 for _, kml, fk in tk if fk in avg and avg[fk] > 0]
        kml = {"n": len(tk), "avg": round(statistics.fmean(k for _, k, _ in tk), 2) if tk else None,
               "vs_media_pct": round(statistics.fmean(diffs), 1) if len(diffs) >= MIN_TANKS else None}
        stft = None
        if with_stft and mine:
            ds = []
            for f in mine[-6:]:
                a, _ = _stft_window(con, f["ts"], True)
                b, _ = _stft_window(con, f["ts"], False)
                if a is not None and b is not None:
                    ds.append(a - b)
            stft = {"n": len(ds), "delta": round(statistics.fmean(ds), 1) + 0.0 if ds else None}
        n_stops = con.execute("SELECT COUNT(*) FROM fuel_stop WHERE place_id=? AND status!='descartado'", (pid,)).fetchone()[0]
        out.append({"place_id": pid, "name": p["name"], "lat": p["lat"], "lon": p["lon"], "deleted": bool(p["deleted"]),
                    "n": len(mine), "paradas": n_stops, "last_ts": mine[-1]["ts"] if mine else None,
                    "last_price": next((f["price_l"] for f in reversed(mine) if f.get("price_l")), None),
                    "fuels": fuels, "kml": kml, "stft": stft, "opiniao": opinion(kml, stft)})
    out.sort(key=lambda s: (-(s["kml"]["vs_media_pct"] if s["kml"]["vs_media_pct"] is not None else -1e9), -s["n"]))
    return out


def opinion(kml: dict, stft: dict | None) -> str:
    if kml.get("vs_media_pct") is None:
        return f"Poucos dados: são precisos {MIN_TANKS} tanques cheios medidos aqui (há {kml.get('n') or 0})."
    v = kml["vs_media_pct"]
    br = f"{abs(v):.1f}".replace(".", ",")
    txt = (f"O combustível daqui rende {br} % a mais que a sua média" if v >= 2 else
           f"O combustível daqui rende {br} % a menos que a sua média" if v <= -2 else "Rendimento igual à sua média")
    if stft and stft.get("delta") is not None and abs(stft["delta"]) >= 4:
        txt += f"; o ajuste curto {'sobe' if stft['delta'] > 0 else 'desce'} {abs(stft['delta']):.0f} pontos depois de abastecer aqui".replace(".", ",")
    return txt + "."


def best_near(con, lat: float, lon: float, radius_m: float = 5000) -> dict | None:
    """O posto mais bem colocado do ranking a até radius_m (para o aviso de autonomia baixa)."""
    best = None
    for s in stations(con, with_stft=False):
        if s["deleted"] or s["lat"] is None:
            continue
        if haversine_m(lat, lon, s["lat"], s["lon"]) <= radius_m:
            return s                                  # a lista já vem ordenada do mais bem colocado para o pior
    return best


def recent_stops(con, geo=None, now: int | None = None) -> list[dict]:
    """Paradas suspeitas dos últimos 3 dias (cartão "Abastecimento detectado")."""
    now = now or int(time.time() * 1000)
    out = []
    for r in con.execute("SELECT s.*, p.name AS place_name FROM fuel_stop s LEFT JOIN place p ON p.id = s.place_id"
                         " WHERE s.status='suspeito' AND s.ts_end >= ? ORDER BY s.ts_end DESC LIMIT 10", (now - STOP_SHOW_MS,)):
        d = dict(r)
        if not d.get("place_name"):
            q = poi_near(geo, d["lat"], d["lon"]) if d.get("lat") is not None else None
            d["place_name"] = (q or {}).get("name") or "um posto"
        d["min"] = max(1, round(((d["ts_end"] or 0) - (d["ts_start"] or 0)) / 60000))
        out.append(d)
    return out

