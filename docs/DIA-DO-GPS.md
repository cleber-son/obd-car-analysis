# Dia do GPS — lista única (F10)

> Preparada em 2026-11-22, **sem hardware**: nada aqui foi executado no Pi, na nuvem nem com o receptor. É a F10 da
> `docs/GPS-ARQUITETURA.md` (seção 12) em forma de lista: junta a lista 10.5, todos os itens **[HW]** das notas da F1 à F9
> e os passos de publicação registrados no `docs/PROCESSO.md`. Para o dono e para quem estiver no PC com ele.
>
> Três partes, nesta ordem: **A** publicar (só quando o dono liberar) · **B** quando o VK-172 chegar · **C** o que só o
> dono decide. Ao terminar cada item da parte B: anotar o resultado no `PROCESSO.md` e marcar o **[HW]** correspondente
> na `GPS-ARQUITETURA.md` como conferido ou corrigido.

Atalhos usados abaixo:

```bash
NUVEM=https://obd.cleberson.social.br
PI=obd.local                  # no hotspot do iPhone: PI=172.20.10.14
OBDCTL='cd /opt/obd && PYTHONPATH=src venv/bin/python -m obdpi.cli'      # no Pi: ssh obd@$PI "$OBDCTL gps status"
```

`obdctl gps check` (novo, só leitura) resume num lugar só o que dá para conferir por software no Pi: gpsd, receptor,
posição, taxa, chrony, `geo.db`, senha de localização na nuvem (pelo último ping do `obd-sync`) e subtensão. Cada linha
sai como `ok`, `aviso`, `falha` ou `—`, já com o que conferir. Ele não muda nada: não pede taxa ao receptor, não reinicia
serviço e não fala com a nuvem.

---

## A) Deploy (quando o dono liberar)

**Nada desta parte roda sem o dono pedir.** A ordem é a nuvem primeiro e o Pi depois. Um Pi novo com o Worker antigo não
envia posição (o `ping` antigo não traz `loc`). Um Worker novo com o Pi antigo só mostra "ainda não sincronizado pelo Pi"
nas telas do GPS.

### A0. Antes de começar (no PC)

1. `git status` limpo, no commit que vai ser publicado. Se houver trabalho em andamento, publicar a partir de uma cópia
   do HEAD (`git worktree add ../delfos-pub HEAD` e rodar tudo de lá), porque o `cloud/build.sh` copia o `src/` inteiro.
2. Testes:
   `…/venv/bin/python -m pytest -q` (todos passando) e `cd cloud && npm test`.
3. Conta Cloudflare no ambiente: `CLOUDFLARE_API_TOKEN` e `CLOUDFLARE_ACCOUNT_ID` (o mesmo token da publicação de
   2026-11-19). Conferir com `cd cloud && npx wrangler whoami`.
4. A publicação também leva o que entrou fora do GPS desde o último deploy: alertas da chave desligada (`c94349c`),
   teste dos bicos (`6e21acc`, `b2c1b40`) e o "Ao vivo sem dados velhos" (`5bac622`).

### A1. Nuvem (Worker + D1)

Tudo dentro de `cloud/`:

| # | Fazer | Esperado |
|---|---|---|
| 1 | *(opcional, recomendado)* cópia do banco: `npx wrangler d1 export civic-obd --remote --output=../secrets/d1-antes-gps.sql` | arquivo criado em `secrets/`, que fica fora do git |
| 2 | `npx wrangler d1 execute civic-obd --remote --file=schema.sql` | sem erro. Cria a tabela `share` se não existir; só tem `IF NOT EXISTS`, então pode repetir |
| 3 | `npx wrangler d1 execute civic-obd --remote --file=migrations/0001-share-f9.sql` | sem erro. **`duplicate column name: opts`** = já estava aplicada, nada a fazer. Rodar uma vez só |
| 4 | `npx wrangler d1 execute civic-obd --remote --command "PRAGMA table_info(share)"` | a lista de colunas termina em `opts` |
| 5 | Senha de localização, gerada e guardada **sem aparecer na tela** (bloco abaixo) | `secrets/cloud.txt` ganha a linha `LOC_PASSWORD=…`; o wrangler responde "Success" |
| 6 | `npx wrangler secret list` | aparecem `LOC_PASSWORD` e `SYNC_TOKEN` (só os nomes). **Não** pode aparecer `LOC_DELAY_MS`, que só existe para os testes |
| 7 | `npm run deploy` | `build.sh` diz "public/ pronto" e o wrangler publica `civic-obd` em `obd.cleberson.social.br` |

