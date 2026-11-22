"""obd-sync: espelha o painel no Cloudflare sempre que o Pi tem internet.

Gera cada resposta da API local (http://127.0.0.1) e envia as que mudaram como documentos para o Worker.
Também busca as ações feitas no painel online (tanque cheio, anotações, editar viagem, análise) e aplica no Pi.

Config em /etc/obd/secrets.toml:
    [cloud]
    url = "https://civic-obd.<conta>.workers.dev"
    token = "<SYNC_TOKEN>"
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, config

log = logging.getLogger("sync")

LOCAL = os.environ.get("OBD_LOCAL", "http://127.0.0.1")
STATE = Path("/data/obd/sync_state.json")
LIVE_EVERY = 10          # s — snapshot ao vivo com o motor desligado
LIVE_DRIVING = 5.0       # s — com a ignição ligada: cópia no banco (o tempo real vai pelo canal ao vivo, a cada leitura)
LIVE_FILE = Path("/run/obd/live.json")
FULL_EVERY = 300         # s — reconstrução completa (só envia o que mudou); 10 min com o motor ligado
CMD_EVERY = 30           # s — ações pendentes do painel online
SPANS = [3600_000, 21600_000, 86400_000, 604800_000, 2592000_000]   # períodos dos gráficos do painel
KEEP = {
    "/api/series": ["signals", "span"], "/api/alerts": ["days", "severity"], "/api/report": ["days"],
    "/api/report/period": ["kind", "offset", "day", "trip"], "/api/summary": ["days"], "/api/throttle": ["days"],
    "/api/diagnostics": ["days"], "/api/trips": ["limit"], "/api/advice": ["rule"], "/api/ai/analysis": ["trip"],
    # GPS (docs/GPS-ARQUITETURA.md, seção 7.2): igual ao KEEP do Worker
    "/api/gps/logbook": ["limit"], "/api/gps/alerts": ["days"], "/api/gps/events": ["days"], "/api/gps/heat": ["signal", "days"],
    "/api/gps/traffic": ["days"], "/api/gps/terrain": ["days"], "/api/gps/roadmix": ["days"], "/api/gps/score": ["days"],
}
GPS_FILE = Path("/run/obd/gps.json")
SYNC_CMD = Path("/run/obd/sync_cmd")     # F8: ação "sincronizar" de um lugar (chegou em casa): sincronização completa já
SHARE_FILE = Path("/data/obd/share.json")        # F9: links criados no painel do Pi (só hashes), até serem enviados
SHARE_CLOUD = Path("/run/obd/share_cloud.json")  # F9: links ativos na nuvem (resposta do ping), para o painel do Pi listar
TRACK_KEY = "/api/gps/current_track"   # trajeto da viagem aberta (mapa do Ao vivo no painel online); documento de localização
TRACK_EVERY = 30.0       # s — pontos novos do trajeto da viagem aberta
TRACK_CHUNK = 400        # pontos por envio incremental (~20 m cada: 8 km em 30 s nem acontece)
TRACK_FULL_MAX = 3000    # pontos no envio completo (o mesmo teto de gps/share.py)
# Localização (seção 7.2): todo /api/gps/* leva posição ou nome de lugar, menos estas rotas só com números.
# Mesma regra do Worker (cloud/src/index.js isLoc). Documentos de localização só sobem se a nuvem confirmar, no ping,
# que tem a senha de localização (LOC_PASSWORD) — e o Worker ainda descarta se não tiver.
LOC_OPEN = re.compile(r"^/api/gps/(status|gears|speedo|score|perf|dyno|terrain|roadmix)$")
HEAT_SIGNALS = ("stft1", "load", "kml", "speed", "ect", "map")


def is_loc(key: str) -> bool:
    path = urllib.parse.unquote(key.split("?", 1)[0]).lower()
    path = re.sub(r"/{2,}", "/", path)
    return (path == "/api/gps" or path.startswith("/api/gps/")) and not LOC_OPEN.match(path)


def doc_key(url: str) -> str:
    """Mesma normalização do Worker (cloud/src/index.js docKey)."""
    u = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    keep = sorted(k for k in KEEP.get(u.path, []) if k in q)
    return u.path + ("?" + "&".join(f"{k}={q[k]}" for k in keep) if keep else "")


STATIC = (["/api/summary?days=14", "/api/throttle?days=30", "/api/trips?limit=1000", "/api/fuel", "/api/dtc", "/api/signals",
           "/api/diagnostics?days=60", "/api/accel", "/api/annotations", "/api/system", "/api/ai/analysis", "/api/ai/settings", "/api/repairs",
           "/api/fingerprint", "/api/impact", "/api/maintenance", "/api/ai/chat", "/api/checkup",
           "/api/alerts?days=90"] + [f"/api/alerts?days=90&severity={s}" for s in ("crit", "warn", "info")]
          + [f"/api/report?days={d}" for d in (1, 7, 30, 90, 365)]
          + [f"/api/report/period?kind=week&offset={-i}" for i in range(12)]
          + [f"/api/report/period?kind=month&offset={-i}" for i in range(12)])


class Sync:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        c = cfg.get("cloud", {})
        self.url = (c.get("url") or "").rstrip("/")
        self.token = c.get("token") or ""
        try:
            self.state = json.loads(STATE.read_text())
        except Exception:
            self.state = {}
        self.state.setdefault("hashes", {})
        self.state.setdefault("gps_live_sent", False)
        self.last_live = self.last_full = self.last_cmd = 0.0
        self.last_live_hash = None
        self._conn = None
        self._last_meta = 0.0
        self.last_ai = 0.0
        self.last_live_engine = False
        self.loc = False             # a nuvem tem senha de localização? (resposta do ping; até lá, não)
        self.last_gps_hash = None
        # F9: compartilhar a viagem ao vivo
        self.share_on = False        # a nuvem disse (na resposta do canal ao vivo) que há link ativo
        self.share_links: list = []  # links ativos na nuvem (ping): modo, se mostra o destino…
        self.share_track_trip = None # viagem cujo trajeto já foi mandado para o link
        self.share_last_trip = None  # última viagem vista no live.json (o fim dela encerra os links "até o fim da viagem")
        self.guard_alarm = False     # F9: vigia do estacionamento disparou (posição sobe a cada 5 s)
        self.tt: dict | None = None  # trajeto da viagem aberta já na nuvem: {"trip", "last" (ts do último ponto), "at"}
        self.tt_try = -1e9           # última tentativa (com ou sem sucesso): no máximo uma a cada TRACK_EVERY

    # -------------------------------------------------------------- HTTP
    def local(self, path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(LOCAL + path, data=body, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:      # painel local reiniciando: pula este documento
            log.debug("local %s indisponível: %s", path, e)
            return 0, b""

    def cloud(self, path: str, payload=None, method: str | None = None, timeout: int = 60):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                                              "User-Agent": f"civic-obd-sync/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def online(self) -> bool:
        try:
            r = self.cloud("/api/sync/ping", timeout=8)
        except Exception as e:
            log.debug("sem nuvem: %s", e)
            return False
        loc = r.get("loc") is True
        if loc != self.loc:
            log.info("localização na nuvem: %s", "liberada (senha configurada)" if loc else "desativada (sem LOC_PASSWORD): posição não sobe")
        self.loc = loc
        sh = r.get("share") if loc else None
        self.share_links = [x for x in (sh or {}).get("links") or [] if isinstance(x, dict)]
        if not loc:
            self.share_on = False
        self.write_share_cache(loc)
        return bool(r.get("ok"))

    def write_share_cache(self, loc: bool) -> None:
        """Para o painel do Pi listar os links ativos (criados aqui ou no painel online) e saber se a nuvem aceita."""
        try:
            SHARE_CLOUD.parent.mkdir(parents=True, exist_ok=True)
            tmp = SHARE_CLOUD.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": int(time.time() * 1000), "loc": loc, "links": self.share_links}, ensure_ascii=False))
            tmp.replace(SHARE_CLOUD)
        except OSError as e:
            log.debug("share_cloud.json: %s", e)

    def loc_ok(self) -> bool:
        """Enviar documentos com posição? Só se a nuvem confirmou a senha de localização e gps.cloud = "auto"."""
        return self.loc and str((self.cfg.get("gps") or {}).get("cloud", "auto")).lower() == "auto"

    def purge_loc(self) -> int:
        """Sem senha de localização na nuvem (ou gps.cloud = "off"): apaga de lá os documentos de localização que este Pi
        já tinha enviado. O Worker já não os serve sem a senha; isto tira a cópia do banco da nuvem."""
        keys = sorted(k for k in list(self.state["hashes"]) if is_loc(k))
        if self.state.get("gps_live_sent"):                  # a posição ao vivo vai pelo fio do ao vivo, fora de 'hashes'
            keys.append("/api/gps/live")
        if self.state.get("track_sent"):                     # trajeto da viagem aberta (trip_track), também fora de 'hashes'
            keys.append(TRACK_KEY)
            self.tt = None
        for i in range(0, len(keys), 50):
            self.cloud("/api/sync/docs", {"docs": [], "delete": keys[i:i + 50]})
            for k in keys[i:i + 50]:
                self.state["hashes"].pop(k, None)
        if keys:
            self.state["gps_live_sent"] = False
            self.state["track_sent"] = False
            self.last_gps_hash = None
            self.save_state()
            log.info("localização desativada na nuvem: %d documento(s) de posição apagados de lá", len(keys))
        return len(keys)

    def save_state(self) -> None:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(STATE)

    # -------------------------------------------------------------- envio
    def push(self, docs: list[tuple[str, bytes]], force: bool = False) -> int:
        """Envia só os documentos cujo conteúdo mudou desde o último envio."""
        batch, size, sent = [], 0, 0
        hashes = self.state["hashes"]
        pending_hashes = {}

        def flush():
            nonlocal batch, size, sent
            if batch:
                self.cloud("/api/sync/docs", {"docs": batch}, timeout=120)
                sent += len(batch)
                hashes.update({k: pending_hashes[k] for k in (d["key"] for d in batch)})
                batch, size = [], 0
        loc_ok = self.loc_ok()
        for key, raw in docs:
            if is_loc(key) and not loc_ok:           # falha fechada: posição só sobe com a senha confirmada pela nuvem
                continue
            h = hashlib.sha1(raw).hexdigest()
            if not force and hashes.get(key) == h:
                continue
            pending_hashes[key] = h
            batch.append({"key": key, "json": raw.decode("utf-8")})
            size += len(raw)
            if size > 700_000 or len(batch) >= 40:
                flush()
        flush()
        return sent

    def gather(self, path: str, out: list) -> dict | list | None:
        st, raw = self.local(path)
        if st != 200:
            return None
        out.append((doc_key(path), raw))
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def full(self, driving: bool = False) -> int:
        docs: list[tuple[str, bytes]] = []
        data = {p: self.gather(p, docs) for p in STATIC}
        # histórico de cada sensor nos períodos que o painel oferece (dirigindo: só os curtos, para poupar o Pi)
        spans = SPANS[:3] if driving else SPANS
        for s in data.get("/api/signals") or []:
            for span in spans:
                self.gather(f"/api/series?signals={urllib.parse.quote(s['key'])}&span={span}&max_points=600", docs)
        # detalhe de cada viagem (só reenviada se mudou) + relatório por viagem e "desde o dia" (histórico)
        trips = [t for t in (data.get("/api/trips?limit=1000") or []) if not t.get("hidden")]
        days = set()
        for t in trips:
            self.gather(f"/api/trips/{t['id']}", docs)
            self.gather(f"/api/report/period?kind=trip&trip={t['id']}", docs)
            if (t.get("duration_s") or 0) >= 300 or (t.get("distance_km") or 0) >= 1:
                self.gather(f"/api/ai/analysis?trip={t['id']}", docs)      # laudo da IA desta viagem (ou {"status":"idle"})
            if t.get("start_ts") and t["start_ts"] > time.time() * 1000 - 120 * 86400_000:
                days.add(time.strftime("%Y-%m-%d", time.localtime(t["start_ts"] / 1000)))
        for d in sorted(days):
            self.gather(f"/api/report/period?day={d}&kind=since", docs)
        # recomendações de todos os alertas e estados que aparecem
        rules = {a["rule"] for a in (data.get("/api/alerts?days=90") or []) if a.get("rule")}
        st, raw = self.local("/api/live")
        if st == 200:
            for v in (json.loads(raw).get("status") or {}).values():
                if len(v) > 3 and v[3]:
                    rules.add(v[3])
        rules |= {"z", "missing", "R9"}                   # orientações gerais das famílias de alerta (plano B do painel)
        for r in sorted(rules):
            self.gather(f"/api/advice?rule={urllib.parse.quote(r)}", docs)
        self.gather_gps_open(docs)
        self.gather_gps_perf(docs)
        self.gather_gps_roads(docs)
        self.gather_gps(docs)
        n = self.push(docs)
        return n

    def gather_gps_open(self, docs: list) -> None:
        """F5: documentos do GPS só com números (marchas, velocímetro, nota de condução). Abertos na nuvem (LOC_OPEN):
        sobem sempre, como o /api/gps/status."""
        for p in ("/api/gps/gears", "/api/gps/speedo", "/api/gps/score?days=90"):
            self.gather(p, docs)

    def gather_gps_perf(self, docs: list) -> None:
        """F6: 0–100 e retomadas, dinamômetro, inclinação e altitude. Só números (abertos na nuvem, LOC_OPEN): sobem
        sempre, como os da F5."""
        for p in ("/api/gps/perf", "/api/gps/dyno", "/api/gps/terrain?days=90"):
            self.gather(p, docs)

    def gather_gps_roads(self, docs: list) -> None:
        """F7: cidade × estrada. Só números (aberto na nuvem, LOC_OPEN): sobe sempre. Os radares e o geo.db ficam no Pi."""
        self.gather("/api/gps/roadmix?days=30", docs)

    def gather_gps(self, docs: list) -> None:
        """Documentos do GPS (seção 4.6). Sem posição: sempre. Com posição: só com a senha de localização na nuvem
        (as rotas das fases seguintes entram aqui quando existirem; até lá o painel local responderia 404)."""
        self.gather("/api/gps/status", docs)
        if not self.loc_ok():
            return
        lb = self.gather("/api/gps/logbook?limit=1000", docs) or {}
        for p in ("/api/gps/places", "/api/gps/parked", "/api/gps/alerts?days=90", "/api/gps/events?days=30"):
            self.gather(p, docs)
        for s in HEAT_SIGNALS:
            self.gather(f"/api/gps/heat?signal={s}&days=30", docs)
        for t in lb.get("items") or []:
            self.gather(f"/api/gps/trips/{t['id']}", docs)
            if t.get("has_track"):
                self.gather(f"/api/gps/trips/{t['id']}/track", docs)
        # F4: rotas e trechos de referência (levam nomes de lugares e posições dos portões)
        for x in (self.gather("/api/gps/routes", docs) or {}).get("routes") or []:
            self.gather(f"/api/gps/routes/{x['id']}", docs)
        for x in (self.gather("/api/gps/segments", docs) or {}).get("segments") or []:
            self.gather(f"/api/gps/segments/{x['id']}", docs)
        # F8: postos (nomes e posições) e trânsito por rota (nomes de lugares, pontos onde o carro fica parado)
        for p in ("/api/gps/stations", "/api/gps/traffic?days=60"):
            self.gather(p, docs)

    def live_raw(self) -> bytes | None:
        try:
            return LIVE_FILE.read_bytes()          # direto do coletor: não depende do painel local
        except OSError:
            st, raw = self.local("/api/live")
            return raw if st == 200 else None

    def cloud_post(self, path: str, body: bytes, gz: bool = False) -> dict:
        """POST numa conexão HTTPS mantida aberta (vários envios por segundo sem refazer o TLS a cada vez)."""
        import http.client
        u = urllib.parse.urlsplit(self.url)
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "User-Agent": f"civic-obd-sync/{__version__}"}
        if gz:
            import gzip
            body = gzip.compress(body, 1)              # ~4 kB → ~1 kB: poupa o plano de dados do hotspot
            headers["Content-Encoding"] = "gzip"
        for attempt in (0, 1):
            try:
                if self._conn is None:
                    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
                    self._conn = cls(u.netloc, timeout=15)
                self._conn.request("POST", path, body=body, headers=headers)
                r = self._conn.getresponse()
                data = r.read()
                if r.status >= 400:
                    raise OSError(f"nuvem respondeu {r.status}")
                try:
                    out = json.loads(data or b"{}")
                except ValueError:
                    out = {}
                return out if isinstance(out, dict) else {}
            except (OSError, http.client.HTTPException):
                try:
                    self._conn and self._conn.close()
                finally:
                    self._conn = None
                if attempt:
                    raise

    def gps_live_raw(self) -> bytes | None:
        """/api/gps/live como o painel do Pi responde (gps.json sem o bloco de estado, com a idade). Privado."""
        try:
            g = json.loads(GPS_FILE.read_text())
        except (OSError, ValueError):
            return None
        g.pop("status", None)
        g["age_s"] = round(time.time() - g["ts"] / 1000, 1) if g.get("ts") else None
        self.guard_alarm = (g.get("guard") or {}).get("state") == "alarme"
        return json.dumps(g, ensure_ascii=False).encode()

    def live(self, raw: bytes) -> None:
        """Cópia do estado no banco da nuvem (quem abre o painel depois, carro desligado, navegador sem WebSocket).
        Com a senha de localização confirmada, a posição atual (/api/gps/live) vai no mesmo pedido."""
        now = time.monotonic()
        docs = []
        if now - self._last_meta >= LIVE_EVERY:      # "o Pi está vivo": basta a cada 10 s
            docs.append(("_meta", json.dumps({"last_sync_ts": int(time.time() * 1000), "version": __version__}).encode()))
        h = hashlib.sha1(raw).hexdigest()
        if h != self.last_live_hash:
            docs.append(("/api/live", raw))
        gh = None
        if self.loc_ok():
            g = self.gps_live_raw()
            gh = hashlib.sha1(g).hexdigest() if g else None
            if g and gh != self.last_gps_hash:
                docs.append(("/api/gps/live", g))
        if not docs:
            return
        self.cloud_post("/api/sync/docs", json.dumps({"docs": [{"key": k, "json": v.decode()} for k, v in docs]}, ensure_ascii=False).encode())
        self.last_live_hash = h
        if gh:
            self.last_gps_hash = gh
            self.state["gps_live_sent"] = True
        if docs[0][0] == "_meta":
            self._last_meta = now

    def live_loop(self) -> None:
        """Fio próprio: o estado ao vivo não espera a sincronização completa (que pode levar dezenas de segundos).
        Com a ignição ligada, cada leitura nova do coletor vai na hora para o canal ao vivo (WebSocket dos painéis
        abertos); a cópia no banco segue a cada LIVE_DRIVING segundos."""
        last_m, last_doc, active = None, 0.0, False
        while True:
            try:
                if not (self.url and self.token):
                    time.sleep(30)
                    continue
                try:
                    m = LIVE_FILE.stat().st_mtime_ns
                except OSError:
                    m = None
                now = time.monotonic()
                fresh = m is not None and m != last_m
                due = now - last_doc >= (LIVE_DRIVING if active or self.guard_alarm else LIVE_EVERY)   # F9: vigia = 5 s
                if fresh or due:
                    raw = self.live_raw()
                    if raw:
                        try:
                            d = json.loads(raw)
                            self.last_live_engine = bool(d.get("engine_on"))
                            active = d.get("state") == "coletando"       # ignição ligada (motor ligado ou não)
                        except ValueError:
                            raw = None
                    if raw:
                        self.share_trip_watch(d)
                        self.trip_track(d)
                    if raw and fresh and active:
                        last_m = m
                        resp = self.cloud_post("/api/sync/live", self.live_body(raw, d), gz=True)
                        self.share_on = bool(resp.get("share")) and self.loc_ok()
                        if self.share_on:
                            self.share_track(d)
                        else:
                            self.share_track_trip = None      # link novo mais tarde na mesma viagem: manda o trajeto de novo
                    if raw and due:
                        last_doc = now
                        self.live(raw)
                time.sleep(0.2 if active else 2)
            except Exception as e:
                log.debug("ao vivo falhou: %s", e)
                time.sleep(5)

    # -------------------------------------------------------------- F9: compartilhar a viagem ao vivo (seção 9.21)
    def live_body(self, raw: bytes, d: dict | None) -> bytes:
        """Leitura ao vivo para o canal da nuvem. Com link ativo (e só com a senha de localização confirmada), leva junto
        o quadro do link em "_share" (posição e números da viagem atual); o Worker tira antes de mandar ao painel."""
        if not (self.share_on and self.loc_ok() and isinstance(d, dict)):
            return raw
        from .gps import share as sharemod
        try:
            g = json.loads(GPS_FILE.read_text())
        except (OSError, ValueError):
            g = None
        fr = sharemod.frame(d, g, want_dest=any(x.get("dest") for x in self.share_links))
        return json.dumps({**d, "_share": fr}, ensure_ascii=False).encode()

    def share_track(self, d: dict) -> None:
        """Link ativo e viagem nova para ele: manda o trajeto desta viagem desde a saída (uma vez por viagem)."""
        tid = (d or {}).get("trip_id")
        if not tid or tid == self.share_track_trip:
            return
        self.share_track_trip = tid
        st, raw = self.local(f"/api/gps/share/track?trip={int(tid)}")
        if st != 200:
            return
        try:
            trk = json.loads(raw)
            self.cloud("/api/sync/share", {"track": {"trip": int(tid), "pts": trk.get("pts") or []}}, timeout=20)
        except Exception as e:
            log.debug("trajeto do link: %s", e)

    def trip_track(self, d: dict | None, now: float | None = None) -> str | None:
        """Trajeto da viagem aberta para o mapa do Ao vivo no painel online (documento TRACK_KEY, atrás da senha de
        localização). Na viagem nova vai o trajeto desde a saída (uma vez, como o do link da F9); depois, a cada ~30 s,
        só os pontos novos (a nuvem acrescenta). Fim da viagem: a nuvem apaga o documento. Só com a senha confirmada.
        Devolve o que fez ("full", "append", "end"), para os testes."""
        now = time.monotonic() if now is None else now
        if not self.loc_ok():
            self.tt = None                                   # purge_loc apaga a cópia de lá
            return None
        tid = d.get("trip_id") if isinstance(d, dict) else None
        tt = self.tt
        if tid is None:
            if not tt:
                return None
            if now - self.tt_try < 5:
                return None
            self.tt_try = now
            try:
                self.cloud("/api/sync/track", {"end": int(tt["trip"])}, timeout=15)
            except Exception as e:
                log.debug("fim do trajeto da viagem: %s", e)
                return None
            self.tt = None
            return "end"
        same = bool(tt and tt["trip"] == tid)
        if now - self.tt_try < (TRACK_EVERY if same else 5):
            return None
        self.tt_try = now
        st, raw = self.local(f"/api/gps/share/track?trip={int(tid)}")
        if st != 200:
            return None
        try:
            j = json.loads(raw)
            pts = [p for p in (j.get("pts") or []) if isinstance(p, list) and len(p) >= 3]
            if same:
                new = [p for p in pts if p[2] > tt["last"]][:TRACK_CHUNK]
                if not new:
                    tt["at"] = now
                    return None
                r = self.cloud("/api/sync/track", {"trip": int(tid), "append": new}, timeout=20)
                if r.get("reset"):                           # a nuvem perdeu o documento: manda tudo de novo
                    self.tt = None
                    self.tt_try = -1e9
                    return None
                tt.update(last=new[-1][2], at=now)
                return "append"
            if len(pts) > TRACK_FULL_MAX:                    # rareia mantendo a saída e a ponta atual
                k = -(-len(pts) // (TRACK_FULL_MAX - 1))
                pts = pts[:-1:k] + [pts[-1]]
            self.cloud("/api/sync/track", {"trip": int(tid), "start_ts": j.get("start_ts"), "pts": pts}, timeout=20)
            self.tt = {"trip": tid, "last": pts[-1][2] if pts else 0, "at": now}
            self.state["track_sent"] = True
            return "full"
        except Exception as e:
            log.debug("trajeto da viagem aberta: %s", e)
            return None

    def share_trip_watch(self, d: dict | None) -> None:
        """A viagem acabou (o live.json trocou de viagem ou ficou sem): avisa a nuvem, que encerra os links "até o fim
        da viagem" e apaga a posição do link. Com o Pi desligando junto com o carro, a nuvem encerra sozinha (15 min)."""
        tid = (d or {}).get("trip_id") if isinstance(d, dict) else None
        prev, self.share_last_trip = self.share_last_trip, tid
        if prev and prev != tid and self.loc_ok() and (self.share_on or self.share_links):
            try:
                self.cloud("/api/sync/share", {"trip_end": int(prev)}, timeout=15)
                log.info("fim da viagem %s avisado ao link de compartilhamento", prev)
            except Exception as e:
                log.debug("fim da viagem para o link: %s", e)
            self.share_track_trip = None

    def share_sync(self) -> int:
        """Envia os links criados no painel do Pi e os pedidos de encerrar. Só com a senha de localização na nuvem."""
        from .gps import share as sharemod
        store = sharemod.LinkStore(SHARE_FILE)
        data = store.read()
        now = int(time.time() * 1000)
        creates = [x for x in data["links"] if x.get("state") == "pendente" and not x.get("revoke") and x.get("expires_ts", 0) > now]
        revokes = [x["id"] for x in data["links"] if x.get("revoke") and x.get("state") not in ("encerrado",)]
        if not creates and not revokes:
            return 0
        if not self.loc_ok():
            return 0                                 # espera a senha de localização (o painel do Pi avisa)
        r = self.cloud("/api/sync/share", {"create": [{k: x.get(k) for k in ("hash", "created_ts", "expires_ts", "mode", "label", "dest", "trip")}
                                                      for x in creates], "revoke": revokes}, timeout=20)
        made, refused = set(r.get("created") or []), set(r.get("refused") or [])
        with store.edit() as d:
            for x in d["links"]:
                if x.get("id") in made and x.get("state") == "pendente":
                    x["state"] = "enviado"
                elif x.get("id") in refused and x.get("state") == "pendente":
                    x.update(state="recusado", error=r.get("error") or "recusado pela nuvem")
                if x.get("id") in revokes:
                    x["state"] = "encerrado"
        if isinstance(r.get("links"), list):
            self.share_links = [x for x in r["links"] if isinstance(x, dict) and x.get("state") in ("ativo", "aguardando a viagem")]
            self.write_share_cache(True)
        log.info("links de compartilhamento: %d enviado(s), %d encerrado(s)", len(made), len(revokes))
        return len(made) + len(revokes)

    # -------------------------------------------------------------- ações do painel online
    def commands(self) -> int:
        cap = "total,maint,gps" if self.loc_ok() else "total,maint"     # ações com posição (lugares…) só com a localização liberada
        cmds = self.cloud(f"/api/sync/commands?cap={cap}").get("commands", [])
        done, results = [], {}
        for c in cmds:
            body = c.get("body") or "{}"
            try:
                b = json.loads(body)
            except ValueError:
                b = {}
            if c["path"] in ("/api/fuel/fill", "/api/annotations") and not b.get("ts"):
                b["ts"] = c["ts"]           # vale a hora em que foi feito no celular
            st, raw = self.local(c["path"], c.get("method", "POST"), json.dumps(b).encode())
            results[c["id"]] = {"status": st, "response": raw[:500].decode("utf-8", "replace")}
            done.append(c["id"])
            log.info("ação do painel online aplicada: %s %s → %s", c.get("method"), c["path"], st)
        if done:
            self.cloud("/api/sync/commands", {"ids": done, "results": results})
            self.last_full = 0          # reflete o resultado no próximo ciclo
        return len(done)

    def purge_after_reset(self) -> None:
        """Depois de um "Resetar tudo" no Pi, apaga da nuvem tudo o que este Pi já enviou (os ids das viagens recomeçam
        do 1: sem isso, o laudo de uma viagem antiga apareceria numa nova) e reenvia do zero."""
        from . import reset
        ts = reset.last(self.cfg)
        if not ts or self.state.get("reset_ts") == ts:
            return
        keys = sorted(self.state["hashes"])
        for i in range(0, len(keys), 50):
            self.cloud("/api/sync/docs", {"docs": [], "delete": keys[i:i + 50]})
        self.state["hashes"] = {}
        self.state["reset_ts"] = ts
        self.save_state()
        self.last_full = 0
        log.info("reset no Pi: %d documento(s) antigos apagados da nuvem", len(keys))

    def sync_requested(self) -> bool:
        """F8: o coletor cria /run/obd/sync_cmd quando um lugar com a ação "sincronizar" dispara. Consumido aqui (só
        com a nuvem respondendo: sem internet o pedido espera)."""
        try:
            if not SYNC_CMD.exists():
                return False
            SYNC_CMD.unlink()
        except OSError:
            return False
        log.info("sincronização completa pedida por um lugar (ação sincronizar)")
        return True

    # -------------------------------------------------------------- laço
    def run(self) -> None:
        if not self.url or not self.token:
            log.error("nuvem não configurada ([cloud] url/token em /etc/obd/secrets.toml); aguardando")
        import threading
        threading.Thread(target=self.live_loop, daemon=True, name="live").start()
        while True:
            try:
                if not self.url or not self.token:
                    time.sleep(300)
                    cfg = config.load()
                    self.url, self.token = (cfg.get("cloud", {}).get("url") or "").rstrip("/"), cfg.get("cloud", {}).get("token") or ""
                    continue
                if not self.online():
                    time.sleep(30)
                    continue
                self.purge_after_reset()
                if not self.loc_ok():
                    self.purge_loc()
                try:
                    self.share_sync()                    # F9: links criados no painel do Pi e pedidos de encerrar
                except Exception as e:
                    log.warning("links de compartilhamento: %s", e)
                if self.sync_requested():                # lugar com a ação "sincronizar" (ex.: chegou em casa, no WiFi)
                    self.last_cmd = self.last_full = 0
                now = time.monotonic()
                if now - self.last_cmd >= CMD_EVERY:
                    self.last_cmd = now
                    self.commands()
                if now - self.last_ai >= 120:           # com internet: laudo da IA da última viagem terminada, se faltar
                    self.last_ai = now
                    st, raw = self.local("/api/ai/auto", "POST", b"{}")
                    if st == 200 and json.loads(raw or b"{}").get("started"):
                        log.info("laudo da IA pedido para a viagem %s", json.loads(raw).get("trip"))
                        self.last_full = now - FULL_EVERY + 150                    # sobe o laudo assim que ficar pronto
                driving = bool(self.last_live_engine)
                if now - self.last_full >= (FULL_EVERY * 2 if driving else FULL_EVERY):
                    t0 = time.monotonic()
                    n = self.full(driving)
                    self.last_full = time.monotonic()
                    self.save_state()
                    log.info("sincronização completa: %d documento(s) novos/alterados em %.1f s", n, self.last_full - t0)
                time.sleep(LIVE_EVERY)
            except Exception as e:
                log.warning("sincronização falhou: %s", e)
                time.sleep(30)


def main() -> None:
    logging.basicConfig(level=os.environ.get("OBD_LOG", "INFO"), stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Sync(config.load()).run()


if __name__ == "__main__":
    main()
