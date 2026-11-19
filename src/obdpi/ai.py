"""Análise completa dos dados pelo Claude (botão "Analisar dados" no painel).

Monta um resumo compacto (JSON) de tudo que o Pi sabe do carro e pede ao Claude uma análise em português.
Chave da API: /etc/obd/secrets.toml → [ai] api_key = "sk-ant-..." (ou variável ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
DAY = 86400_000

SYSTEM = """Você é um mecânico especialista em injeção eletrônica e diagnóstico OBD-II, com profundo conhecimento do
Honda Civic 8ª geração 1.8 i-VTEC flex (motor R18A, câmbio manual, corpo de borboleta eletrônico/DBW, sonda A/F de faixa
larga antes do catalisador, ECU Keihin). Você recebe um resumo em JSON dos dados registrados por um Raspberry Pi ligado
ao OBD do carro do dono e escreve um laudo para ele.

Escreva em português do Brasil, direto e fácil de entender por quem não é mecânico, em Markdown, com estas seções:
## Veredito
Uma linha começando com 🟢 (tudo ok), 🟡 (atenção) ou 🔴 (problema) e um parágrafo curto.
## Pontos de atenção
Do mais para o menos importante. Para cada um: o que os dados mostram (com os números), causa mais provável,
alternativas, como confirmar, o que fazer e a urgência.
## Sistema por sistema
Motor e marcha lenta · Mistura e combustível · Corpo de borboleta e pedal · Arrefecimento · Bateria e carga ·
Sondas e catalisador · Consumo e direção. Diga o que está normal também; seja breve onde estiver tudo ok.
## O que fazer agora
Lista numerada e curta.
## Limitações dos dados
O que não dá para concluir e quais dados ajudariam (ex.: rodar mais, completar monitores, teste de fumaça).

