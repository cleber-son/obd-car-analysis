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
- **Redes WiFi sumiram** (2026-11-20 09:19): corte de energia enquanto o NetworkManager regravava a configuração do netplan apagou as três redes (sobrou um `90-NM-*.yaml` de 0 byte); o perfil `hotspot-iphone` criado pelo instalador estava sem senha e nunca conectou. Agora as redes são perfis próprios do NetworkManager gravados por `deploy/set-wifi.sh` (senhas de `os/boot/network-config`, pelo `scripts/deploy.sh`), com cópia em `/etc/obd/wifi/`; o `obd-netprefer` restaura da cópia ao iniciar e a cada 5 min se um perfil sumir, ficar vazio ou sem senha (testado: perfil zerado à mão voltou em 1 s).
- **Autoconserto de rede** (`deploy/obd-netprefer.sh`): sem rede e com rede conhecida à vista sobe um degrau por vez (reconectar a cada ~30 s dos 45 s aos 3 min 45 s → rádio off/on → reiniciar NetworkManager aos 6 min → recarregar `brcmfmac` aos 9 min); sem nenhuma rede à vista por 5/10 min, rádio e driver. Na estrada sem rede conhecida não faz nada; nunca reinicia o Pi. Falha no hotspot tem espera crescente (20 s a 5 min) para não derrubar o WiFi de casa. Diário em `/data/obd/net.log`.
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

## 2026-11-20 — Sistema somente leitura (proteção do cartão contra corte de energia)
- `deploy/overlay.sh` (pacote overlayroot + `overlayroot=tmpfs` no cmdline.txt, como o raspi-config, mas sem travar a partição de boot). Com a proteção ativa, o que o sistema grava fora de `/data` vai para a RAM; os arquivos do sistema no cartão não são reescritos (foi a reescrita durante o corte que apagou as redes WiFi).
- `/data`, `/var/lib/bluetooth` e `/var/lib/systemd/timesync` continuam no cartão: `obd-data.service` remonta o cartão (`/media/root-ro`) como gravável e liga essas pastas por bind. Se isso falhar, ele desliga a proteção e reinicia (para não gravar dados que sumiriam).
- Não há partição `/data` separada (a raiz se expandiu no 1º boot), por isso o cartão fica montado gravável por baixo; só `/data` e o obd-doctor (`overlay.sh persist`, depois de uma correção aprovada) escrevem nele.
- Ligar/desligar: `scripts/protect.sh <host> on|off|status` (reinicia o Pi). Com a marca `/etc/obd/protegido`, o `scripts/deploy.sh` destrava, reinicia, atualiza, religa e reinicia de novo.
- Não resolve a reinicialização na partida por subtensão (isso é elétrico: no-break/conversor).

## 2026-11-21 — Alertas zerados a cada partida
- Pedido: o painel recomeça zerado a cada partida, para saber se **esta** sessão tem alerta.
- Coletor (`collector/main.py`): `recent_alerts` passa a ser só da viagem atual (viagem nova → vazio; viagem retomada após queda do Pi → recarrega do banco). Novo campo ao vivo `session_alerts` {trip_id, start, n, crit, warn}.
- Painel: o número vermelho do Diagnóstico/Alertas conta só os alertas da partida (amarelo se só avisos); novo quadro "🚦 Nesta partida" em Diagnóstico → Alertas; o histórico de 90 dias fica abaixo. Modo direção mostra "✓ nenhum alerta nesta partida" no rodapé.
- Análise do dia após trocar a junta da borboleta (bateria desligada): ECU zerada (0 km / 0 aquecimentos desde a limpeza). Na mesma temperatura (60–80 °C, lenta) os números são iguais aos de antes: ajuste curto +46,9 % cravado, λ 1,17, pós-cat 0,01 V, MAF ÷ esperado 0,55 × 0,56. A junta não resolveu a entrada de ar.
- **Testes guiados com voz** (`tests.js`): fala em pt-BR (Web Speech; liberada no 1º toque, exigência do iPhone), botão 🔊/🔇 no teste. Cada fase fala a instrução; resultado final falado.
  - Caça-vazamento refeito: "posicione o spray" → 3, 2, 1 → **"borrife agora"** (3 s) → **"pare de borrifar"** (9 s observando) → fala "reagiu"/"sem reação". Bipe tipo contador Geiger que acelera e fica mais agudo conforme o ajuste cai. Opção **Sequência por voz** passa por todos os pontos sem tocar na tela.
  - Novo teste **Reaprendizado da marcha lenta**: espera a ventoinha ligar e desligar, conta 10 min, avisa "faltam X minutos", pede para tirar o pé se a rotação subir e diz quando pode desligar.
- Pi atualizado (deploy com proteção do cartão) e painel online publicado (wrangler, versão 2b600cb5). Conferido no ar: tests.js novo, quadro "Nesta partida", `session_alerts` chegando ao vivo.
- Caça-vazamento: pontos "Base do bico 1…4" (a sequência por voz passa bico por bico). Publicado no painel online; o Pi recebe no próximo deploy (estava desligado).
- Voz não tocou no iPhone: o Safari descarta speak() logo depois de cancel() e pode descartar a fala se o objeto for coletado. Agora cancela e fala 150 ms depois, guarda as falas, chama resume(); botão "🔊 Testar o som" antes de começar (com dica do modo silencioso).

