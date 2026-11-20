# Diário do processo — OBD Civic 2008

Registro cronológico de tudo que foi feito no projeto.

## Objetivo

Raspberry Pi 3 Model B v1.2 ligado no carro (Honda Civic 2008 1.8 flex, manual), conectado via
Bluetooth a um adaptador OBD-II ELM327. A cada 20 s ele coleta todos os sensores suportados pela
ECU e grava localmente. Um painel web servido pelo próprio Pi (acesso por IP / `obd.local`) mostra
dados em tempo real, relatórios e uma página dedicada à saúde do **corpo de borboleta** (TBI/DBW).
Substitui o app Car Scanner, que só registra com o app aberto.

## 2026-11-18 — Fase 0: sistema operacional no cartão SD

### Decisões
| Item | Escolha | Motivo |
|---|---|---|
| SO | Raspberry Pi OS Lite 64-bit **Trixie** (2026-10-06) | Sem desktop → sobra RAM (1 GB); BlueZ e NetworkManager oficiais |
| Hostname | `obd` → `obd.local` via avahi (mDNS) | Acessar sem saber o IP |
| Usuário | `obd` (senha aleatória em `secrets/credenciais.txt`, fora do git) + chaves SSH ed25519 deste PC | SSH sem senha |
| WiFi | "Wake Up 2" (principal) e "garagem" (alternativa), país BR, via `network-config` (cloud-init/netplan → NetworkManager) | Pi entra sozinho no WiFi |
| Fuso / teclado | America/Sao_Paulo / br | — |

### Como foi gravado (WSL2 + Windows)
O WSL não enxerga leitores USB, então a gravação foi feita pelo lado Windows:

1. Baixada `2026-10-06-raspios-trixie-arm64-lite.img.xz` de `downloads.raspberrypi.com`
   e conferido o SHA256 oficial (OK). Descompactada (3,08 GB).
2. Gerados `os/boot/user-data` e `os/boot/network-config` (cloud-init — o Trixie não usa mais
   `wpa_supplicant.conf` nem `userconf.txt`). Versões sem segredo: `*.example`.
3. Script `os/flash-sd.ps1`, executado elevado (UAC), que:
   - valida o alvo (disco 3, USB, 32 010 928 128 bytes, não é disco de sistema);
   - limpa o disco e grava a imagem raw em `\\.\PhysicalDrive3`;
   - lê de volta e compara SHA256 → **Verificação OK**;
   - monta a partição boot e copia `user-data`, `network-config`, cria `ssh` e acrescenta
     `cfg80211.ieee80211_regdom=BR` no `cmdline.txt`.
4. Arquivos temporários com senha apagados de `D:\SDs\obd`.

> Problemas encontrados: a 1ª execução gravou certo mas falhou na verificação
> (`[math]::Min` com Int32 > 2 GB). Corrigido e criado `-SkipWrite` para refazer só verificação + config.

### Primeiro boot (próximo passo — com o Pi)
1. Ejetar o cartão no Windows, colocar no Pi, ligar (fonte 5 V 2,5 A).
2. Esperar ~3–5 min (cloud-init cria usuário, expande partição, instala avahi, reinicia).
3. No PC: `ssh obd@obd.local` (ou pelo IP que aparecer no roteador).
4. Conferir: `cloud-init status`, `nmcli dev`, `bluetoothctl show`.

## Próximas fases
Ver `docs/ARQUITETURA.md`.

## 2026-11-18 — Prévia da interface
- Mockup navegável (dados simulados) em `web/mockup/painel.html`, publicado como artefato:
  https://claude.ai/artifact/1NkrbnkLgDTzTbuu9sXMSY
- Telas: Ao vivo (rotação, velocidade, temperatura, bateria; gráfico pedal × borboleta; nota de
  saúde da borboleta; grade de todos os PIDs), Corpo de borboleta (tendência), Viagens e alertas (DTCs).
- WiFi alternativo "garagem" adicionado ao `network-config` do cartão.

## 2026-11-18 — Arquitetura (Claude Fable)
- Plano completo em `docs/ARQUITETURA.md`: driver ELM327 próprio via socket RFCOMM, coletor
  Python síncrono (varredura completa a cada 20 s + grupo rápido da borboleta a 1–2 Hz),
  SQLite WAL, FastAPI + SSE, SPA Svelte + uPlot compilada no PC, mDNS + ntfy com o IP,
  10 regras para o corpo de borboleta (R1–R10) e roteiro em fases com critérios de aceite.