Regras: baseie-se nos números do JSON e diga quando algo é inferência. Considere as particularidades conhecidas
listadas em "observacoes" (ex.: PIDs que este ECU informa de forma não confiável). Não invente leituras que não estão
nos dados. Prefira causas comuns a raras."""


SETTINGS = Path("/data/obd/ai.json")       # chaves salvas pelo painel (modo 600, fora do git)


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def save_settings(new: dict) -> dict:
    cur = settings()
    for k in ("provider", "openai_model"):
        if new.get(k):
            cur[k] = str(new[k]).strip()
    for k in ("anthropic_key", "openai_key"):
        if new.get(k) is not None:
            v = str(new[k]).strip()
            if v == "":
                continue                    # campo vazio = mantém a chave atual
            if v == "-":
                cur.pop(k, None)            # "-" apaga
            else:
                cur[k] = v
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur))
    tmp.chmod(0o600)
    tmp.replace(SETTINGS)
    return public_settings()


def public_settings() -> dict:
    s = settings()
    hint = lambda k: ("…" + s[k][-4:]) if s.get(k) else None
    ak = s.get("anthropic_key") or _secrets_key()
    return {"provider": s.get("provider", "anthropic"), "openai_model": s.get("openai_model", OPENAI_MODEL),
            "anthropic_key": ("…" + ak[-4:]) if ak else None, "openai_key": hint("openai_key"), "anthropic_model": MODEL}


OPENAI_MODEL = "gpt-5"


def _secrets_key() -> str | None:
    try:
        import tomllib
        p = Path("/etc/obd/secrets.toml")
        if p.exists():
            return tomllib.loads(p.read_text()).get("ai", {}).get("api_key")
    except Exception:
        pass
    return None


def client() -> anthropic.Anthropic | None:
    key = settings().get("anthropic_key") or _secrets_key()
    try:
        return anthropic.Anthropic(api_key=key, max_retries=2, timeout=600) if key else None
    except Exception:
        return None


def _stats(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return {"n": len(v), "min": round(min(v), 3), "media": round(statistics.fmean(v), 3), "max": round(max(v), 3),
            "desvio": round(statistics.pstdev(v), 3) if len(v) > 1 else 0}


def build_context(db_path: str, live: dict, days: int = 14) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    now = int(time.time() * 1000)
    since = now - days * DAY
    sid = {r["key"]: (r["id"], r["name"], r["unit"]) for r in con.execute("SELECT id, key, name, unit FROM signal")}

    # sensores por condição (motor quente parado / rodando), últimos N dias
    rpm_id = sid.get("rpm", (None,))[0]
    ect_id = sid.get("ect", (None,))[0]
    spd = {}
    if "speed" in sid:
        spd = dict(con.execute("SELECT ts, value FROM sample WHERE signal_id=? AND ts>?", (sid["speed"][0], since)).fetchall())
    rpm = dict(con.execute("SELECT ts, value FROM sample WHERE signal_id=? AND ts>?", (rpm_id, since)).fetchall()) if rpm_id else {}
    ect = dict(con.execute("SELECT ts, value FROM sample WHERE signal_id=? AND ts>?", (ect_id, since)).fetchall()) if ect_id else {}
    ctx_ts = {"marcha_lenta_quente": set(), "rodando_quente": set(), "motor_frio": set()}
    for t, r in rpm.items():
        if not r or r < 400:
            continue
        e = ect.get(t)
        if e is not None and e < 70:
            ctx_ts["motor_frio"].add(t)
        elif (spd.get(t) or 0) == 0:
            ctx_ts["marcha_lenta_quente"].add(t)
        else:
            ctx_ts["rodando_quente"].add(t)
    sensores = {}
    for key, (i, name, unit) in sid.items():
        rows = con.execute("SELECT ts, value FROM sample WHERE signal_id=? AND ts>?", (i, since)).fetchall()
        if not rows:
            continue
        entry = {"nome": name, "unidade": unit}
        for c, tss in ctx_ts.items():
            st = _stats([v for t, v in rows if t in tss])
            if st:
                entry[c] = st
        if len(entry) > 2:
            sensores[key] = entry

    def q(sql, args=()):
        return [dict(r) for r in con.execute(sql, args)]
    trips = q("""SELECT start_ts, duration_s, distance_km, idle_s, avg_speed, max_speed, max_rpm, max_ect, min_volt, max_volt,
                 fuel_l, km_l, tb_score, eco_score, warmup_s, start_ect, crank_v, rest_v, air_verdict, air_json, n_alerts, end_reason
                 FROM trip WHERE finalized=1 AND start_ts>? ORDER BY start_ts DESC LIMIT 25""", (since,))
    for t in trips:
        t["inicio"] = time.strftime("%d/%m %H:%M", time.localtime(t.pop("start_ts") / 1000))
        if t.get("air_json"):
            t["air_json"] = json.loads(t["air_json"])
    alerts = q("""SELECT severity, title, rule, detail, SUM(COALESCE(count,1)) ocorrencias, MAX(COALESCE(ts_end, ts)) ultimo
                  FROM alert WHERE ts>? GROUP BY title ORDER BY ocorrencias DESC LIMIT 30""", (since,))
    for a in alerts:
        a["ultimo"] = time.strftime("%d/%m %H:%M", time.localtime(a["ultimo"] / 1000))
    fast = con.execute("""SELECT AVG(tps) tps, AVG(tps_b) tps_b, AVG(tps_cmd) cmd, AVG(pedal_d) pedal_d, AVG(pedal_e) pedal_e,
                          AVG(maf) maf, AVG(map) map, AVG(rpm) rpm, COUNT(*) n FROM fast_sample
                          WHERE ts>? AND rpm BETWEEN 400 AND 1300 AND speed=0""", (since,)).fetchone()
    sess = con.execute("SELECT elm_version, protocol, supported_pids, vin FROM session ORDER BY id DESC LIMIT 1").fetchone()
    out = {
        "carro": "Honda Civic 2008 1.8 i-VTEC flex (R18A), manual 5 marchas, ~50 L de tanque",
        "periodo_dias": days,
        "observacoes": [
            "O corpo de borboleta foi trocado recentemente; os códigos de falha foram apagados há poucos km.",
            "O PID 06 (ajuste curto) fica cravado em +46,9 % em marcha lenta (limite de correção da ECU) e varia normalmente rodando (−14 a +47 %): é real, indica mistura pobre em marcha lenta.",
            "PID 07 (ajuste longo) não é suportado por este ECU.",
            "PID 34: os bytes A/B (λ) às vezes não batem com a corrente da sonda (bytes C/D); a corrente ≈ 0 mA indica mistura próxima do estequiométrico.",
            "Consumo é estimado pelo MAF + λ comandado, calibrado a cada tanque cheio.",
            "Diagnóstico de ar: 'ratio' = MAF medido / ar esperado por speed-density (eficiência volumétrica aparente).",
        ],
        "adaptador": dict(sess) if sess else None,
        "agora": {"estado": live.get("state"), "motor_ligado": live.get("engine_on"), "valores": live.get("values"),
                  "status_por_sensor": live.get("status"), "luz_injecao": live.get("mil"), "dtc_ativos": live.get("dtc"),
                  "prontidao_monitores": live.get("readiness")},
        "sensores_por_condicao": sensores,
        "borboleta_marcha_lenta_media": dict(fast) if fast else None,
        "viagens": trips,
        "alertas": alerts,
        "dtc_historico": q("SELECT code, kind, event, ts FROM dtc_event WHERE ts>? ORDER BY ts DESC LIMIT 30", (since,)),
        "abastecimentos": q("SELECT ts, fuel, liters, price_l, km_since_prev, real_kml, mix_after FROM fuel_fill ORDER BY ts DESC LIMIT 10"),
        "testes_0_100": q("SELECT ts, t60, t80, t100, hz FROM accel_run ORDER BY ts DESC LIMIT 5"),
        "diario_manutencao": q("SELECT ts, kind, text FROM annotation ORDER BY ts DESC LIMIT 20"),
    }
    con.close()
    return out


# ---------------------------------------------------------------- execução em segundo plano
_lock = threading.Lock()
_job: dict = {"status": "idle"}


def job() -> dict:
    return dict(_job)


def start(db_path: str, live: dict) -> dict:
    with _lock:
        if _job.get("status") == "running":
            return job()
        _job.clear()
        _job.update(status="running", started=time.time())
    threading.Thread(target=_run, args=(db_path, live), daemon=True).start()
    return job()


def _run_openai(db_path: str, payload: str) -> None:
    import urllib.error
    import urllib.request
    st = settings()
    key, model = st.get("openai_key"), st.get("openai_model") or OPENAI_MODEL
    if not key:
        raise RuntimeError("Chave da OpenAI não configurada (aba Sistema → Análise por IA).")
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Dados do carro (JSON):\n" + payload + "\n\nFaça a análise completa: está tudo ok? O que merece atenção?"}]}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OpenAI respondeu {e.code}: {msg}") from None
    except urllib.error.URLError:
        raise RuntimeError("Sem internet no Pi (conecte no WiFi ou hotspot).") from None
    text = (j["choices"][0]["message"].get("content") or "").strip()
    usage = {"input": j.get("usage", {}).get("prompt_tokens"), "output": j.get("usage", {}).get("completion_tokens")}
    _save(db_path, j.get("model", model), text, usage)


def _save(db_path: str, model: str, text: str, usage: dict) -> None:
    rw = sqlite3.connect(db_path, timeout=30)
    rw.execute("CREATE TABLE IF NOT EXISTS ai_analysis (id INTEGER PRIMARY KEY, ts INTEGER, model TEXT, text TEXT, usage TEXT)")
    rw.execute("INSERT INTO ai_analysis(ts, model, text, usage) VALUES (?,?,?,?)", (int(time.time() * 1000), model, text, json.dumps(usage)))
    rw.commit()
    rw.close()
    _job.update(status="done", text=text, model=model, usage=usage, finished=time.time())


def _run(db_path: str, live: dict) -> None:
    try:
        if settings().get("provider") == "openai":
            _run_openai(db_path, json.dumps(build_context(db_path, live), ensure_ascii=False, default=str))
            return
        c = client()
        if c is None:
            raise RuntimeError("Chave da Anthropic não configurada (aba Sistema → Análise por IA).")
        ctx = build_context(db_path, live)
        payload = json.dumps(ctx, ensure_ascii=False, default=str)
        with c.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": "Dados do carro (JSON):\n" + payload +
                       "\n\nFaça a análise completa: está tudo ok? O que merece atenção?"}],
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise RuntimeError("O modelo recusou a análise.")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if msg.stop_reason == "max_tokens":
            text += "\n\n_(resposta cortada por tamanho)_"
        _save(db_path, msg.model, text, {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens})
    except anthropic.AuthenticationError:
        _job.update(status="error", error="Chave da API inválida.")
    except anthropic.RateLimitError:
        _job.update(status="error", error="Limite de uso da API atingido. Tente de novo em alguns minutos.")
    except anthropic.APIConnectionError:
        _job.update(status="error", error="Sem internet no Pi (conecte no WiFi ou hotspot).")
    except anthropic.APIStatusError as e:
        _job.update(status="error", error=f"Erro da API ({e.status_code}): {e.message}")
    except Exception as e:
        _job.update(status="error", error=str(e))


def last(db_path: str) -> dict | None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        r = con.execute("SELECT ts, model, text, usage FROM ai_analysis ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        return {"ts": r[0], "model": r[1], "text": r[2], "usage": json.loads(r[3])} if r else None
    except sqlite3.Error:
        return None