## 2026-11-21 — Teste nos bicos: anéis de baixo vazando
- Caça-vazamento só nos bicos (ordem 1, 1, 4, 3, 2): bico 1 fraco na 1ª vez (λ 1,11→1,00) e forte na 2ª (λ 0,86); bico 4 forte (λ 0,90); bicos 3 e 2 quase apagaram o motor (624 e 656 rpm, a ECU abriu a borboleta para segurar). Reteste às 15:57: reação λ 0,84.
- Conclusão: **anéis de vedação de baixo dos bicos vazando**, pior no 2 e no 3. Foto do dono: anel de baixo rasgado e esfarelando. Bate com o caça-vazamento de 21/01 (o ajuste caiu 32 pontos em volta dos bicos).
- Dono tirou os bicos; não conseguiu trocar só os anéis (bicos muito sujos) e **comprou bicos novos (chegam 2026-11-22)**. Peça mais citada: 16450-RNA-A01 (8 furos, flex, 2007–2011); um anúncio de LXS 2008 mostra 16450-RNV-M01. "WL03 AASS" gravado no bico não é o código da peça: conferir pelo chassi ou comparando com o velho (comprimento, conector, ponta).
- Furos dos bicos no coletor sujos, com restos de anel velho grudados. Orientação: tampar os furos, aspirar (sem ar comprimido), tirar a borracha com ferramenta de plástico/madeira, um furo por vez, pano com limpa-carburador. Anéis novos de cima e de baixo, com óleo de motor; conferir o clique da presilha verde; ligar a ignição 2–3 vezes e procurar vazamento antes da partida.
- **Próximo passo (quando o dono voltar):** rodar 🔁 Reaprendizado da marcha lenta + 30–50 km, e comparar com hoje. Linha de base a vencer: ajuste curto +46,9 % cravado na lenta, λ 1,17, MAF ÷ esperado ≈ 0,55. Resolvido se o ajuste curto ficar entre −10 e +10 % na lenta e a sonda perto de λ 1,00.
- Pendente: deploy do `tests.js` atual no Pi (nomes Bico 1…4, voz no iPhone, Testar o som); o painel online já tem. Conferir se a anotação do teste (vinda da nuvem) chegou ao banco do Pi.

## 2026-11-22 — Teste guiado "Bicos injetores novos"
- Pedido: prioridade para testar a troca dos bicos, feita hoje. Novo teste 💉 no topo de Diagnóstico → Testes (`tests.js`, `injTest`), com voz:
  1. **Antes da partida**: confirma montagem (bicos encaixados e presilhas, anéis novos de cima e de baixo com óleo de motor, flauta e conectores, nada esquecido no motor); 3 ciclos de ignição para a bomba pressurizar; pergunta se há gota/cheiro de combustível. Vazou → "não dê a partida" e encerra.
  2. **Partida** (até 40 s) e nova olhada nos bicos com o motor ligado (vazou → "desligue o motor agora").
  3. Espera o motor chegar a 80 °C, mede **1 min de marcha lenta** (descarta os 15 s iniciais) e **2.500 rpm por 25 s**.
  4. Compara com a linha de base de 21/11 (ajuste curto +46,9 %, λ 1,17). Meta: ajuste −10 a +10 % na lenta e a 2.500, λ 0,95–1,05, lenta 650–900 rpm estável (±30, sem quedas de 100 rpm), sem código/MIL. Diz a causa provável (ainda entra ar / falta combustível / rica: bico pingando).
  5. Se ainda pobre, emenda no **caça-vazamento só nos 4 bicos** (`leakTest(BICOS)` já começa a sequência pelos bicos).
- Já dá para começar com o motor ligado (pula a parte de antes da partida).
- Simulado no PC (DOM falso, relógio acelerado, carro falso) nos cenários resolvido, ainda pobre e rico.
- Publicado no painel online (versão 30466ddf). O Pi recebe no próximo deploy.

## 2026-11-22 — GPS, Fase 1: fonte, gravação e relógio (sem receptor ainda)
Plano completo em `docs/GPS-ARQUITETURA.md` (seção 12, F1). Receptor VK-172 (u-blox 7, USB) ainda não chegou: tudo feito e testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **Código novo:** `src/obdpi/gps/` — `nmea.py` (RMC/GGA/GSA/VTG, montador de época, quadros UBX de taxa e modelo automotivo), `source.py` (gpsd por TCP/JSON, serial direta de reserva, replay de arquivo, nula; "auto" = gpsd e, se ele não existir, a serial), `reader.py` (fio "gps" dentro do coletor: filtros de aquisição, salto impossível e data absurda; fila de 5 min; reconexão 2→30 s; nunca morre), `geo.py`, `store.py`, `hub.py` (ponte com o coletor; engole qualquer erro), `sim.py` (viagem com GPS e OBD coerentes, `FakeGpsd`, cenários commute/tunnel/towed). `src/obdpi/timesrc.py` (relógio sincronizado com timesyncd **ou** chrony). `web/gps_api.py` (`/api/gps/status`, `/api/gps/live`).
- **Banco:** todas as tabelas do GPS (`gps_fix`, `trip_gps`, `place`, `geo_event`, `route`, `segment`, `segment_pass`, `gps_model`, `dyno_run`, `fuel_stop`) e as colunas novas (`trip.gps_km/obd_km/drive_score`, `accel_run.*`, `fuel_fill.place_id`, `outbox.expire_ts`, `session.clock_source`). Só acréscimos.
- **Coletor:** todos os ganchos da seção 6. Posições gravadas a 1 Hz andando, 1 a cada 30 s parado, com a hora no mesmo relógio do `fast_sample` (intervalo de 200 ms do receptor preservado). Sem internet, a hora do GPS acerta a sessão inteira para trás (inclusive `gps_fix`, `geo_event`, `accel_run`). `live.json` ganha `gps` **sem posição**; a posição fica em `/run/obd/gps.json` (privado). Com ignição desligada ou Bluetooth fora, o GPS segue gravando.
- **Sem GPS nada muda:** `live.gps = {"state": "desconectado"}`, distância e resumo da viagem iguais aos de hoje (teste `test_hub_without_gps_is_silent`).
- **Painel:** cartão "📡 GPS" em Sistema (estado, satélites, HDOP, taxa, origem da hora, 1ª posição, posições hoje, mapa offline) e chip "GPS" no cabeçalho quando há GPS configurado. Sem receptor: "GPS não conectado…".
- **Ferramentas:** `obdctl gps status`, `obdctl gps sim` (`--out` .nmea/.jsonl, `--to-db --days N`, `--serve PORTA`). Retenção de `gps_fix` (24 meses, só com disco < 15 % livre). `obd-doctor` vigia gpsd e chrony só se instalados.
- **Deploy preparado (não aplicado):** `install.sh` instala gpsd + chrony (chrony substitui o timesyncd), regra udev `/dev/gps0`, `obd-clock.service` (no boot, acerta o relógio pela última gravação do banco), `/var/lib/chrony` na proteção do cartão (`overlay.sh`).
- **Testes:** `tests/test_gps.py` (17 testes) + fixtures `tests/fixtures/gps/{sample.nmea,gpsd-session.jsonl}`; os 12 antigos continuam passando. Integração: coletor real contra ELM327 falso (TCP) + gpsd falso por 10 min, tirando e recolocando o receptor no meio: sem exceção, fio do GPS < 0,1 % de CPU no PC.
- **Pendente para o dia do hardware (lista 10.5, itens 1–9):** código USB (`lsusb` = 1546:01a7?), nome do pacote (`gpsd-tools` ou `gpsd-clients` no Trixie), se o gpsd aceita `cycle` 0,2 (5 Hz), `adjtimex` mudando de 5 para 0 com o chrony, remoção do timesyncd sem quebrar o boot, `offset` do chrony × NTP, receptor tirado/recolocado com o motor ligado, consumo na partida (`vcgencmd get_throttled`). O deploy da F1 só depois de o dono pedir (ele está no diagnóstico dos bicos).

