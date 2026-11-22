"""Diagnóstico da viagem (src/obdpi/analytics/tripstory.py): regras fixas, tudo determinístico.
A viagem real de 22/11/2026 (tests/fixtures/trip32.json, cópia de /api/trips/32 sem posição) calibra os textos; as
demais são viagens sintéticas montadas aqui."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import copy, json, tempfile
from pathlib import Path

import pytest

from obdpi import db
from obdpi.analytics import tripstory

TZ = -3 * 3600_000
T0 = 1_795_000_000_000                       # 22/11/2026 06:06:40 em Brasília... só precisa ser fixo
REAL = json.loads((Path(__file__).parent / "fixtures" / "trip32.json").read_text())


def real(**ctx):
    return tripstory.build(copy.deepcopy(REAL), {"now": REAL["end_ts"] + 3600_000, "tz_ms": TZ, **ctx})


def sysof(s, key):
    return next(x for x in s["systems"] if x["key"] == key)


def make(dur=1200, warm_start=88.0, stft_idle=2.0, stft_run=0.0, lam=1.0, volt=14.1, ect_max=92.0, idle_rpm=750, dips=(), cruise=60,
         stop_at=(600, 700), fuel=True, finalized=True, slow=True, **over):
    """Viagem sintética: 60 s parado, roda a `cruise` km/h, uma parada em stop_at e chega. Rápidas a 1 s, lentas a 5 s."""
    rpm, spd, ped = [], [], []
    stopped = lambda s: s < 60 or stop_at[0] <= s < stop_at[1] or s >= dur - 20
    for s in range(dur + 1):
        ts = T0 + s * 1000
        st = stopped(s)
        r = idle_rpm if st else 2200
        lo = r
        for a, v in dips:
            if a <= s < a + 2:
                lo = v
        rpm.append([ts, (r + lo) / 2, lo, r])
        v = 0.0 if st else float(cruise)
        spd.append([ts, v, v, v])
        ped.append([ts, 20.0 if st else 32.0, 20.0 if st else 32.0, 20.0 if st else 32.0])
    S = {"rpm": rpm, "speed": spd, "pedal_d": ped}
    if slow:
        for k in ("ect", "stft1", "o2c1_lambda", "batt", "load", "timing", "fuel_lh"):
            S[k] = []
        for s in range(0, dur + 1, 5):
            ts = T0 + s * 1000
            st = stopped(s)
            e = min(ect_max, warm_start + s * 0.1)
            S["ect"].append([ts, round(e, 1)])
            S["stft1"].append([ts, stft_idle if st else stft_run])
            S["o2c1_lambda"].append([ts, lam])
            S["batt"].append([ts, volt])
            S["load"].append([ts, 18.0 if st else 45.0])
            S["timing"].append([ts, 10.0 if st else 25.0])
            S["fuel_lh"].append([ts, 0.9 if st else 6.0])
    km = sum(p[1] for p in spd) / 3600
    idle_s = sum(1 for s in range(dur) if stopped(s))
    t = {"id": 7, "start_ts": T0, "end_ts": T0 + dur * 1000, "finalized": int(finalized), "duration_s": dur if finalized else None,
         "distance_km": round(km, 2) if finalized else None, "idle_s": idle_s if finalized else None, "max_speed": None, "max_rpm": None,
         "max_ect": None, "start_ect": warm_start if slow else None, "crank_v": 10.4, "rest_v": 12.6, "warmup_s": None, "fuel_l": None, "km_l": None,
         "cost": None, "eco_score": None, "eco_json": None, "drive_score": None, "title": None, "alerts": [], "dtc_events": [], "series": S,
         "habits": None}
    if fuel:
        lit = round(km / 11.0, 2)
        t.update(fuel_l=lit, km_l=round(km / lit, 1), cost=round(lit * 6.0, 2))
        t["habits"] = {"litros": lit, "segundos": dur, "preco_l": 6.0, "custo_total": round(lit * 6.0, 2),
                       "parado": {"litros": round(idle_s * 0.9 / 3600, 2), "segundos": idle_s, "parte": round(idle_s * 0.9 / 3600 / lit, 3), "custo": round(idle_s * 0.9 / 3600 * 6.0, 2)},
                       "giro_alto": {"litros": 0.0, "segundos": 0, "parte": 0.0, "custo": 0.0}}
    t.update(over)
    return t


def story(t, **ctx):
    return tripstory.build(t, {"now": t["end_ts"] + 3600_000, "tz_ms": TZ, **ctx})


# ---------------------------------------------------------------- a viagem real
def test_viagem_real_conta_a_historia_com_os_numeros_medidos():
    s = real()
    assert s["narrative"][0] == "Saiu às 19:01 com o motor frio (47 °C); em 6 min já estava na temperatura de trabalho."
    n = s["numbers"]
    assert 360 <= n["warmup_s"] <= 380 and n["max_ect"] == 94 and n["start_ect"] == 47
    assert 0 <= n["stft_idle"] <= 6 and -2 <= n["stft_run"] <= 2 and 0.98 <= n["lam"] <= 1.01
    assert n["volt"] == pytest.approx(14.1, abs=0.05) and 730 <= n["idle_rpm"] <= 770
    assert [sysof(s, k)["status"] for k in ("mistura", "temperatura", "eletrica")] == ["ok", "ok", "ok"]
    assert s["mood"]["level"] == "note" and s["mood"]["emoji"] == "🙂" and "explicação" in s["mood"]["headline"]
    assert s["when"] == "22/11 · 19:01 → 19:20" and s["title"] == "Viagem de 22/11 às 19:01"


def test_viagem_real_quedas_da_lenta_sao_explicadas_pela_carga_do_compressor():
    s = real()
    lenta = sysof(s, "lenta")
    assert lenta["status"] == "note" and "19:06" in lenta["txt"] and "compressor do ar-condicionado" in lenta["txt"]
    assert any("carga foi a" in e and "ponto de ignição" in e for e in lenta["ev"])
    assert s["alerts"] == {"real": 0, "explicados": 2, "habitual": 4, "dtc": []}
    av = sysof(s, "avisos")
    assert av["status"] == "note" and "alarme falso" in av["txt"] and "❄️" in av["txt"]
    assert "ajuste de combustível" in av["txt"] and "stft1" not in av["txt"]          # nome de gente, não de sensor
    assert "Nenhum é falha confirmada." in " ".join(s["narrative"])
    assert all(m["k"] == "carga" for m in s["spark"]["marks"]) and s["spark"]["marks"]


def test_viagem_real_dinheiro_parado_e_a_dica_do_se_tivesse():
    s = real()
    m = s["money"]
    assert m["custo"] == 5.67 and m["litros"] == 1.23 and m["parado"]["custo"] == 1.38 and m["parado"]["parte"] == 0.243
    assert "Gastou 1,23 L, cerca de R$ 5,67" in " ".join(s["narrative"]) and "24 foram com o carro parado: R$ 1,38" in " ".join(s["narrative"])
    tip = s["tips"][0]
    assert tip["txt"].startswith("Você ficou 8 min parado logo na saída") and "Se tivesse saído" in tip["txt"]
    assert 0.9 <= tip["save_rs"] <= 1.3 and m["economia"] >= tip["save_rs"]
    assert tip["save_rs"] < m["parado"]["custo"]                                       # nunca promete poupar mais do que gastou parado


def test_viagem_real_em_andamento_ou_ainda_nao_fechada():
    s = real()                                            # uma hora depois da última leitura, sem o Pi ter fechado
    assert s["unclosed"] and not s["in_progress"] and "números parciais" in s["mood"]["sub"]
    assert 7 <= s["numbers"]["km"] <= 9                   # distância integrada das leituras (a viagem ainda não tem o campo)
    s = tripstory.build(copy.deepcopy(REAL), {"now": REAL["end_ts"] + 20_000, "tz_ms": TZ})
    assert s["in_progress"] and s["mood"]["headline"].startswith("Viagem em andamento") and s["mood"]["emoji"] == "⏳"
    assert s["narrative"][1].startswith("Até agora rodou") and s["timeline"][-1]["txt"].startswith("Viagem em andamento")
    assert s["compare"] is None


def test_documento_pequeno_sem_posicao_e_deterministico():
    a, b = real(), real()
    assert a == b
    raw = json.dumps(a, ensure_ascii=False)
    assert len(raw.encode()) < 16_000
    assert '"lat"' not in raw and '"lon"' not in raw
    assert len(a["spark"]["speed"]) == tripstory.SPARK_N and a["spark"]["dt"] == pytest.approx(1140 / 72, abs=0.2)
    assert a["share"].startswith("🚗 Delfos · Viagem de 22/11 às 19:01\n🙂 ")


def test_ar_condicionado_marcado_pelo_dono_vira_a_explicacao():
    t0 = REAL["start_ts"]
    notes = [{"ts": t0 + 280_000, "kind": "ar", "text": "Ar-condicionado ligado"}, {"ts": t0 + 700_000, "kind": "ar", "text": "Ar-condicionado desligado"},
             {"ts": t0 + 900_000, "kind": "manutencao", "text": "Barulho na roda dianteira"}]
    s = real(annotations=notes)
    assert "o ar-condicionado estava ligado (marcado por você)" in sysof(s, "lenta")["txt"]
    assert "marcado por você" in sysof(s, "avisos")["txt"] and "toque no ❄️" not in sysof(s, "avisos")["txt"]
    assert s["numbers"]["ac_s"] == 420 and any(h["icon"] == "❄️" and h["v"] == "7 min" for h in s["highlights"])
    tl = [e["txt"] for e in s["timeline"]]
    assert "Ar-condicionado ligado (até 19:12)" in tl and "Barulho na roda dianteira" in tl
    assert s["alerts"]["explicados"] == 3                 # com o ar marcado, "carga fora do habitual parado" também se explica


# ---------------------------------------------------------------- viagens sintéticas: cada regra
def test_viagem_saudavel_e_redonda():
    s = story(make())
    assert s["mood"]["level"] == "ok" and s["mood"]["emoji"] == "😎"
    assert [x["status"] for x in s["systems"]] == ["ok"] * 5
    assert "5 de 5 sistemas" in s["mood"]["sub"] and not s["unclosed"] and not s["short"]
    assert s["timeline"][0]["icon"] == "🔑" and s["timeline"][-1]["txt"] == "Motor desligado"
    assert "já quente (88 °C)" in s["narrative"][0]


def test_mistura_pobre_parado_aponta_entrada_de_ar():
    s = story(make(stft_idle=22.0, stft_run=4.0))
    m = sysof(s, "mistura")
    assert m["status"] == "warn" and "pobre" in m["txt"] and "+22 % parado e +4 % rodando" in m["txt"] and "ar entrando sem ser medido" in m["txt"]
    assert s["mood"]["level"] == "warn" and s["mood"]["emoji"] == "🤔" and "mistura" in s["mood"]["headline"]
    assert sysof(story(make(stft_idle=-31.0, stft_run=-28.0)), "mistura")["status"] == "bad"
    assert "rica" in sysof(story(make(stft_idle=-14.0, stft_run=-12.0)), "mistura")["txt"]
    assert sysof(story(make(lam=1.08)), "mistura")["status"] == "warn"


def test_mistura_nao_e_julgada_com_o_motor_frio():
    s = story(make(dur=400, warm_start=20.0, stft_idle=30.0, stft_run=30.0, stop_at=(200, 220)))     # nunca passa de 60 °C
    assert sysof(s, "mistura")["status"] == "na" and sysof(s, "lenta")["status"] == "na"


def test_temperatura_alta_e_motor_que_nao_aquece():
    assert sysof(story(make(ect_max=107.0)), "temperatura")["status"] == "warn"
    hot = story(make(ect_max=112.0))
    assert sysof(hot, "temperatura")["status"] == "bad" and hot["mood"]["emoji"] == "🚨" and "112 °C" in sysof(hot, "temperatura")["txt"]
    cold = story(make(dur=1800, warm_start=30.0, ect_max=62.0))
    assert sysof(cold, "temperatura")["status"] == "warn" and "termostática" in sysof(cold, "temperatura")["txt"]


def test_bateria_e_alternador():
    low = sysof(story(make(volt=12.1)), "eletrica")
    assert low["status"] == "warn" and "carregando pouco" in low["txt"]
    assert sysof(story(make(volt=15.7)), "eletrica")["status"] == "bad"
    eld = sysof(story(make(volt=12.9)), "eletrica")                       # modo econômico do alternador do Civic: não é defeito
    assert eld["status"] == "ok" and "baixa a carga sozinho" in eld["txt"]
    assert sysof(story(make(crank_v=8.2)), "eletrica")["status"] == "bad"
    weak = sysof(story(make(crank_v=9.1, rest_v=12.0)), "eletrica")
    assert weak["status"] == "warn" and "fraca" in weak["txt"] and "baixa" in weak["txt"]


def test_quedas_da_lenta_sem_explicacao_pedem_atencao():
    s = story(make(dips=((620, 480), (660, 500))))
    l = sysof(s, "lenta")
    assert l["status"] == "warn" and "sem causa aparente" in l["txt"] and "480 rpm" in l["txt"]
    one = sysof(story(make(dips=((640, 600),))), "lenta")
    assert one["status"] == "note"                                        # uma queda leve isolada é só observação


def test_alerta_de_verdade_e_codigo_de_falha():
    al = [{"ts": T0 + 300_000, "severity": "crit", "rule": "ect_hi", "source": "limits", "signal": "ect", "title": "Motor superaquecendo", "detail": "ECT = 111 °C", "count": 1},
          {"ts": T0 + 310_000, "severity": "info", "rule": "x", "source": "limits", "signal": "iat", "title": "informativo", "detail": "", "count": 1}]
    s = story(make(alerts=al))
    a = sysof(s, "avisos")
    assert a["status"] == "bad" and "Motor superaquecendo" in a["txt"] and s["alerts"]["real"] == 1
    assert {"s": 300, "k": "aviso"} in s["spark"]["marks"]
    d = story(make(dtc_events=[{"ts": T0 + 100_000, "code": "P0171", "kind": "confirmed", "event": "new"}, {"ts": T0 + 100_000, "code": "P0300", "kind": "pending", "event": "cleared"}]))
    assert sysof(d, "avisos")["status"] == "bad" and d["alerts"]["dtc"] == ["P0171"] and "P0171" in sysof(d, "avisos")["txt"]
    lenta = story(make(alerts=[{"ts": T0 + 650_000, "severity": "warn", "rule": "R4", "source": "throttle", "signal": "rpm", "title": "Marcha lenta oscilando", "detail": "", "count": 1}]))
    assert lenta["alerts"]["real"] == 1                                   # sem carga entrando nem ar marcado, o aviso vale


def test_viagem_curta_nao_inventa_veredito():
    s = story(make(dur=100, warm_start=30.0, stop_at=(50, 55)))
    assert s["short"] and s["mood"]["emoji"] == "🤏" and "curtinha" in s["mood"]["headline"]
    assert sysof(s, "mistura")["status"] == "na" and sysof(s, "lenta")["status"] == "na"
    assert "sem veredito" in s["mood"]["sub"] and s["tips"] == []


def test_sem_combustivel_e_sem_sinais():
    s = story(make(fuel=False))
    assert s["money"]["litros"] is None and any(x.startswith("Sem dados de consumo") for x in s["narrative"])
    assert not any(h["l"] in ("Custo", "Consumo") for h in s["highlights"])
    bare = story(make(slow=False, fuel=False, crank_v=None, rest_v=None))
    assert [x["status"] for x in bare["systems"]] == ["na", "na", "na", "na", "ok"]
    assert bare["mood"]["level"] == "ok" and bare["mood"]["emoji"] == "🙂" and "faltou leitura" in bare["mood"]["headline"]      # sem aviso não é "saudável"
    assert bare["narrative"][0].startswith("Saiu às ")
    empty = tripstory.build({"id": 1, "start_ts": T0, "end_ts": T0 + 5000, "finalized": 1, "series": {}}, {"tz_ms": TZ})
    assert empty["short"] and empty["numbers"]["km"] is None and empty["spark"]["speed"] == []
    assert tripstory.build({"id": 2, "start_ts": T0, "finalized": 0}, None)["mood"]["headline"]      # nem end_ts nem séries: não quebra


def test_paradas_longas_viram_dica_com_valor():
    s = story(make(stop_at=(400, 760)))
    tip = next(x for x in s["tips"] if x["icon"] == "🅿️")
    assert "1 parada de mais de 1 min" in tip["txt"] and tip["save_rs"] == pytest.approx(0.9 * 300 / 3600 * 6.0, abs=0.02)
    assert any(h["l"] == "Maior parada" and h["v"] == "6 min" for h in s["highlights"])
    rev = make()
    rev["habits"]["giro_alto"] = {"litros": 0.12, "segundos": 95, "parte": 0.08, "custo": 0.72}
    assert any(x["icon"] == "⚙️" and "R$ 0,72" in x["txt"] for x in story(rev)["tips"])


def test_consumo_contra_o_normal_e_viagens_parecidas():
    sim = [{"id": i, "start_ts": T0 - i * 86400_000, "duration_s": 900, "distance_km": 14.0, "km_l": 12.5, "fuel_l": 1.1} for i in range(1, 5)]
    s = story(make(), baseline={"kml_usual": 12.4, "kml_n": 9, "similar": sim, "similar_by": "rota"})
    assert s["money"]["km_l_usual"] == 12.4 and "o seu normal é 12,4, então foi pior" in " ".join(s["narrative"])
    c = s["compare"]
    assert c["n"] == 4 and c["d_dur_s"] == 300 and "neste mesmo trajeto" in c["txt"] and "5 min mais demorada" in c["txt"] and "pior" in c["txt"]
    assert story(make(), baseline={"similar": sim[:1]})["compare"] is None                    # uma viagem só não é comparação


def test_nota_de_conducao_do_gps_e_do_obd():
    s = story(make(), gps={"drive_score": 82, "freadas": 3, "arrancadas": 1, "curvas": 0, "acima_pct": 4.0})
    assert s["driving"]["drive_score"] == 82 and s["driving"]["fonte"] == "GPS"
    assert "Nota de condução pelo GPS: 82 de 100, com 3 freadas fortes." in s["narrative"]
    assert any(h["l"] == "Freadas fortes" and h["v"] == "3" for h in s["highlights"])
    o = story(make(eco_score=71, eco_json={"freadas_bruscas": 2, "aceleracoes_bruscas": 5, "dicas": ["Suba a marcha mais cedo."]}))
    assert o["driving"]["fonte"] == "OBD" and o["driving"]["freadas"] == 2 and o["driving"]["dicas"] == ["Suba a marcha mais cedo."]


# ---------------------------------------------------------------- contexto tirado do banco e pergunta para a IA
def test_contexto_do_banco_sem_posicao():
    con = db.init(str(Path(tempfile.mkdtemp()) / "x.db"))
    for i in range(1, 6):
        con.execute("INSERT INTO trip(id, start_ts, end_ts, finalized, duration_s, distance_km, km_l, fuel_l) VALUES (?,?,?,?,?,?,?,?)",
                    (i, T0 - (6 - i) * 86400_000, T0 - (6 - i) * 86400_000 + 900_000, 1, 900, 12.0, 10.0 + i, 1.0))
        con.execute("INSERT INTO trip_gps(trip_id, route_id, start_name, end_name) VALUES (?,?,?,?)", (i, 3 if i > 1 else 9, "Casa", "Trabalho"))
    con.execute("INSERT INTO trip(id, start_ts, end_ts, finalized, duration_s, distance_km) VALUES (7,?,?,1,1200,12.3)", (T0, T0 + 1200_000))
    con.execute("INSERT INTO trip_gps(trip_id, route_id, drive_score, drive_json, n_stops, start_name, end_name) VALUES (7,3,91,?,2,'Casa','Trabalho')",
                (json.dumps({"freadas": 1, "arrancadas": 0, "curvas": 2, "acima_pct": 0.0, "eventos": [{"lat": -23.5, "lon": -46.6}]}),))
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (T0 + 60_000, "ar", "Ar-condicionado ligado"))
    con.execute("INSERT INTO annotation(ts, kind, text) VALUES (?,?,?)", (T0 - 3600_000, "manutencao", "fora da janela"))
    con.commit()
    t = make()
    ctx = tripstory.context(con, t, T0 + 2 * 3600_000, TZ)
    assert [a["kind"] for a in ctx["annotations"]] == ["ar"]
    assert ctx["baseline"]["kml_usual"] == 13.0 and ctx["baseline"]["similar_by"] == "rota"
    assert sorted(x["id"] for x in ctx["baseline"]["similar"]) == [2, 3, 4, 5]
    assert ctx["gps"]["drive_score"] == 91 and ctx["gps"]["freadas"] == 1
    s = tripstory.build(t, ctx)
    raw = json.dumps(s, ensure_ascii=False)
    assert "Casa" not in raw and "Trabalho" not in raw and "-23.5" not in raw
    assert s["compare"]["n"] == 4 and s["numbers"]["ac_s"] == 1140
    # sem rota: cai para viagens de distância parecida
    con.execute("UPDATE trip_gps SET route_id=NULL")
    con.commit()
    assert tripstory.context(con, t, T0, TZ)["baseline"]["similar_by"] == "distancia"


def test_pergunta_para_a_ia_leva_o_diagnostico_e_as_evidencias():
    q, label = tripstory.ai_question(real())
    assert "Viagem tranquila" in q and "Ajuste curto parado" in q and label.startswith("🩺 Comentário sobre a Viagem de 22/11")
    with pytest.raises(ValueError):
        tripstory.ai_question({})
