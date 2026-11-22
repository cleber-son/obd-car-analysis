"""Diagnóstico geral: guarda o relatório montado no painel (static/checkup.js), que junta as respostas do dono, as
medições ao vivo e o histórico do carro. As regras do veredito rodam no navegador (o painel online não tem Python);
aqui ficam a validação do que chega, o armazenamento, o texto para o diário e o pedido de segunda opinião à IA.

O relatório chega de fora (no painel online, de qualquer visitante): tudo é cortado, tipado e limitado antes de gravar."""
from __future__ import annotations

import json
import re
import sqlite3
import time

KEEP = 20                                   # relatórios guardados
ROTULOS = 4                                 # níveis: 0 tudo certo, 1 pode rodar, 2 agende, 3 pare
URG = ["Informação", "Pode rodar", "Agende", "Pare"]
ACOES = {"trocar", "verificar", "fazer"}
CONF = {"alta", "média", "baixa"}
_TAG = re.compile(r"<(?!/?b>)[^>]*>")       # só <b> sobrevive; o resto do HTML sai


def _s(v, n: int) -> str:
    return _TAG.sub("", str(v if v is not None else "")).replace("\x00", "").strip()[:n]


def _list(v, n: int, size: int) -> list[str]:
    return [x for x in (_s(i, size) for i in (v if isinstance(v, list) else [])[:n]) if x]


def _measures(m, depth: int = 0) -> dict:
    out = {}
    for k, v in list(m.items())[:60] if isinstance(m, dict) else []:
        k = _s(k, 24)
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)) and v == v and abs(v) < 1e7:
            out[k] = round(v, 3)
        elif isinstance(v, list):
            out[k] = [_s(i, 12) for i in v[:12]]
        elif isinstance(v, dict) and depth == 0:
            out[k] = _measures(v, 1)
    return out


def clean(rep: dict, now_ms: int | None = None) -> dict:
    """Devolve o relatório só com os campos esperados, dentro dos limites. ValueError se não for um relatório."""
    if not isinstance(rep, dict) or not isinstance(rep.get("achados"), list) or not _s(rep.get("rotulo"), 80):
        raise ValueError("relatório inválido")
    now = now_ms or int(time.time() * 1000)
    try:
        ts = int(rep.get("ts") or now)
    except (TypeError, ValueError):
        ts = now
    if not 1_500_000_000_000 < ts <= now + 86_400_000:
        ts = now
    achados = []
    for a in rep["achados"][:40]:
        if not isinstance(a, dict) or not _s(a.get("t"), 200):
            continue
        try:
            sev = max(0, min(ROTULOS - 1, int(a.get("sev") or 0)))
        except (TypeError, ValueError):
            sev = 0
        achados.append({"id": _s(a.get("id"), 40), "sis": _s(a.get("sis"), 40), "sev": sev, "urg": URG[sev],
                        "conf": a.get("conf") if a.get("conf") in CONF else "baixa",
                        "acao": a.get("acao") if a.get("acao") in ACOES else "verificar",
                        "t": _s(a.get("t"), 200), "pq": _s(a.get("pq"), 600), "ev": _list(a.get("ev"), 6, 300),
                        "fz": _s(a.get("fz"), 500), "teste": _s(a.get("teste"), 12) or None})
    nivel = max((a["sev"] for a in achados), default=0)      # o nível é sempre o do achado mais urgente, não o que veio escrito
    out = {"v": 1, "ts": ts, "nivel": nivel, "rotulo": _s(rep.get("rotulo"), 80), "resumo": _s(rep.get("resumo"), 500),
           "parcial": bool(rep.get("parcial")), "achados": achados, "bons": _list(rep.get("bons"), 30, 200),
           "pulados": _list(rep.get("pulados"), 30, 200), "obs": _list(rep.get("obs"), 6, 400), "hist": _list(rep.get("hist"), 12, 400),
           "m": _measures(rep.get("m") or {})}
    out["mecanico"] = to_text(out)
    return out


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def to_text(rep: dict) -> str:
    """Relatório em texto corrido: o que vai para o mecânico e para a IA. Montado aqui, nunca aceito pronto de fora."""
    d = time.strftime("%d/%m/%Y %H:%M", time.localtime(rep["ts"] / 1000))
    rot = re.sub(r"^\W+", "", _plain(rep["rotulo"]))
    out = [f"DIAGNÓSTICO GERAL · Honda Civic 2008 1.8 flex (R18A), manual · {d}",
           "Feito pelo dono com o Delfos (leitor OBD do carro): respostas dele + medições ao vivo. Os achados são hipóteses a confirmar.",
           "", f"RESULTADO: {rot}. {_plain(rep['resumo'])}"]
    for title, acao in (("PROVÁVEL TROCA", "trocar"), ("VERIFICAR / INSPECIONAR", "verificar"), ("SERVIÇO OU AJUSTE", "fazer")):
        xs = [a for a in rep["achados"] if a["acao"] == acao]
        if not xs:
            continue
        out += ["", title + ":"]
        for a in xs:
            out.append(f"• [{a['urg']}] {_plain(a['t'])} ({a['sis']}; confiança {a['conf']})")
            out += [f"    - {_plain(e)}" for e in a["ev"]]
            if a["fz"]:
                out.append(f"    → {_plain(a['fz'])}")
    for title, key in (("SEM PROBLEMA NO QUE FOI AVALIADO", "bons"), ("HISTÓRICO RECENTE DO CARRO", "hist"), ("OBSERVAÇÕES", "obs"), ("NÃO AVALIADO", "pulados")):
        if rep.get(key):
            out += ["", title + ":"] + ["• " + _plain(x) for x in rep[key]]
    return "\n".join(out)


