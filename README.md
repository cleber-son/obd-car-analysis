# 🚗 Delfos (obd-car-analysis)

*o motor fala, Delfos traduz.*

**Telemetria contínua do Honda Civic 2008 1.8 flex com Raspberry Pi 3 + ELM327 Bluetooth.**

Um registrador que fica no carro e grava *sempre* (não só quando um app está aberto), analisa os dados e avisa no
iPhone quando algo sai do normal. Feito para substituir o Car Scanner e acompanhar de perto o **corpo de borboleta
eletrônico**, que foi trocado recentemente.

```
 Civic (ECU, CAN 29 bits)            Raspberry Pi 3 (no carro)                    iPhone
 ┌──────────────┐  Bluetooth  ┌──────────────────────────────────────┐  WiFi  ┌───────────────┐
 │ ELM327 "OBD2"│ ──────────► │ obd-collector → SQLite → obd-web :80 │ ─────► │ http://obd.local │
 └──────────────┘    SPP      │ alertas · consumo · viagens · 0–100  │        │ /dirigir       │
                              └──────────────┬───────────────────────┘        └───────────────┘
                                             └──── ntfy (push) ─────────────► notificações
```

**Painel online (público, só leitura): https://obd.cleberson.social.br** · modo direção: https://obd.cleberson.social.br/dirigir

## O que ele faz

