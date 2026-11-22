"""Exportação GPX e KML (docs/GPS-ARQUITETURA.md, seção 9.24). Só no painel do Pi: na nuvem estas rotas respondem 404.

Geradores (a resposta sai em fluxo, como o export.csv): resolução cheia de gps_fix, hora em UTC ISO 8601, texto com
escape de XML. Funções puras: recebem linhas já lidas do banco.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Iterator
from xml.sax.saxutils import escape

GPX_NS = "http://www.topografix.com/GPX/1/1"
OBD_NS = "https://obd.local/gpx/1"          # extensão própria: velocidade Doppler do receptor em km/h e satélites
KML_NS = "http://www.opengis.net/kml/2.2"


def iso_utc(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{int(ms) % 1000:03d}Z"


def _g(r, k):
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def _f(v, d: int) -> str:
    return f"{float(v):.{d}f}"


def _trip_name(t: dict) -> str:
    when = time.strftime("%d/%m/%Y %H:%M", time.localtime((t.get("start_ts") or 0) / 1000))
    label = t.get("route") or t.get("title")
    return f"Viagem {t.get('id')} · {when}" + (f" · {label}" if label else "")


# ---------------------------------------------------------------- GPX 1.1
def gpx(trips: list[dict], fixes_of: Callable[[dict], Iterable]) -> Iterator[str]:
    """Um <trk> por viagem; <trkpt lat lon> com <ele>, <time> e, em <extensions>, velocidade (km/h), satélites e HDOP.
    fixes_of(trip) devolve as posições boas da viagem em ordem de ts."""
    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield (f'<gpx version="1.1" creator="obdpi" xmlns="{GPX_NS}" xmlns:obd="{OBD_NS}">\n'
           f'<metadata><name>{escape("Viagens do carro")}</name><time>{iso_utc(int(time.time() * 1000))}</time></metadata>\n')
    for t in trips:
        yield f"<trk><name>{escape(_trip_name(t))}</name><number>{int(t.get('id') or 0)}</number><trkseg>\n"
        buf = []
        for r in fixes_of(t):
            p = [f'<trkpt lat="{_f(r["lat"], 7)}" lon="{_f(r["lon"], 7)}">']
            if _g(r, "alt") is not None:
                p.append(f"<ele>{_f(r['alt'], 1)}</ele>")
            p.append(f"<time>{iso_utc(r['ts'])}</time>")
            if _g(r, "hdop") is not None:
                p.append(f"<hdop>{_f(r['hdop'], 1)}</hdop>")
            ext = []
            if _g(r, "speed") is not None:
                ext.append(f"<obd:speed_kmh>{_f(r['speed'], 2)}</obd:speed_kmh>")
            if _g(r, "heading") is not None:
                ext.append(f"<obd:course>{_f(r['heading'], 1)}</obd:course>")
            if _g(r, "sats") is not None:
                ext.append(f"<obd:sats>{int(r['sats'])}</obd:sats>")
            if ext:
                p.append("<extensions>" + "".join(ext) + "</extensions>")
            p.append("</trkpt>\n")
            buf.append("".join(p))
            if len(buf) >= 200:
                yield "".join(buf)
                buf = []
        if buf:
            yield "".join(buf)
        yield "</trkseg></trk>\n"
    yield "</gpx>\n"


# ---------------------------------------------------------------- KML 2.2
def _placemark(name: str, lat: float, lon: float, style: str, desc: str = "") -> str:
    d = f"<description>{escape(desc)}</description>" if desc else ""
    return (f"<Placemark><name>{escape(name)}</name>{d}<styleUrl>#{style}</styleUrl>"
            f"<Point><coordinates>{_f(lon, 7)},{_f(lat, 7)}</coordinates></Point></Placemark>\n")


def kml(trip: dict, fixes: list, alerts: list, stops: list | None = None) -> Iterator[str]:
    """LineString do trajeto (resolução cheia) + alfinetes de início, fim, paradas e alertas com posição.
    trip: {id, start_ts, end_ts, start_name, end_name, route, title}; alerts: dicts com lat/lon (None = sem posição)."""
    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield f'<kml xmlns="{KML_NS}"><Document><name>{escape(_trip_name(trip))}</name>\n'
    for sid, color, scale in (("trajeto", "ff3dc8ff", None), ("inicio", "ff9de52e", 1.0), ("fim", "ff5e4dff", 1.0),
                              ("parada", "ff20b0ff", 0.8), ("alerta", "ff3d6bff", 0.9)):
        if scale is None:
            yield f'<Style id="{sid}"><LineStyle><color>{color}</color><width>4</width></LineStyle></Style>\n'
        else:
            yield f'<Style id="{sid}"><IconStyle><color>{color}</color><scale>{scale}</scale></IconStyle></Style>\n'
    if fixes:
        yield "<Placemark><name>Trajeto</name><styleUrl>#trajeto</styleUrl><LineString><tessellate>1</tessellate><coordinates>\n"
        buf = []
        for r in fixes:
            alt = _g(r, "alt")
            buf.append(f"{_f(r['lon'], 7)},{_f(r['lat'], 7)}" + (f",{_f(alt, 1)}" if alt is not None else "") + "\n")
            if len(buf) >= 500:
                yield "".join(buf)
                buf = []
        if buf:
            yield "".join(buf)
        yield "</coordinates></LineString></Placemark>\n"
        a, b = fixes[0], fixes[-1]
        yield _placemark("Início" + (f": {trip['start_name']}" if trip.get("start_name") else ""), a["lat"], a["lon"], "inicio", iso_utc(a["ts"]))
        yield _placemark("Fim" + (f": {trip['end_name']}" if trip.get("end_name") else ""), b["lat"], b["lon"], "fim", iso_utc(b["ts"]))
    for s in stops or []:
        if s.get("lat") is None:
            continue
        yield _placemark(f"Parada de {round(s.get('s') or 0)} s" + (f" ({s['name']})" if s.get("name") else ""), s["lat"], s["lon"], "parada", iso_utc(int(s.get("ts") or 0)) if s.get("ts") else "")
    for al in alerts:
        if al.get("lat") is None:
            continue
        yield _placemark(str(al.get("title") or al.get("rule") or "Alerta"), al["lat"], al["lon"], "alerta",
                         f"{al.get('severity') or ''} · {iso_utc(al['ts'])}")
    yield "</Document></kml>\n"


def safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in s)[:80] or "viagem"