def annotation_text(rep: dict) -> str:
    """Uma linha para o diário de manutenção (entra no contexto da IA e na página da oficina)."""
    n = len(rep["achados"])
    head = f"Diagnóstico geral: {str(n) + ' ponto(s) de atenção' if n else 'tudo certo'}"
    tops = "; ".join(f"{_plain(a['t'])} ({a['urg'].lower()})" for a in rep["achados"][:5])
    return (head + (". " + tops if tops else ". " + "; ".join(rep["bons"][:3])))[:480]


def _table(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS checkup (id INTEGER PRIMARY KEY, ts INTEGER UNIQUE, nivel INTEGER, n INTEGER, json TEXT)")


def save(con: sqlite3.Connection, rep: dict, now_ms: int | None = None) -> dict:
    """Valida e grava. O mesmo relatório (mesmo ts) enviado duas vezes não duplica nem repete a linha no diário."""
    rep = clean(rep, now_ms)
    _table(con)
    if con.execute("SELECT 1 FROM checkup WHERE ts=?", (rep["ts"],)).fetchone():
        return {"ok": True, "duplicate": True, "ts": rep["ts"]}
    con.execute("INSERT INTO checkup(ts, nivel, n, json) VALUES (?,?,?,?)",
                (rep["ts"], rep["nivel"], len(rep["achados"]), json.dumps(rep, ensure_ascii=False)))
    con.execute("DELETE FROM checkup WHERE id NOT IN (SELECT id FROM checkup ORDER BY ts DESC LIMIT ?)", (KEEP,))
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (rep["ts"], "teste", annotation_text(rep)))
    con.commit()
    return {"ok": True, "ts": rep["ts"], "nivel": rep["nivel"]}


def latest(con: sqlite3.Connection) -> dict:
    """{"last": relatório mais recente ou None, "history": [{ts, nivel, n}]} — o que a página da oficina e o painel leem."""
    try:
        rows = con.execute("SELECT ts, nivel, n, json FROM checkup ORDER BY ts DESC LIMIT ?", (KEEP,)).fetchall()
    except sqlite3.Error:                       # tabela ainda não criada: nenhum diagnóstico feito
        return {"last": None, "history": []}
    last = None
    if rows:
        try:
            last = json.loads(rows[0][3])
        except ValueError:
            last = None
    return {"last": last, "history": [{"ts": r[0], "nivel": r[1], "n": r[2]} for r in rows]}


AI_ASK = """O dono fez agora o "Diagnóstico geral" guiado pelo painel: respondeu perguntas, olhou níveis e pneus e o painel mediu o carro
ao vivo pelo OBD. Abaixo está o relatório que as regras fixas do painel montaram, com as evidências.

Dê uma segunda opinião de mecânico, em português simples, para quem não é mecânico:
1. Você concorda com a ordem de prioridade? Se não, qual seria a sua e por quê.
2. Há alguma causa única que explique vários achados ao mesmo tempo?
3. Algum achado parece falso alarme diante dos dados gravados do carro (JSON acima)? Diga qual evidência o contradiz.
4. O que conferir primeiro, do mais barato para o mais caro, e o que dizer ao mecânico.
Não invente medições que não estão no relatório nem nos dados. Trate tudo como hipótese e diga o que confirmaria cada uma.

RELATÓRIO:
"""


def ai_question(rep: dict) -> tuple[str, str]:
    """(pergunta completa para o modelo, rótulo curto que aparece na conversa do painel)."""
    rep = clean(rep)
    d = time.strftime("%d/%m %H:%M", time.localtime(rep["ts"] / 1000))
    return AI_ASK + to_text(rep)[:9000], f"Parecer sobre o Diagnóstico geral de {d} ({len(rep['achados'])} ponto(s) de atenção)"
