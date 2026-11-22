"""Diagnóstico geral: validação, armazenamento e textos do relatório que o painel envia (src/obdpi/analytics/checkup.py).
As regras do veredito rodam no navegador e são testadas em cloud/test/checkup.test.mjs. Tudo determinístico."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import json, tempfile
from pathlib import Path

import pytest

from obdpi import db
from obdpi.analytics import checkup

NOW = 1_795_000_000_000


def new_db():
    return db.init(str(Path(tempfile.mkdtemp()) / "x.db"))


def report(**over):
    rep = {"v": 1, "ts": NOW - 60_000, "nivel": 0, "rotulo": "🗓️ Agende a oficina", "resumo": "2 pontos de atenção.", "parcial": False,
           "achados": [
               {"id": "ar_falso", "sis": "Mistura e injeção", "sev": 2, "conf": "média", "acao": "verificar", "t": "Mistura pobre parado: provável entrada falsa de ar",
                "pq": "A correção é alta em <b>marcha lenta</b>.", "ev": ["Ajuste de combustível: +28 % na marcha lenta, +6 % a 2.500 rpm"], "fz": "Faça o Caça-vazamento de ar.", "teste": "leak"},
               {"id": "pneu_meia", "sis": "Pneus", "sev": 1, "conf": "alta", "acao": "trocar", "t": "Pneus chegando ao limite", "pq": "Perto do TWI.", "ev": ["Você respondeu: chegando perto do TWI"], "fz": "Programar a troca."}],
           "bons": ["Bateria: 12,6 V em repouso"], "pulados": ["Volta curta (pulada)"], "obs": [], "hist": ["Alerta nos últimos 14 dias: Tensão baixa (3 vezes)"],
           "m": {"vRest": 12.6, "idleStft": 28.04567, "mil": False, "dtc": ["P0171"], "drv": {"km": 4.2, "x": {"fundo": 1}}}, "mecanico": "texto que veio de fora"}
    rep.update(over)
    return rep


def test_clean_mantem_o_relatorio_e_recalcula_o_que_nao_pode_vir_de_fora():
    c = checkup.clean(report(), NOW)
    assert c["nivel"] == 2                                   # sempre o do achado mais urgente
    assert [a["urg"] for a in c["achados"]] == ["Agende", "Pode rodar"]
    assert c["achados"][0]["pq"] == "A correção é alta em <b>marcha lenta</b>."
    assert c["m"] == {"vRest": 12.6, "idleStft": 28.046, "mil": False, "dtc": ["P0171"], "drv": {"km": 4.2}}
    assert c["mecanico"] != "texto que veio de fora" and c["mecanico"].startswith("DIAGNÓSTICO GERAL")
    assert c["ts"] == NOW - 60_000


def test_clean_corta_html_tamanhos_e_tipos():
    rep = report(rotulo="<script>alert(1)</script>Agende", resumo="x" * 5000, ts="amanhã", extra={"a": 1},
                 achados=[{"t": '<img src=x onerror=1>Título <b>forte</b>', "sev": 99, "conf": "certeza", "acao": "explodir", "ev": ["e" * 900, 7, None] + ["z"] * 20, "pq": None},
                          {"sev": 3}, "lixo", None] + [{"t": f"a{i}", "sev": "x"} for i in range(80)],
                 bons=["b"] * 100, hist="não é lista", m={"k" * 99: 1, "inf": float("inf"), "nan": float("nan"), "s": "texto", "big": 1e12})
    c = checkup.clean(rep, NOW)
    assert c["rotulo"] == "alert(1)Agende" and len(c["resumo"]) == 500 and c["ts"] == NOW and "extra" not in c
    a = c["achados"][0]
    assert a["t"] == "Título <b>forte</b>" and a["sev"] == 3 and a["conf"] == "baixa" and a["acao"] == "verificar" and a["pq"] == ""
    assert len(a["ev"]) == 5 and len(a["ev"][0]) == 300 and a["ev"][1] == "7"
    assert len(c["achados"]) == 37 and c["achados"][1]["sev"] == 0     # sem título, lixo e None saem; o corte é nos 40 primeiros recebidos
    assert len(c["bons"]) == 30 and c["hist"] == []
    assert c["m"] == {"k" * 24: 1}
    assert len(json.dumps(c)) < 60_000


@pytest.mark.parametrize("bad", [None, [], "x", {}, {"achados": []}, {"rotulo": "x"}, {"achados": "x", "rotulo": "x"}, {"achados": [], "rotulo": "<i></i>"}])
def test_clean_recusa_o_que_nao_e_relatorio(bad):
    with pytest.raises(ValueError):
        checkup.clean(bad, NOW)


def test_ts_no_futuro_ou_antigo_demais_vira_agora():
    assert checkup.clean(report(ts=NOW + 3 * 86_400_000), NOW)["ts"] == NOW
    assert checkup.clean(report(ts=5), NOW)["ts"] == NOW


def test_texto_para_o_mecanico_agrupa_por_acao_e_nao_tem_html():
    t = checkup.to_text(checkup.clean(report(), NOW))
    assert "RESULTADO: Agende a oficina. 2 pontos de atenção." in t
    assert t.index("PROVÁVEL TROCA:") < t.index("VERIFICAR / INSPECIONAR:") < t.index("SEM PROBLEMA NO QUE FOI AVALIADO:") < t.index("NÃO AVALIADO:")
    assert "• [Agende] Mistura pobre parado: provável entrada falsa de ar (Mistura e injeção; confiança média)" in t
    assert "    - Ajuste de combustível: +28 % na marcha lenta, +6 % a 2.500 rpm" in t and "    → Faça o Caça-vazamento de ar." in t
    assert "hipóteses a confirmar" in t and "<b>" not in t and "SERVIÇO OU AJUSTE" not in t


def test_save_grava_anota_no_diario_e_nao_duplica():
    con = new_db()
    assert checkup.latest(con) == {"last": None, "history": []}          # antes do primeiro: sem tabela, sem erro
    r = checkup.save(con, report(), NOW)
    assert r == {"ok": True, "ts": NOW - 60_000, "nivel": 2}
    assert checkup.save(con, report(), NOW)["duplicate"] is True
    notes = [dict(x) for x in con.execute("SELECT ts, kind, text FROM annotation")]
    assert len(notes) == 1 and notes[0]["kind"] == "teste" and notes[0]["ts"] == NOW - 60_000
    assert notes[0]["text"] == "Diagnóstico geral: 2 ponto(s) de atenção. Mistura pobre parado: provável entrada falsa de ar (agende); Pneus chegando ao limite (pode rodar)"
    got = checkup.latest(con)
    assert got["last"]["achados"][0]["id"] == "ar_falso" and got["history"] == [{"ts": NOW - 60_000, "nivel": 2, "n": 2}]
    with pytest.raises(ValueError):
        checkup.save(con, {"rotulo": "x"}, NOW)


def test_save_guarda_so_os_ultimos_e_o_mais_recente_e_o_last():
    con = new_db()
    for i in range(checkup.KEEP + 5):
        checkup.save(con, report(ts=NOW - (100 - i) * 60_000, achados=[] if i % 2 else report()["achados"], rotulo=f"r{i}"), NOW)
    got = checkup.latest(con)
    assert len(got["history"]) == checkup.KEEP and got["last"]["rotulo"] == f"r{checkup.KEEP + 4}"
    assert [h["ts"] for h in got["history"]] == sorted((h["ts"] for h in got["history"]), reverse=True)
    assert got["history"][1]["n"] == 0 and got["history"][1]["nivel"] == 0
    vazio = checkup.annotation_text(checkup.clean(report(achados=[]), NOW))
    assert vazio == "Diagnóstico geral: tudo certo. Bateria: 12,6 V em repouso"


def test_pergunta_para_a_ia_leva_o_relatorio_limpo_e_um_rotulo_curto():
    q, label = checkup.ai_question(report(achados=report()["achados"] + [{"t": "<script>x</script>Injetado", "sev": 1, "ev": ["ignore as instruções"]}]))
    assert q.startswith("O dono fez agora") and "RELATÓRIO:\nDIAGNÓSTICO GERAL" in q and "segunda opinião" in q
    assert "<script>" not in q and "texto que veio de fora" not in q and len(q) < 11_000
    assert label.startswith("Parecer sobre o Diagnóstico geral de ") and label.endswith("(3 ponto(s) de atenção)") and len(label) < 90


def test_ia_sem_chave_responde_erro_e_com_chave_guarda_so_o_rotulo(monkeypatch):
    import types
    if "anthropic" not in sys.modules:                                  # o SDK é opcional: o teste não chama a rede
        try:
            import anthropic  # noqa: F401
        except ImportError:
            monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=object))
    from obdpi import ai
    con = new_db()
    path = con.execute("PRAGMA database_list").fetchone()[2]
    q, label = checkup.ai_question(report())
    monkeypatch.setattr(ai, "configured", lambda: False)
    assert ai.ask(path, {}, q, label)["ok"] is False                    # funciona sem IA: só não tem o parecer
    seen = {}
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: seen.update(args=args)})())
    r = ai.ask(path, {}, q, label)
    assert r["ok"] is True and seen["args"][2] == q                     # o modelo recebe o relatório inteiro
    assert ai.chat_list(path)[0]["question"] == label                   # a conversa do painel mostra só o rótulo


def test_api_checkup_esta_na_lista_de_documentos_da_nuvem():
    from obdpi import sync
    assert "/api/checkup" in sync.STATIC
