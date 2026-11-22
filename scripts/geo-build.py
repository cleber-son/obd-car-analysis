#!/usr/bin/env python3
"""Gera o geo.db (mapa offline do Delfos) a partir do OpenStreetMap: radares, vias com limite de velocidade e mão única,
postos, oficinas e bairros/cidades. Roda no PC (só Python, sem pacotes extras); o arquivo vai para o Pi pelo
scripts/deploy.sh (docs/GPS-ARQUITETURA.md, seções 4.3 e 9.17).

Uso (a área é obrigatória; use a região inteira por onde o carro anda, não um ponto: o pedido revela a área de interesse):

    python3 scripts/geo-build.py --bbox S,W,N,E [--inner LAT,LON,KM] [--out data/geo/geo.db]
    python3 scripts/geo-build.py --city "Nome da cidade" [--uf SP] [--inner LAT,LON,KM]
    python3 scripts/geo-build.py --from-json resposta1.json [resposta2.json …] --bbox S,W,N,E     (sem rede)

    --bbox S,W,N,E   sul, oeste, norte, leste em graus decimais (ex.: -23.80,-46.90,-23.35,-46.35)
    --city NOME      acha a caixa da cidade pelo limite administrativo no OSM (um pedido a mais ao Overpass)
    --inner LAT,LON,KM  ruas locais (residential, unclassified) só dentro deste raio; fora dele, só vias principais
                     (até tertiary). Sem --inner, todas as ruas da área. O centro NÃO vai para o painel online.
    --save-json DIR  guarda as respostas do Overpass (para refazer depois com --from-json, sem pedir de novo)
    --overpass URL   servidor (padrão: https://overpass-api.de/api/interpreter)
    --timeout S      tempo máximo de cada pedido (padrão 180 s); --pause S entre blocos (padrão 10 s)

Depois: `scripts/deploy.sh` envia data/geo/geo.db para /data/obd/geo/geo.db (o Pi reabre o arquivo sozinho em até 1 min).
Refazer a cada poucos meses: o painel avisa quando o mapa passa de 6 meses. Os dados são do OpenStreetMap (© OpenStreetMap
contributors, ODbL) e podem estar desatualizados: o aviso de radar e o limite são um auxílio; a placa da via vale sempre.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from obdpi.gps.geo import bearing_deg, cell, cross_track_m, haversine_m, simplify   # noqa: E402
from obdpi.gps import roads                                                          # noqa: E402

UA = "delfos-geo-build/1.0"            # genérico: sem dado pessoal
OVERPASS = "https://overpass-api.de/api/interpreter"
CHUNK_DEG = 0.25
MAX_AREA_DEG2 = 4.0                    # acima disso pede --force (Overpass é um serviço público gratuito)
LOCAL = ("unclassified", "residential", "living_street")
PLACES = {"city": "cidade", "town": "cidade", "village": "vila", "hamlet": "vila", "suburb": "bairro",
          "neighbourhood": "bairro", "quarter": "bairro"}
CARDINAL = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5, "S": 180,
            "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
            "L": 90, "O": 270, "NO": 315, "SO": 225}
CAMERA_WAY_M = 20.0                    # radar fora da via (no poste): associado à via mais próxima a até 20 m

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE road (way_id INTEGER PRIMARY KEY, name TEXT, highway TEXT, maxspeed INTEGER, oneway INTEGER);
CREATE TABLE road_pt (cell INTEGER NOT NULL, way_id INTEGER NOT NULL, seq INTEGER NOT NULL, lat REAL, lon REAL);
CREATE TABLE camera (id INTEGER PRIMARY KEY, cell INTEGER, lat REAL, lon REAL, maxspeed INTEGER, dir REAL, way_id INTEGER);
CREATE TABLE poi (id INTEGER PRIMARY KEY, cell INTEGER, lat REAL, lon REAL, kind TEXT, name TEXT, brand TEXT);
CREATE TABLE locality (id INTEGER PRIMARY KEY, cell2 INTEGER, lat REAL, lon REAL, kind TEXT, name TEXT, city TEXT, uf TEXT);
"""
INDEXES = """
CREATE INDEX road_pt_cell ON road_pt(cell);
CREATE INDEX camera_cell ON camera(cell);
CREATE INDEX poi_cell ON poi(cell);
CREATE INDEX locality_cell ON locality(cell2);
"""


