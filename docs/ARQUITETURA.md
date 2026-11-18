# Arquitetura — Telemetria OBD do Civic 2008 (Raspberry Pi 3 + ELM327)

> Desenhada com Claude Fable em 2026-11-18. Nada foi testado em hardware ainda; itens marcados "confirmar" dependem do carro/adaptador.

Objetivo: substituir o app Car Scanner por um registrador autônomo que grava sempre que o carro está ligado, com foco no corpo de borboleta eletrônico (DBW), e que serve um dashboard web na rede local.

Decisões principais:

| Tema | Decisão |
|---|---|
| Linguagem | Python 3.11+ (stdlib sempre que possível) |
| Driver ELM327 | Próprio, síncrono, cerca de 500 linhas; não usar python-OBD |
| Bluetooth | Socket RFCOMM direto do Python (`AF_BLUETOOTH`), sem `rfcomm bind`; BLE via `bleak` só se o adaptador exigir |
| Processos | 2 serviços: `obd-collector` (escreve) e `obd-web` (lê), mais timers de manutenção |
| Banco | SQLite único em `/data/obd/obd.db`, WAL, `synchronous=FULL`, commit em lote a cada 5 s |
| Modelo de amostras | Híbrido: tabela estreita para todos os PIDs (20 s) e tabela larga `tb_sample` para o grupo rápido da borboleta |
| Web | FastAPI + uvicorn (1 worker), SSE para tempo real |
| Frontend | SPA estática Svelte 5 + Vite + Tailwind + uPlot, compilada no PC (Node nunca roda no Pi) |
| Descoberta | mDNS `obd.local`, notificação ntfy com o IP, AP de fallback (fase 2.5) |
| Config | TOML em `/etc/obd/obd.toml` (`tomllib` da stdlib) |

---

## 1. Visão geral e serviços

```
        CARRO                                  RASPBERRY PI 3
 ┌───────────────┐   BT SPP    ┌─────────────────────────────────────────────────┐
 │ ECU (CAN 500k)│◄──────────► │ obd-collector.service  (Python, síncrono)       │
 │      ▲        │   RFCOMM    │  transporte → ELM327 → ISO-TP → decodificador   │
 │  ELM327 (OBD) │             │  escalonador (rápido / 5 s / 20 s / DTC)        │
 └───────────────┘             │  viagem, relógio, regras da borboleta (online)  │
                               │  thread escritora ──► SQLite (WAL)              │
                               │        │  datagramas JSON                       │
                               │        ▼  /run/obd/live.sock                    │
                               │ obd-web.service  (FastAPI/uvicorn, porta 80)    │
                               │  REST (SQLite somente leitura) + SSE /api/live  │
                               │  arquivos estáticos da SPA                      │
                               │        ▲  /run/obd/control.sock (comandos)      │
                               │ obd-maint.timer    agregados, backup, limpeza   │
                               │ obd-announce       ntfy com IP (dispatcher NM)  │
                               │ obd-netwatch.timer AP de fallback (fase 2.5)    │
                               │ avahi-daemon       obd.local + _http._tcp       │
                               └────────────────────────┬────────────────────────┘
                                                        │ WiFi 2,4 GHz
                                           celular / PC ─► http://obd.local
```

### Unidades systemd

| Unidade | Tipo | Pontos importantes |
|---|---|---|
| `obd-collector.service` | `Type=notify` | `User=obd`, `WatchdogSec=60`, `Restart=always`, `RestartSec=3`, `After=bluetooth.target`, sem dependência de rede, `RuntimeDirectory=obd`, `MemoryMax=150M`, `Nice=-5`, `ExecStartPre=+/opt/obd/deploy/bt-ensure.sh` |
| `obd-web.service` | simple | `User=obd`, `AmbientCapabilities=CAP_NET_BIND_SERVICE` (porta 80), `MemoryMax=200M`, `Nice=5`, `Restart=always` |
| `obd-maint.service` + `.timer` | oneshot | `OnBootSec=3min`, `OnUnitActiveSec=30min`, `IOSchedulingClass=idle` |
| `obd-announce.service` | oneshot | Disparado por `/etc/NetworkManager/dispatcher.d/90-obd-announce` em `up` e `dhcp4-change` |
| `obd-netwatch.service` + `.timer` | oneshot | Fase 2.5: sobe o AP se não houver rede conhecida |

Por que dois processos: um travamento ou pico de memória do web não pode derrubar a coleta. O coletor é o único que escreve telemetria. O web só escreve em `annotation`, `saved_report` e `setting`.

Orçamento de RAM: SO cerca de 120 MB, coletor cerca de 40 MB, web 70–90 MB. Total abaixo de 300 MB de 1 GB. Sem swap em SD (zram é aceitável).

---

## 2. Stack e justificativas

**Driver ELM327 próprio em vez de python-OBD.** O python-OBD exige porta serial (`/dev/rfcomm0`), tem manutenção fraca e não faz requisição multi-PID, que é o que permite amostrar a borboleta a mais de 1 Hz. Também não expõe bem os erros do adaptador, e sua lógica de reconexão não serve para um carro que liga e desliga. O protocolo é simples (texto terminado em `>`), e o driver próprio dá controle de timeouts, ISO-TP e gravação bruta para depuração.

**Coletor síncrono, sem asyncio.** O ELM327 é estritamente requisição-resposta, uma por vez. Um laço bloqueante com `socket.settimeout()` é mais simples e mais robusto. Há só duas threads auxiliares: a escritora do banco (para o fsync não atrasar o polling) e a de controle.

**FastAPI + uvicorn.** Cabe com folga em 1 GB, tem wheels aarch64 e gera OpenAPI em `/docs` de graça. Um worker, sem uvloop. Consultas SQLite rodam no threadpool com conexões `mode=ro`.

**SSE em vez de WebSocket.** O fluxo é unidirecional, o `EventSource` reconecta sozinho e não precisa de biblioteca. Comandos vão por POST normal.

**SQLite.** Sem servidor, um arquivo, WAL permite leitores concorrentes com um escritor. InfluxDB, Timescale ou Prometheus são pesados demais para o Pi 3 e piores diante de corte de energia.

**Frontend Svelte 5 + Vite + Tailwind + uPlot.** O bundle esperado é menor que 150 KB gzip. O uPlot (cerca de 45 KB) desenha dezenas de milhares de pontos com fluidez no celular; ECharts e Plotly são grandes demais. Medidores são SVG/CSS próprios. O build acontece no PC (WSL) e o `dist/` vai por rsync para o Pi.

**Desenvolvimento no WSL sem carro.** Usar o pacote `ELM327-emulator` via transporte TCP ou pty, mais um transporte de replay de transcrições gravadas no carro real.

---

## 3. Bluetooth e ELM327

### 3.1 Pareamento (uma vez, na Fase 0)

1. Rodar `bluetoothctl`: `power on`, `agent KeyboardOnly`, `default-agent`, `scan on`, `pair <MAC>` (PIN 1234 ou 0000), `trust <MAC>`.
2. Rodar `bluetoothctl info <MAC>`. Se listar o UUID `00001101-...` (Serial Port), é Bluetooth Classic SPP. Se o dispositivo só aparece em varredura LE e não tem esse UUID, é BLE.
3. Canal RFCOMM: padrão 1. O coletor sonda os canais 1 a 5 se o configurado falhar.
4. Documentar MAC, nome, tipo e canal em `docs/01-bluetooth.md`. O script `scripts/bt-pair.sh` automatiza o processo.

As chaves ficam em `/var/lib/bluetooth`. Parear antes de ativar o overlay de root (seção 6).

### 3.2 Socket RFCOMM direto