Bloco do passo 5 (32 caracteres aleatórios, mais que os 24 pedidos; reaproveita a senha se ela já estiver no arquivo):

```bash
cd cloud
if ! grep -q '^LOC_PASSWORD=' ../secrets/cloud.txt; then
  LOC=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  printf 'LOC_PASSWORD=%s\n' "$LOC" >> ../secrets/cloud.txt
  unset LOC
fi
sed -n 's/^LOC_PASSWORD=//p' ../secrets/cloud.txt | tr -d '\n' | npx wrangler secret put LOC_PASSWORD
```

O `scripts/deploy.sh` lê só a linha `SYNC_TOKEN=` desse arquivo, então a linha nova não atrapalha. O dono copia a senha
de `secrets/cloud.txt` para o gerenciador de senhas do iPhone: é ela que destranca o mapa no painel online.
A senha do painel (`DASH_PASSWORD`) **não** libera a localização. Trocar a `LOC_PASSWORD` derruba todos os cookies já
emitidos.

**Conferir a nuvem (antes de mexer no Pi):**

```bash
curl -s $NUVEM/api/env                                    # "loc":"trancada"  (era "desativada" antes do passo 5)
curl -s -o /dev/null -w '%{http_code}\n' $NUVEM/api/share/AAAAAAAAAAAAAAAAAAAAAA
#   404 = tudo certo (senha e migração ok, link inexistente) · 410 = sem LOC_PASSWORD · 503 = falta a migração 0001
curl -s $NUVEM/api/gps/logbook                            # 401 {"locked":true,"configured":true}
curl -s -o /dev/null -w '%{http_code}\n' $NUVEM/s/AAAAAAAAAAAAAAAAAAAAAA      # 200 (a página abre e diz "não encontrado")
for f in gps.js gpsmap.js leaflet.js voice.js; do curl -s -o /dev/null -w "$f %{http_code}\n" $NUVEM/static/$f; done   # todos 200
curl -s -H "Authorization: Bearer $(sed -n 's/^SYNC_TOKEN=//p' secrets/cloud.txt)" $NUVEM/api/sync/ping   # "loc":true,"share":{…}
```

No iPhone, em `obd.cleberson.social.br`, aba Mapa: pede a senha de localização; com a senha certa, abre (vazio até o Pi
sincronizar). O resto do painel continua público, como antes.

### A2. Pi

**Condições:** carro estacionado, **motor em marcha lenta** (melhor) ou só a ignição ligada, uns **15 min** (até 25 min com
a proteção do cartão ligada, que reinicia o Pi duas vezes na ida e duas na volta), hotspot do iPhone com *Maximizar
Compatibilidade* e o PC no mesmo hotspot. **Não dar partida durante o deploy:** o Pi reinicia na partida.

> ⚠️ **Subtensão:** o Pi já marca `get_throttled = 0x50005` (subtensão agora e CPU limitada) com a fonte atual e reinicia
> na partida. Isso não impede o deploy, mas o conversor **12→5 V de 3 A reais** tem de estar instalado antes de avaliar o
> receptor (item B8) e antes de qualquer ideia de modo sempre ligado (C4). O `obdctl gps check` mostra isso na linha
> "alimentação do Pi".

| # | Fazer | Esperado |
|---|---|---|
| 1 | `ssh obd@$PI 'vcgencmd get_throttled; uptime; df -h /data; systemctl is-active obd-collector obd-web obd-sync'` | Pi responde; `/data` com folga (o `geo.db` ocupa 34 MB) |
| 2 | Cópia do que está no Pi hoje, para voltar se precisar: `ssh obd@$PI 'sudo tar czf /data/obd/backup/opt-antes-gps.tgz -C /opt/obd src deploy config && sqlite3 /data/obd/obd.db ".backup /data/obd/backup/obd-antes-gps.db"'` | dois arquivos em `/data/obd/backup/` (o `/data` aceita gravação mesmo com a proteção ligada) |
| 3 | `scripts/deploy.sh $PI` | termina com `== OK`. Na primeira vez o `install.sh` baixa e instala `gpsd`, `gpsd-tools` (ou `gpsd-clients`) e `chrony`, que remove o `systemd-timesyncd`. Depois manda o `data/geo/geo.db` (34 MB de dados móveis na 1ª vez; nas seguintes, só a diferença) e grava a URL e o token da nuvem |
| 4 | Se aparecer `AVISO: gpsd/chrony não instalados (sem internet?)`: conferir a internet do hotspot e rodar o `scripts/deploy.sh` de novo, porque ele é idempotente | sem o aviso |