## 2026-11-22 — Ao vivo sem dados velhos; "Analisar dados" vai para o Diagnóstico
- Bug: com o carro desligado o Ao vivo mostrava a última leitura (rotação 146, sonda, etc.) como se fosse atual. A nuvem guardava o último estado do Pi e o devolvia inteiro, só trocando o `state` para `pi_offline`.
- Correção em três lugares: o Worker esvazia `values` quando o Pi está sem sinal há mais de 60 s; o painel (`app.js`, `freshLive`) e o modo direção (`drive.html`) descartam, com o Pi ligado, qualquer sinal lido há mais de 45 s (o grupo lento é lido a cada 20 s). Com a central calada (ignição desligada, Pi ainda ligado) só a tensão da bateria continua aparecendo.
- O botão "🤖 Analisar dados" saiu do topo; virou o cartão **🤖 Laudo da IA** no início de Diagnóstico (mesma janela de laudo e perguntas).

## 2026-11-22 — GPS, Fase 2: diário de bordo, lugares e estacionamento (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F2, com as "Notas da implementação da F2"). Tudo testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **Fechamento da viagem** (`gps/trip.py`, primeiro item de `FINALIZERS`): distância híbrida (velocidade do GPS segundo a segundo, OBD × fator do velocímetro onde falta GPS; só vale com ≥ 50 % de cobertura), origem e destino com nome do lugar (ou coordenada), origem herdada do estacionamento quando o GPS demora a pegar sinal, paradas, altitude, custo (R$ e R$/km pelo último preço), trajeto reduzido (≤ 800 pontos, colunas do motor alinhadas, buracos de túnel marcados) e lugar automático ("Lugar frequente N") para um destino que se repete em 3 dias.
- **Lugares** (`gps/places.py`, `gps/geocode.py`): cerca virtual com histerese (entrou/saiu em `geo_event`, visitas), "onde estacionei" (`gps_model['parked']`, link do Mapas do iPhone) e aviso crítico **"o carro mudou de lugar desligado"** no boot seguinte, descontando o que o OBD e o PID 1F explicam. As ações dos lugares ficam guardadas (padrões: Casa sincroniza, Oficina lembra do serviço) e passam a rodar na F8.
- **Km sem OBD:** carro andando pelo GPS sem viagem do OBD vira `geo_event km_sem_obd`; o hodômetro estimado soma e a tela de Manutenção mostra a composição ("informado 182.000 + … km pelo GPS + … km pelo velocímetro + … km sem OBD").
- **Resumo no celular:** "Viagem encerrada: Casa → Trabalho · …" (decisão do dono: com os nomes; `gps.summary_names = false` volta a só números). Só nomes de lugares salvos, nunca coordenada. Sem trajeto o resumo é igual ao de antes.
- **API** (`web/gps_api.py`, bloco F2): `/api/gps/logbook`, `/api/gps/trips/{id}`, `/api/gps/trips/{id}/track`, `/api/gps/places` (listar/criar/editar/apagar), `/api/gps/parked`, `/api/gps/events`. A tela (aba Mapa) é da F3.
- **Outros:** `notify.flush_outbox` não envia aviso vencido (`expire_ts`); "Resetar tudo" leva lugares, trechos e modelos do GPS para o banco novo; `obdctl gps reanalyze [--trip N | --all]`; simulador com `straight()` e Casa/Trabalho no banco de desenvolvimento. Configuração: `gps.summary_names`, `vehicle.tyre = "205/55 R16"`, `vehicle.occupants = 2` (decisões do dono).
- **Sem GPS nada muda:** `test_no_gps_trip_matches_old_distance` (velocidades e intervalos irregulares) confere a distância igual à conta antiga, sem `gps_km`/`obd_km`, e o título "Viagem encerrada" de sempre.
- **Testes:** `tests/test_gps_trip.py` (18 testes); suíte inteira 47 testes, repetida 12 vezes seguidas e 2 com a CPU ocupada, sem falha intermitente.
- **Pendente para o dia do hardware:** precisão real da origem na partida a frio; raio adequado dos lugares (80 m); limiares de parada (20 s) e de altitude (5 m) com o ruído real do VK-172.

