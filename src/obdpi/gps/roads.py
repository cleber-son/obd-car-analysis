"""Vias pelo geo.db (docs/GPS-ARQUITETURA.md, seções 4.3, 9.17, 9.20 e 9.1 passo 4): via atual, limite de velocidade,
radar à frente, cidade × estrada e nome de rua/bairro de um ponto.

O geo.db é gerado no PC por scripts/geo-build.py a partir do OpenStreetMap e copiado para /data/obd/geo/geo.db. É um
**auxílio**: o mapa pode estar desatualizado (radar que saiu, limite que mudou); a placa da via vale sempre. Limite
presumido pela classe da via (sem `maxspeed` no OSM) aparece esmaecido e nunca gera fala nem "acima do limite".

- `GeoDb`: abre o arquivo só leitura (`immutable=1`) e reabre quando a data do arquivo muda (conferida a cada 60 s).
- `RoadMatcher`: via atual (pontos a até 25 m com rumo compatível; troca só depois de 3 leituras seguidas).
- `RoadEngine` (hub.LIVE_ENGINES): limite, "acima do limite" por 5 s, aviso de radar à frente. Nada de posição, nome
  de rua nem identificador do OSM vai para o live.json (público): só limite, classe da via e a distância do radar.
- `finalize` (hub.FINALIZERS): trip_gps.road_json = km, tempo, consumo, rotação, carga, ajuste curto, temperatura e
  marchas por classe (cidade / estrada / misto); `report` junta o período para /api/gps/roadmix (sem posição).
- `describe`: "Rua X, Bairro" para o diário de bordo (geocode.name_of).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import time
from pathlib import Path

from .geo import ang_diff, bearing_deg, cell, cross_track_m, haversine_m, neighbors

VERSION = 1
# ---------------------------------------------------------------- vias
HIGHWAYS = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
            "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link")
MAIN = ("motorway", "trunk", "primary", "secondary", "tertiary")            # fora do raio --inner, só estas (e os _link)
ESTRADA = ("motorway", "trunk", "motorway_link", "trunk_link")              # rodovia; o resto é cidade (ou limite ≥ 80)
ESTRADA_LIMIT = 80
# limite presumido pela classe (CTB: local 30, coletora 40, arterial 60, trânsito rápido 80, rodovia 100/110)
PRESUMED = {"motorway": 110, "trunk": 100, "primary": 60, "secondary": 60, "tertiary": 40, "unclassified": 30,
            "residential": 30, "living_street": 30, "motorway_link": 60, "trunk_link": 60, "primary_link": 40,
            "secondary_link": 40, "tertiary_link": 40}
RANK = {h: i for i, h in enumerate(HIGHWAYS)}
DENSIFY_M = 40.0            # distância máxima entre pontos gravados (o casamento usa as 9 células em volta: ~100 m)
SIMPLIFY_M = 6.0

# ---------------------------------------------------------------- casamento com a via (valores iniciais)
MATCH_M = 25.0              # ponto de via a até 25 m…
MATCH_ANG = 35.0            # …com rumo a ±35° (ou o oposto, se a via não é de mão única)
STICKY_M = 8.0              # a via atual continua se estiver até 8 m (de pontuação) da melhor
SWITCH_N = 3                # troca de via só depois de 3 leituras seguidas
MIN_KMH = 5.0               # abaixo disso o rumo é ruído: a via fica a mesma
ANG_W = 0.3                 # pontuação = distância (m) + 0,3 × diferença de rumo (graus)
STEP_MS = 900               # uma avaliação por segundo

# ---------------------------------------------------------------- radar e limite
RADAR_M = 600               # radares a até 600 m…
RADAR_CONE = 30.0           # …cuja direção a partir do carro está a ±30° do rumo
RADAR_KEEP_CONE = 40.0      # o radar já avisado continua com ±40° (menos piscar perto dele)
RADAR_DIR_TOL = 60.0        # sentido do radar (quando o OSM informa) compatível com o do carro
RADAR_NEAR_WAY_M = 30.0     # radar sem via associada: a até 30 m da via atual
RADAR_MIN_KMH = 10.0
OVER_K, OVER_ADD, OVER_MS = 1.07, 2.0, 5000     # "acima do limite": limite × 1,07 + 2 por 5 s (só limite do OSM)
STALE_S = 5.0               # sem posição nova há 5 s: nada de limite nem aviso no live.json

# ---------------------------------------------------------------- cidade × estrada sem geo.db (perfil)
WIN_S = 60
ROAD_KMH, CITY_KMH, STOP_KMH = 70.0, 45.0, 3.0
CLASSES = ("cidade", "estrada", "misto")

# ---------------------------------------------------------------- nomes
STREET_M = 60.0
BAIRRO_M = 3000.0
CITY_M = 30000.0
BAIRRO_KINDS = ("bairro", "vila")
CITY_KINDS = ("cidade",)


def parse_maxspeed(v) -> int | None:
    """"60", "60 km/h", "40 mph", "60;40" → km/h inteiro; "BR:urban", "none", "signals", "walk" → None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(mph|km/h|kmh|kph)?", s)
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    if m.group(2) == "mph":
        x *= 1.609344
    x = int(round(x))
    return x if 5 <= x <= 150 else None