**Conferir o Pi (sem receptor ainda):**

```bash
ssh obd@$PI 'systemctl is-active obd-collector obd-web obd-sync obd-btagent gpsd.socket chrony; systemctl --failed --no-legend'
#   todos "active" e nenhuma unidade falhada (gpsd.service pode estar "inactive": sobe sozinho pelo socket)
ssh obd@$PI 'systemctl status obd-clock --no-pager | head -5'      # rodou neste boot (oneshot: "inactive (dead)" com status=0 é normal)
ssh obd@$PI 'timedatectl | grep -i synchronized; chronyc tracking | head -3; dpkg -l systemd-timesyncd 2>/dev/null | tail -1'
#   "System clock synchronized: yes", chrony seguindo um servidor NTP, timesyncd removido
ssh obd@$PI "$OBDCTL gps status"        # estado=desconectado (ou sem_sinal) · posições gravadas=0
ssh obd@$PI "$OBDCTL gps check"
curl -s http://$PI/api/gps/status | python3 -m json.tool | head -40   # "state" desconectado, "geo": {"mb": 34.4, "cameras": "1287", …}
ssh obd@$PI 'sudo systemctl start obd-doctor.service; journalctl -u obd-doctor -n 20 --no-pager'
#   sem "falhou"; o doctor só vigia gpsd/chrony porque estão instalados e aceita o gpsd parado à espera do socket
ssh obd@$PI 'journalctl -u obd-sync -b --no-pager | grep -i "localização na nuvem"'   # "liberada (senha configurada)"
```

`obdctl gps check` esperado **antes de o receptor chegar**: `gpsd responde` ok · `receptor no gpsd` **falha** ("nenhum
aparelho") · `coletor e GPS` aviso ou falha · `relógio` ok (NTP) · `chrony com o GPS` aviso (fonte GPS sem leituras,
reach 0) · `mapa offline` ok (34,4 MB, 1287 radares) · `localização na nuvem` ok · `alimentação do Pi` falha ou aviso
enquanto a fonte for a atual. Qualquer outra falha é problema do deploy.

No painel do Pi (`http://$PI`): Sistema → cartão "📡 GPS" com "GPS não conectado…" e o mapa offline listado; Ao vivo,
Viagens e Consumo iguais a antes. Na nuvem: `curl -s $NUVEM/api/env` com `"pi_online":true` e `last_sync_ts` recente.

**Mapa offline: o que vai e o que falta.** O `data/geo/geo.db` de hoje cobre **só a Grande SP** (93 mil vias, 1.287
radares, ruas locais só num raio de 15 km de casa). O **corredor até Cristais Paulista** (Bandeirantes/Anhanguera,
Ribeirão Preto, Franca) **ainda não está no arquivo**. O `geo-build.py` gera um arquivo por vez, e o Pi lê um só; para
incluir o corredor é preciso gerar de novo uma caixa única que cubra os dois, com `--inner` no mesmo centro de casa
(fora dele vão só as vias principais) e `--force` (passa de 4 graus²; são umas 100 consultas ao Overpass público, de 1 a
3 horas). Depois basta outro `scripts/deploy.sh`. Decisão do dono (C5).

**Voltar atrás (se o coletor não subir):** `ssh obd@$PI 'sudo tar xzf /data/obd/backup/opt-antes-gps.tgz -C /opt/obd && sudo systemctl restart obd-collector obd-web obd-sync'`.
O banco só ganhou tabelas e colunas, e a versão anterior ignora as novas. gpsd e chrony podem ficar instalados.
Na nuvem, `npx wrangler rollback` volta o Worker para a versão anterior. A tabela `share` e a coluna `opts` não atrapalham.

---

## B) Quando o GPS chegar

**Preparação:** carro em **céu aberto** (fora da garagem), Pi ligado pelo carro, SSH aberto, painel do Pi aberto no iPhone,
um aplicativo de GPS de velocidade/0–100 no celular para comparar, e o **cabo extensor USB** para o receptor ficar no
painel, longe de película metálica. Rodar `obdctl gps check` no começo e depois de cada bloco.

Para cada item: **Fazer** · **Esperado** · **Conferir** (comando ou tela) · **Se falhar** (arquivo ou limiar para ajustar).

### Bloco 1: receptor, gpsd e relógio (parado, 30–40 min)