Usar `socket.socket(AF_BLUETOOTH, SOCK_STREAM, BTPROTO_RFCOMM)` e `connect((mac, canal))`. Motivos para não usar `rfcomm bind`:

- O TTY conecta de forma preguiçosa no `open()` e os erros são opacos (trava ou EIO).
- Após queda do link o estado fica preso e exige `release` e `bind`.
- A ferramenta `rfcomm` está depreciada no BlueZ.
- O socket não precisa de root e não depende de pyserial.
- O socket devolve errnos claros:

| errno | Significado prático | Ação |
|---|---|---|
| `EHOSTDOWN` / `ETIMEDOUT` | Adaptador fora de alcance ou sem energia | Backoff |
| `ECONNREFUSED` | Canal errado ou adaptador ocupado (o celular com Car Scanner conectou antes) | Registrar `system_event` "adaptador ocupado" |
| `ECONNRESET` / `EPIPE` | Link caiu | Reconectar |

Abstração `Transport` (`read`, `write`, `close`) com cinco implementações: `RfcommTransport`, `SerialTransport` (USB ou pty), `TcpTransport` (emulador), `ReplayTransport` (testes) e `BleTransport`. O BLE usa `bleak` em uma thread com event loop próprio, escrita em blocos de até 20 bytes e remontagem das notificações até o `>`. Os pares de características são detectados automaticamente (FFF0/FFF1/FFF2, FFE0/FFE1, 18F0/2AF0/2AF1). Só implementar se a Fase 0 mostrar que o adaptador é BLE.

### 3.3 Máquina de estados e reconexão

```
DESCONECTADO → CONECTANDO_BT → INICIALIZANDO_ELM → CONECTANDO_ECU → COLETANDO
      ▲              │                  │                 │   ▲          │
      └──────────────┴──── falha ───────┴─────────────────┘   └ ECU_SILENCIOSA
```

- **Backoff de conexão BT:** 2, 4, 8 e depois 15 s (teto baixo, porque o Pi só está ligado com o carro ligado).
- **Sem resposta válida por 10 s em COLETANDO:** fechar o socket e voltar a DESCONECTADO.
- **ECU_SILENCIOSA:** `ATRV` responde mas os PIDs dão `NO DATA` ou `UNABLE TO CONNECT` cinco vezes seguidas (ignição desligada com o Pi ainda energizado). Sondar `0100` a cada 5 s e continuar gravando a tensão.
- **Oito falhas seguidas de conexão BT:** criar `/run/obd/bt-reset-requested` e sair com erro. O `ExecStartPre` (`bt-ensure.sh`, como root) reinicia o `hci0` e o systemd relança o coletor.
- **Watchdog:** `sd_notify("WATCHDOG=1")` a cada volta do laço. Um travamento vira restart automático.
- Toda transição gera uma linha em `system_event` e um evento `state` no SSE.

### 3.4 Sequência de inicialização do ELM

| # | Comando | Timeout | Observação |
|---|---|---|---|
| 1 | `ATZ` | 3 s | Reset; guardar a string de versão. Descartar lixo até o `>` |
| 2 | `ATE0` | 1 s | Sem eco |
| 3 | `ATL0` | 1 s | Sem linefeed |
| 4 | `ATS0` | 1 s | Sem espaços (menos bytes pelo BT) |
| 5 | `ATH1` | 1 s | Cabeçalhos ligados (ver abaixo) |
| 6 | `ATAT1` | 1 s | Timing adaptativo padrão; `ATAT2` só após benchmark |
| 7 | `ATST32` | 1 s | Timeout de 200 ms (0x32 × 4 ms) |
| 8 | `ATSP6`, ou `ATSP0` na primeira vez | 1 s | Ver "protocolo" abaixo |
| 9 | `0100` | 10 s com SP0, 3 s com SP6 | Abre o barramento; pode imprimir `SEARCHING...` |
| 10 | `ATDPN` | 1 s | Protocolo efetivo; persistir em `/data/obd/state.json` |
| 11 | `ATI`, `AT@1`, `ATRV` | 1 s | Identidade do adaptador e tensão |

**Cabeçalhos ligados (`ATH1`).** As respostas vêm como `7E8064100BE3FA813`. O parser filtra por ECU (7E8 é o motor) e remonta o ISO-TP por conta própria: quadro único `0N`, primeiro quadro `1N NN`, consecutivos `2N`. Isso é mais confiável que o formato `0:`/`1:` dos clones, resolve respostas de mais de uma ECU e é necessário para multi-PID, DTCs e VIN.

**Protocolo.** Na primeira conexão usar `ATSP0` (auto). Depois fixar o protocolo descoberto, provavelmente `ATSP6` (CAN 11 bits 500k). O protocolo fixo conecta mais rápido e não se perde em busca quando a ECU ainda está acordando. Se houver três `UNABLE TO CONNECT` com tensão acima de 13 V, voltar a `ATSP0` uma vez.

**Dígito de contagem de respostas.** Enviar `010C1` em vez de `010C`: o ELM retorna assim que recebe uma resposta, sem esperar o timeout. Isso costuma dobrar a taxa. Sondar na inicialização; se o clone responder `?`, desligar.

**Multi-PID.** Em ISO 15765-4 a norma permite até 6 PIDs por requisição (`010C1149 4C0B0D`), com resposta multi-quadro. Sondar na inicialização. Clones "v2.1" costumam falhar aqui; nesse caso cair para PID único.

**Erros tratados explicitamente:** `NO DATA`, `UNABLE TO CONNECT`, `CAN ERROR`, `BUS INIT: ...ERROR`, `BUS BUSY`, `BUFFER FULL`, `STOPPED`, `DATA ERROR`, `<RX ERROR`, `FB ERROR`, `LV RESET`, `?`. `LV RESET` e `STOPPED` forçam reinicialização do ELM.

**Gravação bruta.** Com `--record-raw`, cada comando e resposta vai para `/data/obd/raw/AAAAMMDD.jsonl` (rotação de 7 dias). Vale para depurar decodificação e gerar fixtures de teste.

---

## 4. Coletor

### 4.1 Descoberta de PIDs (uma vez por conexão)

1. Ler `0100`, `0120`, `0140`, `0160`, `0180`, `01A0`, `01C0`, parando quando o bit do próximo bloco vier zerado. Resultado: conjunto de PIDs suportados.
2. Ler uma vez por sessão: `0902` (VIN), `0904` (calibração), `011C` (padrão OBD), `0113`/`011D` (sondas presentes), `0151` (tipo de combustível). Guardar em `session.info_json`.
3. Ler `0200` (PIDs disponíveis em freeze frame).
4. Gravar a lista em `session.supported_pids` e gerar `docs/pids-civic-2008.md` com `obdctl probe`.

### 4.2 Tabela de decodificação

Arquivo `src/obdpi/obd/pids.py`, com uma entrada por PID: `pid`, número de bytes e lista de sinais (`key`, `nome_pt`, `unidade`, `min`, `max`, `decode`). Regras:

- Um PID pode gerar mais de um sinal (sondas `0x14–0x1B` e `0x24–0x2B`, `0x34`).
- Enumerações (`0x03`, `0x1C`, `0x51`) viram inteiro com rótulo.
- PID suportado sem entrada na tabela é gravado como inteiro bruto, com `signal.raw=1`.

Sinais centrais para a borboleta:

