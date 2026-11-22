"""Nome de um ponto (docs/GPS-ARQUITETURA.md, seção 9.1, passo 4): lugar salvo → geo.db → coordenada formatada.

O ramo do geo.db ("Rua X, Bairro") entra na F7, junto com o arquivo; até lá ctx.geo é None e o nome cai na coordenada.
"""
from __future__ import annotations

from . import places as placesmod


def fmt_coord(lat: float, lon: float) -> str:
    """"23,5501° S, 46,6331° O" (4 casas ≈ 11 m)."""
    ns = "S" if lat < 0 else "N"
    eo = "O" if lon < 0 else "L"
    return f"{abs(lat):.4f}".replace(".", ",") + f"° {ns}, " + f"{abs(lon):.4f}".replace(".", ",") + f"° {eo}"


def _from_geodb(geo, lat: float, lon: float) -> str | None:
    """Via nomeada a até 60 m + bairro a até 3 km (F7, gps/roads.py). Nesta fase não há geo.db."""
    return None


def name_of(con, geo, lat: float, lon: float, places: list[dict] | None = None) -> tuple[str, int | None]:
    """(nome, id do lugar ou None). 'places' evita reler a tabela quando o chamador já tem a lista."""
    p = placesmod.place_at(placesmod.load_places(con) if places is None else places, lat, lon)
    if p:
        return p["name"], p["id"]
    if geo is not None:
        try:
            n = _from_geodb(geo, lat, lon)
            if n:
                return n, None
        except Exception:
            pass
    return fmt_coord(lat, lon), None