**B1. Ligar na USB**
- Fazer: ligar o VK-172 numa USB do Pi (de preferência pelo extensor).
- Esperado: `lsusb` mostra `1546:01a7 U-Blox AG` (o 7) ou `01a8` (o 8); `/dev/gps0 → ttyACM0`.
- Conferir: `ssh obd@$PI 'lsusb | grep -i 1546; ls -l /dev/gps0 /dev/ttyACM*; dmesg | tail -5'`.
- Se falhar: outro código USB → `deploy/70-obd-gps.rules` (`idVendor`/`idProduct`) e deploy de novo. Sem `ttyACM0` →
  `dmesg` (o módulo `cdc_acm` carregou?).

**B2. gpsd enxerga o receptor e dá posição**
- Fazer: esperar até 60 s em céu aberto.
- Esperado: `DEVICES` com o receptor e `driver` `u-blox`; `TPV` com `mode` 3 em **até 60 s** a frio. **Anotar o tempo
  até a 1ª posição.**
- Conferir: `ssh obd@$PI 'systemctl status gpsd --no-pager | head -5; gpspipe -w -n 12'`. Ver também qual pacote entrou:
  `dpkg -l | grep gpsd` (`gpsd-tools` ou `gpsd-clients` no Trixie).
- Se falhar: `deploy/gpsd.default` (`USBAUTO="true"`, `GPSD_OPTIONS="-n"`) e o bloco GPS do `deploy/install.sh`. Sem
  `gpspipe` → o pacote de clientes não entrou.

**B3. Painel e coletor**
- Esperado: Sistema → GPS "ok", **satélites ≥ 7, HDOP ≤ 1,5**; chip "GPS" no cabeçalho.
- Conferir: `ssh obd@$PI "$OBDCTL gps status"` e `"$OBDCTL gps check"` (linhas `coletor e GPS`, `satélites e HDOP`,
  `posição no gpsd`).
- Se falhar: poucos satélites → posição do receptor (no painel, sem nada metálico por cima). Os limiares para "posição
  boa" ficam em `[gps] min_sats = 5` e `max_hdop = 5.0` (`src/obdpi/config.py`); só mexer depois de uma semana de dados.

**B4. Taxa de 5 Hz** (precisa disso para o 0–100 e o dinamômetro)
- Esperado: `obdctl gps status` com `taxa=5.0 Hz (pedida 5)`; no `gpspipe`, `DEVICES` com `"cycle":0.20`.
- Conferir: `ssh obd@$PI 'timeout 10 gpspipe -w | grep -c TPV'` → ~50 em 10 s; `obdctl gps check` → `taxa no receptor` e
  `taxa medida pelo coletor` ok.
- Se ficar em 1 Hz: com o gpsd rodando, `ssh obd@$PI 'ubxtool -p CFG-RATE,200'` e conferir de novo. Se só assim
  funcionar: criar `/etc/systemd/system/gpsd.service.d/rate.conf` com `[Service]` +
  `ExecStartPost=/bin/sh -c 'sleep 3; ubxtool -p CFG-RATE,200'`, levar isso para o `deploy/install.sh` e registrar
  (o `?DEVICE` com `cycle` 0,2 em `src/obdpi/gps/source.py`, `set_rate`, não foi aceito por esse gpsd). 10 Hz é possível
  (`[gps] rate_hz`), mas não vale o tráfego.

**B5. Relógio pelo GPS (chrony)**
- Esperado: `chronyc sources -v` com a linha `GPS` e `Reach` > 0; timesyncd fora; com o Pi **sem internet**, o relógio
  acerta pelo GPS em **menos de 2 min** depois do boot.
- Conferir (com internet): `ssh obd@$PI 'chronyc sources -v; chronyc tracking'` e `obdctl gps check` → `chrony com o GPS`.
  Para conferir **sem internet** sem perder o SSH: desligar o hotspot, reiniciar o Pi pela chave, esperar 3 min com céu
  aberto, ligar o hotspot e logo em seguida
  `ssh obd@$PI 'journalctl -u chrony -b --no-pager | grep -i "selected source" | head -3'`. A **primeira** fonte escolhida
  tem de ser `GPS`, a menos de 2 min do boot. No painel: Sistema → GPS, campo "Hora" = "GPS"; `/api/system` com
  `clock_synced: true`. A sessão desse boot fica com `clock_source = 'gps'`. Depois disso o ntfy e a nuvem conectam
  (TLS) logo no boot, graças ao `obd-clock`.