| | |
|---|---|
| 📡 **Coleta contínua** | Descobre os PIDs da ECU e lê **todos os sensores a cada 5 s**, os principais (~1,7×/s), códigos de falha e luz de injeção a cada 20 s. Pareia sozinho com o adaptador. |
| 🦋 **Corpo de borboleta** | Regras dedicadas: comando × posição, sensores A/B e pedal D/E divergentes (precursores de P2135/P2138), borboleta travada / modo de emergência, marcha lenta oscilando, estol, picos de sinal. Nota 0–100 por viagem. |
| 🚨 **Alertas inteligentes** | Faixa normal por sensor **e** comportamento habitual aprendido do seu carro. Cada alerta explica **o que é, causas prováveis, o que verificar e o que fazer**, com urgência. Repetições viram "continua N×". |
| ⛽ **Consumo** | km/L instantâneo, por viagem e **real de tanque a tanque** (botão *Tanque cheio*). Estimativa pelo MAF + λ comandado, corrigida pela mistura gasolina/etanol e recalibrada a cada abastecimento. Custo por km, autonomia. |
| 🏁 **Modo direção** `/dirigir` | Tela cheia para o iPhone 17 Pro Max (retrato/paisagem): velocidade, rpm com luz de troca, **marcha engatada**, quadros configuráveis, **modo gráfico em tempo real**, vermelho quando algo foge do normal. |
| ⏱ **Teste 0–100 km/h** | O Pi lê só velocidade e rpm na taxa máxima e interpola 0–60, 0–80 e 0–100. |
| 📊 **Painel web** | Ao vivo, Borboleta, Viagens (histórico de motor ligado), Consumo, Alertas/DTCs, Sensores (gráfico individual tempo real + histórico), Relatório + CSV, Sistema. Tema escuro. |
| 🩺 **Diagnóstico** | **Entrada falsa de ar × MAF** (MAF medido ÷ ar esperado por carga, com veredito por viagem), **bateria na partida** (repouso e queda ao dar partida), **válvula termostática** (tempo até 80 °C), **autotestes da ECU** (monitores prontos/pendentes). |
| 🌱 **Direção econômica** | Nota 0–100 por viagem (giro alto, arrancadas e freadas bruscas, tempo parado ligado, acima de 110 km/h, marcha alta forçando) com dicas. |
| 🩺 **Análise por sistema** | "Como está o carro" em linguagem simples: mistura, marcha lenta, borboleta, temperatura, ar admitido, elétrica, códigos e consumo, com causas prováveis em % e evolução dia a dia. Roda no Pi, sem IA. |
| 🤖 **IA por viagem** | Com a chave configurada, um laudo curto ao fim de cada viagem (≥ 5 min ou ≥ 1 km) e aviso no celular. |
| 🧬 **Impressão digital do motor** | Grava como o motor se comporta saudável e avisa de qualquer desvio pequeno depois. |
| 🔧 **Antes e depois** | Anote um conserto: o painel compara os dias antes e depois e diz se resolveu. |
| 🎯 **Testes guiados** | 8 testes caça-problema com o celular ao lado do carro (vazamento de ar, raio-X da mistura, catalisador, bateria, ventoinha…). |
| 🛠️ **Manutenção** | Plano pelos km reais, com uso severo detectado pelos dados. |
| 👨‍🔧 **Página do mecânico** | `/oficina`: problema, evidências e o que já foi testado. |
| 📱 **Aplicativo** | Instalável na tela inicial e abre sem internet com a última cópia. |
| 🗓 **Relatórios** | Semanal, mensal, **desde um dia escolhido** ou **de uma viagem só**, cada um com "O que fazer" e plano de ação geral. Semanal e mensal são **enviados no iPhone** toda segunda e todo dia 1º. |
| 🤖 **Analisar dados (IA)** | Um botão envia o resumo de tudo (sensores por condição, viagens, diagnósticos, alertas, códigos) para o **Claude Opus 5.5** (ou OpenAI, à escolha), que devolve um laudo completo: veredito, pontos de atenção, sistema por sistema e o que fazer. Chave configurada no próprio painel. |
| 📲 **Notificações** | Push no iPhone via [ntfy](https://ntfy.sh) para alertas e para avisar o IP sempre que o Pi entra em uma rede. |
| ☁️ **Painel online** | Cópia do painel na Cloudflare (Worker + D1) em `obd.cleberson.social.br`, acessível a qualquer hora mesmo com o carro desligado. O Pi (`obd-sync`) envia o estado ao vivo a cada 10 s e o restante (viagens, gráficos, alertas, relatórios, histórico dos sensores) a cada 5 min, só o que mudou. Mostra **carro ligado/desligado** (Pi offline há mais de 1 min = desligado). Público e só leitura; ações ficam no painel do Pi. |
| 🗺️ **Mapa (GPS)** | Com o receptor GPS na USB do Pi: aba **Mapa** com diário de bordo ("Casa → Trabalho", km, custo, paradas), trajetos, **mapa de calor** do motor (ajuste curto, carga, consumo, velocidade, temperatura, MAP), **alertas no lugar onde aconteceram** (com linha do tempo e pontos repetidos), lugares e onde o carro está estacionado. No detalhe da viagem, o trajeto **colorido por qualquer sinal** e sincronizado com o replay (×1, ×10, ×60); exportação **GPX/KML** (só no painel do Pi). **Rotas**: cada caminho repetido vira uma rota com km/L, L/100 km, R$/km e tempo (medianas, por combustível e por horário de saída). **Trechos de referência**: marque início e fim de um pedaço de rua sobre o traçado; toda passagem por ali é medida (ajuste curto, consumo × esperado, ar medido ÷ esperado) e o "antes e depois" de cada serviço ganha a linha "Trecho N". No painel online, tudo o que tem posição fica atrás de uma **senha de localização** própria. Sem GPS: "GPS não conectado" e nada mais muda. Mapa: Leaflet + OpenStreetMap. |
| 🗂 **Viagens gerenciáveis** | Todas registradas, com nome, anotação e opção de ocultar; detalhe com consumo, custo, direção econômica, ar × MAF, partida, aquecimento e 6 gráficos. |
| 📶 **Rede** | Prioridade sempre para o **hotspot do celular** (troca sozinho quando ele aparece), com **IP fixo `172.20.10.14`** nele; em casa, `obd.local`. |

Prévia da interface (dados simulados): https://claude.ai/artifact/1NkrbnkLgDTzTbuu9sXMSY

## Hardware

- Raspberry Pi 3 Model B v1.2 · Raspberry Pi OS Lite 64-bit (Trixie)
- Adaptador ELM327 Bluetooth clássico (testado: clone "OBD2" v1.5, PIN 1234)
- **Fonte 12 V → 5 V de 3 A de verdade** (a tomada USB do carro derruba o Pi na partida)
- Recomendado: RTC DS3231 (o Pi não tem relógio) e cartão SD de alta durabilidade

## Uso

| Onde | Como |
|---|---|
| Em casa | `http://obd.local` (ou o IP que chega no ntfy) |
| No hotspot do iPhone | **`http://172.20.10.14`** (IP fixo) ou a notificação **"Civic OBD online"**. Ative *Maximizar Compatibilidade*: o Pi 3 só enxerga 2,4 GHz |
| Dirigindo | `http://172.20.10.14/dirigir` → Compartilhar → *Adicionar à Tela de Início*. Bloqueio automático do iPhone em "Nunca" |
| Análise por IA | aba **Sistema → Análise por IA**: escolha Claude (Anthropic) ou OpenAI e cole a chave (salva só no Pi em `/data/obd/ai.json`, modo 600) |
| Notificações | app **ntfy** → assinar o tópico em *Sistema* no painel |

## Instalação

1. Gravar o cartão: `os/flash-sd.ps1` (Windows, elevado) com `os/boot/user-data` e `network-config` baseados nos `*.example`.
2. Primeiro boot → `ssh obd@obd.local`.
3. No PC: `scripts/deploy.sh obd.local` (rsync + `deploy/install.sh`, idempotente; também grava a URL/token da nuvem a partir de `secrets/cloud.txt`).
4. Painel online: `cd cloud && npm install && npm run deploy` (com `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` no ambiente; segredo `SYNC_TOKEN` via `wrangler secret put`). Para exigir senha: `wrangler secret put DASH_PASSWORD`.

Diagnóstico no Pi:

```bash
cd /opt/obd && PYTHONPATH=src venv/bin/python -m obdpi.cli probe      # lista PIDs e lê tudo uma vez
PYTHONPATH=src venv/bin/python -m obdpi.cli status                       # estado ao vivo
journalctl -u obd-collector -f                                            # logs
```

Testes: `python3 tests/test_parsing.py` · emulador: `pip install ELM327-emulator && elm -n 35000 -s car` e `transport = "tcp"`.

## Estrutura

```
src/obdpi/
├── elm/          driver ELM327 (socket RFCOMM, parser, multi-frame CAN)
├── obd/          tabela de PIDs (nomes em PT-BR) e códigos de falha
├── collector/    coleta, viagens, relógio, DTCs, 0–100, pareamento Bluetooth
├── analytics/    limites + linha de base, borboleta, consumo, diagnósticos por viagem, recomendações
├── reports.py    relatórios semanal, mensal, desde o dia e por viagem (painel + push)
├── ai.py         análise completa pelo Claude
├── web/          FastAPI + SSE + painel (JS puro, sem build) e modo direção
├── db/           esquema SQLite (WAL) e migrações
└── notify.py     ntfy com fila offline
deploy/           systemd, bluetooth agent, journald volátil, avahi, anúncio de IP, prioridade do hotspot, set-cloud.sh
cloud/            painel online: Worker Cloudflare (src/index.js), schema D1, build.sh (copia o painel do Pi)
docs/             ARQUITETURA.md · PROCESSO.md (diário) · pids-civic-2008.md
```

## Documentação

- [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md): arquitetura completa (desenhada com Claude Fable)
- [`docs/PROCESSO.md`](docs/PROCESSO.md): diário de tudo que foi feito e descoberto
- [`docs/pids-civic-2008.md`](docs/pids-civic-2008.md): o que este Civic informa pelo OBD
- [`docs/DIAGNOSTICO-2026-11-18.md`](docs/DIAGNOSTICO-2026-11-18.md): análise do ajuste de combustível após a troca da borboleta

## Qualidade

- Testes unitários do parser, DTCs, regras da borboleta e limites (`tests/test_parsing.py`).
- Teste ponta a ponta no Pi com `ELM327-emulator`.
- Revisão de código pelo Claude Fable antes da primeira volta: corrigidos correção de relógio que deslocava o banco inteiro,
  viagens indevidamente unidas, perda de viagem após queda longa do Bluetooth, valores antigos gravados como novos,
  descoberta parcial de PIDs, alertas que não escalavam, DTCs "sumindo" com NO DATA, 0–100 travando a coleta e falsos alertas
  (ajuste curto fixo do ECU, carga controlada do alternador Honda, estol ao desligar a chave).

## Segredos

Senhas de WiFi, usuário e tópico ntfy ficam em `secrets/` e `/etc/obd/secrets.toml`, **fora do git**.