| PID | Chave | Fórmula |
|---|---|---|
| 0x0C | `rpm` | (256A+B)/4 |
| 0x0D | `speed` | A km/h |
| 0x11 | `tps` | A·100/255 |
| 0x45 | `tps_rel` | A·100/255 |
| 0x47 / 0x48 | `tps_b` / `tps_c` | A·100/255 |
| 0x49 / 0x4A / 0x4B | `pedal_d` / `pedal_e` / `pedal_f` | A·100/255 |
| 0x4C | `tps_cmd` | A·100/255 |
| 0x0B | `map` | A kPa |
| 0x04 | `load` | A·100/255 |
| 0x05 / 0x0F | `ect` / `iat` | A−40 °C |
| 0x06 / 0x07 | `stft` / `ltft` | (A−128)·100/128 |
| 0x0E | `timing` | A/2−64 graus |
| 0x42 | `ecu_volt` | (256A+B)/1000 |
| 0x52 | `ethanol` | A·100/255 (se suportado) |

### 4.3 Escalonador

O escalonador é genérico: cada item tem um período e o laço executa sempre o item mais atrasado (earliest-deadline-first), com prioridade por grupo.

| Grupo | Período alvo | Conteúdo | Destino |
|---|---|---|---|
| `rapido_A` | 0,5 s (mínimo aceitável 1 s) | `0C, 11, 49, 4C, 0B, 0D` em uma requisição multi-PID | `tb_sample` + SSE |
| `rapido_B` | 1 s | `11, 47, 49, 4A, 45, 4C` (correlação entre sensores, mesma requisição) | `tb_sample` + SSE |
| `medio` | 5 s | `04, 06, 07, 0E` | `sample` |
| `completo` | 20 s | Todos os PIDs suportados + `ATRV` | `sample` |
| `dtc` | 20 s | `0101`, modo `03`, modo `07` | `dtc_*` |

Regras:

- **O requisito dos 20 s é cumprido pelo grupo `completo`.** A cada 20 s há uma linha em `sample` para todos os sinais suportados. Os PIDs do grupo rápido não são relidos; copia-se a leitura mais recente.
- **A varredura completa é intercalada.** No máximo 3 requisições lentas entre dois ciclos rápidos, para o grupo rápido nunca ficar mais de cerca de 400 ms sem rodar.
- **Sem multi-PID, degradar.** `rapido_A` vira 5 PIDs individuais (`0C, 11, 49, 4C, 0B`) a 1 Hz, e o grupo B desce para 5 s.
- **A taxa real é medida** e exposta em `/api/status` (`fast_hz`, `req_per_s`, `sweep_ms`).

Orçamento de throughput (confirmar com `obdctl bench` na Fase 0):

| Cenário | Capacidade | Uso |
|---|---|---|
| Clone lento, PID único, cerca de 10 req/s | 200 requisições por 20 s | Rápido 5×1 Hz = 100; médio 16; completo cerca de 35; DTC 4. Aproximadamente 78% |
| Com multi-PID | Cada requisição de 6 PIDs leva 80–150 ms | Grupo A a 2–4 Hz, com folga |

Com PID único, os valores de um ciclo rápido têm defasagem de 50–100 ms entre si. Cada linha de `tb_sample` guarda `span_ms`, e as regras de discrepância só avaliam em regime quase estacionário (seção 7).

**Modo diagnóstico.** Ativado pelo dashboard por N minutos. Suspende os grupos `medio` e `completo`, mantém DTC a cada 20 s e roda só `11, 4C, 49, 0C` na taxa máxima (alvo 8–10 Hz). Serve para o teste de varredura do pedal e para medir tempo de resposta.

### 4.4 DTCs

- **Modo 03 (confirmados) e 07 (pendentes) a cada 20 s.** Em CAN o primeiro byte após `43`/`47` é a contagem; tratar também o formato sem contagem.
- **Só transições são gravadas.** O estado atual fica em memória e em `dtc_state` (`last_seen_ts` atualizado a cada 5 min). `dtc_event` recebe `appeared` e `cleared`.
- **`0101`:** MIL, contagem e monitores de prontidão. Gravado em `monitor_status` só quando muda.
- **Freeze frame:** quando aparece um DTC novo, ler `0202`. Se for diferente de zero, ler cada PID de `0200` com `02 <pid> 00` e gravar em `freeze_frame` como JSON.
- **Modo 0A (permanentes):** tentar uma vez; um carro de 2008 provavelmente não suporta.
- **Lista de vigilância da borboleta:** gera alerta crítico imediato (SSE + ntfy).
- **Modo 04 (apagar DTCs):** fora do escopo por padrão. Se entrar, fica atrás de `allow_clear_dtc=false`, porque zera adaptações e monitores.

### 4.5 Estado do motor, sessões e viagens

- **Sessão:** uma execução do coletor (na prática, um boot).
- **Motor ligado:** `rpm ≥ 400` por 3 s. Sem RPM, usar tensão ≥ 13,2 V como indício.
- **Viagem:** começa em "motor ligado". Termina por (a) RPM 0 por mais de 120 s com a ECU respondendo, (b) ECU silenciosa por mais de 120 s ou (c) corte de energia.
- **Corte de energia é o fim normal de viagem.** `trip.end_ts` é atualizado a cada commit (5 s). No boot seguinte, um finalizador fecha viagens abertas com `end_reason='power_loss'`, calcula o resumo (distância por integração da velocidade, tempos, máximos, score) e grava.
- **Religar em menos de 3 min na mesma sessão** continua a mesma viagem e registra o evento `restart`.
- **Estol** é evento de primeira classe (regra R6).

Limitação: o Pi 3 leva 25–40 s para subir. Se a chave for direto para a partida, os primeiros segundos da partida a frio não são gravados. Mitigações: enxugar o boot (desativar `NetworkManager-wait-online`, cloud-init após o primeiro boot, `apt-daily*`, `man-db.timer`, ModemManager) e, opcionalmente, alimentação permanente com sensor de ignição (seção 6.3). Meta: coletor rodando em até 35 s após a energia.

### 4.6 Relógio (Pi sem RTC)

Problema: sem rede, o relógio volta do último valor salvo. Com corte abrupto, esse valor pode ter até 1 h de atraso, ou seja, anterior às últimas amostras gravadas.

Projeto:

1. **Timestamps ancorados no relógio monotônico.** No início da sessão: `ancora_wall = max(relogio_do_sistema, ultimo_ts_no_banco + 1 s)` e `ancora_mono = CLOCK_BOOTTIME`. Todo `ts` é `ancora_wall + (mono_agora − ancora_mono)`, em milissegundos UTC. Saltos de NTP no meio da sessão não bagunçam a série, e os timestamps nunca retrocedem entre sessões.
2. **Detecção de sincronismo.** Existência de `/run/systemd/timesync/synchronized`, ou ano do sistema anterior ao do build indica "não sincronizado". Checar a cada 10 s.
3. **Correção retroativa.** Quando o NTP sincroniza, calcular `offset = wall_real − ts_estimado`. Se `|offset| > 2 s`, em uma única transação somar o offset a todos os `ts` da sessão (`sample`, `tb_sample`, `dtc_event`, `tb_event`, `system_event`, `trip`). Em `sample` a chave primária inclui `ts`; para evitar colisão transitória fazer em dois passos (`ts = -(ts + off)` e depois `ts = -ts`). Gravar `session.clock_synced=1` e `ts_offset_applied_ms`. Uma sessão de 2 h tem cerca de 40 mil linhas, alguns segundos no Pi.
4. **Sessões que nunca sincronizaram** ficam com `clock_synced=0`, e a interface mostra "horário aproximado".
5. **Dica de hora pelo celular.** Se o relógio não está sincronizado e alguém abre o dashboard, o JavaScript envia `Date.now()` para `POST /api/system/time-hint`. Um helper com sudoers restrito (`/opt/obd/scripts/set-clock`, só aceita quando o NTP não está sincronizado) ajusta o relógio e dispara a correção do item 3.
6. **Persistência do relógio.** `fake-hwclock` no Bookworm; no Trixie confirmar se o pacote existe ou se vale o arquivo do `systemd-timesyncd`. Com overlay de root, apontar o arquivo para `/data`.