- Se falhar: sem linha GPS → `/etc/chrony/conf.d/obd-gps.conf` (vem de `deploy/chrony-obd-gps.conf`) e o gpsd com `-n`.
  `adjtimex` sem mudar de 5 para 0 → `src/obdpi/timesrc.py` (`system_synced`).

**B6. Diferença entre o GPS e a internet**
- Esperado: com internet, a diferença entre GPS e NTP em `chronyc sources` **abaixo de 0,2 s**.
- Conferir: `ssh obd@$PI 'chronyc sources -v; chronyc sourcestats'` (coluna de offset da linha GPS × a dos servidores).
- Se falhar: ajustar `offset` (hoje `0.10`) em `deploy/chrony-obd-gps.conf`, fazer o deploy e repetir até ficar abaixo
  de 0,2 s. A linha GPS marcada `x` (falseticker) também pede esse ajuste.

**B7. Proteção do cartão**
- Fazer: com a proteção ligada (`scripts/protect.sh`), reiniciar duas vezes.
- Esperado: gpsd e chrony sobem nas duas vezes; o arquivo de deriva do chrony fica no cartão.
- Conferir: `ssh obd@$PI 'systemctl is-active gpsd.socket chrony; findmnt /var/lib/chrony'`.
- Se falhar: lista `KEEP` em `deploy/overlay.sh` (precisa ter `/var/lib/chrony`).

**B8. Alimentação na partida**
- Fazer: dar a partida com o receptor ligado; depois ler `vcgencmd get_throttled`.
- Esperado: o receptor (~50 mA) não pode piorar as quedas. **Só dá para julgar com o conversor 12→5 V de 3 A**: com a
  fonte atual o Pi já marca `0x50005` sem GPS.
- Conferir: `ssh obd@$PI 'vcgencmd get_throttled'` antes e depois; `obdctl gps check` → `alimentação do Pi`.
- Se falhar: é elétrico (conversor, cabo curto e grosso); não tem ajuste no código.

**B9. Tirar e recolocar o receptor com o motor ligado**
- Esperado: painel com "GPS não conectado" em **até 10 s**, e o coletor segue gravando o OBD; recolocado, o GPS volta
  sozinho em **até 30 s**.
- Conferir: `ssh obd@$PI "$OBDCTL status; $OBDCTL gps status"` (`reconexões` sobe de 1 ou 2, não sem parar).
- Se falhar: `src/obdpi/gps/reader.py` (`LOST_S = 5`, reconexão 2→30 s) e `source.py` (`GpsdSource`, regra dos 10 s sem
  linha, que só vale com receptor presente).

### Bloco 2: primeira volta (10 min no bairro)

**B10. Diário, trajeto e distância**
- Fazer: uma volta de 10 min com o painel ou o modo direção aberto.
- Esperado: no diário (Mapa → Diário), a viagem com origem e destino, rua e bairro pelo `geo.db` e paradas; trajeto no
  mapa sem "dentes"; distância do GPS × velocímetro com **0–5 %** de diferença; nenhuma reconexão.
- Conferir: telas Mapa → Diário e detalhe da viagem; `obdctl gps status`; `flags` da viagem sem `origem_tardia` numa
  partida normal.
- Se falhar (os limiares em `src/obdpi/gps/trip.py`): paradas a mais ou a menos → `STOP_MIN_S = 20` e `STOP_SPEED = 3`;
  subida inventada em trecho plano → `ALT_HYST_M = 5`; origem errada na partida a frio → `ORIGIN_LATE_MS = 20 s` e
  `ORIGIN_PARKED_M = 150`; trajeto serrilhado → `TRACK_EPS_M = 6` e os filtros de `reader.py`
  (`SKIP_AFTER_ACQUIRE`, `JUMP_SLOW_M`).

**B11. Lugares e cerca**
- Fazer: no mapa, criar **Casa** e **Trabalho** (raio padrão de 80 m); sair e voltar.
- Esperado: "saiu de Casa" e "chegou em Casa" uma vez só, sem piscar parado no portão; ao ligar o carro em casa não
  aparece "entrou em Casa"; o resumo no ntfy vem com "Casa → …" (`gps.summary_names = true`).
- Conferir: Mapa → Lugares, acontecimentos da viagem, notificação no iPhone.
- Se falhar: raio de cada lugar no editor (padrão `DEFAULT_RADIUS_M = 80` em `src/obdpi/gps/places.py`); lugar
  automático → `trip.AUTO_PLACE_M = 80`.

