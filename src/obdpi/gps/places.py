"""Lugares, cerca virtual, estacionamento e "mudou de lugar desligado" (docs/GPS-ARQUITETURA.md, seções 9.7 e 9.16).

- `ParkEngine`: guarda onde o carro parou (gps_model['parked']); no boot seguinte compara com a primeira posição precisa.
- `GeofenceEngine`: entrar/sair de lugares salvos, com histerese (3 posições dentro; 5 além do raio + 30 m).
- Funções de lugar usadas pelo fechamento da viagem, pela API e pelo reset.

As ações dos lugares (avisar, falar, sincronizar, pós-serviço, nota) são executadas a partir da F8; aqui elas só
são guardadas (com os padrões por tipo).
"""
from __future__ import annotations

import json
import logging
import time

from ..analytics import Alert
from . import precise, store
from .geo import haversine_m

log = logging.getLogger("gps")

KINDS = ("casa", "trabalho", "oficina", "posto", "outro", "auto")
DEFAULT_RADIUS_M = 80.0
MIN_RADIUS_M, MAX_RADIUS_M = 20.0, 2000.0
# padrões ao criar (seção 9.16): Casa sincroniza ao entrar; Oficina lembra do serviço ao sair depois de 20 min
DEFAULT_ACTIONS = {"casa": [{"on": "entrou", "do": "sincronizar"}],
                   "oficina": [{"on": "partiu_apos", "do": "pos_servico", "arg": 20}]}
ACTION_ON = ("entrou", "saiu", "partiu_apos")
ACTION_DO = ("avisar", "falar", "sincronizar", "pos_servico", "nota")

PARK_KMH = 3.0               # abaixo disso conta como parado
PARK_VALID_N = 3             # posições paradas seguidas para a posição de estacionamento valer
PARK_SAVE_MS = 5_000         # gravada no máximo a cada 5 s (o coletor faz commit a cada 5 s)
MOVED_PRECISE_N = 5          # posições precisas seguidas antes de comparar com o estacionamento
MOVED_RUN_MPS = 14           # "deu a partida e saiu antes de o Pi subir": 14 m/s ≈ 50 km/h por segundo de motor ligado
MOVED_MARGIN_M = 150
GEOFENCE_IN_N, GEOFENCE_OUT_N, GEOFENCE_OUT_M = 3, 5, 30.0
RELOAD_MS = 30_000


# ---------------------------------------------------------------- distância pelo OBD (a conta antiga do coletor)
def obd_km(rows) -> float:
    """Mesma conta de Collector.finalize_trip: velocidade da leitura anterior × intervalo (máx. 5 s)."""
    dist, prev = 0.0, None
    for r in rows:
        if prev is not None:
            dt = min((r[0] - prev[0]) / 1000, 5)
            dist += (prev[1] or 0) * dt / 3600
        prev = r
    return dist


