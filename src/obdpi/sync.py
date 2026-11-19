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
LIVE_EVERY = 10          # s — snapshot ao vivo
FULL_EVERY = 300         # s — reconstrução completa (só envia o que mudou); 10 min com o motor ligado
CMD_EVERY = 30           # s — ações pendentes do painel online
SPANS = [3600_000, 21600_000, 86400_000, 604800_000, 2592000_000]   # períodos dos gráficos do painel
KEEP = {
    "/api/series": ["signals", "span"], "/api/alerts": ["days", "severity"], "/api/report": ["days"],
    "/api/report/period": ["kind", "offset"], "/api/summary": ["days"], "/api/throttle": ["days"],
    "/api/diagnostics": ["days"], "/api/trips": ["limit"], "/api/advice": ["rule"],
}


def doc_key(url: str) -> str:
    """Mesma normalização do Worker (cloud/src/index.js docKey)."""
    u = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    keep = sorted(k for k in KEEP.get(u.path, []) if k in q)
    return u.path + ("?" + "&".join(f"{k}={q[k]}" for k in keep) if keep else "")


STATIC = (["/api/summary?days=14", "/api/throttle?days=30", "/api/trips?limit=1000", "/api/fuel", "/api/dtc", "/api/signals",
           "/api/diagnostics?days=60", "/api/accel", "/api/annotations", "/api/system", "/api/ai/analysis", "/api/ai/settings",
           "/api/alerts?days=90"] + [f"/api/alerts?days=90&severity={s}" for s in ("crit", "warn", "info")]
          + [f"/api/report?days={d}" for d in (1, 7, 30, 90, 365)]
          + [f"/api/report/period?kind=week&offset={-i}" for i in range(12)]
          + [f"/api/report/period?kind=month&offset={-i}" for i in range(12)])


class Sync:
    def __init__(self, cfg: dict):
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
        # detalhe de cada viagem (só reenviada se mudou)
        for t in data.get("/api/trips?limit=1000") or []:
            self.gather(f"/api/trips/{t['id']}", docs)
        # recomendações de todos os alertas e estados que aparecem
        rules = {a["rule"] for a in (data.get("/api/alerts?days=90") or []) if a.get("rule")}
        st, raw = self.local("/api/live")
        if st == 200:
            for v in (json.loads(raw).get("status") or {}).values():
                if len(v) > 3 and v[3]:
                    rules.add(v[3])
        for r in sorted(rules):
            self.gather(f"/api/advice?rule={urllib.parse.quote(r)}", docs)
        n = self.push(docs)
        return n

    def live(self) -> None:
        st, raw = self.local("/api/live")
        if st != 200:
            return
        meta = json.dumps({"last_sync_ts": int(time.time() * 1000), "version": __version__}).encode()
        h = hashlib.sha1(raw).hexdigest()
        try:
            self.last_live_engine = bool(json.loads(raw).get("engine_on"))
        except ValueError:
            pass
        docs = [("_meta", meta)]
        if h != self.last_live_hash:
            docs.append(("/api/live", raw))
        self.cloud("/api/sync/docs", {"docs": [{"key": k, "json": v.decode()} for k, v in docs]}, timeout=30)
        self.last_live_hash = h

    # -------------------------------------------------------------- ações do painel online
    def commands(self) -> int:
        cmds = self.cloud("/api/sync/commands").get("commands", [])
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

    # -------------------------------------------------------------- laço
    def run(self) -> None:
        if not self.url or not self.token:
            log.error("nuvem não configurada ([cloud] url/token em /etc/obd/secrets.toml); aguardando")
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
                now = time.monotonic()
                if now - self.last_live >= LIVE_EVERY:
                    self.last_live = now
                    self.live()
                if now - self.last_cmd >= CMD_EVERY:
                    self.last_cmd = now
                    self.commands()
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