**Recomendado: módulo DS3231 (I2C, R$ 15–30, `dtoverlay=i2c-rtc,ds3231`).** Elimina quase todo o problema; a lógica acima continua como rede de segurança.

---

## 5. Modelo de dados (SQLite)

PRAGMAs na criação: `page_size=4096`, `auto_vacuum=INCREMENTAL`, `journal_mode=WAL`. Por conexão: `synchronous=FULL` (coletor), `busy_timeout=5000`, `foreign_keys=ON`. Web: `mode=ro`, exceto nas três tabelas de aplicação.

**Formato das amostras: híbrido.**

- `sample` é estreita (sinal, ts, valor). O conjunto de PIDs é descoberto em runtime e pode mudar; esquema estreito não precisa de migração.
- `tb_sample` é larga. As regras da borboleta comparam pedal, comando e posição no mesmo instante. Em tabela estreita isso exigiria auto-joins caros; na larga é uma linha.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);            -- schema_version etc.

CREATE TABLE session (
  id INTEGER PRIMARY KEY, boot_id TEXT, started_ts INTEGER, last_ts INTEGER,
  clock_synced INTEGER DEFAULT 0, ts_offset_applied_ms INTEGER DEFAULT 0, clock_source TEXT,
  adapter_mac TEXT, elm_version TEXT, protocol TEXT, multi_pid INTEGER,
  supported_pids TEXT, info_json TEXT, app_version TEXT, end_reason TEXT);

CREATE TABLE trip (
  id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES session(id),
  start_ts INTEGER, end_ts INTEGER, end_reason TEXT, finalized INTEGER DEFAULT 0,
  duration_s INTEGER, distance_km REAL, idle_s INTEGER, avg_speed REAL, max_speed REAL,
  max_rpm REAL, max_ect REAL, min_volt REAL, avg_ltft REAL, cold_start INTEGER,
  tb_score INTEGER, summary_json TEXT);
CREATE INDEX trip_start ON trip(start_ts);

CREATE TABLE signal (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, mode INTEGER, pid INTEGER,
  name_pt TEXT, unit TEXT, vmin REAL, vmax REAL, decimals INTEGER, raw INTEGER DEFAULT 0);

CREATE TABLE sample (
  signal_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL,
  PRIMARY KEY (signal_id, ts)) WITHOUT ROWID;

CREATE TABLE tb_sample (
  ts INTEGER PRIMARY KEY, session_id INTEGER, grp TEXT,          -- 'A' | 'B' | 'D' (diagnóstico)
  span_ms INTEGER, rpm REAL, speed REAL, map REAL,
  tps REAL, tps_b REAL, tps_rel REAL, tps_cmd REAL, pedal_d REAL, pedal_e REAL);

CREATE TABLE agg_minute (
  signal_id INTEGER, ts_min INTEGER, n INTEGER, vmin REAL, vmax REAL, vavg REAL,
  PRIMARY KEY (signal_id, ts_min)) WITHOUT ROWID;

CREATE TABLE dtc_state (
  code TEXT, kind TEXT, first_seen_ts INTEGER, last_seen_ts INTEGER,
  seen_sessions INTEGER, active INTEGER, PRIMARY KEY (code, kind));
CREATE TABLE dtc_event (
  id INTEGER PRIMARY KEY, ts INTEGER, session_id INTEGER, trip_id INTEGER,
  code TEXT, kind TEXT, event TEXT, mil_on INTEGER, freeze_frame_id INTEGER);
CREATE TABLE freeze_frame (id INTEGER PRIMARY KEY, ts INTEGER, dtc_code TEXT, data_json TEXT);
CREATE TABLE monitor_status (ts INTEGER PRIMARY KEY, mil_on INTEGER, dtc_count INTEGER, readiness_json TEXT);

CREATE TABLE tb_event (
  id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER, session_id INTEGER, trip_id INTEGER,
  rule TEXT, severity TEXT, metrics_json TEXT, rules_version INTEGER);
CREATE INDEX tb_event_ts ON tb_event(ts_start);
CREATE TABLE tb_idle_window (
  ts INTEGER PRIMARY KEY, trip_id INTEGER, ect REAL, rpm_mean REAL, rpm_std REAL, rpm_p2p REAL,
  osc_cycles INTEGER, tps_median REAL, map_mean REAL, stft_mean REAL, ltft REAL);
CREATE TABLE tb_baseline (key TEXT PRIMARY KEY, value REAL, n INTEGER, updated_ts INTEGER, epoch INTEGER);

CREATE TABLE system_event (id INTEGER PRIMARY KEY, ts INTEGER, session_id INTEGER,
  level TEXT, kind TEXT, detail TEXT);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, ts INTEGER, channel TEXT, payload_json TEXT, sent_ts INTEGER);