def presumed_limit(highway: str | None) -> int | None:
    return PRESUMED.get(highway or "")


def road_cls(highway: str | None, maxspeed: int | None) -> str:
    """'estrada' (rodovia: motorway/trunk ou limite do OSM ≥ 80) ou 'cidade'."""
    return "estrada" if (highway in ESTRADA or (maxspeed or 0) >= ESTRADA_LIMIT) else "cidade"


# ---------------------------------------------------------------- arquivo
class GeoDb:
    """geo.db só leitura. get() devolve a conexão (ou None sem arquivo); a cada 60 s confere a data do arquivo e reabre
    se mudou (o deploy troca o arquivo por um novo, com rename)."""
    CHECK_S = 60.0

    def __init__(self, path):
        self.path = Path(path) if path else None
        self._con: sqlite3.Connection | None = None
        self._mtime = None
        self._checked = -1e18
        self.meta: dict = {}
        self.cache: dict = {}

    def get(self) -> sqlite3.Connection | None:
        now = time.monotonic()
        if now - self._checked >= self.CHECK_S:
            self._checked = now
            self._refresh()
        return self._con

    def _refresh(self) -> None:
        try:
            st = self.path.stat() if self.path else None
        except OSError:
            st = None
        if st is None:
            self.close()
            return
        if self._con is not None and st.st_mtime == self._mtime:
            return
        self.close()
        try:
            con = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, timeout=2, check_same_thread=False)
            con.execute("SELECT 1 FROM road_pt LIMIT 1").fetchall()
            self.meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            self._con, self._mtime = con, st.st_mtime
        except sqlite3.Error:
            self._con = None

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
        self._con, self._mtime, self.meta, self.cache = None, None, {}, {}


_OPEN: dict[str, GeoDb] = {}


def open_geo(path) -> sqlite3.Connection | None:
    """Conexão compartilhada por caminho (obdctl gps reanalyze, make_ctx): None sem o arquivo."""
    if not path:
        return None
    g = _OPEN.get(str(path))
    if g is None:
        g = _OPEN[str(path)] = GeoDb(path)
    return g.get()


def meta_of(geo) -> dict:
    try:
        return dict(geo.execute("SELECT key, value FROM meta").fetchall())
    except (sqlite3.Error, AttributeError):
        return {}


# ---------------------------------------------------------------- consultas
_CACHES: dict = {}           # conexão do geo.db → cache (a própria conexão é a chave: um id reaproveitado não confunde)


def _cache(geo) -> dict:
    c = _CACHES.get(geo)
    if c is None:
        if len(_CACHES) >= 8:
            _CACHES.pop(next(iter(_CACHES)))
        c = _CACHES[geo] = {}
    elif len(c) > 20000:
        c.clear()
    return c