# ---------------------------------------------------------------- área e pedidos
def parse_bbox(s: str) -> tuple[float, float, float, float]:
    p = [float(x) for x in s.replace(" ", "").split(",")]
    if len(p) != 4:
        raise ValueError("use --bbox S,W,N,E")
    s_, w, n, e = p
    if not (-90 <= s_ < n <= 90 and -180 <= w < e <= 180):
        raise ValueError("caixa inválida: precisa sul < norte e oeste < leste")
    return s_, w, n, e


def parse_inner(s: str | None) -> tuple[float, float, float] | None:
    if not s:
        return None
    la, lo, km = (float(x) for x in s.replace(" ", "").split(","))
    return la, lo, km


def chunks(bbox, size: float = CHUNK_DEG) -> list[tuple[float, float, float, float]]:
    s, w, n, e = bbox
    out = []
    la = s
    while la < n - 1e-9:
        lo = w
        while lo < e - 1e-9:
            out.append((round(la, 6), round(lo, 6), round(min(la + size, n), 6), round(min(lo + size, e), 6)))
            lo += size
        la += size
    return out


def query(b, inner=None, timeout: int = 180) -> str:
    s, w, n, e = b
    main = "^(motorway|trunk|primary|secondary|tertiary)(_link)?$"
    local = f'way["highway"~"^({"|".join(LOCAL)})$"]'
    if inner:
        la, lo, km = inner
        local += f"(around:{int(km * 1000)},{la},{lo})"
    return (f"[out:json][timeout:{timeout}][bbox:{s},{w},{n},{e}];\n(\n"
            f'  way["highway"~"{main}"];\n  {local};\n'
            '  node["highway"="speed_camera"];\n  relation["type"="enforcement"];\n'
            '  node["amenity"="fuel"];\n  way["amenity"="fuel"];\n  node["shop"="car_repair"];\n'
            f'  node["place"~"^({"|".join(PLACES)})$"];\n);\nout body geom qt;\n')