**B12. Trecho de referência** (depende da escolha do dono, C1)
- Fazer: depois de uma viagem que passe pelo trecho, Mapa → Trechos → Novo trecho, marcando início e fim no traçado.
  De preferência 0,5–3 km sem semáforo, sempre no mesmo sentido, com motor quente.
- Esperado: a passagem da própria viagem fica válida; nas seguintes, passagens válidas (motivos vazios); a partir de
  3, a regressão; no cartão "antes e depois" de um serviço, as linhas "Trecho N". Os bicos novos já foram trocados, então
  o "antes" desse serviço não existe mais: o trecho vale para os próximos reparos.
- Conferir: Mapa → Trechos → o trecho (gráfico com válidas verdes e inválidas cinza; motivos).
- Se falhar (`src/obdpi/gps/segments.py`): passagens perdidas → `RADIUS_M = 35` (portões) e `LEN_TOL = 0.15`; muitas
  "frio" → `WARM_ECT = 75`; muitas "vel" → `SPEED_TOL = 0.20`. Ajuste curto com poucas leituras (o grupo lento vem a cada
  ~20 s) → `SLOW_PAD_MS`.

**B13. Mapa de verdade no iPhone**
- Esperado: ladrilhos carregam pelo hotspot no Safari do iPhone, nos temas claro e escuro; sem internet aparece
  "sem internet: só o traçado"; a camada "📷 Radares" aparece no painel do Pi a partir do zoom 11.
- Conferir: aba Mapa no painel do Pi e, depois de destrancar com a `LOC_PASSWORD`, no painel online.
- Se falhar: `src/obdpi/web/static/gpsmap.js` (ladrilhos e filtro do tema escuro).

**B14. Nuvem com localização**
- Esperado: depois da volta, o painel online (destrancado) mostra o diário, o trajeto e os lugares; o estado ao vivo
  público **nunca** leva coordenada.
- Conferir: `curl -s $NUVEM/api/live | grep -c '"lat"'` → `0`; `curl -s $NUVEM/api/gps/logbook` sem cookie → 401.
- Se falhar: `src/obdpi/sync.py` (`gather_gps`, `is_loc`) e o `isLoc` em `cloud/src/index.js`.

### Bloco 3: desempenho (estrada plana, segura e sem trânsito; nada de excesso de velocidade)

**B15. Velocidade vertical (`climb`) e altitude**
- Fazer: numa reta plana, `ssh obd@$PI 'gpspipe -w -n 40 | grep TPV | grep -o "\"climb\":[-0-9.]*" | head'`; numa subida
  conhecida, olhar a inclinação no trajeto.
- Esperado: `climb` perto de 0 no plano, o que indica Doppler (UBX) e não diferença de altitude; inclinação plausível na
  subida; ruído de altitude parado de poucos metros.
- Se falhar (`src/obdpi/gps/terrain.py`): ruído alto → `WIN_M = 150` (aumentar); `climb` ruim → `CLIMB_SHARE = 0.8` (subir
  para nunca usar).

**B16. Arrancada 0–60 armada, com voz**
- Fazer: no modo direção (`/dirigir`, 🔊 ligado), armar o 0–100 e fazer só um 0–60. Antes, testar o som com
  `/dirigir?demo=1` no iPhone.
- Esperado: rodapé "GPS 5 Hz"; a corrida gravada com `hz = 5` e `source = gps`; a fala do resultado no iPhone.
- Conferir: Mapa → Desempenho → Arrancadas e retomadas (e o detalhe da viagem).
- Se falhar: `hz = 1` → voltar ao B4. Sem voz → `web/static/voice.js` e o botão 🔊 (só aparece com GPS).

**B17. 0–100 contra o aplicativo do celular**
- Fazer: uma arrancada até 100 em lugar seguro e permitido, com o aplicativo do celular gravando ao mesmo tempo.
- Esperado: diferença de **até ~0,2 s** (o receptor a 5 Hz erra ±0,05 s; o aplicativo do celular costuma ser o menos
  preciso dos dois).
- Se falhar (`src/obdpi/gps/perf.py`): largada → `EARLY_KMH = 25`; marcas → `CROSS_W_MS`. Corrida que não foi gravada como
  automática → `AUTO_PEDAL_MS`/`AUTO_PEDAL_SHARE`.

**B18. Calibração do dinamômetro (coastdown) e uma puxada**
- Fazer: Desempenho → Dinamômetro → assistente: duas descidas em **ponto morto** na mesma reta plana, uma em cada
  sentido, de ~100 até ~45 km/h. Depois, com motor quente, uma puxada em 3ª até perto do corte.