- Divergência a resolver: a arquitetura sugere partição `/data` separada, mas o cartão já foi
  gravado com expansão automática do root no 1º boot. Opções: aceitar `/data` como diretório
  no root (mais simples) ou encolher o root depois em outra máquina Linux.

## 2026-11-18 — Respostas do dono
- Corpo de borboleta foi **trocado** → linha de base nova; 1ª anotação do diário de manutenção.
- Requisito: **monitorar todos os sensores e alertar qualquer valor fora do padrão** → seção 7A da arquitetura.
- Celular iPhone → notificações via app ntfy. Hotspot "Android 17" adicionado ao `network-config`
  do cartão (3 redes: casa, garagem, hotspot).
- iPhone: ativar **Ajustes → Acesso Pessoal → Maximizar Compatibilidade** (o Pi 3 só enxerga 2,4 GHz).

## 2026-11-18 — Primeiro boot OK
- `obd.local` → 192.168.200.105 (cabo) e 192.168.200.205 (WiFi "Wake Up 2"). SSH por chave funcionando.
- Kernel 6.18.50 aarch64, cloud-init `done`, root expandido (29 GB, 10% usado), 905 MB RAM (764 livres).
- Bluetooth `hci0` B8:27:EB:15:60:6B ligado; avahi, ssh ativos; relógio sincronizado (NTP); Python 3.13.5.
- ⚠️ `vcgencmd get_throttled = 0x50005` → **subtensão agora e CPU limitada**. A fonte atual é fraca;
  trocar por 5,1 V / 2,5 A (oficial) em casa e um conversor 12→5 V de 3 A reais no carro.
- Obs.: o PC está em 192.168.17.x e o Pi em 192.168.200.x (redes diferentes), mas há rota entre elas.

## 2026-11-18 — Sistema completo instalado no Pi (v0.1.0)
Implementado (`src/obdpi/`), seguindo a arquitetura com simplificações conscientes:
- **Driver ELM327** próprio (`elm/`) com socket RFCOMM direto; cabeçalhos desligados (ATH0) e
  remontagem do multi-frame CAN pelo formato `0:`/`1:` do ELM (mais simples que ATH1, funciona em qualquer protocolo).
- **Coletor** (`collector/main.py`): procura e pareia sozinho qualquer adaptador com nome de OBD
  (OBD, ELM, V-LINK, Vgate…; PIN 1234 via `bt-agent`), descobre os PIDs, grupo rápido da borboleta a ~1 Hz,
  **todos os sensores a cada 20 s**, DTCs 03/07 + luz de injeção a cada 20 s, freeze frame, viagens,
  relógio com correção retroativa, commit a cada 5 s, watchdog systemd.
  Simplificação: uma thread só escreve no banco (sem thread escritora separada).
- **Alertas** (`analytics/limits.py`): faixas absolutas por sensor + linha de base aprendida (|z| > 4,5)
  + sensor que sumiu. **Borboleta** (`analytics/throttle.py`): regras R1–R6, R9 + DTCs da lista de vigilância; nota 0–100 por viagem.
- **Notificações** no iPhone via ntfy (fila `outbox`, envia quando houver internet) e aviso de IP a cada conexão WiFi
  (`/etc/NetworkManager/dispatcher.d/90-obd-announce`). Tópico em `/etc/obd/secrets.toml`.
- **Painel web** (`web/`): FastAPI + SSE, front em JS puro sem build (simplificação: em vez de Svelte/Vite).
  Abas: Ao vivo, Borboleta, Viagens, Alertas/DTCs, Sensores (histórico de qualquer sinal), Relatório (+CSV), Sistema (+diário de manutenção).
- **Serviços**: `obd-collector`, `obd-web` (porta 80), `obd-btagent`, `obd-maint.timer` (backup semanal).
  journald só em RAM, timers apt/man-db e cloud-init desativados.
- Deploy: `scripts/deploy.sh` (rsync + `deploy/install.sh`, idempotente).

Testes:
- `tests/test_parsing.py` (parser, multi-frame, erros, DTC, regras R4/R6, limites) — ok.
- Ponta a ponta no Pi com `ELM327-emulator` (TCP): conectou, CAN 11/500, multi-PID, 47 PIDs descobertos,
  ~1 leitura/s, 38 sinais a cada 20 s, viagem detectada; todas as rotas da API 200, SSE ok.