## 2026-11-22 — GPS, Fase 3: mapa, exportação e painel online protegido (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F3, com as "Notas da implementação da F3"). Tudo testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **Aba Mapa** (no "Mais" do celular): **Diário** (cada viagem com "Casa → Trabalho", km, duração, média, km/L, custo, paradas, alertas; toque abre a viagem; GPX dos últimos 30 dias), **Mapa** (trajetos recentes, **mapa de calor** de ajuste curto, carga, km/L, velocidade, temperatura e MAP, **alertas no lugar onde aconteceram** com filtro por regra, linha do tempo e "este alerta acontece sempre aqui", lugares, carro estacionado e posição atual) e **Lugares** (criar tocando no mapa ou pela posição atual, editar, apagar). Sem GPS: "GPS não conectado…" e telas vazias, sem erro.
- **Detalhe da viagem:** cartão 🗺️ Trajeto com "Colorir por" (velocidade, rotação, marcha, carga, ajuste curto, λ, consumo, temperatura, avanço, MAP, borboleta, altitude), legenda com mínimo/mediana/máximo, saída, chegada, paradas e alertas no mapa, botões GPX/KML (só no Pi). O **replay** move o cursor no mapa, destaca o pedaço percorrido e ganhou ×1/×10/×60; tocar no traçado mostra os sinais daquele ponto e leva o replay até ele. Viagens antigas: "Sem trajeto (antes do GPS)". A lista de Viagens mostra a rota sob a data.
- **API** (`web/gps_api.py`, bloco F3): `/api/gps/alerts` (alertas e códigos com a posição mais próxima, ±10 s), `/api/gps/heat`, `/api/gps/trips/{id}/export.gpx|kml`, `/api/gps/export.gpx`. `gps/export.py` gera GPX 1.1 e KML em fluxo.
- **Leaflet 1.9.4** em `web/static/` (sem CDN), conferido pela integridade do pacote no npm e pelos SRI publicados; ladrilhos do OpenStreetMap carregados pelo navegador. Não há CSP no Pi nem no Worker. `sw.js` (`civic-obd-v2`) guarda os arquivos do mapa e **não guarda nada com posição**.
- **Painel online — localização protegida (falha fechada):** no Worker, todo `/api/gps/*` (menos as rotas só com números: status, gears, speedo, score, perf, dyno, terrain, roadmix) exige o cookie de localização, criado com a senha `LOC_PASSWORD` em `POST /loc/unlock` (30 dias; trocar a senha invalida os cookies; `POST /loc/lock` tranca). **Sem `LOC_PASSWORD` a localização simplesmente não existe na nuvem:** o Worker não serve nem guarda documentos de posição, e o `ping` diz `loc: false`. A senha do painel (`DASH_PASSWORD`) não libera a localização. Exportação GPX/KML na nuvem: 404. O estado ao vivo público nunca leva coordenada (teste no Pi e limpeza extra no Worker).
- **`obd-sync`:** só envia posição (diário, trajetos, lugares, estacionado, alertas com posição, calor, posição ao vivo) **depois** de o `ping` da nuvem responder `loc: true`, e só com `gps.cloud = "auto"` (padrão). Se a senha sumir da nuvem, apaga de lá o que tinha enviado. Lugares criados no painel online entram na fila e o Pi aplica (`cap=…,gps`).
- **Testes:** `tests/test_gps_trip.py` +6 (`test_alerts_with_position`, `test_heat_cells`, `test_gpx_kml_valid`, `test_api_gps_f3_routes`, `test_live_json_no_position_with_places`, `test_sync_sends_location_only_when_cloud_confirms`): suíte inteira 53 testes, 10 vezes seguidas e 2 com a CPU ocupada, sem falha. `cloud/test/loc.test.mjs` (7 testes, `npm test` na pasta `cloud`: 9 no total) cobre `isLoc`/grafias alternativas, cookie válido/adulterado/vencido/de outra senha, sem senha, `DASH_PASSWORD` sozinha, fila só com autorização e só para `cap=gps`, exportação 404 e o ao vivo sem posição. Conferência no Chromium (celular, claro e escuro, sem rede para os ladrilhos, banco simulado e banco vazio): sem erro de JavaScript.
- **Para publicar (quando o dono pedir), nesta ordem:**
  1. Nuvem primeiro: `cd cloud && npx wrangler d1 execute civic-obd --remote --file=schema.sql` (cria a tabela `share`, usada na F9; repetível).
  2. `npx wrangler secret put LOC_PASSWORD` — segredo novo do Worker, **nome exato `LOC_PASSWORD`**, escolhido pelo dono (16+ caracteres aleatórios; o único freio contra tentativa é a espera de 0,8 s por senha errada). Guardar em `secrets/cloud.txt`. Sem ele, o painel online continua igual a hoje e nenhuma posição sobe. Não definir `LOC_DELAY_MS` (é só dos testes).
  3. `npm run deploy` (o `build.sh` já copia `leaflet.js`, `leaflet.css`, `gpsmap.js` e `gps.js`).
  4. Depois o Pi (deploy normal). **No Pi não há nada novo para configurar:** ele não guarda a senha; descobre pelo `ping` se a nuvem tem `LOC_PASSWORD` e usa o `[cloud] url/token` que já existe. Para nunca enviar posição, mesmo com a senha na nuvem: `[gps] cloud = "off"` em `/etc/obd/obd.toml`.
  5. Conferir: sem desbloquear, `https://…/api/gps/live` responde 401 `{"locked": true, "configured": true}`; a aba Mapa pede a senha; depois de desbloquear, o diário e o mapa aparecem (após a próxima sincronização completa, até 5 min).
- **Pendente:** ladrilhos reais e Safari do iPhone não conferidos (sem rede no ambiente de teste); `wrangler dev` não rodado (o Worker foi testado com D1 falso). O mapa real aparece com a primeira viagem com o receptor.

## 2026-11-22 — Nome do programa: Delfos

- O painel passa a se chamar **Delfos**, com o slogan **"o motor fala, Delfos traduz."** (escolha do dono).
- Mudou: cabeçalho do painel (nome + slogan + carro), `manifest.webmanifest` (nome do app no iPhone), títulos de
  `index.html`, `drive.html` ("Delfos · Dirigir"), `oficina.html` (o resumo diz que foi gerado pelo Delfos), tela de
  login da nuvem, título da API, `obdctl notify-test` e **todas as notificações do ntfy** (`notify.send` assina
  "Delfos · …" quando o título ainda não começa com Delfos).
- O cache do app no iPhone virou `delfos-v3` (o antigo é descartado sozinho). Para o ícone mostrar o nome novo na
  tela de início, pode ser preciso remover e adicionar o atalho de novo.
- Não mudou (de propósito): nome do Worker/D1 (`civic-obd`), domínio, User-Agent do sync, nome do repositório e
  pacote Python — trocar isso quebraria a nuvem sem ganho para quem usa.