CREATE TABLE annotation (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, text TEXT);
CREATE TABLE saved_report (id INTEGER PRIMARY KEY, name TEXT, spec_json TEXT, created_ts INTEGER);
CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT);
```

`annotation` é o diário de manutenção (por exemplo "limpeza do TBI", "troca do corpo", "reaprendizado de marcha lenta", "abasteci etanol"). As anotações aparecem como marcadores verticais nos gráficos de tendência.

Migrações: arquivos numerados em `src/obdpi/db/migrations/NNNN_*.sql`, aplicados no início do coletor, com a versão em `meta`.

### Estimativa de tamanho

| Tabela | Taxa | Por hora de motor ligado |
|---|---|---|
| `sample` | cerca de 40 sinais a cada 20 s + 4 a cada 5 s ≈ 2,8 linhas/s × ~22 B | ~0,25 MB |
| `tb_sample` | 2–3 linhas/s × ~50 B | ~0,4–0,5 MB |
| Demais | — | < 0,05 MB |
| **Total** | | **~1 MB/h (estimativa conservadora)** |

Com 1,5 h por dia são cerca de 550 MB por ano. O cartão de 32 GB não é o gargalo.

### Retenção

- Guardar tudo em resolução total por padrão.
- `obd-maint` mantém `agg_minute` para gráficos de longo prazo.
- Guarda-corpo: se o banco passar de 8 GB ou o disco livre ficar abaixo de 20%, apagar `tb_sample` com mais de 12 meses (os eventos e `tb_idle_window` ficam) e rodar `PRAGMA incremental_vacuum`.
- Nunca rodar `VACUUM` completo no Pi.

---

## 6. Robustez contra corte de energia

### 6.1 Banco

- **WAL + `synchronous=FULL` + commit a cada 5 s na thread escritora.** A pedida original era `NORMAL`. `NORMAL` não corrompe, mas pode perder até cerca de 30 s (o que está no page cache), e essa é a janela mais interessante: o estol logo antes de desligar a chave. Com `FULL` e lote de 5 s é um fsync a cada 5 s, cerca de mil por dia de uso, desgaste irrelevante. Perda máxima: 5 s.
- **Checagem no boot.** `PRAGMA quick_check` antes de abrir. Se falhar: mover para `obd.db.corrompido-<ts>`, tentar `.recover`, senão restaurar o último backup, senão criar um banco novo. O coletor nunca fica parado por causa do banco. Registrar em `system_event` e notificar.
- **Backup local semanal.** `VACUUM INTO '/data/obd/backup/obd-AAAAMMDD.db.tmp'`, depois `rename`. Manter 2.
- **Backup fora do Pi.** `scripts/backup-pull.sh` (rsync a partir do PC quando o carro está na garagem). É a única proteção contra morte do cartão.
- **`wal_autocheckpoint=1000`** (padrão).

### 6.2 Sistema de arquivos e SO

- **Cartão de alta durabilidade** (SanDisk High Endurance ou Samsung PRO Endurance).
- **Partição `/data` separada desde a Fase 0.** Root com cerca de 8 GB e `/data` ext4 com o resto, montada com `noatime,commit=5`. A imagem expande o root no primeiro boot; ou se impede isso, ou se encolhe o root offline em outra máquina Linux (gparted). Confirmar o mecanismo na versão exata da imagem (Bookworm e Trixie diferem).
- **journald volátil** (`Storage=volatile`, `RuntimeMaxUse=16M`) em vez de log2ram. Eventos importantes vão para `system_event`.
- **Sem swap em SD.** `/tmp` em tmpfs.
- **fsck automático.** `fsck.repair=yes` no `cmdline.txt` e passo 2 no fstab para `/data`.
- **Overlay de root** (`raspi-config`, Overlay File System) só na Fase 3, quando o sistema estabilizar. Com overlay, deploy exige desativar, reiniciar, atualizar e reativar. O `scripts/deploy.sh` deve automatizar isso.
- **Desativar** `apt-daily*`, `man-db.timer` e atualizações automáticas.

### 6.3 Alimentação (hardware, em ordem de custo/benefício)

1. **Fonte decente (obrigatório).** Conversor buck 12 V → 5 V de 3 A real, com cabo curto. Carregador USB de acendedor barato cai na partida (a tensão vai a cerca de 9 V) e o Pi reinicia. Monitorar `vcgencmd get_throttled` em `/api/system`.
2. **RTC DS3231 (recomendado).** Ver seção 4.6.
3. **Desligamento limpo (opcional, Fase 3).** Duas rotas:
   - **Supercapacitor ou UPS HAT.** Segura 20–30 s; o Pi detecta a perda de entrada e executa `shutdown`. Preferir supercapacitor ou LiFePO4. Li-ion e LiPo dentro de carro estacionado ao sol (cabine a 60–70 °C) são risco real.
   - **Sensor de ignição com desligamento temporizado.** Pi em 12 V permanente (derivação de fusível) através de relé temporizado ou fonte automotiva com "ACC delay-off". O ACC entra por optoacoplador em um GPIO (`dtoverlay=gpio-shutdown`). Ao desligar a chave o Pi faz shutdown limpo e o relé corta 30–60 s depois. Ligado ao ACC ou a uma porta, permite ainda bootar antes da partida.
4. O software já nasce com `power.ignition_gpio` opcional na configuração.

Sem os itens 3 e 4 o sistema continua correto. O projeto assume corte abrupto como caso normal.

---

## 7. Análise do corpo de borboleta

Módulo puro `src/obdpi/analytics/throttle.py`: recebe linhas de `tb_sample` e contexto, e devolve eventos. É usado online no coletor (alerta imediato) e offline (`obdctl reanalyze --desde ...`) quando os limiares mudam. Cada evento guarda `rules_version`.

**Todos os limiares abaixo são valores iniciais.** Recalibrar depois de 2 semanas de dados reais. Ficam na seção `[throttle.rules]` do TOML.

### 7.1 Normalização e linha de base

As porcentagens brutas não são comparáveis entre si. Pedal solto costuma ler 15–20% no sensor D e cerca de metade no E. Borboleta fechada lê 8–15% no TPS absoluto. Por isso:

- **Aprendizado** nas primeiras 10 viagens ou 5 h: `pedal_d_min/max`, `pedal_e_min/max`, `tps_idle_quente` (mediana), `offset_cmd` (mediana de `tps_cmd − tps` em regime estacionário), regressões `pedal_e ≈ a·pedal_d + b` e `tps_b ≈ c·tps + d`, e `rpm_idle_quente` (mediana).
- `pedal_norm = (pedal_d − min) / (max − min)`.
- **Regras que dependem da linha de base** (R1, R2, R5-alvo, R8) ficam em "aprendendo" até ela existir. **Regras absolutas** (R3, R4, R6, R9, DTCs) valem desde o primeiro dia.
- Como o carro teve problema recente, a linha de base pode nascer contaminada. A interface tem "redefinir linha de base" (incrementa `epoch`) e "marcar como referência" após limpeza ou reparo.

**Condição de marcha lenta:** `speed = 0`, `pedal_norm < 2%`, motor ligado há mais de 60 s, 5 s após entrar na condição. Marcha lenta quente exige ECT ≥ 70 °C. Marcha lenta fria (ECT < 50 °C) é analisada à parte: rotação alta e decrescente é normal, e etanol a frio é naturalmente mais áspero.

### 7.2 Regras

| ID | Anomalia | Condição inicial | Severidade |
|---|---|---|---|
| R1 | Erro de rastreio (comando × posição) | `\|tps_cmd − tps − offset_cmd\| > 5 pp` por ≥ 1 s (≥ 3 amostras) em regime estacionário (`\|Δtps_cmd\| < 3 pp/amostra`), RPM > 400 | Aviso; > 10 pp por ≥ 2 s é crítico |
| R2 | Correlação entre sensores (precursor de P2135 / P2138) | Resíduo da regressão `pedal_e × pedal_d` ou `tps_b × tps` > 4 pp em ≥ 2 amostras seguidas do grupo B | Aviso; > 8 pp é crítico |
| R3 | Borboleta travada / modo de emergência | Em janela de 10 s: variação de `pedal_norm` > 20 pp e desvio-padrão de `tps` < 0,5 pp, RPM > 400. Ou `pedal_norm > 30%` com `tps_rel < 5%` por > 3 s | Crítico. Confiança alta se coincidir com P2101/P2118/P2119/P2135 |
| R4 | Marcha lenta oscilando (hunting) | Janela de 30 s em marcha lenta quente: pico a pico > 150 rpm e ≥ 3 ciclos completos (cruzamentos da média) com período de 1–8 s | Aviso; pico a pico > 300 rpm é crítico |
| R5 | Marcha lenta irregular ou fora do alvo | Desvio-padrão de RPM > 40 em 30 s sem periodicidade. Ou média > base + 200 rpm por > 20 s (tipo P0507). Ou média < base − 150 ou < 550 rpm (tipo P0506) | Aviso; desvio > 75 é crítico |
| R6 | Estol | RPM de > 400 para < 100 em ≤ 3 s, com a ECU respondendo e RPM 0 por ≥ 5 s. "Em marcha lenta" se estava na condição há > 3 s; "em arrancada" se pedal pressionado ou velocidade subindo | Marcha lenta: crítico. Arrancada: informativo (câmbio manual; pode ser o motorista) |
| R7 | Resposta lenta | Degrau: `tps_cmd` sobe > 10 pp entre amostras; medir o tempo até `tps` atingir 90% da variação. A 2 Hz só se detecta atraso grosseiro | ≥ 1 s aviso; ≥ 2 s crítico |
| R8 | Deriva da abertura em marcha lenta | Mediana semanal de `tps` em marcha lenta quente > base + 2 pp. Reforço: LTFT em marcha lenta > +10% com LTFT em cruzeiro dentro de ±5% (entrada falsa de ar) | Informativo |
| R9 | Salto de sinal | `tps` ou `pedal_d` salta > 15 pp em uma amostra sem mudança correspondente em `tps_cmd`/pedal e volta na amostra seguinte | Aviso; ≥ 3 por viagem é crítico |
| R10 | DTC da lista de vigilância | Qualquer código da lista, pendente ou confirmado | Crítico (pendente é aviso) |

**Lista de vigilância:** P0121–P0123, P0221–P0223, P0505–P0507, P0638, P2100–P2103, P2108, P2111, P2112, P2118, P2119, P2122, P2123, P2127, P2128, P2135, P2138, P2176. Somar os códigos Honda do sistema ETCS (P1658, P1659, P1683, P1684: confirmar no manual de serviço). Descrições em português ficam em `src/obdpi/obd/dtc_pt.json`. O P2176 (marcha lenta não aprendida) traz a dica de que limpeza do TBI ou bateria desconectada exigem o procedimento de reaprendizado.

**Limite honesto do R7.** Uma borboleta saudável responde em menos de 150 ms. A 2 Hz isso é invisível. Tempo de resposta real só sai do modo diagnóstico (8–10 Hz).

**Teste de varredura (Fase 3).** Assistente no dashboard, com ignição ligada e motor desligado: "pise devagar até o fundo em 5 s, solte devagar". Em modo diagnóstico gera a curva `tps × pedal`, verifica monotonicidade, falhas, histerese e degraus, e compara com a curva de referência salva. É o teste clássico de trilha de sensor, repetível após cada intervenção.

**Contexto para evitar falso positivo:**

- Mudança de LTFT após abastecer com proporção diferente de etanol é adaptação normal.
- Ar-condicionado ligado eleva a marcha lenta; o estado do A/C não é visível via OBD genérico, então o alvo usa faixa em vez de valor único.
- Viagens com ECT < 50 °C são rotuladas como "partida a frio".

### 7.3 Métricas e score

- **`tb_idle_window`:** uma linha por janela de 30 s de marcha lenta, com média, desvio, pico a pico e ciclos de RPM, mediana de TPS, MAP, STFT, LTFT e ECT. É a base dos gráficos de tendência.
- **Score por viagem (0–100):** 100 − 25 por crítico − 8 por aviso − 2 por informativo (cada regra limitada a 2 ocorrências) − penalidade de estabilidade (`min(15, max(0, desvio_rpm_idle − 20) / 4)`).
- **Semáforo:** verde ≥ 85, amarelo 60–84, vermelho < 60. O score do carro é a média ponderada dos últimos 7 dias.

### 7.4 Na interface

Página "Borboleta": semáforo grande com frase em linguagem simples, tendência do score, um cartão por regra (estado, última ocorrência, contagem em 30 dias), gráficos de tendência com marcadores do diário de manutenção, mapa de calor pedal × borboleta com a faixa esperada, histograma do erro de rastreio e lista de eventos. O detalhe do evento mostra ±30 s de pedal, comando, posição, RPM e MAP sobrepostos. Em "Ao vivo", barras gêmeas pedal/comando/posição e avisos imediatos.

---

## 7A. Alertas para qualquer sensor fora do padrão (requisito do dono)

Pedido explícito: **todo sensor que a ECU expõe é monitorado, e qualquer valor fora do padrão gera alerta no celular**, não só os da borboleta.

Módulo `src/obdpi/analytics/limits.py`, avaliado a cada leitura no coletor:

1. **Faixas absolutas (dia 1).** Tabela `config/limits.toml` com faixa normal, aviso e crítico por sinal, já ajustada ao R18A flex. Exemplos iniciais (calibrar):

| Sinal | Normal | Aviso | Crítico |
|---|---|---|---|
| ECT (motor quente) | 80–98 °C | > 102 °C | > 108 °C |
| Tensão (motor ligado) | 13,5–14,7 V | < 13,0 ou > 15,0 V | < 12,4 ou > 15,5 V |
| Tensão (motor desligado) | ≥ 12,4 V | < 12,2 V | < 11,8 V |
| STFT / LTFT | ±10 % | ±15 % | ±25 % (ou soma > ±25 %) |
| IAT | < 55 °C | > 60 °C | > 70 °C |
| RPM em marcha lenta quente | 650–850 | fora por > 20 s | < 550 ou > 1100 |
| Sonda O2 B1S1 | oscilando 0,1–0,9 V | parada > 30 s em malha fechada | — |
| MAP em marcha lenta | 25–40 kPa | > 45 kPa | — |
| Avanço em marcha lenta | 0–20° | < −5° | — |

2. **Faixas aprendidas (após a linha de base).** Para cada sinal, por contexto (marcha lenta quente / cruzeiro / partida a frio), guardar média e desvio. Valor fora de ±4σ por mais de N leituras gera aviso "fora do comportamento habitual". Isso cobre automaticamente PIDs sem faixa na tabela.
3. **Sensor sumiu.** PID que era suportado e passa a responder `NO DATA` repetidamente vira aviso.
4. **Entrega.** Evento em `limit_event` (mesmo formato de `tb_event`), selo vermelho/âmbar na aba Ao vivo, push no iPhone via app **ntfy** (funciona no iOS). Anti-spam: um alerta por sinal por viagem, com resumo no fim da viagem. Críticos (temperatura, tensão, borboleta, DTC) sempre notificam na hora quando há internet (hotspot); sem internet ficam no `outbox`.

Contexto do dono: o **corpo de borboleta foi trocado**. A linha de base da borboleta deve ser aprendida a partir de agora e o diário de manutenção começa com a anotação "troca do corpo de borboleta".

## 8. API e dashboard

### 8.1 Canal ao vivo

- **Coletor → web:** datagramas JSON em socket Unix `SOCK_DGRAM` (`/run/obd/live.sock`), sem bloqueio. Se o web estiver fora, o envio é descartado.
- **Web → coletor:** `/run/obd/control.sock` (requisição e resposta em JSON) para modo diagnóstico, redefinição de linha de base e dica de hora.

`GET /api/live` (SSE) emite:

| Evento | Frequência | Conteúdo |
|---|---|---|
| `fast` | Até 2 Hz (agregado se a coleta for mais rápida) | Linha de `tb_sample` |
| `full` | 20 s | Instantâneo de todos os sinais |
| `state` | Na mudança | Estado BT / ELM / ECU / motor / viagem |
| `dtc` | Na mudança | DTC que apareceu ou sumiu |
| `tb_event` | Na ocorrência | Evento da borboleta |

O cliente fecha o `EventSource` quando a aba fica oculta.

### 8.2 REST

| Método e rota | Função |
|---|---|
| `GET /api/status` | Estado da conexão, motor, viagem atual, taxas medidas, relógio sincronizado |
| `GET /api/live/snapshot` | Último valor de todos os sinais |
| `GET /api/signals` | Catálogo (chave, nome, unidade, faixa, último valor) |
| `GET /api/series?signals=rpm,tps&from=&to=&max_points=600` | Séries. O servidor escolhe a fonte (`tb_sample`, `sample` ou `agg_minute`) e reduz por mín/máx por balde |
| `GET /api/trips?from=&to=&page=` | Lista de viagens |
| `GET /api/trips/{id}` | Resumo, eventos e DTCs da viagem |
| `GET /api/trips/{id}/series?signals=` | Séries da viagem |
| `GET /api/dtc/current` | Confirmados, pendentes, MIL, monitores |
| `GET /api/dtc/history` | Linha do tempo de `dtc_event` |
| `GET /api/dtc/{code}` | Descrição, histórico, freeze frames |
| `GET /api/throttle/summary?days=30` | Semáforo, score, estado por regra, linha de base |
| `GET /api/throttle/events?from=&to=&rule=` | Lista de eventos |
| `GET /api/throttle/events/{id}` | Evento com janela de ±30 s |
| `GET /api/throttle/idle?days=90` | Tendência de marcha lenta |
| `GET /api/throttle/scatter?trip=\|days=` | Histograma 2D pedal × borboleta |
| `POST /api/throttle/baseline/reset` | Nova época de linha de base |
| `POST /api/control/diag-mode` | Liga ou desliga o modo diagnóstico (`{minutes}`) |
| `GET /api/reports/summary?period=day\|week\|month&date=` | Relatório periódico |
| `POST /api/reports/query` | Relatório personalizado (sinais, período, agregação, filtros) |
| `GET/POST/DELETE /api/reports/saved[/{id}]` | Relatórios salvos |
| `GET /api/export.csv?signals=&from=&to=` | Exportação em streaming |
| `GET/POST/DELETE /api/annotations[/{id}]` | Diário de manutenção |
| `GET /api/system` | Temperatura da CPU, `get_throttled`, disco, tamanho do banco, uptime, WiFi/IP, BT, versão |
| `GET /api/system/events` | `system_event` |
| `POST /api/system/time-hint` | Hora do celular quando não há NTP |

**Segurança.** Leitura aberta na rede local. Rotas `POST`/`DELETE` exigem um token simples (`web.admin_token`, guardado no navegador). Nada é exposto à internet.

### 8.3 Páginas

Tema escuro por padrão, cores semânticas (verde, âmbar, vermelho) com um acento de cor por domínio, barra de abas inferior. Atalho na tela inicial via manifest. Service worker fica de fora, porque exige HTTPS e `http://obd.local` não tem.