- Instalação real: serviços ativos, painel em `http://obd.local`, push de teste enviado, varredura BT vendo os
  dispositivos da casa (OBD ainda fora de alcance).

Bug corrigido nos testes: cooldown das regras da borboleta suprimia o 1º alerta.

## 2026-11-18 — Primeira sessão no carro
- Pi pareou sozinho com o adaptador **"OBD2"** (clone ELM327 v1.5, MAC AA:BB:CC:11:22:33). Protocolo **CAN 29 bits 500k**, 31 PIDs
  (lista em `docs/pids-civic-2008.md`). VIN não lido (a investigar).
- Achados do motor: marcha lenta ~830 rpm (oscilação 138 rpm), MAP 32 kPa, bateria 14,1 V carregando.
  ⚠️ **STFT (PID 06) fixo em +46,88 % desde a partida** e λ medido 1,11–1,14 vs 0,98 comandado → indício de mistura pobre /
  **entrada falsa de ar** (suspeita: junta do corpo de borboleta trocado). A confirmar comparando com outro scanner.
- ⚠️ **O Pi reiniciou na partida** (subtensão `0x50005`): precisa de conversor 12→5 V 3 A de qualidade.
- Problemas resolvidos:
  - Adaptador ficava "preso" após reiniciar o coletor → `bluetoothctl disconnect` no início e após 2 falhas + fechamento no SIGTERM.
  - Clone só devolve o **1º quadro** de respostas multi-frame em CAN 29 bits → PIDs agrupados para caber num quadro único
    (`pack_single_frame`). Taxa do grupo rápido: 0,8 → 1,7 leitura/s (~10 pedidos/s no total).
  - `obd-web` demorava a parar com SSE aberto → `timeout_graceful_shutdown=2`, `TimeoutStopSec=8`.
- Funcionalidades novas (pedidos do dono): consumo (MAF + λ comandado, mistura gasolina/etanol, calibração tanque a tanque),
  botão **⛽ Tanque cheio**, aba Consumo, km/L instantâneo/viagem/real; cartão do motor (ligado há, km hoje/total);
  histórico de motor ligado (barras por dia + linha do tempo); **modo direção** `/dirigir` (iPhone 17 Pro Max, retrato/paisagem,
  marcha calculada pelas relações do câmbio, dicas de troca, quadros configuráveis, vermelho fora do normal, previsão e autonomia);
  **teste 0–100** (Pi mede só velocidade+rpm na taxa máxima, interpola 0–60/80/100); ajuste curto e avanço no Ao vivo;
  **recomendações** completas por alerta (`analytics/advice.py`: o que é, causas, verificar, fazer, urgência);
  alertas repetidos agrupados ("continua N×"); sensores com faixa normal escrita, verde/vermelho; gráfico individual de
  qualquer sensor (tempo real 15 min + histórico 1 h–30 dias); tema Escuro/Claro/Auto (padrão escuro).
- Coleta: todos os sensores a cada **5 s** (era 20 s), principais o mais rápido possível, DTCs a cada 20 s.

## 2026-11-18 — Diagnósticos, relatórios, IA, rede e revisão do Fable
- Novos: diagnóstico ar × MAF (speed-density, veredito por viagem), bateria na partida, termostática, monitores (PID 01),
  nota de direção econômica, relatórios semanal/mensal (painel + push automático via `obd-maint`, a cada 1 h verifica se fechou período),
  botão **Analisar dados** (Claude Opus 5.5, streaming + adaptive thinking + fallback automático; chave em `/etc/obd/secrets.toml`),
  modo gráfico no modo direção, viagem retomada quando o Pi cai com o motor ligado.