def road_info(geo, way: int) -> dict | None:
    """name, highway, limite (do OSM ou presumido) e a origem, classe e mão única de uma via (com cache)."""
    cache = _cache(geo)
    r = cache.get(way)
    if r is None:
        r = geo.execute("SELECT way_id, name, highway, maxspeed, oneway FROM road WHERE way_id=?", (way,)).fetchone()
        if r is None:
            return None
        r = cache[way] = tuple(r)
    way_id, name, hw, ms, ow = r
    lim, src = (ms, "osm") if ms else (presumed_limit(hw), "presumido")
    return {"way": way_id, "name": name, "highway": hw, "maxspeed": ms, "oneway": ow or 0, "limit": lim,
            "src": src if lim else None, "cls": road_cls(hw, ms)}


def segments(geo, lat: float, lon: float) -> list[tuple[int, tuple, tuple]]:
    """Pedaços de via (way, (lat, lon) de, (lat, lon) até) com as duas pontas nas 9 células em volta do ponto."""
    cells = neighbors(cell(lat, lon))
    rows = geo.execute(f"SELECT way_id, seq, lat, lon FROM road_pt WHERE cell IN ({','.join('?' * len(cells))}) ORDER BY way_id, seq",
                       cells).fetchall()
    out, prev = [], None
    for r in rows:
        if prev is not None and prev[0] == r[0] and prev[1] + 1 == r[1]:
            out.append((r[0], (prev[2], prev[3]), (r[2], r[3])))
        prev = r
    return out


def candidates(geo, lat: float, lon: float, heading: float | None, max_m: float = MATCH_M, max_ang: float = MATCH_ANG) -> list[dict]:
    """Vias a até max_m com rumo compatível (mão única respeitada), da melhor para a pior: {way, score, dist, ang}."""
    best: dict[int, dict] = {}
    for way, a, b in segments(geo, lat, lon):
        d = cross_track_m((lat, lon), a, b)
        if d > max_m:
            continue
        ang = 0.0
        if heading is not None:
            info = road_info(geo, way)
            ow = (info or {}).get("oneway", 0)
            fwd = ang_diff(heading, bearing_deg(a[0], a[1], b[0], b[1]))
            ang = fwd if ow == 1 else 180 - fwd if ow == -1 else min(fwd, 180 - fwd)
            if ang > max_ang:
                continue
        s = d + ANG_W * ang
        if way not in best or s < best[way]["score"]:
            best[way] = {"way": way, "score": s, "dist": d, "ang": ang}
    return sorted(best.values(), key=lambda c: c["score"])


def way_dist_m(geo, way: int, lat: float, lon: float) -> float | None:
    """Distância do ponto à via (só os pedaços nas 9 células em volta)."""
    ds = [cross_track_m((lat, lon), a, b) for w, a, b in segments(geo, lat, lon) if w == way]
    return min(ds) if ds else None


def cameras_near(geo, lat: float, lon: float, radius_m: float = RADAR_M) -> list:
    r, q = divmod(cell(lat, lon), 1_000_000)
    nr = int(math.ceil(radius_m / 111_000 / 0.001)) + 1
    nq = int(math.ceil(radius_m / (111_000 * max(0.2, math.cos(math.radians(lat)))) / 0.001)) + 1
    out = []
    for dr in range(-nr, nr + 1):
        base = (r + dr) * 1_000_000
        out += geo.execute("SELECT id, lat, lon, maxspeed, dir, way_id FROM camera WHERE cell BETWEEN ? AND ?",
                           (base + q - nq, base + q + nq)).fetchall()
    return out


# ---------------------------------------------------------------- via atual
class RoadMatcher:
    """Via atual a partir de posições (uma avaliação por segundo). step() devolve road_info() da via atual ou None."""

    def __init__(self):
        self.cur: int | None = None
        self.info: dict | None = None
        self.pend: int | None = None
        self.pend_n = 0
        self.changes = 0
        self.last_pt: tuple[float, float] | None = None

    def step(self, geo, lat: float, lon: float, heading: float | None, speed: float | None) -> dict | None:
        if heading is None and self.last_pt is not None and haversine_m(*self.last_pt, lat, lon) >= 5:
            heading = bearing_deg(*self.last_pt, lat, lon)
        moved = self.last_pt is None or haversine_m(*self.last_pt, lat, lon) >= 5
        if moved:
            self.last_pt = (lat, lon)
        if (speed is not None and speed < MIN_KMH) or heading is None:
            return self.info                              # parado: o rumo não vale, a via fica a mesma
        cands = candidates(geo, lat, lon, heading)
        best = cands[0]["way"] if cands else None
        if self.cur is not None and cands:
            mine = next((c for c in cands if c["way"] == self.cur), None)
            if mine and mine["score"] <= cands[0]["score"] + STICKY_M:
                best = self.cur
        if best == self.cur:
            self.pend, self.pend_n = None, 0
        else:
            if best == self.pend:
                self.pend_n += 1
            else:
                self.pend, self.pend_n = best, 1
            if self.pend_n >= SWITCH_N:
                self.cur, self.pend, self.pend_n = best, None, 0
                self.info = road_info(geo, best) if best is not None else None
                self.changes += 1
        return self.info