| Aba | Conteúdo |
|---|---|
| **Ao vivo** | Chip de estado. Medidores grandes: RPM, velocidade, ECT, tensão. Barras pedal/comando/posição. STFT/LTFT. MIL. Minigráficos dos últimos 60 s. Avisos |
| **Viagens** | Cartões (data, duração, km, velocidade média, score, selo de DTC ou evento). Detalhe com gráfico de sinais selecionáveis, marcadores de eventos e estatísticas. Selo "horário aproximado" quando aplicável |
| **Borboleta** | Seção 7.4, mais o assistente do teste de varredura |
| **DTCs** | Atuais, com descrição em português e relação com a borboleta. MIL e monitores. Linha do tempo histórica. Visualizador de freeze frame |
| **Mais → Relatórios** | Resumo diário, semanal e mensal. Construtor personalizado. Salvos. CSV |
| **Mais → Sinais** | Todos os PIDs descobertos, último valor, mín/máx, minigráfico; toque abre o histórico |
| **Mais → Sistema** | Saúde do Pi, BT/ELM/protocolo, PIDs suportados, relógio, rede, eventos, diário de manutenção, modo diagnóstico |

---

## 9. Rede e descoberta

Recomendação em camadas:

1. **mDNS (obrigatório, Fase 0).** Hostname `obd`, `avahi-daemon` e `/etc/avahi/services/obd.service` anunciando `_http._tcp`. Acesso por `http://obd.local`. Funciona bem em iOS, macOS e Windows; no Android depende da versão, daí o item 3.
2. **Redes conhecidas no NetworkManager (Fase 0).** Perfis para o WiFi de casa (prioridade 20) e o hotspot do celular (prioridade 10), com `autoconnect` e sem senha no git. Em casa, fazer reserva de DHCP no roteador. **O Pi 3B só tem 2,4 GHz:** o hotspot precisa estar em 2,4 GHz (Android: banda 2,4 GHz; iPhone: "Maximizar Compatibilidade"). É a causa mais comum de o Pi não achar o hotspot.
3. **Notificação com o IP via ntfy (Fase 2).** O dispatcher do NetworkManager chama `obd-announce`, que faz um POST para `https://ntfy.sh/<tópico-secreto>` com "OBD online em http://192.168.x.y (SSID)" e link clicável. Escolhi ntfy em vez de Telegram: é um POST HTTP, sem bot nem token, com app para Android e iOS. O nome do tópico funciona como segredo e fica em `/etc/obd/secrets.toml`. O mesmo canal carrega os alertas da Fase 3 (DTC, evento crítico) através da tabela `outbox`, esvaziada quando há internet.
4. **AP de fallback (Fase 2.5, recomendado).** Perfil `obd-ap` (`mode ap`, `ipv4.method shared`, SSID `OBD-Civic`, WPA2, IP 10.42.0.1). `obd-netwatch`: se 45 s após o boot não houver rede conhecida, sobe o AP. A cada 5 min sem cliente associado, derruba o AP e tenta as redes conhecidas por 30 s. Desvantagens: o celular fica sem internet enquanto conectado, não há NTP (compensado pela dica de hora) e a coexistência com o Bluetooth fica mais pesada.