- Rede: perfil `hotspot-iphone` com IP fixo 172.20.10.14 e serviço `obd-netprefer` que troca para o hotspot quando ele aparece.
- **Revisão pelo Claude Fable** (somente leitura, 3 bugs reproduzidos). Corrigido:
  1. sincronização do NTP antes da 1ª sessão deslocava o banco inteiro → correção a partir de `boot_ts`;
  2. relógio sem NTP fazia juntar viagens de dias diferentes → junção só se `run_time` da ECU > uptime do Pi;
  3. queda do BT > 120 s deixava o resto da viagem sem `trip_id` → nova viagem se o motor está ligado e não há viagem;
  4. valores antigos gravados como novos com NO DATA → só grava sinais lidos recentemente; 5 ciclos vazios → `EcuSilent`;
  5. descoberta parcial de PIDs → une com a última descoberta, refaz teste multi-PID;
  6. alertas agrupados só na mesma viagem e push quando a severidade sobe;
  7. resposta vazia do modo 03/07 não "apaga" códigos;
  8. 0–100: estado "pronto" expira em 5 min e grava amostras durante o teste;
  9. `TimeoutExpired` do bluetoothctl escapava e derrubava o serviço;
  10. banners de alerta comparavam a hora do Pi com a do celular.
  Ruído: ajuste curto fixo do ECU é ignorado (valor sem variação), limites de tensão ajustados à carga controlada (ELD) da Honda,
  estol só confirmado se a ECU continuar respondendo (desligar a chave não alerta), R3b exige 3 leituras, "sensor parou" só
  para sinais que já responderam, contagens escaladas para o período de 5 s.
- Testado: 9 testes unitários + 70 s contra o emulador no Pi sem erros; instalado no carro e coletando.
- Aba Sistema: cartão **Análise por IA** para escolher o provedor (Claude/Anthropic ou OpenAI), salvar as chaves
  (em `/data/obd/ai.json`, modo 600, nunca devolvidas ao painel; vazio mantém, "-" apaga) e o modelo da OpenAI.
  OpenAI via HTTP (`/v1/chat/completions`). Sem chave, o botão mostra o aviso de configuração (testado).

## 2026-11-19 — Painel online (Cloudflare)
- Pedido do dono: acessar o painel a qualquer hora, mesmo com o Pi desligado, em `obd.cleberson.social.br`, **sem senha** (público),
  com sinal de carro ligado/desligado; todas as viagens registradas e gerenciáveis.
- Arquitetura: "espelho de documentos". O Pi gera cada resposta da API local e envia as que mudaram (hash SHA-1) para um
  Cloudflare Worker, que guarda em D1 (`doc(key, json)`) e serve o mesmo painel estático. Toda a lógica continua no Pi;
  o Worker só lê documentos → cabe no plano grátis (CPU mínima, poucas gravações).
  - `obd-sync` (Pi): estado ao vivo a cada 10 s, conjunto completo a cada 5 min (10 min dirigindo, sem os períodos longos),
    histórico de cada sensor em 1 h/6 h/24 h/7 d/30 d, detalhe de cada viagem, relatórios, recomendações.
  - Worker: público e só leitura (ações recusadas com orientação para o painel do Pi); `DASH_PASSWORD` opcional reativa
    login com sessão assinada e a fila de ações (tanque cheio, anotação, editar viagem, pedir análise) que o Pi aplica ao conectar.
  - Carro desligado: se o Pi não envia há mais de 60 s, o estado ao vivo vira `pi_offline` (motor desligado) com o horário em que foi visto.
- Viagens gerenciáveis: nome, anotação, ocultar (`POST /api/trips/{id}`), lista com km/L e nota eco, detalhe com 6 gráficos.
- Publicado com o token da conta Cloudflare do projeto `cleberson` (zona `cleberson.social.br`): D1 `civic-obd`, Worker
  `civic-obd`, domínio próprio `obd.cleberson.social.br`. Token de sincronização em `secrets/cloud.txt` (fora do git).
- Testes: Worker local (`wrangler dev`) e no ar — páginas 200, só leitura (409), sync exige token (401 sem), ping ok.
- Pendente: o Pi estava desligado; um vigia aguarda ele aparecer na rede para instalar `obd-sync` e fazer a 1ª sincronização.

## 2026-11-19 — Pi instalado na nuvem, correções e relatório do carro
- Pi apareceu na rede às 12:29; `obd-sync` instalado e nuvem configurada (`deploy/set-cloud.sh`). Painel online recebendo dados.
- Bluetooth: no boot o chip BCM43438 não inicializou ("Reset failed -110", com subtensão `0x50005`). Reboot resolveu;
  adicionado `deploy/bt-ensure.sh` (recarrega `hci_uart` se o controlador sumir) antes do `obd-btagent`.
  `install.sh` não aborta mais se um serviço falhar.
