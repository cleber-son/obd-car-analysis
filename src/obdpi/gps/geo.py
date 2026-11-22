"""Geometria (funções puras): distância, rumo, células, Douglas–Peucker. Mesmas funções no Pi e em scripts/geo-build.py."""
from __future__ import annotations

import math

R_EARTH = 6371008.8          # raio médio (m)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Rumo inicial de 1 para 2, em graus verdadeiros (0 = norte, 90 = leste)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def ang_diff(a: float, b: float) -> float:
    """Diferença entre dois rumos, 0..180."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Ponto deslocado em metros (aproximação local; serve para o simulador e para distâncias curtas)."""
    return (lat + math.degrees(north_m / R_EARTH),
            lon + math.degrees(east_m / (R_EARTH * math.cos(math.radians(lat)))))


def cell(lat: float, lon: float, size: float = 0.001) -> int:
    """Célula da grade: 0,001° ≈ 110 m. int((lat + 90) * n) * 1_000_000 + int((lon + 180) * n), n = 1/size."""
    n = round(1 / size)
    return int((lat + 90) * n) * 1_000_000 + int((lon + 180) * n)


def neighbors(c: int, size: float = 0.001) -> list[int]:
    """A célula e as 8 vizinhas."""
    r, q = divmod(c, 1_000_000)
    return [(r + dr) * 1_000_000 + (q + dq) for dr in (-1, 0, 1) for dq in (-1, 0, 1)]


def _xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    k = math.cos(math.radians(lat0))
    return (math.radians(lon - lon0) * R_EARTH * k, math.radians(lat - lat0) * R_EARTH)


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def cross_track_m(p, a, b) -> float:
    """Distância (m) do ponto p ao segmento a–b (pontos (lat, lon)); fora das pontas, distância à ponta mais próxima."""
    lat0, lon0 = a
    px, py = _xy(lat0, lon0, *p)
    bx, by = _xy(lat0, lon0, *b)
    return _seg_dist(px, py, 0.0, 0.0, bx, by)


def simplify(pts: list[tuple[float, float]], eps_m: float) -> list[int]:
    """Douglas–Peucker iterativo (sem recursão). Devolve os índices mantidos, em ordem."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    lat0, lon0 = pts[0]
    xy = [_xy(lat0, lon0, la, lo) for la, lo in pts]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            d = _seg_dist(xy[k][0], xy[k][1], ax, ay, bx, by)
            if d > best:
                best, idx = d, k
        if best > eps_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [i for i in range(n) if keep[i]]


def path_length_m(pts) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))