**No carro, preferir o hotspot do celular.** Ele dá internet, portanto NTP e ntfy. O AP é o plano B.

---

## 10. Repositório, configuração e roteiro

### 10.1 Estrutura

```
obd-car-analysis/
├── README.md
├── Makefile                      # test, lint, build-frontend, deploy, backup
├── pyproject.toml                # pacote "obdpi"; deps: fastapi, uvicorn; extra [ble]: bleak
├── config/obd.example.toml
├── src/obdpi/
│   ├── config.py
│   ├── cli.py                    # obdctl: probe, bench, dump-pids, db-check, reanalyze, export
│   ├── elm/      transport.py  elm327.py  isotp.py  errors.py
│   ├── obd/      pids.py  decode.py  discovery.py  dtc.py  dtc_pt.json
│   ├── collector/ main.py  scheduler.py  trip.py  clock.py  writer.py  live.py  control.py
│   ├── analytics/ throttle.py  idle.py  baseline.py  score.py  trip_summary.py
│   ├── db/       conn.py  queries.py  migrations/0001_init.sql
│   ├── notify/   ntfy.py  outbox.py
│   ├── maint/    aggregate.py  backup.py  retention.py
│   └── web/      app.py  sse.py  routes/{status,series,trips,dtc,throttle,reports,system}.py  static/
├── frontend/                     # Svelte 5 + Vite + Tailwind + uPlot
├── deploy/
│   ├── systemd/                  # obd-collector, obd-web, obd-maint, obd-announce, obd-netwatch
│   ├── networkmanager/90-obd-announce
│   ├── avahi/obd.service
│   ├── journald/volatile.conf
│   ├── sudoers/obd-set-clock
│   ├── bt-ensure.sh
│   └── install.sh                # idempotente: usuário obd, venv em /opt/obd, units, /data/obd
├── scripts/                      # provision-pi.sh  bt-pair.sh  deploy.sh  backup-pull.sh  set-clock
├── tests/                        # unit + integração com emulador + fixtures de transcrições
└── docs/
    ├── ARQUITETURA.md
    ├── 00-preparacao-pi.md  01-bluetooth.md  02-coletor.md  03-dashboard.md
    ├── 04-rede.md  05-robustez-sd.md  06-corpo-de-borboleta.md
    ├── pids-civic-2008.md        # resultado real do obdctl probe
    ├── DIARIO.md                 # o que foi feito e aprendido, por data
    └── adr/                      # decisões curtas (0001-driver-proprio.md ...)
```

Deploy: `make deploy` compila o frontend e faz rsync de `src/`, `deploy/` e `frontend/dist` para `/opt/obd`, depois `systemctl restart`. O Pi só precisa de internet na instalação do venv.

### 10.2 Configuração (`/etc/obd/obd.toml`)

```toml
[adapter]
transport = "rfcomm"            # rfcomm | ble | serial | tcp | replay
mac = "AA:BB:CC:DD:EE:FF"
rfcomm_channel = 1
protocol = "auto"               # auto na 1ª vez; depois persistido (ex.: "6")
adaptive_timing = 1             # 1 ou 2 (ATAT)
record_raw = false

[collector]
full_period_s = 20
medium_period_s = 5
fast_target_hz = 2.0
fast_min_hz = 1.0
fast_group_a = ["0C", "11", "49", "4C", "0B", "0D"]
fast_group_b = ["11", "47", "49", "4A", "45", "4C"]
medium_group = ["04", "06", "07", "0E"]
commit_interval_s = 5
engine_on_rpm = 400

[storage]
db_path = "/data/obd/obd.db"
backup_dir = "/data/obd/backup"
max_db_gb = 8

[web]
bind = "0.0.0.0"
port = 80
admin_token = ""                # definir em secrets.toml

[network]
announce = "ntfy"               # ntfy | none
ap_fallback = false

[power]
ignition_gpio = -1              # -1 = desativado

[throttle.rules]                # limiares da seção 7.2
tracking_warn_pp = 5.0
tracking_crit_pp = 10.0
idle_hunt_p2p_rpm = 150
idle_rough_std_rpm = 40
# ...
```