- Bug de viagem: como o Pi liga depois da partida, "motor ligado antes do Pi" era sempre verdade e juntou a viagem de hoje
  com a de ontem (15 h). Nova regra: continua a viagem só se o contador da ECU de tempo desde a partida (PID 1F) não zerou
  (`trip.last_run_time`). Viagem 11 corrigida (18/11 19:06–21:12, 7,5 km) e viagem 12 iniciada hoje.
- Correção de diagnóstico: o ajuste curto varia rodando e só crava (+46,9 %) em marcha lenta, que é o limite da ECU. Logo é
  **real**, e a supressão de "valor fixo" foi removida para ele.
- Relatório completo em `docs/RELATORIO-2026-11-19.md` (também no botão 🤖 do painel e online): entrada falsa de ar após a
  troca do corpo de borboleta; junta do corpo de borboleta e duto de admissão em primeiro lugar.

## 2026-11-19 — Relatórios por histórico (desde o dia e por viagem)
- Pedido: escolher um dia (ex.: saí com o carro em 17/11) ou uma viagem e ver o relatório só daquele trecho, com "O que fazer" e plano de ação.
- `reports.bounds()` resolve 4 tipos: `week`/`month` (calendário), `since&day=AAAA-MM-DD` (do dia até o fim de hoje, comparado com o mesmo número de dias antes) e `trip&trip=id` (uma viagem, comparada com a anterior, incluindo a viagem em andamento).
- O relatório traz a lista das viagens do período, com o número de alertas de cada uma; tocar numa abre o relatório só dela.
- Painel: botões Semanal · Mensal · Desde o dia · Viagem; seletor só com dias/viagens que existem (mostra km e ⚠️ alertas); ◀ ▶ navegam no histórico.
- Nuvem: o Pi envia também um relatório por viagem e um "desde o dia" para cada dia com viagem nos últimos 120 dias (`KEEP` ganhou `day` e `trip`, igual no Worker e no Pi). Até o Pi sincronizar a versão nova, o painel online avisa que o relatório ainda não está disponível.
- Teste novo: `test_report_since_and_trip`.

## 2026-11-19 (noite) — Tempo real, celular, análise nova, tanque cheio pelo valor
Pedidos: melhorar a análise dos dados; modo direção todo em tempo real; melhorar celular e UI/UX; coleta independente do painel; tanque cheio só com o valor pago; botão de limpar alertas; Alertas e Relatório dentro de Diagnóstico; diagnóstico mais fácil de entender; IA ao fim de cada viagem.

- **Tempo real**
  - Coletor: ajuste curto + avanço (06, 0E) e sonda (34) entram num rodízio dentro do ciclo rápido, só para a tela (a gravação continua na varredura de 5 s).
  - Painel do Pi: SSE confere o estado 4×/s (antes 1×/s).
  - Nuvem: fio próprio no `obd-sync` envia o estado a cada 1 s com o motor ligado (10 s desligado), numa conexão HTTPS mantida aberta, lendo `/run/obd/live.json` direto; o Worker virou SSE de verdade (confere o D1 1×/s, conexões de ~40 s).
- **Coleta independente do painel**: já era (serviço `obd-collector` separado, o painel só lê). O envio ao vivo agora também não passa pelo painel.
- **Regras da borboleta refeitas** (`analytics/throttle.py`): os dados reais mostraram comando 100 % = posição 78 % (posição ≈ 11,7 + 0,667 × comando). As regras antigas comparavam com diferença fixa ou com reta ajustada quase só em marcha lenta e geravam falsos críticos em aberturas grandes.
  - R1/R2/R2t: modelo por faixas de 5 pp (`BinModel`), só em regime, só onde já aprendeu.
  - R3: pedal pisado de verdade (os dois sensores) por ~3 s sem resposta.
  - R9: salto só conta se o sensor par ficou parado (toque rápido no pedal não é salto).
  - R4/R5: marcha lenta só depois de assentar.