def fetch(q: str, url: str = OVERPASS, timeout: int = 180, tries: int = 3) -> dict:
    data = urllib.parse.urlencode({"data": q}).encode()
    for k in range(tries):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            if ex.code in (429, 502, 503, 504) and k + 1 < tries:
                wait = 30 * (k + 1)
                print(f"   servidor ocupado ({ex.code}); nova tentativa em {wait} s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as ex:
            if k + 1 < tries:
                print(f"   falha de rede ({ex}); nova tentativa em 30 s", file=sys.stderr)
                time.sleep(30)
                continue
            raise
    raise RuntimeError("Overpass não respondeu")


def city_bbox(name: str, uf: str | None, url: str, timeout: int) -> tuple[float, float, float, float]:
    """Caixa do limite administrativo da cidade (admin_level 8 no Brasil)."""
    flt = f'["boundary"="administrative"]["admin_level"="8"]["name"="{name}"]'
    q = f"[out:json][timeout:{timeout}];\n"
    q += (f'area["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"="BR-{uf.upper()}"]->.uf;\nrelation{flt}(area.uf);\n'
          if uf else f"relation{flt};\n")
    q += "out bb tags;\n"
    els = [x for x in fetch(q, url, timeout).get("elements", []) if x.get("bounds")]
    if not els:
        raise SystemExit(f'Cidade "{name}" não encontrada no OSM. Confira o nome (com acentos) ou use --bbox.')
    if len(els) > 1:
        print("Mais de uma cidade com esse nome; use --uf ou --bbox:", file=sys.stderr)
        for x in els:
            bd = x["bounds"]
            print(f'  {x["tags"].get("name")} ({x["tags"].get("is_in:state") or "?"}): '
                  f'--bbox {bd["minlat"]},{bd["minlon"]},{bd["maxlat"]},{bd["maxlon"]}', file=sys.stderr)
        raise SystemExit(2)
    bd = els[0]["bounds"]
    return round(bd["minlat"], 5), round(bd["minlon"], 5), round(bd["maxlat"], 5), round(bd["maxlon"], 5)


# ---------------------------------------------------------------- processamento
def oneway_of(tags: dict) -> int:
    ow = str(tags.get("oneway", "")).lower()
    if ow in ("yes", "1", "true"):
        return 1
    if ow in ("-1", "reverse"):
        return -1
    if ow == "no":
        return 0
    if tags.get("highway") in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def maxspeed_of(tags: dict) -> int | None:
    for k in ("maxspeed", "maxspeed:forward", "maxspeed:backward"):
        v = roads.parse_maxspeed(tags.get(k))
        if v:
            return v
    return None


def densify(pts: list[tuple[float, float]], max_m: float = roads.DENSIFY_M) -> list[tuple[float, float]]:
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        d = haversine_m(a[0], a[1], b[0], b[1])
        k = int(math.ceil(d / max_m)) if d > max_m else 1
        for i in range(1, k):
            out.append((a[0] + (b[0] - a[0]) * i / k, a[1] + (b[1] - a[1]) * i / k))
        out.append(b)
    return out


def dir_of(v, way_brg: float | None) -> float | None:
    """Sentido do radar: graus, ponto cardeal, ou forward/backward em relação à via."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("FORWARD", "BACKWARD"):
        if way_brg is None:
            return None
        return way_brg if s == "FORWARD" else (way_brg + 180) % 360
    if s in CARDINAL:
        return float(CARDINAL[s])
    try:
        return float(s.split(";")[0].split("-")[0]) % 360
    except ValueError:
        return None


def _in_bbox(lat, lon, bbox) -> bool:
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


def build(responses: list[dict], out: Path, bbox, inner=None, *, now_ms: int | None = None, verbose: bool = False) -> dict:
    """Monta o geo.db (seção 4.3) a partir das respostas do Overpass (JSON). Grava num arquivo temporário e troca no fim."""
    ways: dict[int, dict] = {}
    nodes: dict[int, dict] = {}
    rels: dict[int, dict] = {}
    osm_ts = None
    for resp in responses:
        osm_ts = osm_ts or (resp.get("osm3s") or {}).get("timestamp_osm_base")
        for el in resp.get("elements", []):
            t, i = el.get("type"), el.get("id")
            {"way": ways, "node": nodes, "relation": rels}.get(t, {}).setdefault(i, el)
    # vias
    road_rows, pts_rows, way_geom, way_dense = [], [], {}, {}
    node_way: dict[int, list[tuple[int, int]]] = {}
    skipped_inner = 0
    for wid, w in ways.items():
        tg = w.get("tags") or {}
        hw = tg.get("highway")
        geom = [(g["lat"], g["lon"]) for g in (w.get("geometry") or []) if g]
        if hw not in roads.HIGHWAYS or len(geom) < 2:
            continue
        if inner and hw in LOCAL and min(haversine_m(inner[0], inner[1], la, lo) for la, lo in geom) > inner[2] * 1000:
            skipped_inner += 1
            continue
        ms = maxspeed_of(tg)
        name = tg.get("name") or tg.get("ref")
        road_rows.append((wid, name, hw, ms, oneway_of(tg)))
        way_geom[wid] = geom
        for k, nid in enumerate(w.get("nodes") or []):
            node_way.setdefault(nid, []).append((wid, k))
        keep = simplify(geom, roads.SIMPLIFY_M)
        dense = way_dense[wid] = densify([geom[k] for k in keep])
        pts_rows += [(cell(la, lo), wid, seq, round(la, 7), round(lo, 7)) for seq, (la, lo) in enumerate(dense)]
    info = {r[0]: r for r in road_rows}

    def way_brg(wid: int, k: int) -> float | None:
        g = way_geom.get(wid)
        if not g:
            return None
        a, b = g[max(0, k - 1)], g[min(len(g) - 1, k + 1)]
        return bearing_deg(a[0], a[1], b[0], b[1]) if a != b else None

    # índice simples de pedaços para associar radar fora da via (célula de 0,001°)
    seg_idx: dict[int, list] = {}
    for wid, g in way_dense.items():
        for a, b in zip(g, g[1:]):
            for c in {cell(*a), cell(*b)}:
                seg_idx.setdefault(c, []).append((wid, a, b))

    def nearest_way(lat, lon, max_m=CAMERA_WAY_M):
        from obdpi.gps.geo import neighbors
        best = None
        for c in neighbors(cell(lat, lon)):
            for wid, a, b in seg_idx.get(c, []):
                d = cross_track_m((lat, lon), a, b)
                if d <= max_m and (best is None or d < best[0]):
                    best = (d, wid, bearing_deg(a[0], a[1], b[0], b[1]))
        return best

    # radares: nós highway=speed_camera e relações type=enforcement de velocidade
    cams: dict[int, dict] = {}
    for nid, n in nodes.items():
        tg = n.get("tags") or {}
        if tg.get("highway") == "speed_camera" and "lat" in n:
            cams[nid] = {"id": nid, "lat": n["lat"], "lon": n["lon"], "maxspeed": maxspeed_of(tg),
                         "dir_tag": tg.get("direction") or tg.get("camera:direction"), "dir": None}
    for rid, r in rels.items():
        tg = r.get("tags") or {}
        if tg.get("type") != "enforcement" or tg.get("enforcement") not in ("maxspeed", "average_speed"):
            continue
        mem = {}
        for m in r.get("members") or []:
            if m.get("type") == "node" and "lat" in m:
                mem.setdefault(m.get("role") or "", (m["ref"], m["lat"], m["lon"]))
        dev = mem.get("device") or mem.get("from")
        if not dev:
            continue
        frm, to = mem.get("from"), mem.get("to")
        d = None
        if frm and to and frm[0] != to[0]:
            d = bearing_deg(frm[1], frm[2], to[1], to[2])
        elif frm and frm[0] != dev[0]:
            d = bearing_deg(frm[1], frm[2], dev[1], dev[2])
        elif to and to[0] != dev[0]:
            d = bearing_deg(dev[1], dev[2], to[1], to[2])
        c = cams.setdefault(dev[0], {"id": dev[0], "lat": dev[1], "lon": dev[2], "maxspeed": None, "dir_tag": None, "dir": None})
        c["maxspeed"] = c["maxspeed"] or maxspeed_of(tg)
        if d is not None:
            c["dir"] = round(d, 1)
    cam_rows = []
    for cid, c in cams.items():
        if not _in_bbox(c["lat"], c["lon"], bbox):
            continue
        on = sorted(node_way.get(cid, []), key=lambda x: roads.RANK.get(info[x[0]][2], 99)) if cid in node_way else []
        wid, brg = None, None
        if on:
            wid, k = on[0]
            brg = way_brg(wid, k)
        else:
            nw = nearest_way(c["lat"], c["lon"])
            if nw:
                _, wid, brg = nw
        if c["dir"] is None:
            c["dir"] = dir_of(c["dir_tag"], brg)
        if c["dir"] is None and wid is not None and info[wid][4] and brg is not None:     # via de mão única: o sentido é o dela
            c["dir"] = round(brg if info[wid][4] == 1 else (brg + 180) % 360, 1)
        ms = c["maxspeed"] or (info[wid][3] if wid is not None else None)
        dr = round(c["dir"], 1) if c["dir"] is not None else None
        cam_rows.append((cid, cell(c["lat"], c["lon"]), round(c["lat"], 7), round(c["lon"], 7), ms, dr, wid))
    # postos e oficinas
    poi_rows = []
    for t, coll in ((0, nodes), (1, ways)):
        for i, el in coll.items():
            tg = el.get("tags") or {}
            kind = "posto" if tg.get("amenity") == "fuel" else "oficina" if tg.get("shop") == "car_repair" else None
            if not kind:
                continue
            if t == 0:
                la, lo = el.get("lat"), el.get("lon")
            else:
                g = [(x["lat"], x["lon"]) for x in el.get("geometry") or [] if x]
                if not g:
                    continue
                la, lo = sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g)
            if la is None or not _in_bbox(la, lo, bbox):
                continue
            poi_rows.append((i * 10 + t, cell(la, lo), round(la, 7), round(lo, 7), kind, tg.get("name"), tg.get("brand")))
    # bairros, vilas e cidades
    loc_rows = []
    for i, n in nodes.items():
        tg = n.get("tags") or {}
        kind = PLACES.get(tg.get("place"))
        if not kind or not tg.get("name") or "lat" not in n:
            continue
        loc_rows.append((i, cell(n["lat"], n["lon"], 0.02), round(n["lat"], 7), round(n["lon"], 7), kind, tg["name"],
                         tg.get("is_in:city") or tg.get("addr:city"), tg.get("is_in:state_code") or tg.get("addr:state")))
    # grava
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO road VALUES (?,?,?,?,?)", road_rows)
    con.executemany("INSERT INTO road_pt VALUES (?,?,?,?,?)", pts_rows)
    con.executemany("INSERT OR REPLACE INTO camera VALUES (?,?,?,?,?,?,?)", cam_rows)
    con.executemany("INSERT OR REPLACE INTO poi VALUES (?,?,?,?,?,?,?)", poi_rows)
    con.executemany("INSERT OR REPLACE INTO locality VALUES (?,?,?,?,?,?,?,?)", loc_rows)
    con.executescript(INDEXES)
    counts = {"roads": len(road_rows), "road_pts": len(pts_rows), "cameras": len(cam_rows),
              "cameras_dir": sum(1 for r in cam_rows if r[5] is not None), "roads_maxspeed": sum(1 for r in road_rows if r[3]),
              "pois": len(poi_rows), "localities": len(loc_rows), "skipped_inner": skipped_inner}
    osm_ms = None
    if osm_ts:
        try:
            osm_ms = calendar.timegm(time.strptime(osm_ts, "%Y-%m-%dT%H:%M:%SZ")) * 1000
        except ValueError:
            osm_ms = None
    meta = {"v": "1", "built_ts": str(now_ms or int(time.time() * 1000)), "osm_ts": str(osm_ms) if osm_ms else "",
            "bbox": ",".join(str(x) for x in bbox), "inner": ",".join(str(x) for x in inner) if inner else "",
            "fonte": "OpenStreetMap (Overpass API)", "atribuicao": "© OpenStreetMap contributors, ODbL",
            "aviso": "auxílio: o mapa pode estar desatualizado; a placa da via vale sempre", **{k: str(v) for k, v in counts.items()}}
    con.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
    os.replace(tmp, out)
    if verbose:
        print("   " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts


# ---------------------------------------------------------------- linha de comando
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox")
    ap.add_argument("--city")
    ap.add_argument("--uf")
    ap.add_argument("--inner")
    ap.add_argument("--out", default="data/geo/geo.db")
    ap.add_argument("--from-json", nargs="+")
    ap.add_argument("--save-json")
    ap.add_argument("--overpass", default=OVERPASS)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--pause", type=float, default=10.0)
    ap.add_argument("--force", action="store_true", help="aceita área maior que 4 graus²")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1):                   # "--bbox -23.8,…": número negativo não é opção
        if argv[i] in ("--bbox", "--inner") and argv[i + 1][:1] == "-" and argv[i + 1][1:2].isdigit():
            argv[i:i + 2] = [argv[i] + "=" + argv[i + 1], ""]
    a = ap.parse_args([x for x in argv if x != ""])
    inner = parse_inner(a.inner)
    if a.bbox:
        bbox = parse_bbox(a.bbox)
    elif a.city and not a.from_json:
        print(f"== procurando a cidade {a.city} no OSM")
        bbox = city_bbox(a.city, a.uf, a.overpass, a.timeout)
        print(f"   --bbox {','.join(str(x) for x in bbox)}")
    else:
        ap.error("informe a área: --bbox S,W,N,E ou --city NOME (o dono escolhe a região)")
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    if area > MAX_AREA_DEG2 and not a.force:
        ap.error(f"área de {area:.1f} graus² é grande demais para o Overpass público; divida ou use --force")
    if a.from_json:
        responses = [json.loads(Path(p).read_text(encoding="utf-8")) for p in a.from_json]
    else:
        responses = []
        parts = chunks(bbox)
        print(f"== {len(parts)} bloco(s) de até {CHUNK_DEG}° em {a.overpass}")
        for k, b in enumerate(parts):
            if k:
                time.sleep(a.pause)
            print(f"   bloco {k + 1}/{len(parts)}: {b}")
            r = fetch(query(b, inner, a.timeout), a.overpass, a.timeout)
            responses.append(r)
            if a.save_json:
                d = Path(a.save_json)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"overpass-{k + 1:03d}.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    counts = build(responses, Path(a.out), bbox, inner)
    size = Path(a.out).stat().st_size / 1e6
    print(f"== {a.out}: {size:.1f} MB · {counts['roads']} vias ({counts['roads_maxspeed']} com limite) · {counts['cameras']} radares "
          f"({counts['cameras_dir']} com sentido) · {counts['pois']} postos/oficinas · {counts['localities']} bairros/cidades")
    print("   Envie para o Pi com scripts/deploy.sh. Dados © OpenStreetMap contributors (ODbL); é um auxílio, a placa vale sempre.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