## 2026-11-22 — GPS, Fase 4: rotas e trecho de referência (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F4, com as "Notas da implementação da F4"). Tudo testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **Rotas** (`gps/routes.py`, finalizador): viagens pelo mesmo caminho (mesmas pontas, células de ~220 m em comum ≥ 60 %, distância a ±20 %) viram uma rota "Casa → Trabalho"; caminho alternativo vira "· via 2"; a volta é outra rota. Por rota: medianas de km, tempo, km/L, L/100 km, R$/km e custo; melhor e pior viagem; por combustível (gasolina/etanol/mistura pelo tanque) e por horário de saída; perfil de tempo a cada 250 m (para a F8). Viagem com parada de mais de 10 min no meio fica "atípica", fora das medianas.
- **Trecho de referência** (`gps/segments.py`, finalizador): o dono marca início e fim sobre o traçado de uma viagem (Mapa → Trechos → Novo trecho). Toda passagem no mesmo sentido é medida: tempo, velocidade, paradas, litros, L/100 km, ajuste curto, erro da sonda, MAP, carga, borboleta, rotação, avanço, ar medido ÷ esperado, temperaturas, pressão, subida e mistura. Só vale com motor quente, sem parar, velocidade normal e GPS bom (as outras aparecem em cinza com o motivo). Histórico em gráfico com os serviços anotados e o veredito de cada um (até 5 passagens antes × 5 depois).
- **"Antes e depois"** (Diagnóstico): cada serviço ganha as linhas "Trecho N · Ajuste curto / Ar medido ÷ esperado / Consumo" quando há passagens válidas dos dois lados. Aparece só "Trecho N" (sem o nome: `/api/impact` é público na nuvem). Sem trecho ou sem GPS a resposta é a mesma de antes.
- **API** (`web/gps_api.py`, bloco F4): `/api/gps/routes`, `/api/gps/routes/{id}` (+ renomear), `/api/gps/segments` (+ criar), `/api/gps/segments/{id}` (+ renomear, ativar/desativar, reprocessar, apagar). `obd-sync` envia rotas e trechos só com a senha de localização.
- **Testes:** `tests/test_gps_routes.py` (8 testes: agrupamento, variante e volta, estatística por combustível e atípica, sentido e validade das passagens, busca idempotente, antes/depois do conserto, integração com o `extras.impact()`, sincronização). Suíte inteira 61 testes, 7 vezes seguidas (uma com outro fuso), sem falha; `npm test` da nuvem passa (Worker não mudou). Conferido no Chromium (celular, claro e escuro, banco simulado de 6 dias): criar trecho tocando no traçado, rotas, histórico e "antes e depois", sem erro de JavaScript.
- **Pendente para o dia do hardware:** o dono escolher o trecho real (item 10 da lista 10.5) — de preferência 0,5–3 km sem semáforo, feito sempre no mesmo sentido; conferir com o VK-172 o raio de 35 m dos portões e a tolerância de ±15 % no comprimento; ver se a leitura lenta a cada ~20 s dá amostras de ajuste curto suficientes em trechos curtos.

## 2026-11-22 — GPS, Fase 5: marchas, embreagem, velocímetro e nota de condução (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F5, com as "Notas da implementação da F5"). Tudo testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **Marchas aprendidas** (`gps/gears.py`): com GPS, o Delfos mede quantos km/h cada marcha dá a cada 1.000 rpm (depende do pneu real) e passa a usar essas relações na marcha exibida. Até aprender (600 leituras em regime), ou sem GPS, vale a tabela de fábrica exatamente como antes.
- **Embreagem patinando:** em 3ª, 4ª ou 5ª, com o pé no acelerador, se a rotação ficar 4–22 % acima do que a velocidade real explica por ~1,5 s e depois voltar ao normal na mesma marcha, sai o aviso "Embreagem patinando" (crítico a partir de 3 vezes em 7 dias), com orientação. Trocas, reduções a fundo e embreagem pisada não contam. O aviso "o carro mudou de lugar desligado" também ganhou orientação.
- **Velocímetro:** compara a velocidade da central com a do GPS em velocidade constante, por faixa de 10 km/h. Com 5 faixas: "A central informa 100 km/h quando o carro está a 96,5 (−3,5 %)". O fator corrige a distância e a velocidade quando o GPS falta. Acompanha os pneus mês a mês (relação da 5ª) e aceita a anotação do ponteiro do painel.
- **Nota de condução** (`gps/score.py`): 0–100 por viagem (≥ 2 km, ≥ 3 min, GPS em 70 % do tempo andando): freadas fortes, arrancadas fortes, curvas fortes, tempo acima de 115 km/h (limites das ruas na F7), motor frio forçado e giro acima de 5.500. Cada freada/arrancada/curva vira alfinete no mapa da viagem; dicas no detalhe; "nota N" no Diário e no resumo do celular. A nota econômica (pelo motor) continua.
- **Tela:** Mapa → 🏁 Desempenho (marchas, embreagem, velocímetro e pneus, nota com tendência). **API:** `/api/gps/gears` (+ `reset`), `/api/gps/speedo` (+ ponteiro), `/api/gps/score`. Na nuvem as três sobem sempre (só números, sem posição).
- **Sem GPS nada muda:** `test_gear_fallback_without_gps` confere a marcha numa grade de velocidade × rotação igual à conta antiga, também com GPS desligado e modelo no banco; `test_hub_without_gps_is_silent` e os testes antigos passam sem alteração.
- **Testes:** `tests/test_gps_drive.py` (11 testes: aprendizado das 5 relações a ±1,5 % com pneu 3 % menor, sem GPS igual a antes, patinação detectada (1 evento) e crítico no 3º em 7 dias, nenhuma patinação em trocas/reduções/arrancadas, fator 0,960 ± 0,005 com central 4 % otimista, "ainda aprendendo" com 4 faixas, nota calma ≥ 90 e agressiva entre 55 e 70 com os 7 eventos a ±30 m, viagem curta sem nota, API, comando de reset, sincronização). Suíte inteira 75 testes, 5 vezes seguidas sem falha; `npm test` da nuvem passa. Conferido no Chromium (celular, claro e escuro, banco simulado): Desempenho, nota no Diário e no detalhe da viagem, sem erro de JavaScript.
- **Pendente para o dia do hardware:** relações reais do carro com o pneu atual; limiar de patinação em uso real (esperado: zero avisos em 2 semanas num carro saudável); se a central trunca ou arredonda o PID 0D; quanto a rotação chega atrasada em relação ao instante gravado (`value_ts`); ruído real do rumo nas curvas e da velocidade nas freadas.

## 2026-11-22 — Dois falsos alarmes depois da troca dos bicos

- **"Motor morreu em marcha lenta" ao desligar a chave.** O estol era confirmado 8 s depois da queda de rotação
  mesmo sem resposta da central (leitura vazia contava como "central respondendo"). Agora só conta rpm 0 lido de
  verdade: precisa de 6 leituras em pelo menos 12 s, ou de uma nova partida logo em seguida. Depois de 60 s sem
  confirmar, entende que foi a chave e esquece. Testes: `test_keyoff_with_ecu_lingering_is_not_stall`,
  `test_stall_then_restart_alerts`.