- **Análise por sistema** (`analytics/insight.py`): veredito em linguagem simples para mistura, marcha lenta, borboleta, temperatura, ar admitido, elétrica, códigos e consumo; causas prováveis da mistura errada com pontuação (entrada de ar × MAF × combustível), evolução dia a dia; mistura e marcha lenta valem pelo último dia com dados. Vai nos relatórios (semana, mês, desde o dia, viagem), no Diagnóstico e no contexto da IA.
- **IA ao fim de cada viagem**: `obd-sync` chama `/api/ai/auto` a cada 2 min com internet; viagem ≥ 5 min ou ≥ 1 km, terminada há mais de 3 min, sem laudo → laudo curto (`SYSTEM_TRIP`, esforço médio) + aviso no celular. Precisa da chave em Sistema.
- **Tanque cheio**: só o valor pago; combustível e preço por litro herdados do último; coluna `fuel_fill.total`; custo real por km; apagar abastecimento. No painel online (público) só esse formulário aceita envio: validado, no máximo 5 pendentes, 1 a cada 20 s; o Worker só entrega ao Pi já atualizado (`?cap=total`).
- **Limpar alertas**: arquiva em `alert_archive` (não apaga); só no painel do Pi.
- **Pi "offline" logo depois de ligar** (2026-11-20): sem relógio próprio, o Pi fica minutos atrasado até o NTP acertar; a nuvem comparava a hora informada por ele e mostrava offline. O Worker agora usa a hora em que recebeu o envio (`updated_ts`) para "último sinal" e para a idade dos dados ao vivo.
- **Hotspot com insistência** (`deploy/obd-netprefer.sh`): nos primeiros 10 min após ligar procura o hotspot a cada ~10 s (depois, 30 s), com busca dirigida ao nome dele; se a conexão falhar, garante a volta para a rede anterior ("garagem") e continua tentando.
- **Viagens curtas fora do histórico**: motor ligado por menos de 3 min (`db.SHORT_TRIP_S`) fica com `hidden=2` ao finalizar (e as antigas, uma vez, na migração); aparecem com "Mostrar ocultas e curtas". Se a viagem for retomada e passar de 3 min, volta.
- **Resetar tudo** (Sistema, só no painel do Pi; `obdpi/reset.py`): o painel grava `reset.pending` e reinicia o coletor, que ao subir move o banco e os backups diários para `backup/antes-do-reset-<data>/`, zera o aprendizado da borboleta e cria um banco novo; o `obd-sync` apaga a cópia antiga na nuvem. Mantém Bluetooth, WiFi, chaves de IA e nuvem.
- **Celular**: cabeçalho compacto, barra inferior com Ao vivo · Viagens · Consumo · Diagnóstico · Mais; Diagnóstico com Resumo · Alertas · Relatório · Borboleta; tabelas viram cartões; alerta do topo só se for recente de verdade (1 + contador); modo direção: faixa de alerta não ocupa mais a tela, quadros esmaecidos com o carro desligado.
- Tanque do Civic 2008: fichas técnicas dão 50 L (mantido).

## 2026-11-19 (noite, parte 2) — Testes guiados, consumo médio ao vivo, canal ao vivo por WebSocket
- **Consumo médio em tempo real**: quadro "Consumo médio da viagem" no Ao vivo (km/L, litros, R$) e "Média do tanque" no modo direção (viagens desde o tanque + a viagem de agora).
- **Testes guiados "caça-problema"** (`web/static/tests.js`, Diagnóstico → Testes): rodam no navegador sobre as leituras ao vivo.
  - Caça-vazamento: mede o repouso, o dono escolhe o ponto e borrifa limpa-carburador; reação = ajuste curto cai, rotação muda ou sonda enriquece → bipe e vibração, resultado por ponto.
  - Varredura do pedal e da borboleta (ignição ligada, motor desligado): sensores D×E, saltos, curso, borboleta A×B.
  - Marcha lenta com tudo ligado: compensação da rotação e tensão do alternador com ar + farol alto + desembaçador.
  - Resultado fica no aparelho e, no painel do Pi, no diário de manutenção (entra no contexto da IA).
