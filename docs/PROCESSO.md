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