- **"Sensor fora do comportamento habitual" sem fim.** A linha de base só aprendia valores dentro do habitual, então
  depois de um reparo (ajuste de +47 % → perto de 0) ela nunca se atualizava e avisava para sempre. Agora, depois de
  ~10 min seguidos fora e sempre do mesmo lado, o novo valor vira o habitual e ela reaprende. Os limites absolutos
  (ajuste fora de ±15 % etc.) continuam valendo. Teste: `test_baseline_adopts_new_level_after_repair`.

## 2026-11-22 — GPS, Fase 6: 0–100, dinamômetro, inclinação e altitude (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F6, com as "Notas da implementação da F6"). Tudo testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **0–100 e retomadas** (`gps/perf.py`): com GPS, toda arrancada de propósito (pé no fundo, 0–60 em até 12 s) e toda retomada 40–80, 60–100 e 80–120 na mesma marcha são medidas sozinhas, pela hora do receptor a 5 Hz (±0,05 s). O botão ⏱ 0–100 do modo direção continua igual; com GPS bom quem mede é o GPS ("GPS 5 Hz" no rodapé). Em subida/descida de mais de 2 % a corrida fica marcada e não vale recorde. Recordes por tipo, marcha e combustível.
- **Dinamômetro** (`gps/dyno.py`): cada puxada em 2ª, 3ª ou 4ª (pé no fundo ≥ 3 s, mesma marcha, motor quente) vira curva de potência e torque, medida e corrigida para o nível do mar. No simulador o pico sai a ±3 % e a rotação a ±250 rpm (~143 cv corrigidos, perto dos ~140 cv de fábrica). Massa e ocupantes ajustáveis no painel; **calibração por descida em ponto morto** nos dois sentidos (CdA e Crr a ±5 %).
- **Inclinação e altitude** (`gps/terrain.py`): inclinação no trajeto (cor "Inclinação" no mapa) e no modo direção; por faixa de inclinação e de altitude: carga, MAP, borboleta, ajuste curto, sonda, avanço, consumo e ar. Leitura automática: ajuste curto bem maior em descida/pé leve que em subida forte = "assinatura de entrada de ar". Altitude × potência com a linha teórica e o aviso de restrição na admissão.
- **Modo direção:** quadros "Velocidade real (GPS)", "Altitude" e "Potência agora" (entram sozinhos quando o GPS aparece) e **voz** (`voice.js`): "Zero a cem em 10,8 segundos. Novo recorde.", retomadas e "Embreagem patinando." Botão 🔊/🔇 só aparece com GPS. `/dirigir?demo=1` fala um resultado por minuto (conferência no iPhone pendente com o dono).
- **Sem GPS nada muda:** `test_hub_without_gps_is_silent` e os 75 testes antigos passam sem alteração; o teste armado sem GPS segue pelo OBD (testado).
- **Testes:** `tests/test_gps_perf.py` (14 testes). Suíte inteira 89 testes, 5 vezes seguidas (uma com outro fuso), sem falha; `npm test` da nuvem passa (Worker não mudou). Conferido no Chromium (celular, claro e escuro, banco simulado): Desempenho, modo direção ao vivo e demonstração, sem erro de JavaScript.
- **Pendente para o dia do hardware:** taxa real de 5 Hz; se a velocidade vertical do gpsd é Doppler; ruído real da altitude (a janela de 150 m pode precisar crescer); comparar uma arrancada com um aplicativo de GPS do celular; valores absolutos de potência (a massa pesa direto); voz no iPhone.

## 2026-11-22 — GPS, Fase 7: radares, limites, cidade × estrada e nomes de rua (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F7, com as "Notas da implementação da F7"). Tudo testado com simulador e com respostas do OpenStreetMap escritas à mão. **Nada foi para o Pi nem para a nuvem.**
- **Mapa offline** (`scripts/geo-build.py`): roda no PC e gera `data/geo/geo.db` a partir do OpenStreetMap (radares, vias com limite e mão única, postos, oficinas, bairros e cidades). A área é escolhida pelo dono: `python3 scripts/geo-build.py --bbox S,W,N,E` (ou `--city "Nome" --uf SP`; `--inner LAT,LON,KM` para ruas locais só perto de casa). O `scripts/deploy.sh` passa a enviar o arquivo para `/data/obd/geo/` (só quando muda). Rodado de verdade uma vez numa área de teste longe do dono (Gramado, RS): 0,3 MB, 773 vias, 2 radares com sentido, abre e responde.
- **Modo direção:** selo do limite ao lado da velocidade (esmaecido quando o mapa não tem a placa e o limite é presumido pelo tipo de via; vermelho acima do limite), faixa "📷 Radar em 380 m · limite 50" e **voz**: "Radar a 500 metros. Limite 60.", "Reduza. 68, limite 60.", "Limite 60.". É **auxílio**: a faixa diz sempre que o mapa pode estar desatualizado e que vale a placa, e a primeira fala de radar do dia lembra disso. Limite presumido nunca é falado.
- **Cidade × estrada** (`gps/roads.py`): cada viagem separada em cidade, estrada e misto (pelo tipo de via do mapa; sem mapa, pela velocidade em janelas de 1 minuto) com km, tempo, km/L, L/100 km, rotação, carga, ajuste curto, temperatura e marchas; período e km/L por mês na aba Mapa → "🏙️ Cidade × estrada". No simulador (10 km de cidade + 30 de rodovia) o km/L de cada tipo bate com o do simulador a ±5 %.
- **Diário de bordo** com rua e bairro na origem e no destino ("Avenida X, Bairro Y"; a cidade só aparece fora da cidade de Casa). O resumo no celular continua só com os nomes dos lugares salvos.
- **Nota de condução** passa a usar o limite de cada rua quando o mapa tem a placa (o presumido não desconta). Trajeto ganha a cor "Limite".
- **Sem GPS e sem `geo.db` nada muda:** `test_hub_without_gps_is_silent` e os 89 testes antigos passam sem alteração; sem o arquivo, nenhum limite nem aviso aparece no `live.json`.
- **Testes:** `tests/test_gps_roads.py` (10 testes; fixtures `tests/fixtures/gps/overpass-*.json`). Suíte inteira 99 testes, 5 vezes seguidas (uma com outro fuso), sem falha; `npm test` da nuvem passa (Worker não mudou). Conferido no Chromium (celular, claro e escuro): aba Cidade × estrada, camada de radares, modo direção ao vivo e demonstração falando o radar, sem erro de JavaScript.
- **Pendente:** o dono dizer **qual região cobrir** (cidade, região metropolitana, estradas habituais) para gerar o `geo.db` de verdade; no dia do hardware, passar por um radar conhecido com o modo direção aberto (item 13 da lista) e conferir a voz no iPhone.