- **Atraso do painel online**: o caminho pelo banco (Pi → D1 → SSE a cada 1 s) dava 1–3 s. Novo canal: Pi envia cada leitura (gzip, conexão mantida) para `POST /api/sync/live`; um Durable Object (`LiveHub`) repassa por WebSocket (`/api/ws`) aos painéis abertos. Medido daqui: **~150 ms** do envio até o navegador. A cópia no D1 segue a cada 5 s (ignição ligada) ou 10 s, para quem abre depois e como reserva (SSE).
- **Testes guiados ampliados (8)**: caça-vazamento, raio-X da mistura (lenta/1.500/2.500 rpm → entrada de ar × MAF × combustível), sondas e catalisador (2.500 rpm por 45 s), acelerada e volta à marcha lenta, varredura do pedal, marcha lenta com carga, bateria na partida, ventoinha do radiador. Todos com condições marcadas ao vivo antes de começar, gráfico dos últimos 45 s, bipes e último resultado no cartão.
- **Sensores coloridos**: aba Sensores agrupada por sistema (motor, ar, combustível, sondas, borboleta, temperatura, elétrica, central), cada grupo com a sua cor, valor grande, barra mín–méd–máx de 24 h e vermelho quando fora do normal.
- **Tanque cheio no painel online**: o abastecimento pendente aparece na hora no histórico ("aguardando o Pi"); mesmo valor em menos de 1 h não duplica.

## 2026-11-19 (noite, parte 3) — Pi mais resistente
O Pi fica fixo no carro e perde a energia a cada desligada. Melhorias só de software, aplicadas pelo `install.sh`:
- Cão de guarda do chip (`RuntimeWatchdogSec=15`): travou, reinicia sozinho.
- `fsck.repair=yes` no boot e gravação no cartão a cada 5 s (antes 30 s).
- WiFi sem economia de energia (menos atraso no ao vivo e no SSH).
- Banco: backup diário conferido (7 cópias, antes semanal com 2); se o banco amanhecer corrompido, o coletor restaura o backup íntegro mais recente, guarda o arquivo ruim (com -wal/-shm, que antes ficavam para trás) e avisa no celular. Teste `test_corrupt_db_restores_backup`.
- Página de ideias de expansão (GPS, 4G, CAN, acelerômetro, câmera…): https://claude.ai/artifact/Kv93ZhNmJaoJuetEdDFvSd

## 2026-11-19/20 — As 12 ideias de software
Pedido: implementar todas, sem perguntar. Código novo em `src/obdpi/extras.py` (Pi) e `web/static/extras.js` (telas).
1. **Impressão digital do motor** (`fp_*`): média/desvio de MAF, MAP, ajuste curto, avanço, sonda e borboleta por faixa de 500 rpm × 20 % de carga, motor quente. Referência gravada pelo dono (painel do Pi); a comparação usa só leituras posteriores e marca o que mudou, com a pior faixa. Diagnóstico → Resumo.
2. **Antes e depois dos consertos** (`impact`): cada serviço anotado compara 4 dias antes × 4 depois (ajuste parado/rodando, variação da lenta, ar medido, temperatura, consumo, alertas/h).
3. **Página do mecânico**: `/oficina` (veredito, causas com evidências, sistemas, DTCs, alertas, evolução, serviços e testes).
4. **Replay da viagem**: controle deslizante com todos os sensores no instante escolhido, reprodução e atalhos para 8 s antes de cada alerta.
5. **Perguntas à IA**: `/api/ai/ask` + histórico (`ai_chat`), na janela "Analisar dados". Só no painel do Pi, com chave.
6. **Gasolina × etanol**: usa o km/L medido em cada combustível (tanque a tanque); sem os dois, regra dos 70 %.
7. **Plano de manutenção**: hodômetro informado + km do Pi; 10 itens com intervalos típicos; uso severo (muito tempo parado ou trajetos curtos) corta pela metade os itens sensíveis. Aba Mais → Manutenção.
8. **Custo dos hábitos**: litros e R$ gastos parado e acima de 3.000 rpm, por viagem e por período.
9. **Resumo da viagem no celular** ao fechar a viagem (km, km/L, R$, tempo parado, alertas).
10. **Aplicativo**: manifest, ícones, atalhos (modo direção, tanque cheio, testes).
11. **Sem internet**: service worker (rede primeiro, cópia guardada se cair). Não guarda o fluxo ao vivo.
12. **Dados além do básico**: `obdctl probe-extra` (ajuste longo, modo 06 com falha de ignição por cilindro, quadro congelado, modo 09). Exploratório: precisa do carro ligado e o adaptador clone pode cortar respostas longas.
- Painel online (público): além do tanque cheio, aceita anotar serviço, hodômetro e item de manutenção feito (campos validados, fila com teto); o Pi só recebe as ações de manutenção depois de atualizado (`cap=maint`).