- Esperado: `CdA` entre 0,45 e 0,90 e `Crr` entre 0,007 e 0,020 aceitos; a puxada com pico **corrigido** entre 60 e
  160 cv (referência de fábrica do R18A: ~140 cv a 6.300 rpm) e sem a marca "subiu só até N rpm".
- Conferir: Desempenho → Dinamômetro (curva, `coast_live` no assistente).
- Se falhar (`src/obdpi/gps/dyno.py`): descida recusada → `COAST_OK`, `COAST_OPPOSITE = 150`; puxada recusada → `MIN_HZ = 3`
  (5 Hz de novo), `RATIO_TOL = 0.04`, `WARM_ECT = 75`. Valor alto ou baixo demais → **primeiro a massa** (C2), que pesa
  direto no resultado.

### Bloco 4: vias e radares

**B19. Radar conhecido**
- Fazer: passar por um radar conhecido da Grande SP com o modo direção aberto, acima de 40 km/h e dentro do limite.
- Esperado: "Radar a 500 metros. Limite 60." entre 520 e 200 m; o selo do limite ao lado da velocidade; na primeira fala
  do dia, o aviso de que o mapa é um auxílio.
- Conferir: modo direção (faixa do radar) e `live.gps.warn` (`curl -s http://$PI/api/live`).
- Se falhar (`src/obdpi/gps/roads.py`): radar não avisado → `RADAR_CONE = 30`, `RADAR_DIR_TOL = 60`, `RADAR_NEAR_WAY_M = 30`;
  via errada → `MATCH_M = 25`, `MATCH_ANG = 35`, `STICKY_M = 8`. Radar ausente no OSM → não tem ajuste no código.
  **Fora da Grande SP** não há aviso enquanto o `geo.db` não tiver o corredor (C5).

**B20. Cidade × estrada**
- Fazer: uma viagem com trecho de rodovia.
- Esperado: Mapa → 🏙️ Cidade × estrada com km e km/L de cada classe plausíveis; no detalhe da viagem, "Cidade X km ·
  Estrada Y km".
- Se falhar: `roads.ESTRADA` / `ESTRADA_LIMIT = 80` e os limites presumidos (`PRESUMED`).

### Bloco 5: rotina (dias a semanas de uso normal)

**B21. Marchas aprendidas e embreagem**
- Esperado: depois de ~600 amostras, as 5 relações aprendidas a menos de 10 % das de fábrica, e nada em `rejected`;
  **zero** avisos de patinação em 2 semanas, num carro saudável.
- Conferir: Desempenho → Marchas e embreagem (`/api/gps/gears`).
- Se falhar (`src/obdpi/gps/gears.py`): relação recusada → `SANITY = 0.10` (pneu diferente do 205/55 R16?, C3); aviso
  falso de patinação → `MATCH_TOL = 0.03`, `SLIP_GAP_MS`, e as condições de carga e pedal na nota da F5. A rotação
  atrasada em relação ao instante gravado (`value_ts`) também atrapalha aqui.

**B22. Velocímetro e pneus**
- Esperado: fator `k` aceito entre 0,85 e 1,15 (os painéis costumam marcar uns 3–7 % acima); a anotação "ponteiro × real"
  feita no painel do Pi bate com o `speedo_err`.
- Conferir: Desempenho → Velocímetro e pneus (`/api/gps/speedo`).
- Se falhar (`src/obdpi/gps/speedo.py`): demora para fechar → `KEEP_PAIRS = 240` e as 5 faixas de 60+ pares; PID 0D
  truncado ou arredondado → anotar e rever a média aparada.

**B23. Parada em posto**
- Fazer: abastecer com o Pi desligando junto com o carro; na partida seguinte, olhar Consumo.
- Esperado: o cartão "⛽ Abastecimento detectado" → valor pago → Registrar; o abastecimento fica ligado ao posto; o
  ranking "🏆 Postos" opina depois de 2 tanques medidos no mesmo posto.
- Se falhar (`src/obdpi/gps/fuelstop.py`): `STATION_M = 60` (raio do posto), `SAME_SPOT_M = 80`; posto ausente → criar um
  lugar do tipo `posto`.

**B24. Chegada prevista, autonomia, rotas e trânsito**
- Esperado: depois de algumas viagens no mesmo caminho, o quadro "Chegada prevista" aparece passados ~1,5 km, com erro
  pequeno no meio do caminho; a autonomia aparece depois de um tanque cheio; rotas com km/L e R$/km; o trânsito pede
  semanas (rotas com 6+ viagens).