def obd_km_between(con, a: int, b: int) -> float:
    return obd_km(con.execute("SELECT ts, speed FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall())


# ---------------------------------------------------------------- lugares
def maps_url(lat: float, lon: float) -> str:
    """Abre o Mapas do iPhone no ponto."""
    return f"https://maps.apple.com/?ll={lat:.6f},{lon:.6f}&q=Civic"


def load_places(con, with_deleted: bool = False) -> list[dict]:
    q = "SELECT * FROM place" + ("" if with_deleted else " WHERE COALESCE(deleted,0)=0") + " ORDER BY id"
    out = []
    for r in con.execute(q).fetchall():
        d = dict(r)
        try:
            d["actions"] = json.loads(d.pop("actions_json") or "[]")
        except ValueError:
            d["actions"] = []
        out.append(d)
    return out


def place_at(places: list[dict], lat: float, lon: float, margin_m: float = 0.0) -> dict | None:
    """Lugar cujo raio (+ margem) contém o ponto; havendo mais de um, o de centro mais próximo."""
    best, bd = None, None
    for p in places:
        d = haversine_m(lat, lon, p["lat"], p["lon"])
        if d <= (p["radius_m"] or DEFAULT_RADIUS_M) + margin_m and (bd is None or d < bd):
            best, bd = p, d
    return best


def _clean_actions(actions) -> list[dict]:
    out = []
    for a in actions or []:
        if not isinstance(a, dict) or a.get("on") not in ACTION_ON or a.get("do") not in ACTION_DO:
            continue
        x = {"on": a["on"], "do": a["do"]}
        if a.get("arg") not in (None, ""):
            x["arg"] = a["arg"] if isinstance(a["arg"], (int, float)) else str(a["arg"])[:200]
        out.append(x)
    return out[:10]


def validate(body: dict, partial: bool = False) -> dict:
    """Campos de um lugar vindos da API. ValueError com a mensagem para o dono."""
    out: dict = {}
    if "name" in body or not partial:
        name = str(body.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("informe o nome do lugar")
        out["name"] = name
    if "kind" in body or not partial:
        kind = str(body.get("kind") or "outro").lower()
        if kind not in KINDS:
            raise ValueError(f"tipo inválido (use: {', '.join(KINDS)})")
        out["kind"] = kind
    for k, lo, hi in (("lat", -90, 90), ("lon", -180, 180)):
        if k in body or not partial:
            try:
                v = float(body.get(k))
            except (TypeError, ValueError):
                raise ValueError(f"{k} inválida") from None
            if not lo <= v <= hi:
                raise ValueError(f"{k} fora da faixa")
            out[k] = v
    if "radius_m" in body or not partial:
        try:
            r = float(body.get("radius_m") or DEFAULT_RADIUS_M)
        except (TypeError, ValueError):
            raise ValueError("raio inválido") from None
        out["radius_m"] = max(MIN_RADIUS_M, min(MAX_RADIUS_M, r))
    if "actions" in body:
        out["actions"] = _clean_actions(body.get("actions"))
    return out


def create_place(con, name: str, kind: str, lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M,
                 actions: list | None = None, ts: int | None = None) -> int:
    if actions is None:
        actions = DEFAULT_ACTIONS.get(kind, [])
    cur = con.execute("INSERT INTO place(name, kind, lat, lon, radius_m, actions_json, created_ts, visits) VALUES (?,?,?,?,?,?,?,0)",
                      (name, kind, lat, lon, radius_m, json.dumps(_clean_actions(actions), ensure_ascii=False),
                       ts or int(time.time() * 1000)))
    return cur.lastrowid


def update_place(con, pid: int, fields: dict) -> None:
    sets, vals = [], []
    for k in ("name", "kind", "lat", "lon", "radius_m"):
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    if "actions" in fields:
        sets.append("actions_json=?")
        vals.append(json.dumps(fields["actions"], ensure_ascii=False))
    if sets:
        con.execute(f"UPDATE place SET {', '.join(sets)} WHERE id=?", (*vals, pid))


# ---------------------------------------------------------------- estacionamento pelo banco (fechamento e reanálise)
def parked_before(con, ts: int, max_age_ms: int = 30 * 86400_000, max_hdop: float = 5.0, min_sats: int = 5) -> dict | None:
    """Onde o carro estava parado antes de 'ts': média das 3 últimas posições boas antes dele, válida se as 3 estão
    paradas (< 3 km/h). É a mesma regra do ParkEngine, mas lida do banco: serve também para viagens antigas."""
    rows = con.execute("SELECT ts, lat, lon, speed, hdop, sats, mode FROM gps_fix WHERE ts < ? AND ts >= ? AND mode >= 2"
                       " ORDER BY ts DESC LIMIT 12", (ts, ts - max_age_ms)).fetchall()
    good = [r for r in rows if (r["hdop"] is None or r["hdop"] <= max_hdop) and (r["sats"] is None or r["sats"] >= min_sats)][:PARK_VALID_N]
    if not good:
        return None
    valid = len(good) == PARK_VALID_N and all((r["speed"] or 0) < PARK_KMH for r in good)
    return {"lat": sum(r["lat"] for r in good) / len(good), "lon": sum(r["lon"] for r in good) / len(good),
            "ts": good[0]["ts"], "valid": valid}


# ---------------------------------------------------------------- motores ao vivo
class ParkEngine:
    """Posição de estacionamento ao vivo e o aviso de "mudou de lugar desligado" (uma vez por boot)."""

    def __init__(self, hub):
        self.con = hub.con
        self.moved_m = float(hub.cfg.get("moved_m", 300))
        try:
            self.prev = store.model_get(self.con, "parked")       # deixado pelo boot anterior
        except Exception:
            self.prev = None
        self.cur: dict | None = None
        self._sum = [0.0, 0.0, 0]
        self.slow_n = 0
        self.last_save: int | None = None
        self.dirty = False
        self.engine_on = False
        self.trip_id: int | None = None
        self.trip_ts: int | None = None
        self.merged = False
        self.checked = False
        self.precise_n = 0
        self._name: tuple | None = None

    def on_trip_start(self, tid: int, ts: int, merged: bool) -> None:
        if self.trip_id is None:                   # a primeira viagem do boot decide a comparação
            self.merged = bool(merged)
        self.trip_id, self.trip_ts = tid, ts

    def shift(self, off: int) -> None:
        if self.cur and self.cur.get("ts"):
            self.cur["ts"] += off
        if self.trip_ts:
            self.trip_ts += off
        if self.last_save is not None:
            self.last_save += off

    def feed(self, ctx) -> list:
        f = ctx.fix
        self.engine_on = bool(ctx.engine_on)
        alerts = self._moved_check(ctx) if not self.checked else []
        valid_before = bool(self.cur and self.cur.get("valid"))
        if (f.speed or 0) < PARK_KMH:
            if self.slow_n == 0:
                self._sum = [0.0, 0.0, 0]
            self.slow_n += 1
            self._sum[0] += f.lat
            self._sum[1] += f.lon
            self._sum[2] += 1
            n = self._sum[2]
            self.cur = {"lat": round(self._sum[0] / n, 7), "lon": round(self._sum[1] / n, 7), "ts": f.ts,
                        "trip_id": ctx.trip_id, "valid": self.slow_n >= PARK_VALID_N}
            self.dirty = True
        else:
            self.slow_n = 0
            if self.cur and self.cur.get("valid"):
                self.cur = {**self.cur, "valid": False}
                self.dirty = True
        changed = valid_before != bool(self.cur and self.cur.get("valid"))
        if self.dirty and (changed or self.last_save is None or f.ts - self.last_save >= PARK_SAVE_MS or f.ts < self.last_save):
            store.model_set(self.con, "parked", self.cur, f.ts)
            self.last_save, self.dirty = f.ts, False
        return alerts

    def _moved_check(self, ctx) -> list:
        """No boot seguinte: na primeira posição precisa (depois de 5 seguidas) compara com onde o carro foi deixado."""
        f = ctx.fix
        if self.trip_id is None:                   # só com viagem aberta (sem OBD não dá para descontar o que ele andou)
            return []
        if self.merged:                            # viagem retomada (o Pi caiu com o motor ligado): não avalia
            self.checked = True
            return []
        self.precise_n = self.precise_n + 1 if precise(f) else 0
        if self.precise_n < MOVED_PRECISE_N:
            return []
        self.checked = True
        p = self.prev
        if not p or not p.get("valid") or p.get("lat") is None:
            return []                              # o Pi caiu com o carro andando: não há onde comparar
        d = haversine_m(f.lat, f.lon, p["lat"], p["lon"])
        km = obd_km_between(self.con, self.trip_ts, f.ts) if self.trip_ts else 0.0
        rt = (ctx.values or {}).get("run_time")
        explained = km * 1000 + (float(rt) * MOVED_RUN_MPS if rt else 0) + MOVED_MARGIN_M
        if d <= max(self.moved_m, explained):
            return []
        txt = f"{d / 1000:.1f}".replace(".", ",") + " km" if d >= 1000 else f"{d:.0f} m"
        store.add_event(self.con, f.ts, "moveu_desligado", trip_id=ctx.trip_id, lat=round(f.lat, 6), lon=round(f.lon, 6),
                        value=round(d), detail=json.dumps({"parked": {"lat": p["lat"], "lon": p["lon"], "ts": p.get("ts")},
                                                           "explicado_m": round(explained)}, ensure_ascii=False))
        # sem link nem coordenada: alertas e avisos vão para o painel público e para o ntfy (o mapa fica em /api/gps/parked)
        return [Alert("gps", "gps:moved_off", "crit", "O carro mudou de lugar desligado",
                      f"O carro está a {txt} de onde foi estacionado (guincho, manobra de terceiros ou furto?). "
                      "Veja no mapa do painel onde ele foi deixado.", value=round(d), ts=f.ts)]

    def live(self) -> dict:
        return {}

    def private(self) -> dict:
        """Para o gps.json (privado): onde o carro está parado, com o motor desligado."""
        c = self.cur
        if self.engine_on or not c or not c.get("valid"):
            return {"parked": None}
        key = (c["lat"], c["lon"])
        if not self._name or self._name[0] != key:
            from .geocode import name_of
            self._name = (key, name_of(self.con, None, c["lat"], c["lon"])[0])
        return {"parked": {"lat": c["lat"], "lon": c["lon"], "ts": c["ts"], "name": self._name[1]}}


class GeofenceEngine:
    """Entrar e sair dos lugares salvos. No boot, o estado inicial é assumido em silêncio (o carro ligado em casa não
    "chegou em casa"); só as mudanças depois disso viram geo_event."""

    def __init__(self, hub):
        self.con = hub.con
        self.places: list[dict] = []
        self.st: dict[int, dict] = {}
        self.last_load: int | None = None
        self.current: dict | None = None

    def reload(self) -> None:
        self.places = load_places(self.con)
        ids = {p["id"] for p in self.places}
        self.st = {k: v for k, v in self.st.items() if k in ids}

    def on_trip_start(self, tid: int, ts: int, merged: bool) -> None:
        pass

    def shift(self, off: int) -> None:
        if self.last_load is not None:
            self.last_load += off

    def feed(self, ctx) -> list:
        f = ctx.fix
        if self.last_load is None or abs(f.ts - self.last_load) >= RELOAD_MS:
            self.reload()
            self.last_load = f.ts
        inside_now = []
        for p in self.places:
            s = self.st.setdefault(p["id"], {"in": None, "n_in": 0, "n_out": 0})
            d = haversine_m(f.lat, f.lon, p["lat"], p["lon"])
            r = p["radius_m"] or DEFAULT_RADIUS_M
            if d <= r:
                s["n_in"], s["n_out"] = s["n_in"] + 1, 0
            elif d > r + GEOFENCE_OUT_M:
                s["n_in"], s["n_out"] = 0, s["n_out"] + 1
            else:                                   # faixa de histerese: não conta para nenhum lado
                s["n_in"] = s["n_out"] = 0
            if s["in"] is not True and s["n_in"] >= GEOFENCE_IN_N:
                if s["in"] is False:
                    self._event(ctx, p, "entrou")
                    self.con.execute("UPDATE place SET visits=COALESCE(visits,0)+1, last_visit_ts=? WHERE id=?", (f.ts, p["id"]))
                s["in"] = True
            elif s["in"] is not False and s["n_out"] >= GEOFENCE_OUT_N:
                if s["in"] is True:
                    self._event(ctx, p, "saiu")
                s["in"] = False
            if s["in"]:
                inside_now.append((d, p))
        self.current = min(inside_now, key=lambda x: x[0])[1] if inside_now else None
        return []

    def _event(self, ctx, p, kind) -> None:
        f = ctx.fix
        store.add_event(self.con, f.ts, kind, trip_id=ctx.trip_id, place_id=p["id"], lat=round(f.lat, 6), lon=round(f.lon, 6))

    def live(self) -> dict:
        return {}                                   # o nome do lugar é privado: só vai para o gps.json

    def private(self) -> dict:
        return {"place": {"id": self.current["id"], "name": self.current["name"], "kind": self.current["kind"]} if self.current else None}