## 2026-11-22 — GPS, Fase 8: postos, ações dos lugares, chegada, autonomia e trânsito (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F8, com as "Notas da implementação da F8"). Tudo testado com simulador. **Nada foi para o Pi nem para a nuvem.**
- **Parada em posto** (`gps/fuelstop.py`): desligou num posto (lugar do tipo posto ou posto do mapa offline) e saiu do mesmo ponto entre 2 e 25 min depois → aparece em Consumo "⛽ Abastecimento detectado": basta digitar o valor pago, e o tanque fica ligado ao posto (ou "Não abasteci"). O botão "Tanque cheio" normal também liga o posto sozinho quando acha a parada ou a posição. **Ranking de postos**: quanto rende o combustível de cada posto contra a sua média (com 2 tanques medidos) e se o ajuste curto muda depois de abastecer ali.
- **Ações dos lugares:** ao chegar, ao sair ou ao partir depois de N minutos parado: avisar no celular (vale 30 min), falar no modo direção, sincronizar com a nuvem na hora (Casa, por padrão) e anotar; Oficina anota "Visita à oficina, 3 h 10" ao sair depois de 20 min. Editáveis em Mapa → Lugares.
- **Chegada e autonomia** (`gps/eta.py`): pelo histórico das rotas, o destino provável aparece depois de ~1,5 km (no simulador, a 14 % do caminho, com erro de ~5 % no tempo restante a meio caminho); quadro "Chegada prevista" no modo direção. Autonomia pelo consumo desde o último tanque cheio e pelo km/L da rota; abaixo de 60 km o modo direção fala.
- **Trânsito** (`gps/traffic.py`, aba Mapa → "🚦 Trânsito"): tempo e litros parado e arrastando por viagem, atraso por rota e horário de saída, **melhor horário** ("Saindo às 7h10 em vez de 7h40 você ganha 10 min e 0,3 L") e mapa dos pontos onde o carro fica parado. Só com as viagens do próprio carro, nenhum serviço de fora.
- **Avisos novos no celular desligados por padrão:** parada em posto, lembrete da oficina e autonomia baixa só vão para o ntfy com `notify_routine = true` na seção `[gps]`; sem isso tudo aparece só no painel. A ação "avisar" de um lugar manda sempre (é o dono que escolhe).
- **Sem GPS nada muda:** `test_hub_without_gps_is_silent` e os 99 testes antigos passam sem alteração; o "Tanque cheio" sem GPS responde e grava igual (`test_fill_without_gps_is_unchanged`).
- **Testes:** `tests/test_gps_daily.py` (16 testes). Suíte inteira 115 testes, 5 vezes seguidas (uma com outro fuso), sem falha; `npm test` da nuvem passa (Worker não mudou). Conferido no Chromium (celular, claro e escuro, banco simulado): Consumo, Trânsito, editor de ações, modo direção ao vivo (chegada, autonomia, falas) e demonstração, sem erro de JavaScript.
- **Pendente para o dia do hardware:** semanas de viagens reais para as estatísticas (rotas com 6+ viagens, 2 tanques por posto); raio real dos postos; conferir a parada em posto com o Pi desligando junto com o carro; ouvir as falas no iPhone; decidir se liga `notify_routine`.

## 2026-11-22 — GPS, Fase 9: compartilhar a viagem ao vivo e modo sempre ligado (sem receptor ainda)
Plano em `docs/GPS-ARQUITETURA.md` (seção 12, F9, com as "Notas da implementação da F9"). Tudo testado com simulador e falsos em memória. **Nada foi para o Pi nem para a nuvem.**
- **Compartilhar a viagem** (pedido do dono: "a pessoa vê em tempo real o trajeto e dados do veículo"): botão **📤** no modo direção (aparece só com GPS) e **Mapa → 📤 Compartilhar** no painel. Cria o link e abre a folha de compartilhar do iPhone (sem ela, copia o link). Quem recebe abre `/s/…` sem senha e sem instalar nada: carro andando no mapa, trajeto desta viagem, velocidade, rotação, temperatura, consumo, tempo e km, o título de um aviso do carro e "chegada prevista 14:35" (o nome do destino só se você marcar). Pelo mesmo canal ao vivo do painel online (WebSocket); sem ele, consulta a cada 3 s.
- **Validade (decisão do dono):** **24 horas** (padrão) ou **até você cancelar**. Os sem prazo aparecem em destaque na lista ("♾️ sem prazo — até você cancelar"), para não ficarem esquecidos. **Encerrar** corta na hora: a página de quem recebeu fecha e passa a dizer "Este link foi encerrado".
- **Privacidade:** com o carro desligado a página mostra só "Carro desligado · última atualização HH:MM", nunca onde ele parou; na viagem seguinte volta o trajeto dela. 90 s sem sinal: "Sem sinal", sem posição. Nada de lugares, histórico, outras viagens ou do resto do painel. Só o hash do link fica guardado. Precisa da senha de localização (`LOC_PASSWORD`) na nuvem; sem ela nada funciona e nenhuma posição sai do Pi.
- **Modo sempre ligado** (`[power] always_on`, **desligado por padrão**, exige alimentação permanente e internet no carro): vigia do estacionamento (alerta crítico se o carro sair mais de 60 m ou andar acima de 8 km/h por 10 s com o motor desligado; a posição sobe a cada 5 s) e desligamento do Pi com a bateria abaixo de 12 V por 10 min. O `install.sh` só cria a regra de sudo do desligamento com o modo ligado.
- **Sem GPS e sem `LOC_PASSWORD` nada muda:** os 115 testes antigos passam sem alteração; o `live.json` público continua sem coordenada (testado também com o quadro do link junto).
- **Testes:** suíte Python 121 testes (6 novos), 5 vezes seguidas sem falha; `npm test` da nuvem 23 testes (14 novos: hash do token, vencer, encerrar, até cancelar entre viagens, fim da viagem, falha fechada, nada de lugares/histórico, limites, painel sem coordenada). Conferência de mutação: 22 mudanças nas regras de segurança, todas pegas pelos testes. Conferido no Chromium (celular, claro e escuro): página do link ao vivo, desligado, vencido e inválido; aba Compartilhar e botão do modo direção.
- **Para publicar a nuvem (quando o dono pedir), nesta ordem:** (1) `cd cloud && npx wrangler d1 execute civic-obd --remote --file=schema.sql` (cria `share` se a F3 ainda não foi aplicada; repetível); (2) `npx wrangler d1 execute civic-obd --remote --file=migrations/0001-share-f9.sql` (uma vez; "duplicate column name: opts" = já aplicada); (3) `LOC_PASSWORD` precisa existir (`wrangler secret put LOC_PASSWORD`, feito pelo dono; já pedido na F3); (4) `npm run deploy` (o `build.sh` copia o `share.html`). Pi: `scripts/deploy.sh` normal (o `install.sh` cria a regra de sudo só se `always_on = true`). Nenhum segredo novo.
- **Pendente para o dia do hardware:** ver o link no Safari do iPhone de outra pessoa (folha de compartilhar, WebSocket pelo celular, ladrilhos); conferir o atraso real Pi → tela do link no hotspot; o modo sempre ligado depende de relé/sensor de ignição e de internet no carro (vigia e bateria só testadas com simulador); conferir o `sudo poweroff` com o serviço do coletor.