- Se falhar: `src/obdpi/gps/eta.py` (`MIN_KM = 1.5`, `MIN_CELLS = 4`, `PACE`), `routes.py` (células das pontas),
  `traffic.py`.

**B25. Compartilhar a viagem em outro iPhone**
- Fazer: no modo direção, 📤 → "24 horas" → mandar pelo iMessage para o iPhone de outra pessoa; dirigir 5 min; depois,
  encerrar o link.
- Esperado: no Safari da outra pessoa, o carro andando no mapa com o trajeto **desta** viagem, a velocidade, a rotação e
  a temperatura, com atraso de poucos segundos pelo hotspot (o Pi envia a cada leitura do canal ao vivo; sem
  WebSocket, a página consulta a cada 3 s). Ao desligar o carro: "Carro desligado · última atualização HH:MM", **sem
  posição**. Encerrado: "Este link foi encerrado". A folha de compartilhar do iOS abre (no Chromium caía em "copiar o
  link").
- Conferir: a lista em Mapa → Compartilhar (visualizações); `obdctl gps check` → `localização na nuvem` ok.
- Se falhar: link "não encontrado" por mais de 30 s → `obd-sync` (`journalctl -u obd-sync`, `share_sync`); sem tempo real
  → `cloud/src/share.js` (`ShareRoom`, WebSocket) e `src/obdpi/gps/share.py` (`FIX_FRESH_MS = 10 s`).

**B26. Depois de uma semana**
- Rever com os dados reais os limiares marcados como iniciais: ruído de altitude (`trip.ALT_HYST_M`, `terrain.WIN_M`),
  tolerância das marchas (`gears.WIN`, `MATCH_TOL`), raio dos lugares (80 m), paradas (20 s), portões dos trechos (35 m),
  raio dos postos (60 m).
- Registrar cada ajuste no `docs/PROCESSO.md` e marcar os **[HW]** na `docs/GPS-ARQUITETURA.md` (F1–F9 e lista 10.5) como
  conferidos ou corrigidos.

---

## C) Pendências do dono

1. **Trecho de referência:** qual pedaço do caminho habitual serve de bancada. De preferência uma subida constante de
   0,5–3 km, sem semáforo, feita sempre no mesmo sentido (item B12).
2. **Massa do carro:** o dinamômetro usa hoje **1.425 kg** (`[vehicle] mass_kg = 1350` + 75 kg × (2 ocupantes − 1)).
   Confirmar o peso com o tanque pela metade e quantas pessoas costumam andar no carro. Muda em Desempenho →
   Dinamômetro (massa e ocupantes) ou em `/etc/obd/obd.toml`, seção `[vehicle]` (`mass_kg`, `occupants`).
3. **Pneu:** confirmar 205/55 R16 (`[vehicle] tyre`), porque entra nas marchas e no velocímetro.
4. **`gps.notify_routine`** (desligado por padrão): ligar ou não os avisos de rotina no iPhone ("Parou em … por N min",
   lembrete do pós-serviço da oficina, "Autonomia baixa"). Desligado, tudo aparece no painel e nada vai para o celular.
   Para ligar: `notify_routine = true` na seção `[gps]` de `/etc/obd/obd.toml` e `sudo systemctl restart obd-collector`.
5. **Mapa do corredor:** gerar ou não o `geo.db` com o corredor até Cristais Paulista (ver o fim da parte A2). Sem ele,
   não há radar nem limite pelo mapa fora da Grande SP.
6. **`power.always_on`** (vigia de estacionamento e corte por bateria baixa): **só** com instalação elétrica permanente
   (relé temporizado ou sensor de ignição, conversor 12→5 V de 3 A, fusível próprio) **e** internet no carro. Antes
   disso fica `false`. Para ligar: `[power] always_on = true` (e `min_batt_v`, padrão 12,0 V) em `/etc/obd/obd.toml` e
   `scripts/deploy.sh` de novo, porque só o `install.sh` cria a regra de sudo do desligamento.
7. **Senha de localização:** guardar a `LOC_PASSWORD` no gerenciador de senhas do iPhone. Para nunca mandar posição para
   a nuvem, mesmo com a senha lá: `[gps] cloud = "off"`.
8. **Cabo extensor USB** para o receptor ficar no painel (B1) e **conversor 12→5 V de 3 A** (A2, B8).
