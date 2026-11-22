"""Notificações push via ntfy (app ntfy no iPhone). Fila 'outbox' no SQLite, enviada quando há internet."""
from __future__ import annotations

import logging
import sqlite3
import time
import urllib.request

log = logging.getLogger("notify")

PRIO = {"crit": 5, "warn": 4, "info": 3}
TAGS = {"crit": "rotating_light", "warn": "warning", "info": "information_source"}


def send(server: str, topic: str, title: str, body: str, priority: int = 3, tags: str = "", click: str = "") -> bool:
    if not topic:
        return False
    req = urllib.request.Request(f"{server.rstrip('/')}/{topic}", data=body.encode("utf-8"), method="POST")
    # cabeçalhos HTTP precisam ser latin-1; o ntfy aceita RFC 2047 para UTF-8
    import base64
    req.add_header("Title", "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=")
    req.add_header("Priority", str(priority))
    if tags:
        req.add_header("Tags", tags)
    if click:
        req.add_header("Click", click)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log.debug("ntfy falhou: %s", e)
        return False


def flush_outbox(db_path: str, server: str, topic: str, click: str = "") -> int:
    if not topic:
        return 0
    con = sqlite3.connect(db_path, timeout=10)
    sent = 0
    try:
        try:
            rows = con.execute("SELECT id, title, body, priority, tags, expire_ts FROM outbox WHERE sent_ts IS NULL ORDER BY id LIMIT 20").fetchall()
        except sqlite3.OperationalError:            # banco antigo, sem a coluna expire_ts
            rows = [(*r, None) for r in con.execute("SELECT id, title, body, priority, tags FROM outbox WHERE sent_ts IS NULL ORDER BY id LIMIT 20")]
        for rid, title, body, prio, tags, expire in rows:
            now = int(time.time() * 1000)
            if expire and expire < now:             # perdeu o sentido ("chegou em casa" horas depois): vencido, não envia
                con.execute("UPDATE outbox SET sent_ts=? WHERE id=?", (-now, rid))    # sent_ts negativo = vencido
                con.commit()
                continue
            if not send(server, topic, title, body, prio or 3, tags or "", click):
                break
            con.execute("UPDATE outbox SET sent_ts=? WHERE id=?", (int(time.time() * 1000), rid))
            con.commit()
            sent += 1
    finally:
        con.close()
    return sent