Segredos (tópico ntfy, token) ficam em `/etc/obd/secrets.toml`, modo 600, fora do git.

### 10.3 Roteiro por fases

**Fase 0 — SO, Bluetooth e sonda**

- Gravar o SO, criar a partição `/data`, usuário `obd`, journald volátil, mDNS, perfis WiFi, enxugar o boot.
- Parear o adaptador. Implementar `elm/` e `obdctl probe` / `obdctl bench`.
- Aceite:
  1. Após reboot, `obdctl probe` conecta sem intervenção.
  2. `docs/pids-civic-2008.md` commitado com: tipo do adaptador, versão do ELM, protocolo, PIDs suportados (em especial 0x11/45/47/49/4A/4C/52), suporte a multi-PID e ao dígito de contagem, requisições por segundo.
  3. `http://obd.local` responde (página provisória).
  4. Boot até o prompt em até 35 s.

**Fase 1 — Coletor**

- Escalonador, decodificação, banco, viagens, relógio, DTCs e freeze frame, `tb_sample`, alerta da lista de vigilância, watchdog, testes com emulador e replay.
- Aceite:
  1. Dez viagens reais sem intervenção.
  2. Com motor ligado, ao menos 99% dos ciclos de 20 s completos (nenhum buraco maior que 25 s).
  3. Grupo rápido a pelo menos 1 Hz.
  4. Vinte cortes abruptos de energia: `PRAGMA integrity_check` ok e perda de até 10 s.
  5. Reconexão em menos de 15 s após tirar e recolocar o adaptador.
  6. RSS abaixo de 80 MB e CPU abaixo de 15% de um núcleo.
  7. DTC simulado no emulador gera `dtc_event` e freeze frame.
  8. Sessão sem rede seguida de WiFi tem os timestamps corrigidos.

**Fase 2 — Dashboard**

- API, SSE, SPA (Ao vivo, Viagens, DTCs, Sinais, Sistema), `obd-announce`.
- Aceite:
  1. No celular, primeira carga em menos de 3 s na rede local, bundle abaixo de 300 KB gzip.
  2. Latência ao vivo abaixo de 1,5 s.
  3. Gráfico de viagem de 1 h em menos de 2 s.
  4. RSS do web abaixo de 120 MB.
  5. Notificação com IP em menos de 30 s após conectar ao WiFi.
  6. A coleta não perde ciclos com o dashboard aberto.

**Fase 2.5 — AP de fallback**

- Aceite: sem rede conhecida, o SSID aparece em até 90 s e o dashboard abre em `http://10.42.0.1`. Ao voltar para casa, o Pi reconecta sozinho.

**Fase 3 — Borboleta, relatórios, alertas e endurecimento**

- Linha de base, regras R1–R9, score, página Borboleta, modo diagnóstico e teste de varredura, relatórios personalizados e CSV, alertas ntfy com `outbox`, backup, overlay de root, hardware opcional.
- Aceite:
  1. `obdctl reanalyze` reproduz os mesmos eventos sobre os dados históricos.
  2. Traços sintéticos de cada anomalia disparam a regra certa nos testes; traços saudáveis não disparam.
  3. Duas semanas de uso real sem falso crítico.
  4. Alerta de DTC chega ao celular.
  5. Trinta dias com overlay ativo sem intervenção.

---

## 11. Riscos e perguntas em aberto

### Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| Civic brasileiro de 2008 é anterior à OBDBr-2; a ECU pode não expor 0x45/47/49/4A/4C | Regras R1/R2 ficam limitadas a pedal × TPS | A Fase 0 responde. Plano B: PIDs proprietários Honda (modo 22), fora do escopo |
| Clone ELM327 ruim (falso "v2.1", sem multi-PID, trava) | Taxa baixa, grupo rápido a 1 Hz ou menos | `obdctl bench`. Se necessário, trocar por um adaptador de qualidade |
| Adaptador é BLE, não SPP | Mais trabalho (`BleTransport`) | Abstração de transporte já prevista |
| Só um cliente BT por vez | Com o Car Scanner aberto no celular, o Pi não conecta | Detectar `ECONNREFUSED`, registrar e avisar; desparear o celular |
| WiFi e BT dividem rádio e antena no BCM43438 | Quedas de BT com tráfego WiFi, pior em modo AP | Medir. Plano B: dongle BT USB |
| Queda de tensão na partida reinicia o Pi | Perda do início da viagem, risco ao SD | Buck de qualidade; monitorar `get_throttled` |
| Cartão SD morre | Perda do histórico | Cartão de alta durabilidade e backup fora do Pi |
| ELM327 sempre plugado consome 20–45 mA | Bateria baixa após semanas parado | Avisar o dono; adaptador com chave ou modo sleep |
| 0x52 (etanol) não suportado | Sem percentual de etanol; consumo estimado impreciso | Diário de abastecimento manual; consumo como estimativa rotulada |
| Linha de base aprendida com o defeito presente | Regras calibradas no "errado" | Redefinir linha de base e marcar referência após reparo |
| Amostragem a 1–2 Hz não enxerga falhas rápidas | Resposta lenta fina e saltos curtos escapam | Modo diagnóstico e teste de varredura |
| Calor na cabine | Throttling e desgaste | Pi longe do sol, dissipador, sem bateria de lítio |

### Perguntas para o dono

1. Qual é exatamente o adaptador (marca, nome que aparece no Bluetooth, versão que o Car Scanner mostra)? Aparece em "Bluetooth clássico" ou só em apps BLE?
2. No Car Scanner, quais sensores de pedal e borboleta aparecem com valor (posição do pedal D/E, atuador comandado, posição relativa)? Isso antecipa a Fase 0.
3. A tomada 12 V do Civic desliga com a chave? Você costuma ligar direto ou deixa em ACC antes?
4. O que foi feito no corpo de borboleta (limpeza, troca, reaprendizado), quando, e quais sintomas e DTCs apareceram? Isso define prioridade das regras e as primeiras anotações.
5. O carro dorme ao alcance do WiFi de casa? Se sim, NTP e backup se resolvem sozinhos na maioria dos dias.
6. Topa comprar um DS3231 (recomendado) e, depois, um supercapacitor/UPS ou relé temporizado?
7. Android ou iPhone? Aceita instalar o app ntfy? Prefere hotspot no carro ou o AP do Pi?
8. Quer apagar DTCs pelo dashboard (modo 04)? O padrão recomendado é não.
9. Quanto tempo o carro fica parado sem rodar? Isso pesa na decisão de deixar o ELM327 sempre plugado.
10. Interesse em consumo de combustível e custo por km? Entraria na Fase 3 como estimativa, com diário de abastecimento.

---

### Arquivos críticos para implementação

Nenhum existe ainda; são os primeiros a criar e os que concentram o risco técnico.

- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/elm/elm327.py` — driver ELM327: inicialização, comandos, erros, multi-PID (junto com `transport.py` e `isotp.py`).
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/collector/main.py` — máquina de estados, reconexão e watchdog (junto com `scheduler.py`, `clock.py` e `writer.py`).
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/obd/pids.py` — tabela de decodificação e descoberta de PIDs.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/db/migrations/0001_init.sql` — esquema SQLite da seção 5.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/analytics/throttle.py` — regras R1–R10, linha de base e score.