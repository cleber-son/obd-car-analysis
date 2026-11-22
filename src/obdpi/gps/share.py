"""Compartilhar a viagem ao vivo (docs/GPS-ARQUITETURA.md, seção 9.21; F9, com o pedido do dono de mostrar o trajeto
da viagem atual e os dados do carro em tempo real).

Quem recebe o link vê só a viagem em andamento: o carro andando no mapa, o trajeto desta viagem, velocidade, rotação,
temperatura, consumo, tempo e km da viagem, o título de um alerta ativo e a chegada prevista. Nada de lugares salvos,
estacionamento, histórico ou outras viagens. Sem viagem aberta, nenhuma posição sai do Pi para o link.

- Token: 16 bytes aleatórios (`secrets.token_urlsafe(16)`, 22 caracteres); só o SHA-256 (hex) fica guardado, aqui e
  na nuvem. O token aparece uma vez, na resposta de quem criou.
- Links criados no painel do Pi ficam em /data/obd/share.json até o obd-sync enviá-los (POST /api/sync/share).
- `frame(live, gps)` monta o quadro que o obd-sync manda junto com cada leitura ao vivo (só com link ativo e com a
  senha de localização confirmada pela nuvem). O Worker confere os campos de novo (lista fechada).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from .geo import haversine_m

# Decisão do dono (F9): a tela oferece "24h" (padrão) e "sempre" (até ele cancelar, sem prazo). "1h", "4h" e "viagem"
# (até o fim da viagem) continuam aceitos pela API, mas não aparecem na tela.
MODES = {"24h": 1440, "sempre": None, "1h": 60, "4h": 240, "viagem": None}
UI_MODES = ("24h", "sempre")
MAX_MIN = 1440                                     # 24 h no máximo, menos o "sempre"
FOREVER_TS = 253402300799000                       # "sempre": vence em 31/12/9999 (igual ao Worker, share.js)
MIN_MIN = 5
MAX_ACTIVE = 5
LABEL_MAX = 40
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
ID_RE = re.compile(r"^[0-9a-f]{8}$")
FIX_FRESH_MS = 10_000          # posição mais velha que isso não vai para o link
VALUE_FRESH_MS = 45_000        # mesma regra do painel: leitura velha não é "ao vivo"
ALERT_RECENT_MS = 10 * 60_000  # alerta "ativo" = dos últimos 10 min (como o modo direção)
TRACK_STEP_M = 20.0            # trajeto da viagem atual: um ponto a cada 20 m
TRACK_MAX = 3000


def new_token() -> tuple[str, str]:
    """(token, hash): 128 bits aleatórios em base64url; o hash é o que se guarda."""
    tok = secrets.token_urlsafe(16)
    return tok, token_hash(tok)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_label(s) -> str:
    """Nome opcional do link ("Para a Ana"): sem caracteres de controle nem < >, até 40 caracteres."""
    s = re.sub(r"[\x00-\x1f\x7f<>]", "", str(s or "")).strip()
    return s[:LABEL_MAX]


def parse_request(body: dict) -> dict:
    """{mode: "24h"|"sempre"} (ou "1h"|"4h"|"viagem"|{minutes: N}); {label?, dest?}. ValueError com a mensagem para o dono."""
    body = body or {}
    mode = str(body.get("mode") or "").lower()
    if mode == "sempre":
        return {"mode": "sempre", "minutes": None, "label": clean_label(body.get("label")), "dest": body.get("dest") is True}
    if mode in MODES:
        minutes = MODES[mode] or MAX_MIN
    elif body.get("minutes") not in (None, ""):
        try:
            minutes = int(float(body["minutes"]))
        except (TypeError, ValueError):
            raise ValueError("validade inválida") from None
        if not MIN_MIN <= minutes <= MAX_MIN:
            raise ValueError(f"validade de {MIN_MIN} min a 24 h")
        mode = "tempo"
    else:
        raise ValueError("escolha a validade: 24 horas ou até você cancelar")
    return {"mode": "viagem" if mode == "viagem" else "tempo", "minutes": min(minutes, MAX_MIN),
            "label": clean_label(body.get("label")), "dest": body.get("dest") is True}


# ---------------------------------------------------------------- links criados no Pi (/data/obd/share.json)
class LinkStore:
    """Arquivo pequeno com trava (o painel cria, o obd-sync envia e marca). Só hashes: o token não fica aqui."""

    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def edit(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            yield data
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, self.path)

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get("links"), list):
                return d
        except (OSError, ValueError):
            pass
        return {"links": []}

    def read(self) -> dict:
        return self._read()


def create_local(store: LinkStore, req: dict, now_ms: int, trip_id) -> tuple[str, dict]:
    """Cria um link no Pi (fica 'pendente' até o obd-sync enviar). Devolve (token, registro sem o token)."""
    tok, h = new_token()
    rec = {"id": h[:8], "hash": h, "created_ts": now_ms,
           "expires_ts": FOREVER_TS if req["mode"] == "sempre" else now_ms + req["minutes"] * 60_000,
           "mode": req["mode"], "label": req["label"], "dest": req["dest"],
           "trip": trip_id if req["mode"] == "viagem" else None, "state": "pendente"}
    with store.edit() as d:
        d["links"] = [x for x in d["links"] if x.get("expires_ts", 0) > now_ms - 7 * 86400_000]   # limpa os velhos
        active = [x for x in d["links"] if x.get("state") in ("pendente", "enviado") and x["expires_ts"] > now_ms and not x.get("revoke")]
        if len(active) >= MAX_ACTIVE:
            raise ValueError(f"já há {MAX_ACTIVE} links ativos; encerre um antes")
        d["links"].append(rec)
    return tok, rec


# ---------------------------------------------------------------- quadro ao vivo para o link
def _fresh(values: dict, value_ts: dict, ref: int, key: str):
    v = values.get(key)
    t = value_ts.get(key)
    if v is None or (ref and t is not None and ref - t > VALUE_FRESH_MS):
        return None
    return v


def _num(v, nd=1):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if x == x and abs(x) < 1e7 else None


def frame(live: dict, gps: dict | None, *, want_dest: bool = False, now_ms: int | None = None) -> dict:
    """Quadro do link a partir do live.json e do gps.json. Sem viagem aberta: só {ts, trip: None} (nenhuma posição).
    Nunca leva lugar atual, estacionamento, fala, rota ou histórico; o nome do destino só com want_dest."""
    live = live or {}
    ts = int(live.get("ts") or now_ms or time.time() * 1000)
    tid = live.get("trip_id")
    if not tid:
        return {"ts": ts, "trip": None}
    vals, vts = live.get("values") or {}, live.get("value_ts") or {}
    tr = live.get("trip") or {}
    out = {"ts": ts, "trip": int(tid), "on": bool(live.get("engine_on"))}
    g = gps or {}
    fix_ts = g.get("fix_ts")
    if (g.get("state") == "ok" and g.get("lat") is not None and g.get("lon") is not None and fix_ts
            and abs(ts - int(fix_ts)) <= FIX_FRESH_MS and g.get("trip_id") in (None, tid)):
        out.update(lat=round(float(g["lat"]), 5), lon=round(float(g["lon"]), 5),
                   hdg=_num(g.get("heading"), 0), gspd=_num(g.get("speed"), 1), fix_ts=int(fix_ts))
    spd = _fresh(vals, vts, ts, "speed")
    out["spd"] = _num(spd if spd is not None else out.get("gspd"), 0)
    for k, src, nd in (("rpm", "rpm", 0), ("ect", "ect", 0), ("kml", "kml_inst", 1), ("lh", "fuel_lh", 2)):
        out[k] = _num(_fresh(vals, vts, ts, src), nd)
    st = (live.get("status") or {}).get("ect")
    out["ect_st"] = st[0] if isinstance(st, (list, tuple)) and st and st[0] in ("ok", "warn", "crit") else None
    out["trip_s"] = _num(tr.get("elapsed_s"), 0)
    out["trip_km"] = _num(tr.get("km"), 1)
    out["trip_kml"] = _num(tr.get("kml"), 1)
    al = next((a for a in (live.get("alerts") or []) if a.get("sev") in ("warn", "crit")
               and ts - int(a.get("ts_end") or a.get("ts") or 0) < ALERT_RECENT_MS), None)
    out["alert"] = str(al.get("title") or "")[:60] if al else None
    out["alert_sev"] = al.get("sev") if al else None
    d = g.get("dest") or {}
    if d.get("eta_ts"):
        out["eta"] = int(d["eta_ts"])
        out["left_km"] = _num(d.get("left_km"), 1)
        if want_dest and d.get("name"):
            out["dest"] = str(d["name"])[:LABEL_MAX]
    return out


# ---------------------------------------------------------------- trajeto da viagem aberta (o link pode nascer no meio)
def track_points(con, trip_id: int, step_m: float = TRACK_STEP_M, max_n: int = TRACK_MAX) -> dict:
    """Pontos da viagem aberta (um a cada ~20 m), para o link mostrar o trajeto desde a saída. Só esta viagem."""
    row = con.execute("SELECT start_ts, end_ts FROM trip WHERE id=?", (trip_id,)).fetchone()
    if not row:
        return {"trip": trip_id, "pts": []}
    a = row["start_ts"]
    b = row["end_ts"] or int(time.time() * 1000) + 60_000
    rows = con.execute("SELECT ts, lat, lon FROM gps_fix WHERE ts BETWEEN ? AND ? AND mode >= 2 AND (trip_id IS NULL OR trip_id = ?)"
                       " ORDER BY ts", (a, b, trip_id)).fetchall()
    pts: list[list] = []
    for r in rows:
        if pts and haversine_m(pts[-1][0], pts[-1][1], r["lat"], r["lon"]) < step_m:
            continue
        pts.append([round(r["lat"], 5), round(r["lon"], 5), int(r["ts"])])
    while len(pts) > max_n:                          # metade dos pontos, mantendo a ponta atual
        last = pts[-1]
        pts = pts[::2]
        if pts[-1] is not last:
            pts.append(last)
    return {"trip": trip_id, "pts": pts}
