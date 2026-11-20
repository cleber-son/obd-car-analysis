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
}


def doc_key(url: str) -> str:
    """Mesma normalização do Worker (cloud/src/index.js docKey)."""
    u = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    keep = sorted(k for k in KEEP.get(u.path, []) if k in q)
    return u.path + ("?" + "&".join(f"{k}={q[k]}" for k in keep) if keep else "")


STATIC = (["/api/summary?days=14", "/api/throttle?days=30", "/api/trips?limit=1000", "/api/fuel", "/api/dtc", "/api/signals",
           "/api/diagnostics?days=60", "/api/accel", "/api/annotations", "/api/system", "/api/ai/analysis", "/api/ai/settings", "/api/repairs",
           "/api/fingerprint", "/api/impact", "/api/maintenance", "/api/ai/chat",
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
        self.last_live = self.last_full = self.last_cmd = 0.0
        self.last_live_hash = None
        self._conn = None
        self._last_meta = 0.0
        self.last_ai = 0.0
        self.last_live_engine = False

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
            return bool(self.cloud("/api/sync/ping", timeout=8).get("ok"))
        except Exception as e:
            log.debug("sem nuvem: %s", e)
            return False

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
        for key, raw in docs:
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
        n = self.push(docs)
        return n

    def live_raw(self) -> bytes | None:
        try:
            return LIVE_FILE.read_bytes()          # direto do coletor: não depende do painel local
        except OSError:
            st, raw = self.local("/api/live")
            return raw if st == 200 else None

    def cloud_post(self, path: str, body: bytes, gz: bool = False) -> None:
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
                r.read()
                if r.status >= 400:
                    raise OSError(f"nuvem respondeu {r.status}")
                return
            except (OSError, http.client.HTTPException):
                try:
                    self._conn and self._conn.close()
                finally:
                    self._conn = None
                if attempt:
                    raise

    def live(self, raw: bytes) -> None:
        """Cópia do estado no banco da nuvem (quem abre o painel depois, carro desligado, navegador sem WebSocket)."""
        now = time.monotonic()
        docs = []
        if now - self._last_meta >= LIVE_EVERY:      # "o Pi está vivo": basta a cada 10 s
            docs.append(("_meta", json.dumps({"last_sync_ts": int(time.time() * 1000), "version": __version__}).encode()))
        h = hashlib.sha1(raw).hexdigest()
        if h != self.last_live_hash:
            docs.append(("/api/live", raw))
        if not docs:
            return
        self.cloud_post("/api/sync/docs", json.dumps({"docs": [{"key": k, "json": v.decode()} for k, v in docs]}, ensure_ascii=False).encode())
        self.last_live_hash = h
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
                due = now - last_doc >= (LIVE_DRIVING if active else LIVE_EVERY)
                if fresh or due:
                    raw = self.live_raw()
                    if raw:
                        try:
                            d = json.loads(raw)
                            self.last_live_engine = bool(d.get("engine_on"))
                            active = d.get("state") == "coletando"       # ignição ligada (motor ligado ou não)
                        except ValueError:
                            raw = None
                    if raw and fresh and active:
                        last_m = m
                        self.cloud_post("/api/sync/live", raw, gz=True)
                    if raw and due:
                        last_doc = now
                        self.live(raw)
                time.sleep(0.2 if active else 2)
            except Exception as e:
                log.debug("ao vivo falhou: %s", e)
                time.sleep(5)

    # -------------------------------------------------------------- ações do painel online
    def commands(self) -> int:
        cmds = self.cloud("/api/sync/commands?cap=total,maint").get("commands", [])
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