def _f(f, k):
    try:
        return f[k]
    except (KeyError, IndexError, TypeError):
        return getattr(f, k, None)


def _one_hz(fixes):
    out, last = [], None
    for f in fixes:
        s = _f(f, "ts") // 1000
        if s != last:
            out.append(f)
            last = s
    return out


_MCACHE: dict = {"key": None, "val": None}


def match_by_second(geo, fixes: list) -> dict[int, dict | None]:
    """Via de cada segundo da viagem ({ts // 1000: road_info | None}); uma posição por segundo, a mesma histerese do
    motor ao vivo. Guarda o último resultado: trip, score e roads chamam com as mesmas posições no fechamento."""
    if geo is None or not fixes:
        return {}
    fx = _one_hz(fixes)
    key = (geo, len(fx), _f(fx[0], "ts"), _f(fx[-1], "ts"), round(_f(fx[0], "lat"), 6), round(_f(fx[-1], "lon"), 6))
    if _MCACHE["key"] == key:
        return _MCACHE["val"]
    m, out = RoadMatcher(), {}
    for f in fx:
        out[_f(f, "ts") // 1000] = m.step(geo, _f(f, "lat"), _f(f, "lon"), _f(f, "heading"), _f(f, "speed"))
    _MCACHE.update(key=key, val=out)
    return out


def _per_fix(geo, fixes: list) -> list[dict | None]:
    by = match_by_second(geo, fixes)
    return [by.get(_f(f, "ts") // 1000) for f in fixes]


def track_limits(geo, fixes: list) -> list[int | None] | None:
    """Limite (do OSM ou presumido) de cada posição, para a coluna 'limit' do trajeto; None sem geo.db ou sem via."""
    if geo is None or not fixes:
        return None
    lims = [i["limit"] if i else None for i in _per_fix(geo, fixes)]
    return lims if any(x is not None for x in lims) else None


def osm_limits(geo, fixes: list) -> list[int | None] | None:
    """Só os limites do OSM (o presumido não pesa na nota); None sem geo.db ou sem nenhum limite conhecido."""
    if geo is None or not fixes:
        return None
    lims = [i["maxspeed"] if i and i["src"] == "osm" else None for i in _per_fix(geo, fixes)]
    return lims if any(x is not None for x in lims) else None


# ---------------------------------------------------------------- radar à frente
def radar_ahead(geo, lat: float, lon: float, heading: float, road: dict | None, keep_id: int | None = None) -> tuple | None:
    """(id, distância m, limite) do radar mais próximo à frente, na via atual e no sentido do carro; None se não há."""
    if road is None:
        return None
    best = None
    for c in cameras_near(geo, lat, lon):
        cid, clat, clon, cms, cdir, cway = c
        d = haversine_m(lat, lon, clat, clon)
        if d > RADAR_M:
            continue
        cone = RADAR_KEEP_CONE if cid == keep_id else RADAR_CONE
        if ang_diff(bearing_deg(lat, lon, clat, clon), heading) > cone:
            continue
        if cdir is not None and ang_diff(cdir, heading) > RADAR_DIR_TOL:
            continue                                   # radar do outro sentido
        if cway is not None:
            if cway != road["way"]:
                ci = road_info(geo, cway)               # outro pedaço da mesma rua (a rua é partida em várias vias no OSM)
                if not ci or not ci["name"] or ci["name"] != road["name"]:
                    continue
        else:
            wd = way_dist_m(geo, road["way"], clat, clon)
            if wd is None or wd > RADAR_NEAR_WAY_M:
                continue
        if best is None or d < best[1]:
            lim = cms or (road["maxspeed"] if road["src"] == "osm" else None)
            best = (cid, d, lim)
    return best


class RoadEngine:
    """Motor ao vivo (hub.LIVE_ENGINES): via atual, limite, "acima do limite" e radar à frente, uma vez por segundo.
    live() é público: só números (limite, distância do radar, ordem do aviso), nunca posição, nome ou id do OSM."""

    def __init__(self, hub):
        cfg = getattr(hub, "cfg", None) or {}
        self.say = {"radar": bool(cfg.get("voice_radar", True)), "limit": bool(cfg.get("voice_limit", True))}
        self.m = RoadMatcher()
        self.geo = None
        self.last_ts: int | None = None
        self.last_mono = -1e18
        self.road: dict | None = None
        self.road_n = 0
        self.over_since: int | None = None
        self.over = False
        self.warn: dict | None = None
        self.warn_id: int | None = None
        self.cam_n: dict[int, int] = {}
        self.geo_month: str | None = None

    def on_trip_start(self, tid, ts, merged) -> None:
        pass

    def shift(self, off: int) -> None:
        if self.last_ts is not None:
            self.last_ts += off
        if self.over_since is not None:
            self.over_since += off

    def feed(self, ctx) -> list:
        geo = getattr(ctx, "geo", None)
        if geo is None:
            self.road = self.warn = None
            self.over = False
            self.geo = None
            return []
        if geo is not self.geo:                       # arquivo novo (ou primeiro): recomeça
            self.geo, self.m, self.road, self.warn, self.warn_id = geo, RoadMatcher(), None, None, None
            ts = meta_of(geo).get("osm_ts") or meta_of(geo).get("built_ts")
            self.geo_month = _month_of(ts)
        f = ctx.fix
        if self.last_ts is not None and 0 <= f.ts - self.last_ts < STEP_MS:
            return []
        self.last_ts, self.last_mono = f.ts, time.monotonic()
        hdg = f.heading
        if hdg is None and self.m.last_pt is not None and haversine_m(*self.m.last_pt, f.lat, f.lon) >= 5:
            hdg = bearing_deg(*self.m.last_pt, f.lat, f.lon)
        prev = self.road["way"] if self.road else None
        self.road = self.m.step(geo, f.lat, f.lon, f.heading, f.speed)
        if (self.road["way"] if self.road else None) != prev:
            self.road_n += 1
            self.over_since, self.over = None, False
        spd = f.speed or 0.0
        r = self.road
        if r and r["src"] == "osm" and spd > r["limit"] * OVER_K + OVER_ADD:
            if self.over_since is None:
                self.over_since = f.ts
            self.over = f.ts - self.over_since >= OVER_MS
        else:
            self.over_since, self.over = None, False
        hit =radar_ahead(geo, f.lat, f.lon, hdg, r, self.warn_id) if hdg is not None and spd >= RADAR_MIN_KMH else None
        if hit:
            cid, d, lim = hit
            if cid not in self.cam_n:
                self.cam_n[cid] = len(self.cam_n) + 1
                if len(self.cam_n) > 500:
                    self.cam_n = {cid: self.cam_n[cid]}
            self.warn_id = cid
            self.warn = {"kind": "radar", "dist_m": int(round(d / 10) * 10), "limit": lim, "n": self.cam_n[cid]}
        else:
            self.warn_id, self.warn = None, None
        return []

    def live(self) -> dict:
        if self.geo is None or time.monotonic() - self.last_mono > STALE_S:
            return {}
        d: dict = {"geo_m": self.geo_month, "say": dict(self.say), "road_n": self.road_n}
        if self.road:
            d.update(limit=self.road["limit"], limit_src=self.road["src"], over=self.over, via=self.road["cls"])
        if self.warn:
            d["warn"] = dict(self.warn)
        return d


def _month_of(ts) -> str | None:
    try:
        return time.strftime("%Y-%m", time.gmtime(int(ts) / 1000))
    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------- cidade × estrada (9.20)
def profile_classes(fixes: list) -> list[str]:
    """Sem geo.db: janelas de 60 s desde a primeira posição. Média ≥ 70 km/h sem parada = estrada; média < 45 ou com
    parada (< 3 km/h) = cidade; entre os dois = misto."""
    if not fixes:
        return []
    t0 = _f(fixes[0], "ts")
    win: dict[int, list[float]] = {}
    for f in fixes:
        win.setdefault((_f(f, "ts") - t0) // (WIN_S * 1000), []).append(_f(f, "speed") or 0.0)
    cls = {}
    for k, v in win.items():
        avg, stop = sum(v) / len(v), min(v) < STOP_KMH
        cls[k] = "estrada" if avg >= ROAD_KMH and not stop else "cidade" if avg < CITY_KMH or stop else "misto"
    return [cls[(_f(f, "ts") - t0) // (WIN_S * 1000)] for f in fixes]


def road_class(fixes: list, geo=None) -> tuple[list[str], str]:
    """Classe de cada posição e a fonte ('mapa', 'perfil' ou 'mapa+perfil'). Com geo.db vale a via casada; os segundos
    sem via (fora do mapa, estacionamento) ficam com o perfil."""
    prof = profile_classes(fixes)
    if geo is None:
        return prof, "perfil"
    per = _per_fix(geo, fixes)
    out, n_map = [], 0
    for p, info in zip(prof, per):
        if info:
            out.append(info["cls"])
            n_map += 1
        else:
            out.append(p)
    src = "mapa" if n_map == len(out) else "perfil" if n_map == 0 else "mapa+perfil"
    return out, src


def _acc() -> dict:
    return {"s": 0.0, "km": 0.0, "fuel_l": 0.0, "fuel_s": 0.0, "sum": {}, "n": {}, "gears": {}}


def _add(a: dict, k: str, v) -> None:
    if v is not None:
        a["sum"][k] = a["sum"].get(k, 0.0) + float(v)
        a["n"][k] = a["n"].get(k, 0) + 1


def _nearest(ts_list, t, tol):
    from .trip import _nearest as near
    return near(ts_list, t, tol)


def mix(fixes: list, fast_rows: list, slow: dict, classes: list[str]) -> dict:
    """Função pura: por classe, segundos, km (velocidade do GPS), litros (fuel_lh do OBD), rotação, carga, ajuste curto,
    temperatura e segundos por marcha (somas e contagens, para o período somar viagens)."""
    fts = [r["ts"] for r in fast_rows]
    sts = {k: [x[0] for x in v] for k, v in slow.items()}
    out = {c: _acc() for c in CLASSES}
    for i, f in enumerate(fixes):
        nxt = fixes[i + 1] if i + 1 < len(fixes) else None
        dt = min((_f(nxt, "ts") - _f(f, "ts")) / 1000, 5.0) if nxt is not None else 1.0
        if dt <= 0:
            continue
        a = out[classes[i]]
        spd = _f(f, "speed") or 0.0
        a["s"] += dt
        a["km"] += spd * dt / 3600
        j = _nearest(fts, _f(f, "ts"), 1500)
        if j is not None:
            r = fast_rows[j]
            if r["fuel_lh"] is not None:
                a["fuel_l"] += r["fuel_lh"] * dt / 3600
                a["fuel_s"] += dt
            if spd >= 3:
                _add(a, "rpm", r["rpm"])
                _add(a, "load", r["load"])
                g = r["gear"]
                if g:
                    a["gears"][str(int(g))] = a["gears"].get(str(int(g)), 0.0) + dt
        for key, name in (("stft1", "stft"), ("ect", "ect")):
            k = _nearest(sts.get(key) or [], _f(f, "ts"), 10_000)
            if k is not None and spd >= 3:
                _add(a, name, slow[key][k][1])
    for c in out.values():
        for k in ("s", "km", "fuel_l", "fuel_s"):
            c[k] = round(c[k], 4)
        c["gears"] = {g: round(v, 1) for g, v in sorted(c["gears"].items())}
        c["sum"] = {k: round(v, 3) for k, v in c["sum"].items()}
    return out


def _slow(con, a: int, b: int) -> dict:
    out = {}
    for key in ("stft1", "ect"):
        out[key] = [(r[0], r[1]) for r in con.execute(
            "SELECT s.ts, s.value FROM sample s JOIN signal g ON g.id = s.signal_id WHERE g.key=? AND s.ts BETWEEN ? AND ? ORDER BY s.ts",
            (key, a - 10_000, b + 10_000))]
    return out


def finalize(con, tid: int, ctx=None) -> dict | None:
    """Item de FINALIZERS: grava trip_gps.road_json (nada sem trajeto). Funciona sem geo.db (pelo perfil)."""
    cfg = getattr(ctx, "cfg", None) or {}
    geo = getattr(ctx, "geo", None)
    t = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (tid,)).fetchone()
    g = con.execute("SELECT n_fix FROM trip_gps WHERE trip_id=?", (tid,)).fetchone()
    if not t or not g or (g["n_fix"] or 0) < 10:
        return None
    from .trip import good_fixes
    a, b = t["start_ts"], t["end_ts"] or t["start_ts"]
    fx = _one_hz(good_fixes(con, a, b, cfg))
    fast = con.execute("SELECT ts, speed, rpm, gear, load, fuel_lh FROM fast_sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (a, b)).fetchall()
    cls, src = road_class(fx, geo)
    d = {"v": VERSION, "fonte": src, "classes": mix(fx, fast, _slow(con, a, b), cls)}
    if geo is not None:
        d["mapa"] = _month_of(meta_of(geo).get("osm_ts") or meta_of(geo).get("built_ts"))
    con.execute("UPDATE trip_gps SET road_json=? WHERE trip_id=?", (json.dumps(d, ensure_ascii=False, separators=(",", ":")), tid))
    return d


def means(a: dict) -> dict:
    """Médias de uma classe: km/L, L/100 km, rotação, carga, ajuste curto, temperatura e % do tempo por marcha."""
    s, km, fl = a.get("s") or 0.0, a.get("km") or 0.0, a.get("fuel_l") or 0.0
    sm, n = a.get("sum") or {}, a.get("n") or {}
    m = {k: round(sm[k] / n[k], 1) if n.get(k) else None for k in ("rpm", "load", "stft", "ect")}
    gs = a.get("gears") or {}
    tot = sum(gs.values())
    return {"s": round(s), "km": round(km, 2), "fuel_l": round(fl, 3),
            "kml": round(km / fl, 2) if fl > 0.05 and km > 0.2 else None, "l100": round(fl / km * 100, 2) if fl > 0.05 and km > 0.2 else None,
            "avg_kmh": round(km / s * 3600, 1) if s > 0 else None, **m,
            "gears": {g: round(v / tot * 100, 1) for g, v in gs.items()} if tot else {}}


def _merge(x: dict, y: dict) -> dict:
    out = _acc()
    for k in ("s", "km", "fuel_l", "fuel_s"):
        out[k] = (x.get(k) or 0.0) + (y.get(k) or 0.0)
    for part in ("sum", "n", "gears"):
        for src in (x, y):
            for k, v in (src.get(part) or {}).items():
                out[part][k] = out[part].get(k, 0) + v
    return out


def _br(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def report(con, since: int) -> dict:
    """/api/gps/roadmix: as viagens do período por classe e a tendência mensal do km/L de cidade e de estrada (separadas).
    Só números: rota aberta na nuvem."""
    tot = {c: _acc() for c in CLASSES}
    months: dict[str, dict] = {}
    n, srcs = 0, {}
    for r in con.execute("""SELECT t.start_ts, g.road_json FROM trip_gps g JOIN trip t ON t.id = g.trip_id
                            WHERE t.start_ts >= ? AND g.road_json IS NOT NULL AND COALESCE(t.hidden, 0) = 0 ORDER BY t.start_ts""", (since,)):
        try:
            d = json.loads(r["road_json"])
        except ValueError:
            continue
        n += 1
        srcs[d.get("fonte") or "perfil"] = srcs.get(d.get("fonte") or "perfil", 0) + 1
        mk = time.strftime("%Y-%m", time.localtime(r["start_ts"] / 1000))
        mo = months.setdefault(mk, {c: _acc() for c in CLASSES})
        for c in CLASSES:
            x = (d.get("classes") or {}).get(c) or {}
            tot[c] = _merge(tot[c], x)
            mo[c] = _merge(mo[c], x)
    rows = [{"cls": c, **means(tot[c])} for c in CLASSES]
    km_all = sum(x["km"] for x in rows)
    for x in rows:
        x["pct_km"] = round(x["km"] / km_all * 100, 1) if km_all > 0 else None
    trend = [{"month": k, **{c: means(v[c])["kml"] for c in CLASSES}, **{c + "_km": round(v[c]["km"], 1) for c in CLASSES}}
             for k, v in sorted(months.items())]
    return {"trips": n, "classes": rows, "monthly": trend, "fontes": srcs, "texto": _text(rows)}


def _text(rows: list[dict]) -> str:
    by = {x["cls"]: x for x in rows}
    c, e = by.get("cidade") or {}, by.get("estrada") or {}
    if not (c.get("km") or e.get("km")):
        return "Ainda não há viagens com GPS no período."
    parts = []
    if c.get("kml"):
        parts.append(f"na cidade {_br(c['kml'])} km/L")
    if e.get("kml"):
        parts.append(f"na estrada {_br(e['kml'])} km/L")
    head = ("Consumo " + " e ".join(parts) + ". ") if parts else ""
    share = f"{_br(c.get('pct_km') or 0, 0)} % dos km foram na cidade e {_br(e.get('pct_km') or 0, 0)} % na estrada."
    return head + share + " Compare cidade com cidade e estrada com estrada: assim dá para saber se o consumo piorou ou se só mudou o tipo de uso."


# ---------------------------------------------------------------- nomes de rua e bairro (9.1, passo 4)
def street_at(geo, lat: float, lon: float, max_m: float = STREET_M) -> str | None:
    """Nome da via nomeada mais próxima, a até 60 m."""
    best = None
    for way, a, b in segments(geo, lat, lon):
        info = road_info(geo, way)
        if not info or not info["name"]:
            continue
        d = cross_track_m((lat, lon), a, b)
        if d <= max_m and (best is None or d < best[0]):
            best = (d, info["name"])
    return best[1] if best else None


def locality_near(geo, lat: float, lon: float, kinds: tuple, max_m: float) -> dict | None:
    dl = max_m / 111_000
    dn = dl / max(0.2, math.cos(math.radians(lat)))
    rows = geo.execute(f"SELECT name, kind, lat, lon, city FROM locality WHERE kind IN ({','.join('?' * len(kinds))})"
                       " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND name IS NOT NULL",
                       (*kinds, lat - dl, lat + dl, lon - dn, lon + dn)).fetchall()
    best = None
    for r in rows:
        d = haversine_m(lat, lon, r[2], r[3])
        if d <= max_m and (best is None or d < best[0]):
            best = (d, {"name": r[0], "kind": r[1], "city": r[4], "dist_m": d})
    return best[1] if best else None


def city_of(geo, lat: float, lon: float) -> str | None:
    c = locality_near(geo, lat, lon, CITY_KINDS, CITY_M)
    return c["name"] if c else None


def describe(geo, lat: float, lon: float, home: tuple[float, float] | None = None) -> str | None:
    """"Rua X, Bairro" (via nomeada a até 60 m; bairro mais próximo a até 3 km); a cidade (até 30 km) entra no fim só
    se for diferente da cidade de "Casa" (home). Sem rua nem bairro: None (o chamador usa a coordenada)."""
    st = street_at(geo, lat, lon)
    b = locality_near(geo, lat, lon, BAIRRO_KINDS, BAIRRO_M)
    parts = [x for x in (st, b["name"] if b else None) if x]
    if not parts:
        return None
    parts = list(dict.fromkeys(parts))
    city = city_of(geo, lat, lon)
    if city and city not in parts:
        hc = None
        if home is not None:
            cache = _cache(geo)
            key = ("home_city", round(home[0], 4), round(home[1], 4))
            hc = cache.get(key)
            if hc is None:
                hc = cache[key] = city_of(geo, *home) or ""
        if not hc or hc != city:
            parts.append(city)
    return ", ".join(parts)