## 2026-11-22 — GPS, Fase 10 preparada: lista do dia do hardware (sem receptor ainda)
O VK-172 ainda não chegou e **nada foi publicado** (nem nuvem nem Pi). A F10 ficou pronta para ser executada:
- **`docs/DIA-DO-GPS.md`**, uma lista única em três partes. **(A) Publicar, quando o dono liberar:** primeiro a nuvem (cópia opcional do D1, `schema.sql`, migração `0001-share-f9.sql`, `LOC_PASSWORD` gerada com 32 caracteres aleatórios direto para `secrets/cloud.txt` e para o `wrangler secret put` sem aparecer na tela, `npm run deploy`), com as conferências por `curl` (`/api/env` "trancada"; `/api/share/<token falso>` 404, 410 sem senha ou 503 sem migração). Depois o Pi: carro parado em marcha lenta, ~15 min, hotspot, cópia do `/opt/obd` e do banco em `/data/obd/backup`, `scripts/deploy.sh` (instala gpsd e chrony e manda o `geo.db` de 34 MB), conferência por `systemctl`, `chronyc`, `obdctl gps status/check`, `/api/gps/status` e `obd-doctor`, e como voltar atrás. **(B) Quando o receptor chegar:** a lista 10.5 e todos os [HW] da F1 à F9 em 26 itens ordenados (USB, gpsd, 5 Hz, chrony com e sem internet, offset, proteção do cartão, partida, tirar e recolocar, primeira volta, lugares, trecho de referência, ladrilhos no iPhone, nuvem, `climb`, 0–60 com voz, 0–100 contra o celular, coastdown e puxada, radar, cidade × estrada, marchas, velocímetro, posto, chegada prevista, link em outro iPhone, revisão depois de uma semana), cada um com o esperado, o comando ou a tela e o arquivo ou limiar a ajustar. **(C) Pendências do dono:** trecho de referência, massa (1.425 kg = 1.350 + 75 × 1) e ocupantes, pneu, `gps.notify_routine`, mapa do corredor, `power.always_on` só com alimentação permanente, guardar a `LOC_PASSWORD`, extensor USB e conversor 12→5 V 3 A.
- **`obdctl gps check`** (novo, `src/obdpi/gps/check.py`, só leitura): uma linha ok/aviso/falha para gpsd, receptor e driver, taxa (cycle do gpsd e a medida pelo coletor), posição 3D, satélites/HDOP, relógio, fonte GPS no chrony, `geo.db` (tamanho, radares, idade), senha de localização na nuvem (pelo último ping do `obd-sync`) e subtensão (`vcgencmd`). Sai com código 1 se houver falha. Não pede taxa ao receptor, não reinicia serviço e não fala com a nuvem; sem GPS só mostra as falhas. Teste `test_gps_check_readonly` (com o `FakeGpsd`, confere que nenhum `?DEVICE` é enviado).
- **Achado:** o `data/geo/geo.db` local cobre **só a Grande SP** (93.337 vias, 1.287 radares, ruas locais num raio de 15 km de casa); o corredor até Cristais Paulista ainda não está nele. Gerar de novo numa caixa única com `--inner` e `--force` (umas 100 consultas ao Overpass, de 1 a 3 horas) fica para o dono decidir.
- **Lembrete:** o Pi marca subtensão (`0x50005`) com a fonte atual; o conversor 12→5 V de 3 A precisa vir antes de avaliar o receptor na partida.
- Nenhum [HW] da `GPS-ARQUITETURA.md` foi marcado como conferido. A seção F10 ganhou "Notas da preparação da F10", que aponta para a lista.


## 2026-11-22 — Deploy (nuvem + Pi) com o dono liberando

- **Nuvem:** cópia do D1 em `secrets/d1-antes-gps.sql`; `schema.sql` e `migrations/0001-share-f9.sql` aplicados (`share`
  com `opts`); `LOC_PASSWORD` gerada (32 caracteres) e guardada em `secrets/cloud.txt`, enviada ao Worker sem aparecer
  na tela; `npm run deploy` (versão `7757a0c2`). Conferido: `/api/env` com `"loc":"trancada"`, `/api/gps/*` 401,
  `/api/share/<falso>` 404, `/s/<falso>` 200, estáticos 200, ping com `"loc":true`, título "Delfos".
- **Pi:** alcançado pelo WiFi "garagem" (o PC não tem WiFi; o hotspot do iPhone não serve para o deploy daqui). Backup
  em `/data/obd/backup/opt-antes-gps.tgz` e `obd-antes-gps.db`; `scripts/deploy.sh` terminou com `== OK` (gpsd 3.25,
  chrony no lugar do timesyncd, `obd-clock`, `geo.db` de 56,5 MB, proteção do cartão religada).
- `obdctl gps check` depois do deploy, sem receptor: o esperado (gpsd ok, receptor falha "nenhum aparelho", NTP ok,
  mapa ok com 1.549 radares, localização liberada na nuvem) e **subtensão agora** (0x50005) — falta o conversor 12→5 V.
- `systemd-remount-fs` aparece como falho: é efeito da proteção do cartão (raiz em overlay), não do deploy.
