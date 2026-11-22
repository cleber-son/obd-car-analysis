# Arquitetura do GPS — VK-172 no Civic (Raspberry Pi 3 + gpsd)

> Desenhada com Claude Fable em 2026-11-22, depois de ler o código atual (`src/obdpi`, `cloud/`, `deploy/`). **O GPS ainda não chegou**: nada aqui foi testado em hardware. Itens marcados **[HW]** só podem ser conferidos com o receptor ligado no Pi; todo o resto é construído e testado com simulador. Este documento é o plano de execução: cada fase da seção 12 é fechada em si, com arquivos, funções, tabelas, rotas e testes.

Objetivo: acrescentar posição, velocidade real, altitude e hora exata ao registrador, e com isso entregar as 24 funcionalidades da seção 9 (diário de bordo, relógio, consumo por rota, mapa de calor, trecho de referência, marchas e embreagem, cerca virtual, 0–100, dinamômetro, nota de condução e as demais), sem deixar o Pi mais pesado nem menos confiável.

Decisões principais:

| Tema | Decisão |
|---|---|
| Quem fala com o receptor | `gpsd` (pacote do sistema), com `-n`. O coletor é cliente TCP de `127.0.0.1:2947` (JSON), em Python puro |
| Onde o GPS entra | **Fio (thread) dentro do `obd-collector`**, não um serviço novo. O laço principal continua sendo o único que grava no banco |
| Abstração | `GpsSource` com 4 implementações: `GpsdSource`, `NmeaSerialSource` (reserva sem gpsd, `termios` da stdlib), `ReplaySource` (arquivo NMEA/JSON) e `NullSource` |
| Sem GPS | Nada quebra: `live.gps.state = "desconectado"`, a interface mostra "GPS não conectado", viagens e consumo seguem pelo OBD como hoje |
| Taxa | Receptor a **5 Hz** o tempo todo; grava **1 Hz** em movimento, 1 posição a cada 30 s parado, e os 5 Hz inteiros só nas janelas de desempenho (0–100, retomada, dinamômetro) |
| OBD | **Não precisa acelerar.** O ELM327 segue em ~10 pedidos/s; velocidade e aceleração passam a vir do GPS. Só o teste armado mantém o modo "só rotação e velocidade" que já existe |
| Relógio | `chrony` com `refclock SHM 0` do gpsd no lugar do `systemd-timesyncd`; o coletor continua com o relógio ancorado no monotônico e corrige a sessão para trás quando o sistema acerta. Reserva: correção direta pela hora do GPS |
| Banco | Mesmas regras do esquema atual: tabelas novas em `schema.sql` (`CREATE TABLE IF NOT EXISTS`), colunas novas por `_add_col` em `db.migrate()` |
| Análises | Tudo o que é por viagem é calculado **depois**, no fechamento (`finalize_trip`), por funções puras sobre `gps_fix` + `fast_sample`. Só reage ao vivo o que precisa: lugares, radar/limite, patinação, 0–100, previsão de chegada |
| Mapa | Leaflet 1.9.4 copiado para `web/static/` (sem CDN, sem imagens); ladrilhos do OpenStreetMap carregados pelo navegador. Sem internet o traçado aparece sobre fundo liso |
| Dados offline | Um arquivo `geo.db` (SQLite só leitura) gerado no PC por `scripts/geo-build.py` com radares, limites, tipo de via, postos e nomes de bairros/ruas do OSM; vai para `/data/obd/geo/` |
| Privacidade | **O painel online é público.** Nenhuma coordenada nem nome de lugar sai de `/api/gps/*`, e na nuvem esse prefixo exige a senha de localização (`LOC_PASSWORD`). `live.json` (público) nunca leva latitude/longitude |
| Compartilhar posição | Link `/s/<token>` no Worker, token de 128 bits guardado como hash, com validade (padrão 1 h, máximo 24 h) e botão de revogar |

---

## 1. Visão geral

```
   VK-172 (u-blox 7, USB)                         RASPBERRY PI 3
 ┌─────────────────────┐  /dev/ttyACM0  ┌───────────────────────────────────────────────────────┐
 │ NMEA/UBX 5 Hz       │──────────────► │ gpsd -n  (pacote do sistema, usuário gpsd)             │
 └─────────────────────┘   udev/hotplug │   ├─ SHM 0 ──► chronyd ──► relógio do sistema          │
                                        │   └─ TCP 127.0.0.1:2947 (JSON)                         │
        CARRO                           │              │                                         │
 ┌───────────────┐   BT SPP             │ obd-collector.service                                  │
 │ ECU ◄► ELM327 │◄───────────────────► │   laço principal (ELM327, único que grava no SQLite)   │
 └───────────────┘                      │        ▲ pump() a cada ciclo                           │
                                        │   fio "gps" (GpsReader) ── fila de posições            │
                                        │   GpsHub: lugares, marchas, 0–100, radar, viagem       │
                                        │        │                                               │
                                        │        ├─► SQLite /data/obd/obd.db (gps_fix, trip_gps…)│
                                        │        ├─► /run/obd/live.json  (campo gps, sem posição)│
                                        │        └─► /run/obd/gps.json   (posição; privado)      │
                                        │ geo.db (só leitura): radares, limites, vias, postos    │
                                        │ obd-web: /api/gps/*  + Leaflet em /static              │
                                        │ obd-sync: documentos /api/gps/* ──► Worker (protegidos)│
                                        └───────────────────────────────────────────────────────┘
```

### 1.1 Por que um fio no coletor e não um serviço novo

1. **Um só escritor.** O coletor deixa a transação aberta entre um `commit` e outro (até 5 s). Um segundo processo gravando no mesmo banco ficaria esperando o `busy_timeout` o tempo todo. Outro arquivo de banco resolveria a disputa, mas obrigaria a juntar dois bancos e a manter duas correções de relógio.
2. **Um só relógio.** As posições recebem o `ts` do `Clock` do coletor, o mesmo de `fast_sample`. Quando o relógio é corrigido para trás (`apply_clock_offset`), OBD e GPS andam juntos na mesma transação.
3. **O coletor precisa da velocidade do GPS ao vivo** (marcha, patinação, 0–100, km da viagem). Com outro processo haveria mais um canal entre processos só para isso.
4. **O risco fica fora.** A parte nativa e sujeita a travar com USB (o `gpsd`) já é outro processo. O fio do coletor só lê um socket, com `try/except` em volta de tudo; se morrer, é recriado. A regra do projeto vale aqui: **o GPS nunca derruba o coletor**.

Custo no Pi 3 (estimado; conferir no dia do hardware):

| Item | RAM | CPU |
|---|---|---|
| `gpsd` a 5 Hz | 4–6 MB | 1–2 % de um núcleo |
| `chronyd` | 2–3 MB | ~0 |
| Coletor (fio, filas, motores, `geo.db`) | +6–10 MB (limite do serviço: 180 MB, hoje usa ~40) | < 2 ms por ciclo |
| Web | +0 (Leaflet é arquivo estático) | consultas de trajeto: 20–80 ms |

O cão de guarda (`WatchdogSec=90`) continua sendo alimentado só pelo laço principal. O fio do GPS não chama `sd_notify`.

### 1.2 Taxas de amostragem

| O quê | Taxa | Onde fica |
|---|---|---|
| Receptor | 5 Hz (`gps.rate_hz`; cai para 1 Hz se o receptor recusar) | memória: anel de 30 s com tudo |
| Gravação em movimento | 1 Hz (a posição mais próxima de cada segundo cheio) | `gps_fix` |
| Gravação parado (< 1,5 km/h por 10 s) | 1 a cada 30 s | `gps_fix`, `flags` bit 1 |
| Janela de desempenho | 5 Hz, incluindo os 5 s anteriores (tirados do anel) | `gps_fix`, `flags` bit 0 |
| Sem viagem (ignição ligada, motor desligado, ou OBD fora) | igual às regras acima, `trip_id` nulo | `gps_fix` |
| Estado para a tela | a cada ciclo rápido (~2 Hz) | `live.json` e `gps.json` |

**O OBD não precisa acelerar.** O orçamento do ELM327 clone é de ~10 pedidos/s e já está ocupado pelo grupo rápido (~1,7 leitura/s) mais a varredura de 5 s. As funcionalidades novas foram desenhadas para isso:

- Marcha e patinação comparam rotação (2 Hz) com velocidade do GPS interpolada no instante da leitura da rotação; só avaliam em regime de 1,5 s, então 2 Hz basta.
- 0–100 e retomadas usam **só o GPS** para o tempo. A rotação é informativa.
- No dinamômetro a rotação de cada ponto sai da velocidade do GPS e da relação aprendida da marcha (`rpm = v ÷ km/h por 1000 rpm`), que é mais lisa que a rotação lida a 2 Hz.
- O teste armado (`/api/accel/arm`) continua suspendendo a varredura e os DTCs como hoje (`testing` em `run_connected`), agora com o GPS marcando o tempo.

---

## 2. Receptor, gpsd e a abstração de fonte

### 2.1 O receptor

VK-172: chip u-blox 7 (G7020), só GPS L1 (com SBAS), USB CDC-ACM, `idVendor=1546`, `idProduct=01a7` **[HW: conferir com `lsusb`]**. Sai de fábrica em NMEA a 1 Hz. Não tem memória flash na maioria das unidades: a configuração de taxa se perde quando falta energia, então é reenviada a cada conexão. Precisão típica 2,5–5 m de posição e ~0,1 m/s de velocidade (Doppler). A altitude é o dado mais fraco (±5–10 m).

Pontos práticos:

- O Pi liga junto com a ignição e leva ~35 s para subir. O receptor é alimentado ao mesmo tempo, então a busca de satélites corre em paralelo com o boot. Partida fria (carro parado há horas): 30–60 s. O começo da viagem pode ficar sem posição; a seção 9.1 resolve com a última posição de estacionamento.
- Usar um **cabo extensor USB de 0,5–1 m** e deixar o receptor no painel, perto do para-brisa. Colado no Pi ele sofre com o ruído do próprio Pi e do WiFi/Bluetooth.
- O `ModemManager` já está desativado pelo `install.sh` (ele costuma abrir portas `ttyACM` e atrapalhar o gpsd).

### 2.2 gpsd

- Pacotes: `gpsd` e `gpsd-tools` (traz `cgps`, `gpsmon`, `ubxtool`, `gpsfake`; conferir o nome exato no Trixie, pode ser `gpsd-clients`).
- `/etc/default/gpsd`: `START_DAEMON="true"`, `USBAUTO="true"`, `DEVICES=""`, `GPSD_OPTIONS="-n"`. O `-n` faz o gpsd ler o receptor mesmo sem cliente, o que mantém o `chrony` alimentado.
- Hotplug: as regras de udev do próprio gpsd já reconhecem o `1546:01a7` e chamam `gpsdctl add`. Uma regra nossa cria o apelido estável `/dev/gps0` (seção 11.1), usado só pela fonte de reserva.
- O gpsd não grava nada em disco, então o sistema somente leitura (overlay) não o afeta.

Protocolo usado pelo cliente (uma linha JSON por mensagem):

| Envia / recebe | Conteúdo usado |
|---|---|
| → `?WATCH={"enable":true,"json":true};` | liga o fluxo |
| → `?DEVICE={"path":"<dev>","cycle":0.2};` | pede 5 Hz (o driver u-blox do gpsd troca a taxa) **[HW]** |
| ← `VERSION`, `DEVICES`, `DEVICE` | presença do receptor (`path`, `driver`, `activated`); `DEVICE` com `activated` 0 ou ausente = removido |
| ← `TPV` | `mode` (0/1 sem posição, 2, 3), `time` (ISO 8601 UTC), `lat`, `lon`, `altMSL` (ou `alt` em versões antigas), `speed` (m/s), `track` (graus), `climb` (m/s) |
| ← `SKY` | `hdop`, `uSat` (ou contar `satellites[].used`), `nSat` |

### 2.3 Fontes (`src/obdpi/gps/source.py`)

```python
class SourceError(Exception): ...

class GpsSource:
    name = "base"
    def open(self) -> None: ...
    def read(self, timeout: float) -> list[Fix]: ...    # bloqueia até 'timeout'; [] se não chegou nada
    def set_rate(self, hz: float) -> bool: ...          # True se a fonte aceitou
    def info(self) -> dict: ...                         # {"connected": bool, "device": str|None, "driver": str|None, "sats_seen": int|None}
    def close(self) -> None: ...

class GpsdSource(GpsSource):        # __init__(self, host="127.0.0.1", port=2947)
class NmeaSerialSource(GpsSource):  # __init__(self, device="/dev/gps0")
class ReplaySource(GpsSource):      # __init__(self, path, speed=1.0, loop=False, rebase=True)
class NullSource(GpsSource):        # nunca entrega nada; info()["connected"] = False

def open_source(cfg: dict) -> GpsSource
```

Regras:

- **Receptor ausente não é erro.** `read()` devolve `[]` e `info()["connected"]` fica `False`. `SourceError` só para quebra do transporte (socket fechado, `EIO` na serial); o `GpsReader` reabre com espera de 2, 4, 8… até 30 s.
- `GpsdSource`: socket TCP com `settimeout`; junta os bytes até `\n`; ignora classes que não usa; guarda o último `SKY` e aplica `hdop`/`sats` ao `TPV` seguinte. Se ficar 10 s sem nenhuma linha, fecha e reconecta. `set_rate` envia o `?DEVICE` com `cycle` e confere pelo intervalo real entre posições nos 5 s seguintes.
- `NmeaSerialSource`: `os.open(dev, O_RDWR | O_NOCTTY | O_NONBLOCK)`, `termios` em modo cru (9600 8N1; no USB a velocidade é ignorada), `select` para o timeout. Usa `nmea.NmeaAssembler`. `set_rate` escreve o quadro `UBX-CFG-RATE` e, uma vez por abertura, `UBX-CFG-NAV5` com modelo dinâmico 4 (automotivo). Sem `pyserial`.
- `ReplaySource`: lê `.nmea` (sentenças) ou `.jsonl` (linhas `TPV`/`SKY` do gpsd, ou objetos `{"t":…, "lat":…}` do simulador). Entrega respeitando os intervalos originais divididos por `speed`. Com `rebase=True` desloca as horas para "agora", para a viagem simulada cair no presente.
- `open_source`: `gps.source = "auto"` tenta o gpsd; se a conexão for recusada 3 vezes seguidas **e** existir `gps.device`, usa `NmeaSerialSource`. Se o gpsd responde mas não lista receptor, fica no gpsd (estado "desconectado") esperando o hotplug. Nunca abrir a serial direto com o gpsd rodando: os dois disputariam os bytes.

### 2.4 Leitor (`src/obdpi/gps/reader.py`)

```python
class GpsReader(threading.Thread):                 # daemon=True, name="gps"
    def __init__(self, cfg: dict): ...
    def run(self) -> None: ...                     # abre a fonte, lê, filtra, enfileira; nunca propaga exceção
    def latest(self) -> Fix | None: ...            # última posição (qualquer qualidade)
    def drain(self) -> list[Fix]: ...              # posições na fila para gravar/processar (tira da fila)
    def recent(self, seconds: float) -> list[Fix]: ...   # anel de 30 s na taxa cheia
    def status(self) -> dict: ...                  # state, src, device, mode, sats, hdop, hz, age_s, ttff_s, reconnects
    def request_rate(self, hz: float) -> None: ...
    def stop(self) -> None: ...
```

- Fila de saída: `collections.deque(maxlen=1500)` (5 min a 5 Hz). O laço principal pode ficar preso num `connect` do Bluetooth por dezenas de segundos; nada se perde nesse intervalo.
- Filtros antes de enfileirar: descarta `mode < 2`; descarta as 3 primeiras posições depois de adquirir sinal; descarta salto impossível (velocidade implícita > 250 km/h entre duas posições seguidas, ou salto > 100 m com velocidade informada < 5 km/h).
- Hora do GPS só é aceita entre 2026-01-01 e 2040-01-01 (proteção contra a virada de semana do GPS em clones).
- Estados: `desligado` (`gps.enabled = false` ou `source = "off"`), `desconectado` (sem receptor), `sem_sinal` (receptor presente, sem posição válida há mais de 5 s), `ok`.

---

## 3. Relógio sem RTC

Situação atual: o Pi não tem RTC e apaga a cada desligada. O `systemd-timesyncd` devolve no boot a última hora salva (`/var/lib/systemd/timesync`, mantida no cartão pelo `overlay.sh`), e o `Clock` do coletor ancora os `ts` no relógio monotônico; quando o NTP sincroniza, `apply_clock_offset()` corrige tudo desde `boot_ts`. Sem internet (carro na rua sem hotspot) a sessão inteira fica com hora aproximada.

Com o GPS a hora certa chega em 30–60 s depois de ligar, em qualquer lugar com céu aberto.

### 3.1 Cadeia

1. **`obd-clock.service`** (novo, `deploy/obd-clock.sh`, roda como root antes do `chrony` e do coletor): acerta o relógio do sistema para o maior valor entre a hora atual e a data de modificação de `/data/obd/obd.db-wal` (ou `obd.db`). O coletor grava a cada 5 s, então isso devolve "a hora em que o carro foi desligado" com erro de segundos. Substitui o que o arquivo do `timesyncd` fazia, sem depender de detalhes do `chrony`. Importante para o TLS do ntfy e da nuvem funcionar antes da primeira sincronização.
2. **`gpsd -n`** publica a hora de cada posição no segmento de memória compartilhada `SHM 0`.
3. **`chronyd`** lê `SHM 0` e também a internet quando há:

   ```
   # /etc/chrony/conf.d/obd-gps.conf
   refclock SHM 0 refid GPS precision 1e-1 offset 0.10 delay 0.4 poll 2 filter 8
   ```

   Em `/etc/chrony/chrony.conf` o `install.sh` troca `makestep 1 3` por `makestep 1.0 -1` (pode dar salto a qualquer momento, não só nas 3 primeiras medidas: o GPS pode demorar a pegar sinal). O `offset 0.10` compensa o atraso típico do NMEA pelo USB; o erro final fica em ±50–100 ms, sobra para este uso. **[HW: `chronyc sources -v` deve mostrar `GPS` com `Reach` diferente de 0; ajustar o `offset` se a diferença para o NTP passar de 0,2 s.]**
4. Com internet, os servidores NTP do `chrony.conf` padrão têm precisão melhor e são os preferidos; o GPS fica de reserva.
5. **Coletor**: `clock_synced()` passa a usar `timesrc.system_synced()`. Quando o sistema sincroniza, o caminho que já existe (`Clock.check_sync()` → `apply_clock_offset()`) corrige a sessão desde o boot, agora incluindo as tabelas do GPS.

### 3.2 `src/obdpi/timesrc.py` (novo)

```python
def system_synced() -> bool
    """True se o relógio do sistema está sincronizado, com timesyncd OU chrony."""
def source() -> str        # "ntp" | "gps" | "none"  (lê `chronyc -c tracking` uma vez por minuto, com cache; "none" se falhar)
```

`system_synced()`: devolve `True` se existir `/run/systemd/timesync/synchronized` (compatível com o Pi de hoje e com o WSL) **ou** se `adjtimex` não acusar relógio fora de sincronia:

```python
buf = ctypes.create_string_buffer(256)                 # struct timex zerada (modes = 0: só leitura)
return ctypes.CDLL(None, use_errno=True).adjtimex(buf) != 5      # 5 = TIME_ERROR (bit STA_UNSYNC ligado)
```

O `chronyd` limpa esse bit do kernel quando sincroniza; é o mesmo dado que o `timedatectl` mostra. Qualquer exceção devolve `False`. **[HW: conferir no Pi que o valor muda de 5 para 0 depois da primeira posição.]**

`web/app.py` (`/api/system`) troca a checagem do arquivo por `timesrc.system_synced()` e acrescenta `clock_source`.

### 3.3 Correção direta pelo GPS (reserva)

Se o sistema não sincronizar (fonte NMEA direta sem gpsd, ou `chrony` com problema), o coletor corrige os próprios `ts`:

```python
# collector/main.py, classe Clock
self.source = "sistema" if self.synced else "nenhuma"
def sync_from_gps(self, utc_ms: int) -> int:
    """Hora do GPS (já descontado gps.latency_ms). Marca como sincronizado e devolve o offset a aplicar (0 se |off| <= 2 s)."""
```

`GpsHub.clock_offset()` só chama isso quando: já chegaram 10 posições com hora válida, passaram 20 s desde a primeira e `clock.synced` continua falso. Em `maybe_commit()`:

```python
off = self.clock.check_sync()
if not off and not self.clock.synced:
    off = self.gps.clock_offset()
if off:
    self.apply_clock_offset(off)
```

Nesse modo o relógio **do sistema** segue errado até haver internet (o coletor roda como `obd` e não pode acertá-lo). Os dados gravados ficam certos; o painel local pode mostrar "há X min" errado. Limitação aceita do modo reserva.

### 3.4 Hora de cada posição

A chegada dos dados pelo USB atrasa e oscila. Para os tempos de 0–100 valerem, os intervalos entre posições têm de ser os do receptor (200 ms exatos), não os de chegada. Por isso o `ts` de cada posição é calculado assim (`GpsHub._stamp`):

```
desloc = mediana(clock.now() na chegada − t_gps) das 10 primeiras posições da conexão − gps.latency_ms (120)
ts     = t_gps + desloc
```

`desloc` é recalculado a cada reconexão da fonte e somado ao offset em `shift_clock()`. Posição sem `t_gps` válido recebe `clock.now() − latency_ms`. Colisão de `ts` (chave primária) usa `INSERT OR REPLACE`.

### 3.5 Correção para trás: o que entra

`apply_clock_offset(off)` chama `self.gps.shift_clock(off, start)`, que na mesma transação faz:

- `gps_fix`: dois passos, como `sample` e `fast_sample` (`ts = -(ts + off)` e depois `ts = -ts`), para `ts >= start`;
- `geo_event.ts`, `dyno_run.ts`, `accel_run.ts`: soma simples para `ts >= start` (o `accel_run` hoje fica de fora da correção; passa a entrar);
- em memória: `desloc`, posições no anel e na fila, estado dos motores (instantes de entrada em lugar, início de arrancada).

Tabelas calculadas no fechamento (`trip_gps`, `segment_pass`, `fuel_stop`, `route`) não entram: são derivadas e, se a viagem foi fechada com hora aproximada, `obdctl gps reanalyze` refaz.

### 3.6 Casos de borda

| Caso | Tratamento |
|---|---|
| GPS sem sinal a viagem inteira (garagem, túnel) e sem internet | Como hoje: `session.clock_synced = 0`, "horário aproximado" |
| Sinal chega no meio da viagem | Correção para trás desde `boot_ts`; a viagem inteira fica certa |
| Data absurda do receptor | Rejeitada pelo filtro de faixa (2026–2040) |
| NTP e GPS discordam em mais de 1 s | O `chrony` decide (NTP tem precisão melhor); registrar `system_event` "gps_hora_divergente" uma vez por sessão |
| Duração do carro parado (entre viagens) | Só é confiável se as duas sessões têm `clock_synced = 1`; quem usa esse intervalo (abastecimento, oficina) confere isso |

---

## 4. Modelo de dados

### 4.1 Tabelas novas (acrescentar ao fim de `src/obdpi/db/schema.sql`)

Todas entram na Fase 1, mesmo as usadas depois, para as fases seguintes não mexerem no esquema.

```sql
-- GPS. ts = relógio do coletor (ms UTC), o mesmo de fast_sample.
-- 1 Hz em movimento; 1 a cada 30 s parado (flags bit 1); 5 Hz nas janelas de desempenho (flags bit 0).
CREATE TABLE IF NOT EXISTS gps_fix (
  ts INTEGER PRIMARY KEY, trip_id INTEGER, lat REAL NOT NULL, lon REAL NOT NULL, alt REAL,
  speed REAL,                 -- km/h (Doppler)
  heading REAL,               -- graus verdadeiros; NULL abaixo de 3 km/h
  climb REAL,                 -- m/s
  hdop REAL, sats INTEGER, mode INTEGER, flags INTEGER DEFAULT 0);

-- Uma linha por viagem com tudo o que depende de posição. Separada de trip de propósito:
-- /api/trips devolve SELECT * FROM trip e é público na nuvem.
CREATE TABLE IF NOT EXISTS trip_gps (
  trip_id INTEGER PRIMARY KEY, v INTEGER DEFAULT 1, n_fix INTEGER, coverage REAL,
  gps_km REAL, obd_km REAL, moving_s INTEGER, stopped_s INTEGER, n_stops INTEGER,
  start_lat REAL, start_lon REAL, start_src TEXT,          -- 'gps' | 'estacionado' (posição herdada da viagem anterior)
  end_lat REAL, end_lon REAL,
  start_place_id INTEGER, end_place_id INTEGER, start_name TEXT, end_name TEXT,
  alt_min REAL, alt_max REAL, climb_m REAL, descent_m REAL,
  route_id INTEGER, cost REAL, cost_km REAL,
  drive_score INTEGER, drive_json TEXT, road_json TEXT, traffic_json TEXT, terrain_json TEXT,
  stops_json TEXT, track_json TEXT, flags TEXT, updated_ts INTEGER);

CREATE TABLE IF NOT EXISTS place (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'outro',                      -- casa | trabalho | oficina | posto | outro | auto
  lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 80,
  actions_json TEXT, osm_id TEXT, visits INTEGER DEFAULT 0, last_visit_ts INTEGER,
  created_ts INTEGER, deleted INTEGER DEFAULT 0);

-- Acontecimentos com posição: entrou/saiu de lugar, estacionou, mudou de lugar desligado, freada forte, patinação, km sem OBD…
CREATE TABLE IF NOT EXISTS geo_event (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, trip_id INTEGER, kind TEXT NOT NULL,
  place_id INTEGER, lat REAL, lon REAL, value REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS geo_event_ts ON geo_event(ts);
CREATE INDEX IF NOT EXISTS geo_event_kind ON geo_event(kind, ts);

CREATE TABLE IF NOT EXISTS route (
  id INTEGER PRIMARY KEY, name TEXT, start_key TEXT, end_key TEXT,   -- 'p<id do lugar>' ou 'c<célula>'
  cells_json TEXT, km REAL, n INTEGER DEFAULT 0, profile_json TEXT, stats_json TEXT,
  created_ts INTEGER, updated_ts INTEGER);
CREATE INDEX IF NOT EXISTS route_keys ON route(start_key, end_key);

CREATE TABLE IF NOT EXISTS segment (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, a_lat REAL, a_lon REAL, b_lat REAL, b_lon REAL,
  a_heading REAL, radius_m REAL DEFAULT 35, length_m REAL, ref_dur_s REAL, ref_trip_id INTEGER,
  active INTEGER DEFAULT 1, created_ts INTEGER);
CREATE TABLE IF NOT EXISTS segment_pass (
  id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL, trip_id INTEGER,
  ts_start INTEGER NOT NULL, ts_end INTEGER NOT NULL, dur_s REAL, km REAL, avg_speed REAL, stops INTEGER,
  fuel_l REAL, l100 REAL, stft REAL, lam_err REAL, map REAL, load REAL, tps REAL, rpm REAL, timing REAL,
  air_ratio REAL, ect REAL, iat REAL, baro REAL, alt_gain REAL, fuel_mix REAL, valid INTEGER, why TEXT,
  UNIQUE(segment_id, ts_start));

-- Modelos aprendidos e ajustes: 'gears', 'speedo', 'coast', 'vehicle', 'odo'…
CREATE TABLE IF NOT EXISTS gps_model (key TEXT PRIMARY KEY, json TEXT NOT NULL, updated_ts INTEGER);

CREATE TABLE IF NOT EXISTS dyno_run (
  id INTEGER PRIMARY KEY, ts INTEGER, trip_id INTEGER, gear INTEGER, rpm_min REAL, rpm_max REAL,
  peak_kw REAL, peak_kw_rpm REAL, peak_nm REAL, peak_nm_rpm REAL, corr REAL, slope_pct REAL,
  alt REAL, baro REAL, iat REAL, fuel_mix REAL, mass_kg REAL, valid INTEGER, why TEXT, curve_json TEXT);

CREATE TABLE IF NOT EXISTS fuel_stop (
  id INTEGER PRIMARY KEY, ts_start INTEGER, ts_end INTEGER, lat REAL, lon REAL, place_id INTEGER,
  trip_before INTEGER, trip_after INTEGER,
  status TEXT DEFAULT 'suspeito',                          -- suspeito | confirmado | descartado
  fill_id INTEGER, notified INTEGER DEFAULT 0);
```

`gps_fix` não ganha índice por `trip_id`: as consultas são por faixa de `ts` (início e fim da viagem), como já se faz com `fast_sample`.

### 4.2 Colunas novas (em `db.migrate()`, com `_add_col`)

```python
for col, decl in (("gps_km", "REAL"), ("obd_km", "REAL"), ("drive_score", "INTEGER")):
    _add_col(con, "trip", col, decl)              # números sem posição: podem aparecer na lista pública de viagens
for col, decl in (("source", "TEXT"), ("kind", "TEXT"), ("v0", "REAL"), ("v1", "REAL"), ("t_s", "REAL"), ("gear", "INTEGER"),
                  ("dist_m", "REAL"), ("slope_pct", "REAL"), ("auto", "INTEGER DEFAULT 0"), ("json", "TEXT")):
    _add_col(con, "accel_run", col, decl)         # 0–100 e retomadas pelo GPS ficam na tabela que já existe
_add_col(con, "fuel_fill", "place_id", "INTEGER") # posto onde abasteceu (só o id; o nome fica em place)
_add_col(con, "outbox", "expire_ts", "INTEGER")   # aviso que perde o sentido depois de um tempo ("chegou em casa")
_add_col(con, "session", "clock_source", "TEXT")  # sistema | gps | nenhuma
```

`trip.distance_km` continua sendo "a distância da viagem" para todo o resto do sistema (consumo, manutenção, relatórios), mas passa a ser a distância híbrida da seção 9.3. `obd_km` guarda a conta antiga e `gps_km` a parte medida pelo GPS.

### 4.3 `geo.db` (arquivo separado, só leitura)

Gerado no PC por `scripts/geo-build.py` (seção 9.17) e copiado para `/data/obd/geo/geo.db`. Aberto com `file:…?mode=ro&immutable=1`; reaberto quando a data do arquivo muda (conferida a cada 60 s).

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);     -- built_ts, bbox, fonte, contagens, atribuição
CREATE TABLE road (way_id INTEGER PRIMARY KEY, name TEXT, highway TEXT, maxspeed INTEGER, oneway INTEGER);
CREATE TABLE road_pt (cell INTEGER NOT NULL, way_id INTEGER NOT NULL, seq INTEGER NOT NULL, lat REAL, lon REAL);
CREATE INDEX road_pt_cell ON road_pt(cell);
CREATE TABLE camera (id INTEGER PRIMARY KEY, cell INTEGER, lat REAL, lon REAL, maxspeed INTEGER, dir REAL, way_id INTEGER);
CREATE INDEX camera_cell ON camera(cell);
CREATE TABLE poi (id INTEGER PRIMARY KEY, cell INTEGER, lat REAL, lon REAL, kind TEXT, name TEXT, brand TEXT);   -- posto | oficina
CREATE INDEX poi_cell ON poi(cell);
CREATE TABLE locality (id INTEGER PRIMARY KEY, cell2 INTEGER, lat REAL, lon REAL, kind TEXT, name TEXT, city TEXT, uf TEXT);
CREATE INDEX locality_cell ON locality(cell2);
```

`cell` = célula de 0,001° (~110 m): `int((lat + 90) * 1000) * 1_000_000 + int((lon + 180) * 1000)`. `cell2` = célula de 0,02° (~2 km) para bairros e cidades. A mesma função `geo.cell()` é usada no script e no Pi.

### 4.4 Espaço no cartão

| Tabela | Taxa | Por hora de motor ligado |
|---|---|---|
| `gps_fix` | 1 linha/s rodando (~60 B com cabeçalho) + rajadas | ~0,2 MB |
| `trip_gps` | 1 linha por viagem, `track_json` ≤ 100 kB | ~0,1 MB |
| `geo_event`, `segment_pass`, `dyno_run` e demais | dezenas de linhas por viagem | < 0,02 MB |
| **Total do GPS** | | **~0,3 MB/h** (o OBD hoje grava ~1 MB/h) |

Com 1,5 h por dia: ~170 MB por ano. `geo.db`: 30–80 MB para uma região metropolitana com ruas locais só no miolo (opção `--inner-km`). O cartão de 32 GB tem ~26 GB livres.

Retenção (`maint.py`, mesmo guarda-corpo de 15 % livres): depois de apagar `fast_sample` com mais de 12 meses, apagar `gps_fix` com mais de 24 meses. `trip_gps.track_json` fica: o trajeto reduzido de cada viagem sobrevive à limpeza.

### 4.5 Redução de pontos

`trip_gps.track_json` é montado uma vez no fechamento (`gps.trip.build_track`): Douglas–Peucker com tolerância de 6 m, mantendo no mínimo um ponto a cada 5 s em movimento, limite de 800 pontos (a tolerância dobra até caber). Formato em colunas, para o navegador colorir por qualquer sinal sem nova consulta:

```json
{"v": 1, "trip": 12, "t0": 1795000000000, "n": 742, "bbox": [-23.61, -46.70, -23.52, -46.61],
 "t": [0, 2, 5], "lat": [-23.55012, -23.55020, -23.55031], "lon": [-46.63311, -46.63318, -46.63330], "alt": [762, 762, 763],
 "cols": {"speed": [0, 12, 24], "rpm": [], "gear": [], "load": [], "map": [], "tps": [], "maf": [], "fuel_lh": [], "kml": [],
          "stft1": [], "lam": [], "timing": [], "ect": [], "iat": [], "slope": [], "limit": []},
 "gaps": [[412, 448]]}
```

`t` em segundos desde `t0`; latitude/longitude com 5 casas (~1 m); `cols` com o valor mais próximo no tempo (`fast_sample` até ±1,5 s; `sample` até ±6 s; `null` se não houver); `gaps` são trechos sem GPS (túnel), desenhados tracejados. Guardar pronto é obrigatório: o `obd-sync` pede o detalhe de **todas** as viagens a cada 5 min e não pode recalcular centenas de trajetos.

### 4.6 O que vai para a nuvem

O espelho continua sendo de documentos (`doc(key, json)` no D1). Nenhuma tabela do Pi é copiada.

| Documento | Frequência | Protegido |
|---|---|---|
| `/api/live` (ganha o campo `gps`, sem posição) | como hoje | não |
| `/api/gps/live` (posição atual, de `/run/obd/gps.json`) | junto com a cópia do ao vivo (5 s com ignição, 10 s sem) | **sim** |
| `/api/gps/logbook?limit=1000`, `/api/gps/places`, `/api/gps/parked`, `/api/gps/routes`, `/api/gps/segments`, `/api/gps/segments/{id}`, `/api/gps/alerts?days=90`, `/api/gps/events?days=30`, `/api/gps/stations`, `/api/gps/traffic?days=60`, `/api/gps/heat?signal=…&days=30` (6 sinais) | sincronização completa (5 min), só o que mudou | **sim** |
| `/api/gps/trips/{id}` e `/api/gps/trips/{id}/track` de cada viagem não oculta | idem | **sim** |
| `/api/gps/status`, `/api/gps/gears`, `/api/gps/perf`, `/api/gps/dyno`, `/api/gps/speedo`, `/api/gps/terrain?days=90`, `/api/gps/roadmix?days=30`, `/api/gps/score?days=90` | idem | não (sem posição nem nomes) |

Fica só no Pi: `gps_fix` em resolução cheia, `geo.db`, exportação GPX/KML e os arquivos de simulação.

Mudança no D1 (`cloud/schema.sql`, aplicada com `wrangler d1 execute civic-obd --remote --file=schema.sql`):

```sql
CREATE TABLE IF NOT EXISTS share (
  token_hash TEXT PRIMARY KEY, created_ts INTEGER NOT NULL, expires_ts INTEGER NOT NULL, label TEXT,
  until_place INTEGER, revoked INTEGER NOT NULL DEFAULT 0, views INTEGER NOT NULL DEFAULT 0, last_view_ts INTEGER);
```

Volume: um trajeto por viagem (≤ 100 kB) e a posição a cada 5 s somam ~1.100 gravações por dia de uso, longe do limite gratuito do D1 (100 mil/dia). No hotspot do celular: ~0,3 MB por viagem.

---

## 5. Módulos e arquivos

### 5.1 Arquivos novos

```
src/obdpi/timesrc.py                 relógio do sistema sincronizado? de onde?
src/obdpi/gps/
  __init__.py     Fix, good(), precise(), constantes
  nmea.py         parser NMEA, montador de época, quadros UBX, gerador de sentenças (simulador)
  source.py       GpsSource + Gpsd / NmeaSerial / Replay / Null, open_source()
  reader.py       GpsReader (o fio)
  geo.py          distância, rumo, células, Douglas–Peucker (funções puras)
  store.py        SQL do GPS: gravar posições, ler por viagem, gps_model, geo_event
  hub.py          GpsHub: ponte com o coletor, lista de motores ao vivo e de finalizadores
  trip.py         fechamento da viagem: distância híbrida, origem/destino, paradas, altitude, trajeto, texto do resumo
  places.py       lugares, cerca (GeofenceEngine), estacionamento e "mudou de lugar desligado", ações
  geocode.py      nome de um ponto: lugar salvo → geo.db → coordenada
  routes.py       agrupamento de viagens em rotas, estatística por rota
  segments.py     trecho de referência: passagens, comparação antes/depois
  gears.py        aprendizado das marchas, patinação da embreagem
  speedo.py       aferição do velocímetro e dos pneus
  score.py        nota de condução
  perf.py         0–100 e retomadas (PerfDetector)
  dyno.py         potência pela aceleração, descida em ponto morto (coastdown)
  terrain.py      inclinação e altitude × carga, ajuste e potência
  roads.py        geo.db: via atual, limite, radar à frente, tipo de via
  fuelstop.py     parada em posto, ranking de postos
  traffic.py      tempo parado, atraso por rota, melhor horário
  eta.py          previsão de destino, chegada e autonomia
  export.py       GPX e KML
  sim.py          gerador de viagem sintética (GPS + OBD coerentes), servidor gpsd falso
src/obdpi/web/gps_api.py             APIRouter com todas as rotas /api/gps/*
src/obdpi/web/static/leaflet.js      Leaflet 1.9.4 (copiado; licença BSD-2 no cabeçalho do arquivo)
src/obdpi/web/static/leaflet.css
src/obdpi/web/static/gpsmap.js       window.obdMap: mapa, traçado colorido, alfinetes, modo sem ladrilhos
src/obdpi/web/static/gps.js          window.obdGps: aba Mapa, diário, lugares, rotas, trechos, desempenho
src/obdpi/web/static/voice.js        window.obdVoice: fala em pt-BR (mesma técnica do tests.js) para o modo direção
src/obdpi/web/static/share.html      página pública do link de compartilhamento
scripts/geo-build.py                 gera data/geo/geo.db no PC a partir do OSM (Overpass)
deploy/obd-clock.sh, deploy/systemd/obd-clock.service
deploy/70-obd-gps.rules, deploy/chrony-obd-gps.conf, deploy/gpsd.default
tests/test_gps.py, tests/test_gps_trip.py, tests/test_gps_routes.py, tests/test_gps_drive.py,
tests/test_gps_perf.py, tests/test_gps_roads.py, tests/test_gps_daily.py, tests/fixtures/gps/*
cloud/test/loc.test.mjs
```

### 5.2 Arquivos existentes que mudam

| Arquivo | Mudança | Fase |
|---|---|---|
| `src/obdpi/config.py` | seções `gps`, `vehicle`, `power` em `DEFAULTS` | F1 |
| `src/obdpi/db/schema.sql`, `src/obdpi/db/__init__.py` | seções 4.1 e 4.2 | F1 |
| `src/obdpi/collector/main.py` | seção 6 (ganchos do `GpsHub`, relógio) | F1; `gear()` na F5; `accel_*` na F6 |
| `src/obdpi/web/app.py` | `include_router(gps_api.build(...))`, `/api/system` com `timesrc`; `fuel_fill` aceita `place_id` | F1; F8 |
| `src/obdpi/notify.py` | `flush_outbox` ignora vencidos (`expire_ts`) | F2 |
| `src/obdpi/extras.py` | `odometer()` soma km sem OBD; `impact()` ganha o trecho de referência | F2; F4 |
| `src/obdpi/maint.py` | retenção de `gps_fix` | F1 |
| `src/obdpi/reset.py` | "Resetar tudo" leva `place`, `segment` e `gps_model` para o banco novo | F2 |
| `src/obdpi/cli.py` | `obdctl gps status|sim|replay|reanalyze` | F1, F2 |
| `src/obdpi/doctor.py` | `gpsd` e `chrony` em `SERVICES` (só reinicia se instalados) | F1 |
| `src/obdpi/sync.py` | documentos `/api/gps/*`, posição ao vivo, `KEEP`, `cap=gps`; comando de sincronizar já; links | F3; F8; F9 |
| `src/obdpi/analytics/advice.py` | orientação para `gps:clutch_slip`, `gps:moved_off` | F5 |
| `web/static/index.html`, `app.js`, `extras.js`, `style.css`, `sw.js` | aba Mapa, ganchos, estilos, cache | F3 (e ajustes pequenos depois) |
| `web/static/drive.html` | quadros do GPS, voz, radar, 0–100 falado | F6, F7 |
| `cloud/src/index.js`, `cloud/schema.sql`, `cloud/build.sh` | proteção de localização, fila, links | F3; F9 |
| `deploy/install.sh`, `deploy/overlay.sh`, `scripts/deploy.sh`, `.gitignore` | seção 11 | F1; F7 |
| `config/obd.example.toml`, `README.md`, `docs/PROCESSO.md` | exemplo, tabela de recursos, registro de cada fase | toda fase |

### 5.3 Assinaturas principais

```python
# gps/__init__.py
@dataclass(slots=True)
class Fix:
    t_gps: int | None      # hora UTC do receptor em ms (None se inválida)
    mono: float            # time.monotonic() na chegada
    lat: float; lon: float
    alt: float | None = None; speed: float | None = None     # km/h
    heading: float | None = None; climb: float | None = None
    hdop: float | None = None; sats: int | None = None; mode: int = 0
    ts: int = 0            # relógio do coletor; preenchido pelo GpsHub
def good(f: Fix, max_hdop: float = 5.0, min_sats: int = 5) -> bool      # mode >= 2 e dentro dos limites
def precise(f: Fix) -> bool                                             # mode 3, hdop <= 2.0, sats >= 7

# gps/nmea.py
def checksum(body: str) -> str
def parse(line: str) -> dict | None                 # RMC, GGA, GSA, VTG (prefixos GP/GN/GL); None se checksum errado
class NmeaAssembler:
    def feed(self, line: str, mono: float) -> Fix | None      # devolve a posição quando a época (hhmmss.ss) fecha
def ubx(cls: int, mid: int, payload: bytes) -> bytes
def ubx_cfg_rate(hz: float) -> bytes
def ubx_cfg_nav5(dyn_model: int = 4) -> bytes
def to_nmea(f: Fix) -> list[str]                    # GGA + RMC com checksum (usado pelo simulador)

# gps/geo.py
def haversine_m(lat1, lon1, lat2, lon2) -> float
def bearing_deg(lat1, lon1, lat2, lon2) -> float
def ang_diff(a: float, b: float) -> float           # 0..180
def cell(lat: float, lon: float, size: float = 0.001) -> int
def neighbors(c: int, size: float = 0.001) -> list[int]      # a célula e as 8 vizinhas
def simplify(pts: list[tuple[float, float]], eps_m: float) -> list[int]     # índices mantidos (Douglas–Peucker iterativo)
def path_length_m(pts) -> float
def cross_track_m(p, a, b) -> float

# gps/store.py
def insert_fixes(con, fixes: list[Fix], trip_id: int | None, flags: int = 0) -> None
def trip_fixes(con, a: int, b: int) -> list[sqlite3.Row]
def nearest_fix(con, ts: int, tol_ms: int = 10_000) -> sqlite3.Row | None
def model_get(con, key: str, default=None)
def model_set(con, key: str, obj, ts: int) -> None
def add_event(con, ts: int, kind: str, *, trip_id=None, place_id=None, lat=None, lon=None, value=None, detail=None) -> int

# gps/hub.py
class GpsHub:
    def __init__(self, cfg, con, clock, state, event, queue_push, run_dir): ...
    def start(self) -> None                                   # cria o GpsReader se gps.enabled
    def pump(self, ts: int, values: dict, value_ts: dict, engine_on: bool, trip_id: int | None) -> list[Alert]
    def live(self) -> dict                                    # campo "gps" do live.json (sem posição)
    def best_speed(self, obd_speed: float | None) -> float | None      # GPS se recente (< 1,5 s) e bom; senão OBD × fator
    def clock_offset(self) -> int
    def shift_clock(self, off: int, start: int) -> None
    def on_trip_start(self, trip_id: int, ts: int, merged: bool) -> None
    def trip_distance(self, tid: int, a: int, b: int, obd_km: float) -> dict | None
    def finalize_trip(self, tid: int) -> None
    def summary(self, tid: int) -> dict                       # {"route": "Casa → Trabalho" | None, "parts": [...]}
    def stop(self) -> None
```

Dentro do `hub.py` duas listas concentram o que cada fase acrescenta, uma linha por fase:

```python
LIVE_ENGINES = []     # classes com feed(ctx) -> list[Alert], live() -> dict, on_trip_start(), shift(off)
FINALIZERS = []       # funções f(con, tid, ctx) chamadas em ordem por finalize_trip(), cada uma no seu try/except
```

Convenção: cada módulo de análise por viagem (`trip`, `routes`, `segments`, `score`, `dyno`, `terrain`, `roads`, `traffic`) expõe uma função com a assinatura `f(con, tid, ctx)` (chamada `finalize`, `assign_trip` ou `scan_trip` conforme o módulo) que lê o banco, chama as funções puras do módulo e grava o resultado em `trip_gps` (ou na tabela própria). As funções puras recebem listas e devolvem dicionários; são elas que os testes exercitam. `obdctl gps reanalyze` chama a mesma lista `FINALIZERS` para viagens antigas.

`ctx` é um objeto simples com `fix`, `values`, `value_ts`, `engine_on`, `trip_id`, `ts`, `reader` (para `recent()`), `cfg`, `con`, `geo` (conexão do `geo.db` ou `None`) e `models` (cache de `gps_model`).

### 5.4 Configuração (`config.py`, `DEFAULTS`)

```python
"gps": {
    "enabled": True, "source": "auto",            # auto | gpsd | nmea | replay | off
    "gpsd_host": "127.0.0.1", "gpsd_port": 2947, "device": "/dev/gps0",
    "rate_hz": 5, "store_hz": 1, "stopped_every_s": 30, "latency_ms": 120,
    "min_sats": 5, "max_hdop": 5.0,
    "replay_file": "", "replay_speed": 1.0, "replay_loop": False,
    "geo_db": "/data/obd/geo/geo.db",
    "moved_m": 300,                               # distância que caracteriza "mudou de lugar desligado"
    "cloud": "auto",                              # auto (envia posição só se a nuvem tiver senha de localização) | off
    "voice_radar": True, "voice_limit": True,
},
"vehicle": {"mass_kg": 1350, "cda": 0.63, "crr": 0.011, "drivetrain_eff": 0.88, "tank_l": 50},
"power": {"always_on": False, "min_batt_v": 12.0},
```

---

## 6. Integração no coletor (`collector/main.py`)

Mudanças pequenas e localizadas; toda chamada ao `GpsHub` passa por um invólucro que engole exceção e registra em `log.warning`.

| Onde | O que entra |
|---|---|
| imports | `from .. import timesrc` e `from ..gps.hub import GpsHub` |
| `clock_synced()` | `return timesrc.system_synced()` |
| `Clock` | atributo `source`; método `sync_from_gps()` (seção 3.3) |
| `Collector.__init__`, logo depois de `self.boot_ts = …` | `self.gps = GpsHub(cfg, self.con, self.clock, self.state, self.event, self.queue_push, self.run_dir)`; e, depois de `finalize_open_trips()`, `self.gps.start()` |
| novo método `gps_pump(self, ts=None)` | `self.handle_alerts(self.gps.pump(ts or self.clock.now(), self.values, self.value_ts, self.engine_on, self.trip_id))` dentro de `try/except Exception` |
| `fast_cycle`, primeira linha depois de `ts = …` | `self.gps_pump(ts)` |
| `ecu_silent_loop` | trocar o `time.sleep(5)` por 5 voltas de `self.gps_pump(); time.sleep(1)` (o GPS segue gravando com a ignição desligada e o Pi ligado) |
| `run()`, laço de espera do `backoff` | a cada segundo: `self.gps_pump(); self.write_live(); self.maybe_commit()` (o GPS segue gravando com o Bluetooth fora) |
| `maybe_commit` | bloco da seção 3.3; gravar `session.clock_source` |
| `apply_clock_offset` | `self.gps.shift_clock(off, start)` |
| `start_trip`, no fim | `self.gps.on_trip_start(self.trip_id, ts, self.trip_merged)` |
| `trip_live_update` | `spd = self.gps.best_speed(vals.get("speed")) or 0` |
| `finalize_trip`, depois do laço que calcula `dist` | `g = self.gps.trip_distance(tid, a, b, dist)`; se vier, `dist = g["km"]` e grava `trip.gps_km`, `trip.obd_km` |
| `finalize_trip`, antes de `self.trip_summary_push(tid)` | `self.gps.finalize_trip(tid)` |
| `trip_summary_push` | `x = self.gps.summary(tid)`; título `"Viagem encerrada: " + x["route"]` quando houver; `parts` recebe `x["parts"]` antes do item de alertas |
| `write_live` | `"gps": self.gps.live()` |
| `main()`, função `stop` | `col.gps.stop()` antes do `os._exit(0)` |

### 6.1 `GpsHub.pump()` em ordem

1. Lê comandos em `/run/obd/gps_cmd` (uma linha JSON por comando: `{"cmd": "coast_arm"}`, `{"cmd": "reload_places"}`, `{"cmd": "rate", "hz": 5}`), no mesmo estilo de `accel_cmd`.
2. `fixes = reader.drain()`; carimba o `ts` de cada uma (seção 3.4).
3. Escolhe o que gravar: 1 Hz; decimação parado; tudo se há rajada ativa. `store.insert_fixes(...)` na transação corrente do coletor (o `commit` continua sendo o de `maybe_commit`).
4. Para cada posição boa, monta `ctx` e chama `feed(ctx)` de cada item de `LIVE_ENGINES`, juntando os alertas.
5. A cada 60 s grava em `gps_model` o que os motores aprenderam (marchas, velocímetro), junto com a persistência que o coletor já faz a cada ~5 min.
6. Atualiza o dicionário de `live()` e reescreve `/run/obd/gps.json` (arquivo temporário + `os.replace`, como o `live.json`).
7. Registra em `system_event` as transições: `gps_conectado`, `gps_removido`, `gps_primeiro_sinal` (com o tempo até a primeira posição). No primeiro sinal da vida do sistema, um aviso no celular: "GPS funcionando ✅".

### 6.2 Campo `gps` do `live.json` (público, sem posição)

```json
"gps": {"state": "ok", "src": "gpsd", "mode": 3, "sats": 9, "hdop": 0.9, "hz": 5.0, "age_s": 0.2,
        "speed": 62.4, "heading": 181, "alt": 762, "slope": 1.8,
        "speedo_err": 3.1, "gear": 4, "slip": false,
        "limit": 60, "limit_src": "osm", "over": false, "warn": {"kind": "radar", "dist_m": 380, "limit": 50},
        "power_kw": 38, "avail_pct": 91, "clock": "gps"}
```

Sem receptor: `{"state": "desconectado"}`. Campos de fases posteriores simplesmente não existem até a fase entrar; a interface trata ausência como "—".

### 6.3 `/run/obd/gps.json` (privado; vira `/api/gps/live`)

```json
{"ts": 1795000000000, "state": "ok", "lat": -23.55012, "lon": -46.63311, "alt": 762, "speed": 62.4, "heading": 181,
 "sats": 9, "hdop": 0.9, "engine_on": true, "trip_id": 31, "place": null,
 "parked": null,
 "dest": {"name": "Trabalho", "conf": 0.82, "eta_ts": 1795000900000, "left_km": 7.4, "range_km": 310}}
```

Com o carro parado e desligado (Pi ainda ligado) `parked` traz `{lat, lon, ts, name}`.

---

## 7. API, nuvem e privacidade

### 7.1 Rotas do Pi (`web/gps_api.py`)

`def build(ro, check_admin, cfg, run_dir) -> APIRouter` recebe os utilitários do `app.py` (evita importação circular). Em `app.py`, antes do `app.mount("/static", …)`: `app.include_router(gps_api.build(ro, check_admin, CFG, LIVE.parent))`. Leitura com `ro()`; escrita com `db.connect(DB)` e `check_admin(x_token)`, como as rotas atuais.

| Método e rota | Função | Fase |
|---|---|---|
| `GET /api/gps/status` | estado da fonte, receptor, satélites, taxa, origem da hora, posições gravadas hoje, data e contagens do `geo.db` (sem a área coberta: esta rota é aberta na nuvem) | F1 |
| `GET /api/gps/live` | conteúdo de `gps.json` | F1 |
| `GET /api/gps/logbook?limit=&offset=` | diário de bordo: viagem + `trip_gps` (origem, destino, km, duração, média, km/L, custo, alertas, notas, rota) | F2 |
| `GET /api/gps/trips/{id}` | `trip_gps` sem o trajeto, paradas, acontecimentos (`geo_event`), alertas com posição, arrancadas | F2 |
| `GET /api/gps/trips/{id}/track` | `track_json` | F2 |
| `GET /api/gps/trips/{id}/export.gpx` e `.kml` | exportação em resolução cheia (só no Pi) | F3 |
| `GET /api/gps/export.gpx?from=&to=` | várias viagens num arquivo | F3 |
| `GET /api/gps/places`, `POST /api/gps/places`, `POST /api/gps/places/{id}` | lugares: listar, criar `{name, kind, lat, lon, radius_m, actions}`, editar ou `{delete: true}` | F2 |
| `GET /api/gps/parked` | onde o carro está estacionado: `{lat, lon, ts, name, maps_url}` | F2 |
| `GET /api/gps/events?days=` | `geo_event` recentes | F2 |
| `GET /api/gps/alerts?days=&rule=` | alertas e DTCs com a posição mais próxima (±10 s) | F3 |
| `GET /api/gps/heat?signal=&days=` | média do sinal por célula de ~55 m sobre os trajetos guardados | F3 |
| `GET /api/gps/routes`, `GET /api/gps/routes/{id}`, `POST /api/gps/routes/{id}` | rotas, estatística, renomear | F4 |
| `GET /api/gps/segments`, `POST /api/gps/segments`, `GET /api/gps/segments/{id}`, `POST /api/gps/segments/{id}` | trechos de referência, passagens, editar/apagar/reprocessar | F4 |
| `GET /api/gps/gears`, `POST /api/gps/gears/reset` | relações aprendidas, patinações | F5 |
| `GET /api/gps/speedo` | aferição do velocímetro e dos pneus | F5 |
| `GET /api/gps/score?days=` | nota de condução por viagem e tendência | F5 |
| `GET /api/gps/perf` | arrancadas e retomadas, recordes por tipo e combustível | F6 |
| `GET /api/gps/dyno`, `POST /api/gps/dyno/config`, `POST /api/gps/dyno/coast` | curvas, massa/CdA/Crr, armar a descida em ponto morto | F6 |
| `GET /api/gps/terrain?days=` | inclinação e altitude × carga, ajuste e potência | F6 |
| `GET /api/gps/roadmix?days=` | cidade × estrada | F7 |
| `GET /api/gps/stations`, `POST /api/gps/fuelstop/{id}` | postos e paradas suspeitas (confirmar/descartar) | F8 |
| `GET /api/gps/traffic?days=` | trânsito por rota e horário | F8 |
| `GET /api/gps/share`, `POST /api/gps/share`, `POST /api/gps/share/{id}` | links de posição ao vivo | F9 |

As rotas que já existem não ganham coordenada nem nome de lugar. Exceções permitidas, por serem só números: `/api/trips` passa a trazer `gps_km`, `obd_km` e `drive_score`; `/api/accel` traz as colunas novas de `accel_run`; `/api/impact` traz o bloco do trecho de referência identificado como "Trecho 1", "Trecho 2" (sem o nome dado pelo dono).

### 7.2 Worker (`cloud/src/index.js`)

O painel online hoje é público e sem senha, a pedido do dono. Posição é outro nível de dado: mostra onde a pessoa mora, trabalha e a que horas o carro não está em casa. Regra única, que **falha fechada**:

```js
const LOC_OPEN = /^\/api\/gps\/(status|gears|speedo|score|perf|dyno|terrain|roadmix)$/;
const isLoc = (p) => p.startsWith("/api/gps/") && !LOC_OPEN.test(p);
```

- `locAuthorized(req, env)`: se há `DASH_PASSWORD`, vale a sessão que já existe. Senão exige o cookie `obd_loc` (`exp.assinatura`, HMAC igual ao da sessão, 30 dias, `HttpOnly; Secure; SameSite=Lax`), criado por `POST /loc/unlock` com a senha `LOC_PASSWORD` (segredo novo: `wrangler secret put LOC_PASSWORD`). Senha errada espera 800 ms, como o login. `POST /loc/lock` apaga o cookie.
- `GET` de documento com `isLoc(p)` sem autorização: `401 {"error": "Localização protegida", "locked": true, "configured": <há senha?>}`.
- `/api/env` ganha `loc`: `"aberta"`, `"trancada"` ou `"desativada"` (sem `LOC_PASSWORD` nem `DASH_PASSWORD`).
- `/api/sync/ping` responde também `loc: true|false`. O `obd-sync` **só envia documentos de localização se vier `true`** (`gps.cloud = "auto"`). Defesa dupla: `syncDocs` descarta chaves `isLoc` quando não há senha configurada.
- Ações com localização autorizada entram na fila `cmd` (lugares, trechos, renomear rota, confirmar abastecimento): `LOC_QUEUE = [/^\/api\/gps\/places(\/\d+)?$/, /^\/api\/gps\/segments(\/\d+)?$/, /^\/api\/gps\/routes\/\d+$/, /^\/api\/gps\/fuelstop\/\d+$/]`, corpo ≤ 4.000 bytes, no máximo 30 pendentes. `syncCommands` só entrega caminhos `/api/gps/` a quem pede com `cap` contendo `gps` (o Pi atualizado pede `?cap=total,maint,gps`).
- `KEEP` (no Worker **e** em `sync.py`, que precisam ficar iguais) ganha: `"/api/gps/logbook": ["limit"]`, `"/api/gps/alerts": ["days"]`, `"/api/gps/events": ["days"]`, `"/api/gps/heat": ["signal", "days"]`, `"/api/gps/traffic": ["days"]`, `"/api/gps/terrain": ["days"]`, `"/api/gps/roadmix": ["days"]`, `"/api/gps/score": ["days"]`.
- Exportação GPX/KML na nuvem: `404` com a mesma mensagem do CSV ("disponível no painel do Pi").
- Links de compartilhamento: seção 9.21.

Não há CSP nem no FastAPI nem no Worker (conferido no código), então os ladrilhos de `https://tile.openstreetmap.org` carregam nos dois painéis. Se um dia entrar CSP: `img-src 'self' data: https://tile.openstreetmap.org`.

### 7.3 Service worker (`sw.js`)

- `CACHE = "civic-obd-v2"`; `SHELL` ganha `/static/gps.js`, `/static/gpsmap.js`, `/static/voice.js`, `/static/leaflet.js`, `/static/leaflet.css`.
- Não interceptar (rede direta, sem guardar cópia): caminhos que começam com `/api/gps/`, `/api/share/`, `/s/` e `/loc/`. Posição não fica guardada no aparelho depois que a senha expira.
- Ladrilhos são de outra origem e o `sw.js` já ignora outras origens. Nada a fazer.

### 7.4 Resumo de privacidade

| Dado | Painel do Pi (rede local) | Nuvem |
|---|---|---|
| `live.json` com `gps` (velocidade, satélites, limite, aviso de radar) | aberto | público |
| Posição atual, trajetos, lugares, rotas, trechos, postos | aberto na rede local, como o resto | só com senha de localização |
| Link `/s/<token>` | — | quem tem o link vê **só a posição atual** até vencer |
| ntfy (resumo com "Casa → Trabalho") | — | tópico secreto que já existe; não leva coordenada |
| Ladrilhos | o navegador pede ao OpenStreetMap os quadros da área vista | idem; a página do link envia só a origem como `Referer` (`strict-origin`), nunca o token |
| IA (`ai.py`) | o contexto enviado ao provedor **não** leva coordenadas nem nomes de lugar; só números (km pelo GPS, nota de condução) | — |
| `/oficina` (página do mecânico) | sem dados de posição | idem |
| Backup do banco | contém `gps_fix`; continua só em `/data/obd/backup` | — |

"Resetar tudo" (`reset.py`) move o banco inteiro como hoje, o que leva junto lugares e trechos. Na Fase 2 o reset passa a copiar `place`, `segment` e `gps_model` do banco antigo para o novo (são configuração do dono, não histórico).

---

## 8. Interface

### 8.1 Painel (`index.html`, `app.js`, `gps.js`, `gpsmap.js`)

- **Aba nova "Mapa"** (`<a href="#mapa" data-v="mapa" class="sec">`, dentro de "Mais" no celular) e `<section class="view" id="v-mapa" hidden>` com botões internos (`data-g`): **Diário · Mapa · Lugares · Rotas · Trechos · Desempenho**. Em `app.js`: `LOAD.mapa = async () => window.obdGps?.render()`.
- **Ganchos no `app.js`** (uma linha cada): em `showTrip`, depois de `window.obdExtras?.trip(t)`, chamar `window.obdGps?.trip(t)`; no fim de `loadTrips`, `window.dispatchEvent(new CustomEvent("obdtrips"))` para o `gps.js` escrever "Casa → Trabalho" sob a data de cada linha.
- **Gancho no `extras.js`**: dentro de `paint()` do replay, `window.dispatchEvent(new CustomEvent("obdreplay", {detail: {ts, trip: t.id}}))`.
- **Viagens → detalhe**: cartão "🗺️ Trajeto" com o mapa, seletor "Colorir por" (velocidade, rotação, marcha, carga, ajuste curto, λ, consumo, temperatura, avanço, inclinação), alfinetes dos alertas, paradas, botões GPX/KML.
- **Consumo**: cartão "Abastecimento detectado" (F8) e ranking de postos.
- **Diagnóstico → Resumo**: no "antes e depois" de cada serviço, a linha do trecho de referência (F4). Em **Alertas**, botão "ver no mapa".
- **Manutenção**: o hodômetro estimado passa a dizer "km pelo GPS".
- **Sistema**: cartão "GPS" (estado, satélites, taxa, origem da hora, `geo.db`, tempo até o primeiro sinal).
- **Sem receptor**: o cartão do Sistema e a aba Mapa mostram "GPS não conectado. Quando o receptor for ligado na USB do Pi, o mapa começa a ser preenchido sozinho." Viagens antigas mostram "Sem trajeto (antes do GPS)".
- **Nuvem trancada**: a aba Mapa mostra o formulário "🔒 Localização protegida" (senha → `POST /loc/unlock`). Se `loc = "desativada"`, explica que é preciso definir `LOC_PASSWORD`.

`gpsmap.js` (`window.obdMap`):

```js
obdMap.create(el, opts)            // L.map + ladrilhos OSM com atribuição; em 'tileerror' mostra "sem internet: só o traçado"
obdMap.track(map, trk, colorBy)    // polilinhas por trecho com a cor do valor (escala de 5 cores por quantis; marcha = cor fixa)
obdMap.pins(map, items)            // L.circleMarker / L.divIcon (sem as imagens padrão do Leaflet)
obdMap.cursor(map, lat, lon, hdg)  // marcador do replay e da posição ao vivo
obdMap.pick(map, cb)               // toque no mapa devolve lat/lon (criar lugar, marcar início/fim de trecho)
obdMap.nearest(trk, lat, lon)      // índice do ponto do trajeto mais próximo (toque no traçado → instante)
```

### 8.2 Modo direção (`drive.html`)

- Quadros novos no catálogo `CAT`: `gpsspd` (velocidade real e diferença para o velocímetro), `alt` (altitude e inclinação), `limit` (limite da via), `power` (potência estimada e % disponível na altitude), `eta` (destino previsto e chegada; só aparece com a posição liberada).
- Selo do limite ao lado da velocidade; fica vermelho quando `gps.over`.
- Faixa de aviso de radar (mesma faixa do alerta): "📷 Radar em 380 m · limite 50".
- Voz por `voice.js`: liberada no primeiro toque (o mesmo `click` que já pede o bloqueio de tela), botão 🔊/🔇 no topo, escolha salva em `localStorage`.
- Tela do 0–100: mesmo desenho; a origem do tempo passa a ser o GPS quando há sinal ("GPS 5 Hz" no rodapé) e o resultado é falado.
- Sem GPS os quadros novos mostram "—" e nada é falado.

### 8.3 Voz e notificações

`voice.js` (`window.obdVoice`): `unlock()`, `speak(text, {key, cooldown_s, interrupt})`, `set(on)`, `enabled()`. Copia a técnica validada no `tests.js` para o iPhone (cancelar e falar 150 ms depois, guardar as falas, `resume()`). O `tests.js` não muda. Limite conhecido: o Safari só fala com a página aberta e a tela acesa; o modo direção já mantém a tela ligada.

| Fala (modo direção) | Quando |
|---|---|
| "Radar a 500 metros. Limite 60." | radar à frente, velocidade > 40 km/h; uma vez por radar |
| "Reduza. 68, limite 60." | a 150 m do radar, acima do limite |
| "Limite 60." | 5 s acima do limite conhecido + 7 %; no máximo 1 vez a cada 2 min por via |
| "Zero a cem em 10 vírgula 8 segundos. Novo recorde." | fim de arrancada válida |
| "Embreagem patinando." | patinação confirmada |
| "Chegando em Casa." | entrada em lugar com ação de voz |
| "Autonomia de 50 quilômetros." | uma vez por viagem |

| Aviso no celular (ntfy) | Prioridade |
|---|---|
| Resumo da viagem (o que já existe, ampliado: "Viagem encerrada: Casa → Trabalho · 12,4 km em 31 min · 11,2 km/L · R$ 6,40 · nota 86 · sem alertas") | info/aviso |
| "O carro mudou de lugar desligado: está a 2,3 km de onde foi estacionado" | crítico |
| "Chegou em …" / "Saiu de …" (por lugar; vence em 30 min) | info |
| "Parou no Posto X por 6 min. Abasteceu? Toque para registrar" | info |
| "Embreagem patinando (3 vezes em 7 dias)" | aviso/crítico |
| "Novo recorde 0–100: 10,8 s" | info |
| "GPS funcionando ✅" (primeiro sinal da vida) | info |

---

## 9. As 24 funcionalidades

Convenções desta seção:

- "Posição boa": `mode ≥ 2`, `hdop ≤ 5`, `sats ≥ 5`. "Posição precisa": `mode = 3`, `hdop ≤ 2`, `sats ≥ 7`.
- Sinais do OBD disponíveis neste carro (`docs/pids-civic-2008.md`): rotação, velocidade (inteira, km/h), MAP, MAF, carga, borboleta e pedal, ajuste curto (`stft1`), λ medido e comandado, avanço, temperaturas, **pressão barométrica (PID 33)** e tempo desde a partida (PID 1F). **Não há** ajuste longo (07), nível de combustível (2F) nem % de etanol (52).
- Todo limiar é valor inicial, a recalibrar com 2 semanas de dados reais (mesma regra da borboleta). Ficam em constantes no topo de cada módulo.
- Casos de borda comuns a todas: **túnel ou sinal ruim** (as posições ruins não entram; o trecho vira `gap` e a distância é completada pelo OBD); **Pi reinicia no meio da viagem** (a viagem é retomada pelo critério atual do PID 1F; as posições ficam no banco com o mesmo `trip_id` e o fechamento refaz tudo a partir do banco, sem depender de memória); **sem GPS** (a funcionalidade não aparece ou mostra "sem dados do GPS").

### 9.1 Diário de bordo automático [alta] — F2

**Entradas:** `gps_fix`, `fast_sample`, `trip`, `place`, `geo.db` (quando existir), `fuel_fill` (preço).

**Algoritmo (`gps/trip.py`, função `finalize(con, tid, ctx)`, primeiro item de `FINALIZERS`):**

1. Lê as posições boas entre `start_ts` e `end_ts`. Menos de 10: grava `trip_gps` só com `n_fix` e `coverage = 0` e termina (viagem sem trajeto).
2. **Origem.** Primeira posição boa. Se ela chegou mais de 20 s depois do início da viagem, ou se há posição de estacionamento registrada (9.7) a menos de `max(150 m, distância explicada)` dela, a origem é a posição de estacionamento (`start_src = 'estacionado'`). "Distância explicada" = km pelo OBD desde o início da viagem até essa posição.
3. **Destino.** Última posição boa. Fim normal de viagem é corte de energia: o banco tem posição até no máximo 5 s antes.
4. **Nomes** (`gps/geocode.py`, `name_of(con, geo, lat, lon) -> tuple[str, int | None]`): lugar salvo cujo raio contém o ponto → nome do lugar; senão `geo.db` → "Rua X, Bairro" (via nomeada a até 60 m; bairro mais próximo a até 3 km; cidade a até 30 km, acrescentada só se diferente da cidade de "Casa"); senão coordenada formatada ("23,5501° S, 46,6331° O"). O resultado fica em `start_name`/`end_name`: renomear um lugar depois não reescreve o histórico, mas `obdctl gps reanalyze` reescreve.
5. **Paradas** (`stops_json`): intervalos com velocidade < 3 km/h por ≥ 60 s, com duração, posição e lugar.
6. **Altitude:** mediana móvel de 9 s; `alt_min`, `alt_max`; `climb_m`/`descent_m` somando só variações acima de 3 m (histerese contra ruído).
7. **Custo:** `cost = trip.fuel_l × preço por litro vigente` (último `fuel_fill.price_l` antes do fim da viagem), `cost_km = cost ÷ distância`.
8. **Trajeto:** `build_track` (seção 4.5).
9. **Lugares automáticos:** um destino sem lugar salvo que se repete (3 fins de viagem a menos de 80 m, em dias diferentes) vira `place` com `kind = 'auto'` e nome provisório "Lugar frequente N"; a interface pede um nome.

**Resumo no celular:** `GpsHub.summary(tid)` devolve `{"route": "Casa → Trabalho", "parts": ["nota 86", "R$ 0,52/km"]}` e o `trip_summary_push` que já existe monta uma única notificação (seção 6). Não há segunda notificação.

**Bordas:** viagem dentro do mesmo lugar (manobra na garagem): origem = destino, aparece como "Casa (manobra)". Viagem oculta por ser curta (`hidden = 2`) também recebe `trip_gps`, mas não aparece no diário. Viagem só em túnel/garagem: sem trajeto. Hora aproximada (`clock_synced = 0`): o diário mostra o selo que a lista de viagens já usa.

**Aceite:** (1) com a viagem sintética `commute()` gravada no banco, `finalize` produz origem "Casa", destino "Trabalho", distância dentro de ±1 % da verdadeira e 6 paradas; (2) `/api/gps/logbook` devolve a viagem com custo e notas; (3) uma viagem sem nenhuma posição não gera erro e mantém a distância pelo OBD igual à de hoje; (4) o resumo do ntfy sai uma vez só, com a rota no título. **[HW]** nomes reais de ruas dependem do `geo.db` da região.

### 9.2 Relógio do Pi acertado pelo GPS [média] — F1

Projeto completo na seção 3. É pré-requisito das demais.

**Aceite:** (1) teste: sessão iniciada com relógio 3 dias atrasado, posições com hora certa → todos os `ts` da sessão (inclusive `gps_fix` e `geo_event`) corrigidos, `session.clock_synced = 1`, `clock_source = 'gps'`; (2) teste: com `system_synced()` verdadeiro desde o início nada é deslocado; (3) teste: hora do GPS fora de 2026–2040 é ignorada. **[HW]** (4) com o Pi sem internet, `chronyc tracking` mostra `Reference ID … (GPS)` em menos de 2 min depois do boot com céu aberto; (5) `/api/system` mostra `clock_synced: true` e `clock_source: "gps"`; (6) o ntfy e a nuvem conectam (TLS) logo no boot graças ao `obd-clock`.

### 9.3 Consumo real por viagem e por rota [média] — F2 (distância) e F4 (rotas)

**Distância híbrida** (`trip.hybrid_distance(fixes, fast_rows, k) -> dict`), usada por `GpsHub.trip_distance`:

- Linha do tempo de 1 s do início ao fim da viagem. Em cada segundo, a velocidade é: a do GPS, se há posição boa a até 1,5 s (abaixo de 1,5 km/h conta como 0; se o OBD diz 0, é 0); senão a do OBD × `k` (fator do velocímetro, 9.11; 1,0 enquanto não aprendido) com amostra a até 2,5 s; senão a última conhecida por no máximo 5 s; senão 0.
- `km = Σ v ÷ 3600`. Usa a velocidade Doppler, não a soma de distâncias entre pontos (parado, o ruído de posição inventaria quilômetros).
- `coverage` = segundos com GPS ÷ segundos em movimento. `gps_km` = parte integrada com GPS. `obd_km` = a conta antiga, intacta.
- `trip.distance_km` recebe `km` se `coverage ≥ 0,5`; senão recebe `obd_km × k`. Com `k = 1` e sem GPS o resultado é idêntico ao de hoje.

Como `km_l`, o custo por km dos tanques (`fuel_fill.km_since_prev`), o hodômetro da manutenção e os relatórios leem `trip.distance_km`, todos melhoram sem outras mudanças. O combustível continua vindo do MAF calibrado tanque a tanque.

**Rotas** (`gps/routes.py`, finalizador):

```python
def signature(track: dict) -> list[int]          # células de 0,002° (~220 m) visitadas, amostrando o trajeto a cada 100 m, sem repetir
def similarity(a: list[int], b: list[int]) -> float     # Jaccard
def assign_trip(con, tid: int, ctx=None) -> int | None      # finalizador; devolve route_id
def stats(con, route_id: int) -> dict
```

- Só viagens com `gps_km ≥ 1` e `coverage ≥ 0,7`.
- Chave de ponta: `p<id>` se cai num lugar salvo, senão `c<célula de 0,003°>`.
- Candidatas: rotas com as mesmas chaves de início e fim. É a mesma rota se Jaccard ≥ 0,6 e a distância difere menos de 20 %. Senão cria rota nova, com nome "Origem → Destino" (e "via 2", "via 3" para variantes).
- `stats_json`: n, medianas de km, duração, km/L, L/100 km, R$/km e custo; melhor e pior viagem; por combustível (mistura ≤ 0,25 gasolina, ≥ 0,75 etanol); por faixa de horário de saída (usado em 9.19). `profile_json`: tempo mediano acumulado a cada 250 m (usado em 9.18).
- Ida e volta são rotas diferentes (chaves invertidas).

**Bordas:** desvio por obra cria variante, não estraga a média da rota principal. Lugar criado depois: `reanalyze` reagrupa. Viagem com parada longa no meio (> 10 min parado com motor ligado) entra na rota mas é marcada `atipica` e fica fora das medianas.

**Aceite:** (1) teste: 5 viagens sintéticas pelo mesmo caminho com ruído de GPS caem numa rota; uma sexta por caminho alternativo vira "via 2"; a volta vira outra rota; (2) teste: com velocímetro simulado 4 % otimista e GPS presente, `distance_km` fica a ±1 % do verdadeiro e `obd_km` a +4 %; (3) teste: viagem com túnel de 90 s tem `coverage < 1` e distância a ±2 %; (4) `/api/gps/routes` mostra km/L e R$/km por rota.

### 9.4 Mapa de calor dos dados do motor [média] — F3

**Por viagem:** `obdMap.track(map, trk, colorBy)` pinta cada pedaço do trajeto pela coluna escolhida de `track_json`. Escala: 5 cores por quantis do próprio trajeto para sinais sem faixa conhecida; faixas fixas para os que têm (ajuste curto: verde dentro de ±10 %, âmbar até ±25 %, vermelho além; λ: 0,97–1,03 verde; temperatura: usa as faixas de `limits.py` entregues em `/api/live.status`). Legenda com mínimo, mediana e máximo. Toque no traçado abre um balão com todos os sinais daquele ponto.

**Agregado:** `GET /api/gps/heat?signal=stft1&days=30` junta os `track_json` do período em células de 0,0005° (~55 m) e devolve `{"cells": [[lat, lon, média, n], …], "min":…, "max":…}`; passa de 5.000 células, dobra o tamanho da célula. Sinais oferecidos (e sincronizados): `stft1`, `load`, `kml`, `speed`, `ect`, `map`.

**Ladrilhos:** `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, atribuição "© OpenStreetMap" sempre visível, sem pré-carregar nem baixar em lote (política de uso do OSM; o volume de um painel pessoal é aceito). No painel do Pi o celular precisa de internet própria para os ladrilhos (pelo hotspot ele tem); sem internet o traçado aparece sobre fundo liso com escala.

**Aceite:** (1) teste do `build_track`: 3.600 posições viram ≤ 800 pontos, erro máximo de posição ≤ 6 m (ou a tolerância final), colunas alinhadas e do mesmo tamanho; (2) teste de `/api/gps/heat` com duas viagens sobrepostas: a célula comum tem `n` somado e média ponderada; (3) manual, no navegador com o banco simulado: trocar "Colorir por" repinta sem nova requisição; sem rede, o traçado continua visível.

### 9.5 Trecho de referência antes e depois do reparo [média] — F4

**Definição pelo dono:** na aba Mapa → Trechos, abre uma viagem, toca no ponto de início e no de fim sobre o traçado e dá um nome. `POST /api/gps/segments {name, trip_id, t_a, t_b}` grava os dois portões (posição, rumo no início, raio de 35 m), o comprimento pelo caminho e a duração de referência, e dispara a busca no histórico.

**Passagens** (`gps/segments.py`):

```python
def find_passes(seg: dict, fixes: list) -> list[tuple[int, int]]        # pares (ts_a, ts_b)
def measure(con, seg: dict, ts_a: int, ts_b: int) -> dict                # linha de segment_pass
def scan_trip(con, tid: int, ctx=None) -> int                            # finalizador: todos os trechos ativos
def rescan(con, seg_id: int) -> int                                      # histórico inteiro de um trecho
def history(con, seg_id: int) -> dict                                    # passagens + anotações + regressão
def impact_for(con, ts: int, window_days: int = 30) -> list[dict]        # para extras.impact()
```

- Entrada no portão A: ponto de maior aproximação dentro do raio, com rumo a até 60° do rumo de referência. Saída: maior aproximação de B dentro de `3 × ref_dur_s`, com comprimento percorrido a ±15 % do de referência. O instante é interpolado entre as duas posições vizinhas.
- Medidas no intervalo: duração, km, velocidade média, número de paradas, litros, L/100 km, médias de `stft1`, erro de λ (`o2c1_lambda − lambda_cmd`, ignorando λ > 1,9, que é corte), MAP, carga, borboleta, rotação, avanço, razão ar medido ÷ ar esperado (`tripdiag.expected_air_gs`), temperaturas, pressão, ganho de altitude e mistura no tanque.
- **Validade** (`valid`, com o motivo em `why`): motor quente (ECT ≥ 75 °C na entrada), sem parada acima de 5 s (a menos que a referência tenha), velocidade média a ±20 % da mediana do trecho, GPS bom o tempo todo.
- **Normalização:** só passagens válidas se comparam. Para consumo, regressão linear de L/100 km contra velocidade média sobre as passagens válidas do trecho; o gráfico mostra o resíduo ("gastou 6 % menos que o esperado para essa velocidade"). Sinais pouco sensíveis à velocidade (ajuste curto, erro de λ, ar medido ÷ esperado) vão direto. Passagens com mistura de combustível muito diferente (|Δ| > 0,3) são marcadas no gráfico.

**Histórico e reparos:** `history()` devolve as passagens em ordem e as anotações `kind = 'manutencao'` do período; o gráfico (o `lineChart` do `app.js`) ganha as linhas verticais dos serviços. Para cada serviço, compara a mediana das até 5 passagens válidas anteriores com a das até 5 seguintes, com os mesmos mínimos de mudança de `extras.IMPACT` (4 pontos de ajuste curto, 6 % de ar, 0,7 km/L).

**Integração com o "antes e depois" que já existe:** `extras.impact()` chama `segments.impact_for(con, ts)` dentro de `try/except` e acrescenta `"trechos": [{"trecho": "Trecho 1", "antes": …, "depois": …, "veredito": …}]` a cada serviço. É a mesma tela, com uma evidência mais forte: mesmo lugar, mesma subida, mesma velocidade. Uso imediato: comparar o ajuste curto no trecho de referência antes e depois dos bicos novos.

**Bordas:** trecho percorrido no sentido contrário não conta (rumo). Trânsito parado no trecho: passagem inválida, aparece cinza. Apagar um trecho apaga as passagens.

**Aceite:** (1) teste: 6 viagens sintéticas passam pelo trecho, 3 "com vazamento" (ajuste curto +30 %) e 3 "consertadas" (+4 %), com anotação de manutenção no meio → `impact_for` devolve "melhorou"; (2) teste: passagem no sentido contrário e passagem com parada de 40 s não entram como válidas; (3) teste: `rescan` é idempotente (`UNIQUE(segment_id, ts_start)`).

### 9.6 Detector de marcha e embreagem patinando [média] — F5

**Aprendizado das relações** (`gps/gears.py`, motor ao vivo `GearEngine` + persistência em `gps_model['gears']`):

- Amostra válida: posição precisa, velocidade do GPS ≥ 8 km/h, rotação ≥ 900, razão `r = v ÷ (rpm ÷ 1000)` estável (variação < 2 % em 3 leituras seguidas de rotação, ~1,5 s). A velocidade do GPS é interpolada para o instante da leitura da rotação (`value_ts["rpm"]`), usando o anel de 5 Hz; por isso o motor trabalha com 1 s de atraso.
- Histograma de `ln r` em caixas de 1 %. Com ≥ 600 amostras, k-médias de 5 centros partindo da tabela de fábrica já no código (`GEAR_KMH_PER_KRPM`: 8,83 / 14,84 / 22,46 / 29,26 / 38,16). Centro = mediana dos membros a ±6 %. Marcha "aprendida" com ≥ 60 amostras.
- Sanidade: centro aprendido a mais de 10 % do de fábrica é descartado e registrado ("relação ou pneu fora do esperado").
- `Collector.gear()` passa a usar `self.gps.gear_table()` (aprendida, senão a de fábrica) e `best_speed()`. A tolerância de 8 % e o "0 = ponto morto/embreagem" continuam.

**Patinação:**

- Condição: marcha `g ≥ 3` reconhecida no último regime estável, velocidade ≥ 30 km/h, rotação ≥ 2.500, carga ≥ 70 % ou pedal normalizado ≥ 0,6.
- `s = rpm_medida ÷ rpm_esperada − 1`, com `rpm_esperada = v_gps ÷ k_g × 1000`.
- Evento se `4 % ≤ s ≤ 22 %` por ≥ 1,5 s (≥ 3 leituras) **e**, ao aliviar o pedal, a razão volta para a mesma marcha `g` (não houve troca).
- Exclusões: redução de marcha (a razão cai para a marcha vizinha: de 5ª para 4ª são 30 %, fora da faixa); embreagem pisada (rotação dispara acima de 22 % ou cai para a lenta); arrancada e patinação de roda (por isso `g ≥ 3`); GPS impreciso; primeiros 2 s depois de uma troca.
- Saída: `geo_event` `kind = 'patinacao'` (valor = `s` máximo) e `Alert("gps", "gps:clutch_slip", "warn", "Embreagem patinando", …)`. Três eventos em 7 dias sobem para crítico. O `handle_alerts` do coletor já agrupa repetições.

**Bordas:** subida forte em marcha alta com giro baixo não é patinação (exige rotação ≥ 2.500). Pneus trocados: as relações derivam devagar (média exponencial com meia-vida de 2.000 amostras) e a mudança aparece em 9.11.

**Aceite:** (1) teste: viagem sintética de 20 min → 5 relações a ±1,5 % das verdadeiras; (2) teste: cenário `slip()` (4ª, rotação 8 % acima por 3 s) gera 1 evento; reduções e trocas normais geram 0; (3) teste: sem GPS, `gear()` devolve o mesmo que hoje. **[HW]** relações reais do carro com o pneu atual.

### 9.7 Cerca virtual e alerta de movimento [média] — F2 (base) e F9 (modo sempre ligado)

Fato confirmado na documentação (`PROCESSO.md`, "Pi mais resistente"): **o Pi perde a energia a cada desligada**. Com o carro parado não há quem vigie. O projeto entrega o que é possível assim, e deixa pronto o modo sempre ligado.

**Posição de estacionamento** (`gps/places.py`, `ParkEngine`): a cada posição boa com velocidade < 3 km/h atualiza `gps_model['parked'] = {lat, lon, ts, trip_id, valid}`; `valid` vira verdadeiro com 3 posições paradas seguidas e falso quando o carro anda. Fica gravado a cada `commit`. No corte de energia o que sobra é a posição real de onde o carro parou. `GET /api/gps/parked` devolve isso com o nome do lugar e o link `https://maps.apple.com/?ll=<lat>,<lon>&q=Civic` (abre o Mapas do iPhone). É o "onde estacionei"; na nuvem funciona com o Pi desligado, porque o documento foi sincronizado segundos antes do corte.

**Mudou de lugar desligado** (no boot seguinte): na primeira posição precisa (depois de 5 seguidas), compara com `parked` se `valid`:

```
explicado_m = km pelo OBD desde o início da viagem × 1000 + (tempo desde a partida pelo PID 1F, se já lido) × 14 + 150
se distância(posição, parked) > max(gps.moved_m, explicado_m): geo_event 'moveu_desligado' + alerta crítico "gps:moved_off"
```

O termo do PID 1F cobre o caso "dei a partida e saí antes de o Pi subir" (14 m/s ≈ 50 km/h). Não avalia se a viagem atual é retomada (`merged`) nem se `parked.valid` é falso (o Pi caiu com o carro andando). Texto do aviso: "O carro está a 2,3 km de onde foi estacionado (guincho, manobra de terceiros ou furto?)" com o link do mapa.

**Cerca (entrar e sair de lugares)** (`GeofenceEngine`): entra quando fica dentro do raio por 3 posições; sai quando fica além do raio + 30 m por 5 posições. Grava `geo_event` (`entrou`/`saiu`), atualiza `place.visits` e dispara as ações do lugar (9.16). Os lugares são relidos do banco a cada 30 s e no comando `reload_places`.

**Modo sempre ligado (opcional, exige hardware):** alimentação permanente com corte por temporizador ou sensor de ignição (seção 6.3 da `ARQUITETURA.md`) e internet própria (em casa o WiFi alcança; na rua precisaria de 4G). Com `power.always_on = true`:

- o `ecu_silent_loop` já roda com a ignição desligada e, com a mudança da seção 6, segue bombeando o GPS; a gravação cai para 1 posição por minuto;
- `ParkGuard`: com o motor desligado, deslocamento > 60 m da posição de estacionamento **ou** velocidade > 8 km/h por 10 s → alerta crítico imediato e posição enviada à nuvem a cada 5 s;
- proteção da bateria: tensão (`ATRV`) < `power.min_batt_v` por 10 min → aviso e `systemctl poweroff` (por regra de sudo restrita, criada pelo `install.sh` só quando o modo está ligado). O Pi 3 consome ~0,15 A em 12 V: uns 3,6 Ah por dia; uma bateria de 45 Ah não aguenta uma semana parada.

**Aceite:** (1) teste: cenário `towed()` (boot a 2 km do estacionamento, motor recém-ligado) gera o alerta; boot no mesmo lugar não gera; "saiu antes do Pi subir" (PID 1F = 120 s, 900 m) não gera; (2) teste: entrar e sair de um lugar com ruído de 5 m na borda gera exatamente 1 `entrou` e 1 `saiu`; (3) `/api/gps/parked` devolve a última posição parada depois de uma viagem simulada. O modo sempre ligado fica implementado atrás da configuração e testado só com simulador **[HW + elétrica]**.

### 9.8 Cronômetro de 0–100 e retomadas [média] — F6

**Receptor a 5 Hz:** `reader.request_rate(gps.rate_hz)` a cada conexão (seção 2). Se o intervalo medido não cair para ~200 ms em 5 s, fica em 1 Hz e as corridas saem marcadas `hz = 1` (precisão ±0,2 s em vez de ±0,05 s). 10 Hz é possível no u-blox 7, mas dobra o tráfego sem ganho prático aqui; fica como opção de configuração **[HW]**.

**Detecção** (`gps/perf.py`, `PerfDetector`, sobre o anel de 5 Hz, com a hora do receptor):

- **Arrancada:** parado (< 2 km/h por ≥ 1 s) e depois ≥ 5 km/h. `t0` = instante interpolado em que a velocidade passou de 1 km/h. Parciais em 60, 80 e 100 km/h por interpolação linear. Termina ao chegar a 100, ao cair mais de 3 km/h, ou em 40 s.
- **Retomada:** pedal normalizado ≥ 0,8 mantido, mesma marcha do começo ao fim, cruzando um dos pares 40→80, 60→100 ou 80→120 km/h em até 30 s. O tipo leva a marcha: "80–120 em 4ª".
- **Validade:** posições precisas o tempo todo; inclinação média do percurso (altitude suavizada no início e no fim ÷ distância) com módulo ≤ 2 %, senão vale mas sai marcada "em subida"/"em descida" e não disputa recorde.
- **Automática ou armada:** corridas não armadas só são gravadas se foram de propósito (pedal ≥ 0,8 em algum momento **e** 60 km/h em até 12 s ou 100 em até 25 s). Armadas (botão que já existe) são sempre gravadas, mesmo incompletas.
- Ao suspeitar de arrancada ou retomada, liga a **rajada**: os 5 s anteriores e a corrida inteira vão para `gps_fix` a 5 Hz (`flags` bit 0), o que permite refazer a conta depois.

**Gravação:** na tabela `accel_run` que já existe, com `source = 'gps'`, `kind` (`'0-100'`, `'60-100'`, `'80-120'`…), `t_s`, `t60`/`t80`/`t100`, `v0`, `v1`, `gear`, `dist_m`, `slope_pct`, `hz`, `auto`, `fuel_mix` e `json` (curva velocidade × tempo). As corridas antigas ficam com `source` nulo (OBD). `/api/accel` devolve tudo; `/api/gps/perf` agrupa recordes por tipo e combustível.

**Integração com o teste armado atual:** com GPS bom, `accel_feed` delega ao `PerfDetector` e só espelha o estado em `self.accel` (`aguardando_parar`, `pronto`, `correndo`, `concluido`, `splits`, `elapsed`), então a tela do modo direção não muda. Sem GPS, o caminho pelo OBD segue como está.

**Voz:** `live.gps.perf = {"state": "concluido", "kind": "0-100", "t_s": 10.8, "record": true}`; o `drive.html` fala uma vez por corrida (chave = `ts` da corrida).

**Aceite:** (1) teste: cenário `launch()` a 5 Hz com 0–100 verdadeiro de 10,40 s → medido 10,40 ± 0,08 s; a 1 Hz, ± 0,25 s; (2) teste: aceleração leve até 100 km/h em 30 s não é gravada como automática; (3) teste: arrancada em rampa de 4 % sai marcada e não vira recorde; (4) teste: retomada com troca de marcha no meio é descartada. **[HW]** conferir a taxa real e comparar uma corrida com um aplicativo de GPS do celular.

### 9.9 Dinamômetro de rua [média] — F6

**Modelo** (`gps/dyno.py`):

```
F = m·δ_g·a + m·g·sen(θ) + m·g·Crr·cos(θ) + ½·ρ·CdA·v²
P_roda = F·v          P_motor = P_roda ÷ η          T = P_motor ÷ ω
```

- `m` = `vehicle.mass_kg` (padrão 1.350: carro ~1.240 kg + motorista + combustível; ajustável no painel, com campo "passageiros").
- `δ_g` (inércia das partes que giram) por marcha: {1: 1,25; 2: 1,12; 3: 1,08; 4: 1,06; 5: 1,05}.
- `a`: derivada da velocidade do GPS a 5 Hz por diferença central em ±0,6 s, depois mediana de 3.
- `θ`: inclinação pela altitude suavizada (9.12). Com módulo acima de 3 % a puxada vale, mas sai marcada.
- `ρ = baro × 1000 ÷ (287,05 × (T_amb + 273,15))`, com `baro` do PID 33 e `T_amb` = menor temperatura do ar admitido da viagem em movimento (o sensor aquece parado).
- `CdA`, `Crr`: padrão 0,63 m² e 0,011; substituídos pela calibração abaixo. `η` = 0,88.
- Rotação de cada ponto: `v ÷ k_g × 1000` (relação aprendida em 9.6).

**Puxada válida** (detectada no fechamento da viagem sobre as rajadas, e ao vivo para a tela): mesma marcha (2ª, 3ª ou 4ª), pedal ≥ 0,85 por ≥ 3 s, faixa de rotação ≥ 1.500 rpm, posições precisas, motor quente. O `PerfDetector` liga a rajada sempre que o pedal passa de 0,85 acima de 2.000 rpm.

**Curva:** potência e torque por caixas de 250 rpm (mediana), `peak_kw`, `peak_nm` e as rotações dos picos; também em cv e kgfm. Fator de correção para condição padrão `corr = (101,3 ÷ baro) × √((T_ar + 273) ÷ 293)`, limitado a 0,90–1,25; a tela mostra medido e corrigido. Referência do R18A flex: ~140 cv a 6.300 rpm e ~17,7 kgfm; resultado fora de 60–160 cv corrigidos é marcado como suspeito, não é apagado.

**Calibração por descida em ponto morto (coastdown):** assistente no painel (`POST /api/gps/dyno/coast`): em reta plana, chegar a ~90 km/h, ponto morto, deixar cair até ~40 km/h; repetir no sentido contrário. Detecção: razão rotação/velocidade fora de qualquer marcha e rotação perto da lenta. Ajuste por mínimos quadrados de `−m·δ0·dv/dt = A + B·v²` (2 parâmetros, fórmula fechada, sem numpy), com `δ0 = 1,03`: `Crr = A ÷ (m·g)`, `CdA = 2B ÷ ρ`. A média dos dois sentidos cancela inclinação e vento. Aceita só `CdA` entre 0,45 e 0,90 e `Crr` entre 0,007 e 0,020; grava em `gps_model['coast']` com data, e a tela mostra o antes e o depois.

**Honestidade na tela:** é uma estimativa (±8–10 % sem calibração, ±5 % com). O valor está em **comparar o carro com ele mesmo**: mesma marcha, mesmo trecho, antes e depois de um reparo, etanol contra gasolina.

**Aceite:** (1) teste: o simulador gera uma puxada em 3ª com curva de torque conhecida → pico de potência recuperado a ±3 % e rotação do pico a ±250 rpm; (2) teste: mesma puxada em rampa de +4 %, com a altitude informada, fica a ±5 %; (3) teste: coastdown sintético com `CdA = 0,70` e `Crr = 0,012` → recuperados a ±5 %; (4) puxada com troca de marcha é rejeitada. **[HW]** valores reais; a massa informada pesa diretamente no resultado.

### 9.10 Nota de condução por viagem [média] — F5 (limites de velocidade entram na F7)

A nota de direção econômica (`eco_score`, pelo OBD) continua. A nota de condução mede segurança e suavidade, pelo GPS. Calculada no fechamento (`gps/score.py`, `drive_score(fixes, fast_rows, limits=None) -> tuple[int | None, dict]`), sobre as posições de 1 Hz:

| Componente | Critério | Desconto |
|---|---|---|
| Freada forte | aceleração longitudinal ≤ −11 km/h por segundo | 5 por evento a cada 10 km, teto 25 |
| Arrancada forte | ≥ +9 km/h por segundo | 4 por evento a cada 10 km, teto 20 |
| Curva forte | aceleração lateral `v·ω` > 3,5 m/s² com v ≥ 20 km/h (`ω` = variação do rumo, mediana de 3) | 4 por evento a cada 10 km, teto 20 |
| Excesso de velocidade | % do tempo em movimento acima de `limite × 1,1 + 3` (limite do `geo.db`; sem ele, acima de 115 km/h) | 0,5 por ponto percentual, teto 20 |
| Motor frio forçado | segundos com ECT < 60 °C e (rotação > 3.000 ou carga > 80 %) | 1 a cada 12 s, teto 10 |
| Giro no limite | segundos acima de 5.500 rpm | 1 a cada 5 s, teto 5 |

Nota = 100 − descontos. Só para viagens com ≥ 2 km, ≥ 3 min e `coverage ≥ 0,7`. Cada evento vira `geo_event` (`freada`, `arrancada`, `curva`) com posição e valor, e aparece como alfinete no mapa da viagem. `trip.drive_score` (número) e `trip_gps.drive_json` (detalhe e dicas no mesmo estilo de `tripdiag.eco_score`).

**Bordas:** lombada e valeta geram desaceleração curta; por isso o evento exige a variação em 1 s inteiro, não um pico. Ruído de rumo em baixa velocidade: curva só conta acima de 20 km/h.

**Aceite:** (1) teste: viagem sintética calma → nota ≥ 90; a mesma com 4 freadas de −14 km/h/s e 3 curvas a 4,5 m/s² em 10 km → entre 55 e 70, com os 7 eventos gravados nas posições certas (±30 m); (2) teste: viagem de 1 km → nota nula; (3) o resumo do ntfy inclui "nota N".

### 9.11 Aferição do velocímetro e dos pneus [baixa] — F5

**Entradas:** pares (velocidade do OBD, velocidade do GPS) em regime (|aceleração| < 0,3 m/s², GPS ≥ 20 km/h, posição precisa).

**Algoritmo** (`gps/speedo.py`, `SpeedoEngine` ao vivo + `gps_model['speedo']`): acumula por faixa de 10 km/h do OBD a mediana da velocidade do GPS; com ≥ 5 faixas de ≥ 60 pares cada, ajusta `v_gps = a·v_obd + b` por mínimos quadrados sobre as medianas (peso igual por faixa, para a cidade não dominar a estrada). `k = a` é o fator usado na distância híbrida (9.3) e em `best_speed()`. O PID 0D é inteiro; o `b` absorve o arredondamento.

**O que mostra:** "A central informa 100 km/h quando o carro está a 97,2 (−2,8 %)", tabela por faixa e a observação de que o **ponteiro do painel** costuma marcar ainda mais que a central (a central é o que o OBD lê). Campo opcional para o dono digitar o que o ponteiro marcava num instante de regime.

**Pneus:** as relações aprendidas das marchas (9.6) dependem só da circunferência real do pneu. Um resumo mensal de `k_5ª` fica em `gps_model['speedo_hist']`; queda de 1 % ou mais em relação ao primeiro mês vira informação ("pneus mais gastos ou com menos pressão: −1,3 % de circunferência"), e um salto para cima indica pneus novos.

**Hodômetro:** quando o dono informa o hodômetro de novo (`/api/maintenance/odometer`), a resposta ganha a diferença entre o estimado e o informado.

**Aceite:** (1) teste: simulador com central 4 % otimista → `k = 0,960 ± 0,005`; (2) teste: menos de 5 faixas → "ainda aprendendo", `k = 1`; (3) teste: a distância de uma viagem sem GPS usa `k`.

### 9.12 Inclinação e altitude × carga e ajuste — F6

**Inclinação** (`gps/terrain.py`, `slope_series(fixes) -> list[float | None]`): altitude com mediana móvel de 9 s; inclinação = regressão linear de altitude contra distância percorrida na janela dos últimos 150 m (mínimo 8 posições), limitada a ±20 %; nula abaixo de 20 km/h ou com posição imprecisa. Vai para a coluna `slope` do trajeto e para `live.gps.slope`.

**Análise** (finalizador → `trip_gps.terrain_json`; agregado em `/api/gps/terrain?days=`): por faixa de inclinação (≤ −6, −6 a −3, −3 a −1, −1 a +1, +1 a +3, +3 a +6, > +6 %) e por faixa de 200 m de altitude: segundos, médias de carga, MAP, borboleta, ajuste curto, erro de λ, avanço, L/100 km e razão ar medido ÷ esperado.

**Leitura automática** (texto simples no painel, sem IA): se o ajuste curto em descida ou pé leve (MAP baixo) é pelo menos 8 pontos maior que em subida forte, a mensagem é "a mistura empobrece quando o vácuo é alto: assinatura de entrada de ar". É a mesma lógica do `air_check`, agora com a estrada como bancada. Este carro não tem ajuste longo; tudo usa o curto.

**Aceite:** (1) teste: cenário `hill()` (2 km a +6 % e 2 km a −6 %) → inclinação estimada +6 ± 1 % e −6 ± 1 % nos trechos de regime, com ruído de altitude de 4 m; (2) teste: viagem sintética "com vazamento" produz a mensagem; a saudável não. **[HW]** o ruído real de altitude do VK-172 pode exigir janela maior que 150 m.

### 9.13 Manutenção por km real — F2

O plano de manutenção (`extras.maintenance`) soma `trip.distance_km`, que passa a ser a distância híbrida: nada a fazer além de 9.3. Acréscimos:

- **Km sem OBD:** com o carro andando (GPS > 10 km/h por 30 s) e sem viagem aberta (adaptador fora, Bluetooth caído), o `GpsHub` acumula a distância e grava um `geo_event` `kind = 'km_sem_obd'` por sessão (valor em km, atualizado a cada minuto). `extras.odometer()` soma esses eventos desde `odo_base_ts`.
- A tela mostra a composição: "informado 182.000 + 1.240 km pelo GPS + 12 km pelo velocímetro + 3 km sem OBD".

**Aceite:** teste: 2 viagens com GPS (10,0 km verdadeiros cada, central 4 % otimista) + 1 evento `km_sem_obd` de 3 km → hodômetro = base + 23,0 ± 0,2.

### 9.14 Detector de abastecimento e ranking de postos — F8

O PID 2F (nível) não existe neste carro. A detecção é por comportamento, e o registro continua sendo o botão "Tanque cheio" (só o valor pago).

**Parada suspeita** (`gps/fuelstop.py`, `detect(con, tid, ctx)`, chamada no início de cada viagem): a viagem anterior terminou a menos de 80 m de onde esta começou, o intervalo ficou entre 2 e 25 min (com as duas sessões de relógio sincronizado) e o ponto está a até 60 m de um posto (`place` de `kind = 'posto'` ou `poi` do `geo.db`). Grava `fuel_stop` e manda o aviso "Parou no Posto X por 6 min. Abasteceu? Toque para registrar" (abre `/#consumo`). Vence em 6 h (`outbox.expire_ts`).

**Ligação com o abastecimento:** o cartão "Abastecimento detectado" na aba Consumo pede só o valor e envia `POST /api/fuel/fill {total, ts: <hora da parada>, stop_id}`; `fuel_fill()` grava `place_id` e marca a parada como confirmada. No sentido contrário: abastecimento registrado pelo botão normal procura parada suspeita a até 30 min, ou a posição daquele instante, e associa o posto (criando o `place` com o nome do `geo.db`, ou "Posto N").

**Ranking** (`stations(con) -> list[dict]`, `/api/gps/stations`): por posto: vezes, preço médio por combustível, último preço, e o **rendimento do combustível comprado ali**: o km/L real medido no abastecimento seguinte pertence ao tanque anterior (mesma regra de `extras.kml_by_fuel`), comparado com a média do dono para o mesmo combustível. Também a variação do ajuste curto nos 20 km depois de abastecer contra os 20 km antes (combustível ruim muda a correção). Precisa de 2 tanques cheios por posto para opinar.

Se um dia houver PID 2F (outro carro), `detect` usa a subida do nível como confirmação; fica um ramo guardado por `"fuel_level" in values`.

**Bordas:** parada em loja de conveniência sem abastecer: o dono descarta com um toque (`POST /api/gps/fuelstop/{id} {status: "descartado"}`). Abastecer com o motor ligado não gera duas viagens; nesse caso vale o registro manual, que associa o posto pela posição.

**Aceite:** (1) teste: cenário `refuel()` gera 1 `fuel_stop` e 1 aviso; intervalo de 40 min ou parada fora de posto não gera; (2) teste: confirmar grava `fuel_fill.place_id` e a parada vira `confirmado`; (3) teste: ranking com 3 postos e 6 tanques atribui o rendimento ao posto certo.

### 9.15 Linha do tempo do problema no mapa — F3

`GET /api/gps/alerts?days=90&rule=` devolve cada alerta (e cada `dtc_event`) com a posição gravada mais próxima (até 10 s), velocidade, altitude e inclinação naquele ponto:

```sql
SELECT a.*, (SELECT ts FROM gps_fix WHERE ts <= a.ts ORDER BY ts DESC LIMIT 1) AS fix_ts FROM alert a WHERE a.ts > ?
```

(depois busca a linha de `gps_fix` e descarta se `a.ts − fix_ts > 10000`).

Na aba Mapa: alfinetes por severidade, filtro por regra, controle deslizante de tempo que acende os alfinetes em ordem (a "linha do tempo"), e destaque de **pontos repetidos**: 3 ou mais alertas da mesma regra na mesma célula de 100 m ("este alerta acontece sempre nesta subida"). O balão liga para o replay da viagem 8 s antes do alerta. No detalhe da viagem os alertas daquela viagem aparecem sobre o trajeto.

**Aceite:** teste: 3 alertas inseridos em instantes conhecidos de uma viagem sintética voltam com posição a ≤ 15 m da verdadeira; alerta fora de qualquer viagem com GPS volta sem posição e não quebra a lista.

### 9.16 Lugares favoritos com ações — F2 (lugares) e F8 (ações)

`place.actions_json` é uma lista de `{"on": "entrou" | "saiu" | "partiu_apos", "do": …, "arg": …}`. Como o Pi desliga com o carro, "ficar parado no lugar" só é conhecido na partida seguinte; por isso existe o gatilho `partiu_apos` (início de viagem no lugar, com intervalo desde a viagem anterior ≥ `arg` minutos, relógios sincronizados).

| Ação (`do`) | Efeito |
|---|---|
| `avisar` | ntfy "Chegou em Casa" / "Saiu do Trabalho" (vence em 30 min) |
| `falar` | campo `say` em `gps.json` (privado, porque leva o nome do lugar); o modo direção, que já consulta `/api/gps/live` a cada 5 s quando a localização está liberada, fala uma vez |
| `sincronizar` | cria `/run/obd/sync_cmd`; o `obd-sync` faz a sincronização completa na hora (em casa, com WiFi, antes de o motor ser desligado) |
| `pos_servico` | padrão da Oficina em `partiu_apos` 20 min: anotação `kind = 'oficina'` ("Visita à oficina, 3 h 10") e aviso "Saiu da oficina: anote o serviço e rode o teste" abrindo `/#testes`. A anotação é de outro tipo de propósito: só vira serviço no "antes e depois" quando o dono confirmar |
| `nota` | anotação com o texto de `arg` |

Padrões ao criar: Casa → `sincronizar` ao entrar; Oficina → `pos_servico`; Posto → nada (9.14 cuida). A edição é na aba Mapa → Lugares: toque no mapa ou "usar a posição atual", nome, tipo, raio, ações.

**Aceite:** (1) teste: entrar em Casa com a ação `sincronizar` cria o arquivo de comando uma vez; (2) teste: partir da Oficina 3 h depois gera a anotação `oficina` e o aviso; partir 5 min depois não gera; (3) teste: aviso de chegada com mais de 30 min não é enviado (`flush_outbox` marca como vencido).

### 9.17 Aviso de radar e limite de velocidade — F7

**Dados** (`scripts/geo-build.py`, roda no PC, só stdlib: `urllib`, `json`, `sqlite3`):

```
python3 scripts/geo-build.py --bbox S,W,N,E [--inner LAT,LON,KM] --out data/geo/geo.db
```

- Consulta a API Overpass do OpenStreetMap em blocos de 0,25° (com pausa entre blocos e nova tentativa em caso de limite): radares (`highway=speed_camera` e relações `type=enforcement` de velocidade, de onde sai o sentido), vias (`highway` de `motorway` a `residential` e os `_link`) com `name`, `maxspeed`, `oneway`, postos (`amenity=fuel`), oficinas (`shop=car_repair`) e localidades (`place` = bairro, vila, cidade).
- Fora do raio `--inner` guarda só vias principais (até `tertiary`), para o arquivo não crescer.
- Simplifica a geometria (6 m), grava as tabelas da seção 4.3 e `meta` (data, área, contagens, "© OpenStreetMap contributors, ODbL").
- Usar uma área grande (a região metropolitana inteira), não um ponto: o pedido ao Overpass revela a área de interesse.
- `data/` entra no `.gitignore`. O `scripts/deploy.sh` envia `data/geo/geo.db` para `/data/obd/geo/geo.db.tmp` e renomeia. Como `/data` é sempre gravável, atualizar o arquivo não exige desligar a proteção do cartão.

**Via atual** (`gps/roads.py`, `RoadEngine`, 1 vez por segundo): pontos de via nas 9 células em volta; candidato = ponto a até 25 m cujo trecho tem rumo compatível com o do carro (±35°, ou o oposto se a via não é de mão única). Troca de via só depois de 3 leituras seguidas (evita pular para a rua paralela). Saída: `way_id`, nome, classe e limite. Sem `maxspeed` no OSM, limite presumido pela classe (CTB: local 30, coletora 40, arterial 60, trânsito rápido 80, rodovia 100/110), marcado `limit_src = "presumido"`: aparece esmaecido e **não gera fala**.

**Radar à frente:** radares a até 600 m cuja direção a partir do carro está a ±30° do rumo, que pertençam à via atual (ou sem via associada, a até 30 m dela) e, quando o OSM informa o sentido, compatível com o do carro. `live.gps.warn = {"kind": "radar", "dist_m": 380, "limit": 50, "n": 7}`, em que `n` é só a ordem do aviso na sessão (o identificador do radar no OSM não vai para o `live.json`, porque dele se tira a posição). As falas e a faixa na tela são do `drive.html` (seção 8.3): a 500 m e a 150 m, uma vez por aviso (`n`). Também `over` quando a velocidade do GPS passa de `limite × 1,07 + 2` por 5 s.

O cálculo é todo no Pi; o celular só mostra e fala, e funciona igual no painel do Pi e no online (o aviso viaja no `live.json`, que não tem posição). Atraso típico até o celular: < 0,5 s local, ~0,3 s pela nuvem; a 60 km/h isso são menos de 10 m.

É um aviso a partir de mapa público, como os aplicativos de navegação; não detecta sinal de radar. O mapa pode estar desatualizado: a tela mostra a data do `geo.db`, e o `status` avisa quando passa de 6 meses.

**Bordas:** viaduto sobre avenida (duas vias no mesmo ponto): a histerese e o rumo resolvem a maioria; a via errada dura no máximo 3 s. Sem `geo.db`: `limit` ausente, nenhum aviso, e o cartão do Sistema diz "mapa de radares não instalado" com o comando para gerar.

**Aceite:** (1) teste: `geo.db` de fixture (2 vias paralelas a 40 m, 1 radar com sentido) + cenário `highway()` → aviso aparece entre 600 e 450 m antes, some depois de passar, não aparece no sentido contrário nem na via paralela; (2) teste: limite presumido não liga `over`; (3) teste do script com respostas do Overpass gravadas em `tests/fixtures/gps/overpass-*.json` (sem rede) gera as tabelas esperadas; (4) manual: `/dirigir?demo=1` fala o aviso no iPhone depois do primeiro toque.

### 9.18 Previsão de chegada e autonomia — F8

**Destino provável** (`gps/eta.py`, `EtaEngine`, reavalia a cada 15 s): candidatas = rotas que começam na origem atual (lugar ou célula). Pontuação = frequência (peso maior para o mesmo tipo de dia e saída a ±90 min) × fração das células percorridas até agora que pertencem à rota (precisa de ≥ 80 %). Mostra quando a melhor passa de 0,6 de confiança e tem o dobro da segunda.

**Chegada:** `profile_json` da rota dá o tempo mediano acumulado a cada 250 m. `restante = T_total − T(km atual)`, multiplicado pelo ritmo de hoje (tempo decorrido ÷ tempo mediano até aqui, limitado a 0,7–1,6). Vai para `gps.json` (`dest`), não para o `live.json`: o nome do destino é dado de localização.

**Autonomia:** litros no tanque = `vehicle.tank_l` − litros estimados desde o último tanque cheio (só depois de um tanque cheio registrado; senão "—"). `range_km = litros × km/L`, com o km/L mediano da rota prevista para o que falta dela e a média real recente para o resto. `live.gps.range_km` (número, público). Abaixo de 60 km: fala e aviso uma vez por viagem, com o posto mais bem colocado do ranking a até 5 km do trajeto, se houver.

**Aceite:** (1) teste: depois de 8 viagens sintéticas casa→trabalho, na 9ª o destino é previsto antes de 30 % do caminho e o erro da chegada a meio caminho é < 10 %; (2) teste: saindo por caminho desconhecido, nenhuma previsão; (3) teste: autonomia = (50 − 30) × 11 = 220 km com os dados montados.

### 9.19 Análise do trânsito — F8

No fechamento (`gps/traffic.py`, `trip_traffic(fixes, fast_rows) -> dict` → `trip_gps.traffic_json`): segundos parado com motor ligado, segundos "arrastando" (3–15 km/h por janelas de ≥ 20 s), litros nesses dois estados (integrando `fuel_lh`), e o atraso em relação à viagem livre da mesma rota (percentil 10 das durações).

Agregado (`/api/gps/traffic?days=60`), por rota com ≥ 6 viagens: mediana de duração, litros e atraso por faixa de 30 min do horário de saída e por tipo de dia (útil, sábado, domingo). **Melhor horário:** a faixa com menor mediana (≥ 3 viagens) a até 90 min do horário habitual: "Saindo às 7h10 em vez de 7h40 você ganha 9 min e 0,3 L". Camada no mapa "onde eu fico parado": células em que a velocidade mediana é menor que 30 % do percentil 85 daquela célula.

**Aceite:** teste: 12 viagens sintéticas na mesma rota, metade saindo às 7h40 com 10 min a mais de semáforos → melhor horário = 7h10, ganho de 10 ± 2 min; rota com 4 viagens → "poucos dados".

### 9.20 Cidade × estrada — F7

Classificação de cada segundo (`gps/roads.py`, `road_class(...)`): pela via atual do `geo.db` (rodovia = `motorway`/`trunk` ou limite ≥ 80; o resto é cidade); sem `geo.db`, pelo perfil em janelas de 60 s (média ≥ 70 km/h sem parada = estrada; média < 45 ou com parada = cidade; entre os dois = misto).

Por classe (`trip_gps.road_json`; agregado em `/api/gps/roadmix?days=`): km, tempo, km/L e L/100 km, rotação e carga médias, ajuste curto, temperatura, % do tempo por marcha. Tendência mensal do km/L de cidade e de estrada em separado. É a comparação certa para dizer se o consumo piorou ou se só mudou o tipo de uso.

**Aceite:** teste: viagem sintética com 10 km urbanos e 30 km de rodovia → divisão dos km a ±5 % pelos dois métodos; o km/L de cada classe confere com o do simulador.

### 9.21 Compartilhamento ao vivo da posição — F9

**Criar:** aba Mapa → "Compartilhar posição": 15 min, 1 h, 4 h ou "até chegar em <lugar>".

- No painel online (com a localização liberada): `POST /api/gps/share {minutes, label, until_place}` → o Worker gera 16 bytes aleatórios, guarda o SHA-256 em `share` e devolve `{url: "https://obd.cleberson.social.br/s/<token>", expires_ts}`.
- No painel do Pi: o Pi gera o token (`secrets.token_urlsafe(16)`), guarda em `/data/obd/share.json` e o `obd-sync` envia a `POST /api/sync/share` no ciclo seguinte (≤ 10 s). Sem nuvem configurada, responde 400.
- Limites: 5 a 1.440 minutos; no máximo 5 links ativos.

**Ver:** `GET /s/<token>` serve `share.html` (mapa, posição, velocidade, "atualizado há N s", contagem regressiva da validade), com `Referrer-Policy: strict-origin`, `X-Robots-Tag: noindex` e `Cache-Control: no-store`. A página consulta `GET /api/share/<token>` a cada 5 s: o Worker valida (existe, não revogado, não vencido; senão `410`) e devolve **apenas** `{lat, lon, speed, heading, updated_ts, engine_on, expires_ts, label}`, tirado do documento `/api/gps/live`. Sem histórico, sem lugares, sem destino. Carro desligado: última posição, com "carro desligado desde …".

**Revogar e vencer:** lista dos links na aba Mapa com "Encerrar" (`POST /api/gps/share/{id} {revoke: true}`; `id` = 8 primeiros caracteres do hash). Link "até chegar" é revogado pelo Pi quando a cerca registra a entrada no lugar. Ao criar um link, o Worker apaga os vencidos há mais de 7 dias.

**Pré-requisito:** `LOC_PASSWORD` definido (sem ele o Pi nem envia a posição para a nuvem).

**Aceite:** (1) `cloud/test/loc.test.mjs`: `isLoc` e `LOC_OPEN`; cookie de localização válido, inválido e vencido; link válido, vencido e revogado (com um D1 falso em memória); a resposta do link só tem os 8 campos; (2) teste no Pi: criar link sem nuvem → 400; com nuvem → entra na fila de envio; (3) manual, com `wrangler dev`: `/api/gps/logbook` devolve 401 sem cookie e 200 com; `/s/<token>` mostra a posição do documento.

### 9.22 Gravação da viagem para assistir de novo — F3

Estende o replay que já existe (controle deslizante com todos os sensores, em `extras.js`):

- o `gps.js` escuta o evento `obdreplay` e move o cursor no mapa da viagem (posição interpolada no `track_json`, seta com o rumo);
- toque no traçado leva o controle deslizante para aquele instante (`obdMap.nearest` → `sl.value = ts` → `dispatchEvent(new Event("input"))`);
- o pedaço já percorrido fica destacado, com a cor do sinal escolhido;
- seletor de velocidade (×1, ×10, ×60) ao lado de "Reproduzir" (hoje é fixo em ×10: 2 s de viagem a cada 200 ms).

Nada novo no servidor.

**Aceite:** manual com o banco simulado: reproduzir move o cursor em sincronia com os números; tocar no traçado atualiza os números; viagem sem trajeto mostra o replay antigo sem erro.

### 9.23 Altitude × potência — F6

Motor aspirado perde potência com a densidade do ar. Aqui o carro já mostrou ~90 kPa de pressão barométrica (PID 33), isto é, cerca de 11 % menos ar que ao nível do mar.

- **Ao vivo:** `live.gps.avail_pct = 100 × (baro ÷ 101,3) × √(298 ÷ (T_ar + 273))`, o quadro "potência disponível agora".
- **Histórico** (`gps/terrain.py`, `altitude_power(con, days) -> dict`, dentro de `/api/gps/terrain`): por faixa de 200 m de altitude do GPS: pressão média, MAP com o pedal no fundo (≥ 0,85 e rotação ≥ 3.000), MAF máximo por faixa de rotação, pico de potência das puxadas (9.9) medido e corrigido. Gráfico com a linha teórica (cerca de −1 % a cada 100 m).
- Serve de conferência cruzada: se a potência corrigida cai com a altitude mais do que a teoria, há algo além do ar rarefeito (e o MAP no fundo abaixo da pressão ambiente em mais de 8 kPa aponta restrição na admissão).

**Aceite:** teste: puxadas sintéticas a 0 m e a 1.000 m com o mesmo motor → potência medida ~11 % menor a 1.000 m e potência corrigida igual a ±3 %. **[HW]** só terá dados reais em viagens com desnível.

### 9.24 Exportar KML/GPX — F3

`gps/export.py`, em geradores (resposta em fluxo, como o `export.csv`):

```python
def gpx(trips: list[dict], fixes_of) -> Iterator[str]     # GPX 1.1: um <trk> por viagem, <trkpt lat lon><ele><time>, <extensions> com speed
def kml(trip: dict, fixes: list, alerts: list) -> Iterator[str]     # LineString do trajeto + Placemark de início, fim, paradas e alertas
```

Rotas: `/api/gps/trips/{id}/export.gpx`, `/api/gps/trips/{id}/export.kml`, `/api/gps/export.gpx?from=&to=`. Resolução cheia (`gps_fix`), hora em UTC ISO 8601, texto com escape de XML. Só no painel do Pi; na nuvem o botão some.

**Aceite:** teste: o GPX de uma viagem sintética passa por `xml.etree.ElementTree.fromstring`, tem um `trkpt` por posição boa, horas crescentes e latitude/longitude iguais às do banco com 6 casas; o KML tem 1 `LineString` e os alfinetes dos alertas.

---

## 10. Testes sem o hardware

Tudo o que não está marcado **[HW]** é provado por teste automático ou por uso do painel com dados simulados. Estilo dos testes igual ao de `tests/test_parsing.py`: funções `test_*` soltas, `sys.path.insert` para `src`, banco temporário com `db.init(tempfile.mkdtemp() + "/x.db")`, sem dependências além do `pytest` (e rodando também com `python tests/arquivo.py`, pelo bloco `__main__` no fim).

### 10.1 Simulador (`src/obdpi/gps/sim.py`)

Gera uma viagem com **GPS e OBD coerentes entre si**, a partir de um modelo simples do carro. Fica em `src/` (não em `tests/`) porque também serve para desenvolver a interface e para ensaiar no Pi antes de o receptor chegar.

```python
@dataclass
class Leg:
    to: tuple[float, float]        # (lat, lon) do fim do trecho
    speed: float                   # km/h de cruzeiro
    stop_s: float = 0              # parada no fim (semáforo)
    grade: float = 0.0             # inclinação em %
    pedal: float | None = None     # None = segue a velocidade; 1.0 = pé no fundo (arrancada/puxada)
    hold_gear: int | None = None   # mantém a marcha (retomada, dinamômetro)

@dataclass
class SimTrip:
    t0: int                        # ms UTC do início
    truth: list[dict]              # a cada 0,1 s: t, lat, lon, alt, v, a, gear, rpm (a verdade, para os testes compararem)
    fixes: list[Fix]               # o que o receptor entregaria (com ruído, na taxa pedida, com os buracos)
    obd: list[dict]                # leituras como o coletor veria: ts + colunas de fast_sample + sinais lentos

def simulate(legs: list[Leg], *, start: tuple[float, float], t0: int, rate_hz: float = 5, obd_hz: float = 2, seed: int = 1,
             speedo: float = 1.0, mass: float = 1350, cda: float = 0.63, crr: float = 0.011, alt0: float = 760,
             leak: bool = False, slip: tuple[float, float, float] | None = None,       # (início s, duração s, fração)
             dropouts: tuple[tuple[float, float], ...] = (), noise: tuple[float, float, float] = (2.5, 0.15, 4.0),
             mix: float = 0.0, ect0: float = 30) -> SimTrip
def to_nmea(trip: SimTrip) -> list[str]            # GGA + RMC por época, com checksum
def to_gpsd(trip: SimTrip) -> list[str]            # linhas JSON como o gpsd envia (VERSION, DEVICES, SKY, TPV)
def to_db(con, trip: SimTrip, *, with_gps: bool = True, finalize: bool = False) -> int     # grava como o coletor gravaria; devolve o id da viagem
class FakeGpsd(threading.Thread):                  # servidor TCP local que fala o protocolo do gpsd a partir de um SimTrip
    def __init__(self, trip: SimTrip, port: int = 0, speed: float = 1.0): ...
```

Modelo, para os dados parecerem de verdade e para as análises terem o que encontrar:

- **Movimento:** segue a velocidade do trecho com aceleração máxima de 1,5 m/s² e freada de 2,0 m/s²; com `pedal = 1.0` a aceleração vem da curva de torque (tabela aproximada do R18A: 150 Nm a 2.000, 172 a 4.300, 160 a 6.300 rpm) menos as resistências. Passo de 0,1 s.
- **Marchas:** as relações reais do código (`GEAR_KMH_PER_KRPM`); sobe a 2.800 rpm (6.300 com pé no fundo), reduz a 1.300; troca dura 0,6 s com a razão solta. Parado: 780 rpm.
- **Motor:** potência pedida = (m·a + m·g·(Crr + inclinação) + ½ρCdA·v²)·v ÷ 0,88; carga = potência ÷ potência máxima naquela rotação (mínimo 18 %); MAP = 28 + 0,72 × carga; MAF pelo ar esperado (`tripdiag.expected_air_gs`) × 0,85; consumo por `fuel.fuel_lh`; pedal e borboleta proporcionais à carga; temperatura sobe de `ect0` a 90 °C em ~8 min; pressão barométrica pela altitude.
- **`leak = True`:** ajuste curto alto com MAP baixo (+35 % na lenta, +5 % com carga) e MAF 40 % menor na lenta: a assinatura do defeito atual do carro.
- **`slip`:** rotação acima da esperada pela fração dada, no intervalo dado.
- **GPS:** ruído de posição correlacionado (AR(1) com coeficiente 0,95, desvio de 2,5 m), velocidade com desvio de 0,15 m/s, altitude com 4 m; `dropouts` são intervalos sem posição (túnel); os 3 primeiros segundos depois de um buraco vêm com `hdop` alto.
- **OBD:** velocidade = `int(v × speedo)` (inteira, como o PID 0D); amostras a `obd_hz` com ±50 ms de variação; sinais lentos a cada 5 s.
- **Determinístico:** tudo sai de `random.Random(seed)`.

Cenários prontos (funções que devolvem `SimTrip`; coordenadas inventadas em torno de um ponto genérico, nunca o endereço do dono):

| Cenário | Conteúdo | Usado em |
|---|---|---|
| `commute()` | 12 km urbanos, 6 semáforos, casa → trabalho | 9.1, 9.3, 9.10, 9.18, 9.19 |
| `commute_alt()` | mesmo par de pontas por outro caminho | 9.3 |
| `highway()` | 40 km a 100 km/h com trecho urbano nas pontas | 9.17, 9.20 |
| `hill()` | 2 km a +6 % e 2 km a −6 % | 9.12, 9.9 |
| `launch()` | parado → 110 km/h com pé no fundo | 9.8 |
| `pull(gear)` | 2.000 → 6.000 rpm numa marcha só | 9.9, 9.23 |
| `coast()` | 90 → 40 km/h em ponto morto, ida e volta | 9.9 |
| `slip()` | 4ª a 3.000 rpm com 8 % de patinação por 3 s | 9.6 |
| `tunnel()` | viagem com 90 s sem GPS | 9.3, bordas |
| `towed()` | duas sessões: estaciona; liga 2 km adiante | 9.7 |
| `refuel()` | duas viagens com 6 min de intervalo num posto | 9.14 |

### 10.2 Arquivos de exemplo (`tests/fixtures/gps/`)

| Arquivo | Conteúdo |
|---|---|
| `sample.nmea` | ~40 sentenças escritas à mão: épocas completas (GGA + RMC), prefixo `GN`, uma com checksum errado, uma sem posição (`V`), campos vazios |
| `gpsd-session.jsonl` | sessão do gpsd: `VERSION`, `DEVICES`, `WATCH`, `SKY`, `TPV` com `altMSL` e com `alt` antigo, `TPV` `mode` 1, remoção do receptor (`DEVICE` sem `activated`) e volta |
| `overpass-roads.json`, `overpass-cameras.json` | respostas pequenas do Overpass para o teste do `geo-build.py` (sem rede) |

O `geo.db` de teste é montado no próprio teste com meia dúzia de `INSERT` (2 vias paralelas, 1 radar, 1 posto, 1 bairro).

### 10.3 Testes por arquivo

| Arquivo | Testes |
|---|---|
| `tests/test_gps.py` (F1) | `test_nmea_parse_and_checksum`, `test_nmea_assembler_epochs`, `test_ubx_frames` (checksum do `CFG-RATE` contra bytes conhecidos), `test_gpsd_source_with_fake_server` (inclui remoção e volta do receptor), `test_replay_source_rebase`, `test_reader_filters_and_decimation` (parado → 1 a cada 30 s; salto impossível descartado), `test_reader_survives_source_errors`, `test_geo_haversine_bearing_cell_simplify`, `test_schema_migration_is_additive` (banco antigo ganha tabelas e colunas sem perder dados), `test_clock_gps_retro_correction`, `test_clock_noop_when_system_synced`, `test_clock_rejects_bad_gps_date`, `test_timesrc_never_raises`, `test_hub_without_gps_is_silent` (`live()` = desconectado, nenhuma exceção, viagem fecha igual a hoje), `test_live_json_has_no_position` (procura `lat`/`lon` em todo o `live`), `test_fix_timestamps_keep_gps_spacing` |
| `tests/test_gps_trip.py` (F2, F3) | `test_hybrid_distance_gps_vs_obd`, `test_hybrid_distance_tunnel`, `test_no_gps_trip_matches_old_distance`, `test_finalize_names_origin_destination`, `test_origin_from_parked_position`, `test_stops_and_altitude`, `test_build_track_limits_and_columns`, `test_auto_place_after_3_visits`, `test_geofence_hysteresis`, `test_parked_and_moved_while_off`, `test_moved_not_raised_when_explained`, `test_summary_push_is_single`, `test_outbox_expiry`, `test_odometer_with_gps_and_no_obd_km`, `test_reset_keeps_places`, `test_alerts_with_position`, `test_heat_cells`, `test_gpx_kml_valid`, `test_api_gps_routes_smoke` (FastAPI `TestClient`, se instalado; senão chama as funções direto) |
| `tests/test_gps_routes.py` (F4) | `test_route_clustering`, `test_route_variant_and_reverse`, `test_route_stats_by_fuel`, `test_segment_passes_direction_and_validity`, `test_segment_rescan_idempotent`, `test_segment_before_after_repair`, `test_impact_includes_segment` |
| `tests/test_gps_drive.py` (F5) | `test_gear_learning`, `test_gear_fallback_without_gps`, `test_clutch_slip_detected`, `test_no_slip_on_shifts`, `test_speedo_factor`, `test_speedo_needs_bins`, `test_drive_score_calm_vs_aggressive`, `test_drive_score_short_trip_none` |
| `tests/test_gps_perf.py` (F6) | `test_launch_timing_5hz_and_1hz`, `test_auto_run_requires_intent`, `test_run_on_grade_flagged`, `test_rolling_run_single_gear`, `test_armed_run_mirrors_accel_state`, `test_dyno_recovers_power_curve`, `test_dyno_on_grade`, `test_coastdown_fit`, `test_slope_estimate`, `test_leak_signature_by_slope`, `test_altitude_power` |
| `tests/test_gps_roads.py` (F7) | `test_geo_build_from_fixtures`, `test_road_match_parallel_streets`, `test_camera_ahead_direction`, `test_presumed_limit_is_silent`, `test_road_class_with_and_without_geodb`, `test_reverse_geocode_from_geodb`, `test_score_uses_limits` |
| `tests/test_gps_daily.py` (F8, F9) | `test_fuel_stop_detected`, `test_fuel_stop_rejected_cases`, `test_fill_links_station`, `test_station_ranking`, `test_place_actions`, `test_left_after_action`, `test_eta_prediction`, `test_eta_unknown_route`, `test_range`, `test_traffic_best_departure`, `test_share_requires_cloud`, `test_park_guard_always_on` |
| `cloud/test/loc.test.mjs` (F3, F9) | `isLoc`/`LOC_OPEN`, `docKey` das rotas novas, cookie de localização, fila só com autorização, link válido/vencido/revogado, campos da resposta do link |

Regra para toda fase: os testes antigos continuam passando sem alteração (`python -m pytest tests/ -q` e `cd cloud && npm test`).

### 10.4 Rodar o sistema inteiro sem receptor

1. **No PC (WSL), interface com dados:** `obdctl gps sim --scenario commute --days 14 --to-db /tmp/dev/obd.db` cria um banco com duas semanas de viagens sintéticas já fechadas. Depois `OBD_CONFIG=/tmp/dev/obd.toml python -m obdpi.web.app` (com `db_path` apontando para ele e `web.port = 8080`) e o painel abre com mapa, diário, rotas e trechos.
2. **No PC, coletor de ponta a ponta:** ELM327-emulator por TCP (como hoje) + `gps.source = "replay"` com `obdctl gps sim --scenario commute --out /tmp/commute.nmea`. Os valores do emulador não combinam com o trajeto (marcha e patinação não fazem sentido), mas o caminho dos dados é exercitado inteiro: fio, fila, gravação, `live.json`, `gps.json`, fechamento.
3. **No Pi, antes de o receptor chegar:** depois do deploy da F1, o `gpsd` está instalado e sem receptor; o painel deve mostrar "GPS não conectado" e nada mais muda. Ensaio opcional: `sudo systemctl stop gpsd.socket gpsd` e `/opt/obd/venv/bin/python -m obdpi.gps.sim --serve 2947 --scenario commute` (o coletor conecta como se fosse o gpsd). Religar o gpsd depois.
4. **Nuvem:** `cd cloud && npx wrangler dev` com `LOC_PASSWORD` em `.dev.vars`, e `OBD_LOCAL=http://127.0.0.1:8080 python -m obdpi.sync` apontando para o Worker local.

### 10.5 Lista do dia em que o GPS for ligado [HW]

Com o carro em céu aberto, o Pi ligado e SSH aberto:

1. `lsusb` mostra `1546:01a7 U-Blox AG`; `ls -l /dev/gps0` aponta para `ttyACM0`. Se o código USB for outro, corrigir `deploy/70-obd-gps.rules`.
2. `systemctl status gpsd` ativo; `gpspipe -w -n 12` mostra `DEVICES` com o receptor e `TPV` com `mode` 3 em até 60 s. Anotar o tempo até a primeira posição.
3. Painel → Sistema → GPS: "ok", satélites ≥ 7, HDOP ≤ 1,5, taxa ~5 Hz. Se a taxa ficar em 1 Hz: `ubxtool -p CFG-RATE,200` com o gpsd rodando; se só assim funcionar, registrar e criar um `ExecStartPost` no serviço do gpsd com esse comando.
4. `chronyc sources -v`: linha `GPS` com `Reach` > 0. Desligar o WiFi do Pi, reiniciar, e conferir `chronyc tracking` (`Reference ID` = GPS) e `/api/system` (`clock_synced`, `clock_source: gps`) em menos de 2 min.
5. Com internet de novo: diferença entre GPS e NTP em `chronyc sources` abaixo de 0,2 s; senão ajustar o `offset` em `deploy/chrony-obd-gps.conf`.
6. Conferir a proteção do cartão: com o overlay ligado, reiniciar duas vezes e ver que gpsd e chrony sobem e que `/var/lib/chrony` está no cartão (`findmnt /var/lib/chrony`).
7. Volta de 10 minutos no bairro. Depois: diário com origem e destino, trajeto no mapa sem "dentes", distância do GPS × velocímetro (diferença esperada de 0–5 %), `obdctl gps status` sem reconexões.
8. Tirar o receptor da USB com o motor ligado: painel mostra "GPS não conectado" em até 10 s, coletor segue; recolocar: volta sozinho em até 30 s.
9. Dar a partida com o receptor ligado e olhar `vcgencmd get_throttled`: o consumo extra (~50 mA) não pode piorar as quedas de tensão.
10. Criar os lugares Casa e Trabalho no mapa; definir o primeiro trecho de referência (uma subida do caminho habitual) **antes** de rodar com os bicos novos, se ainda der tempo, ou logo depois.
11. Uma arrancada 0–60 armada, para conferir a voz e a taxa (`hz` da corrida = 5).
12. Uma descida em ponto morto nos dois sentidos, numa reta plana, para calibrar o dinamômetro.
13. Gerar o `geo.db` da região (`scripts/geo-build.py`) e conferir um radar conhecido passando por ele com o modo direção aberto.
14. Depois de uma semana: revisar os limiares marcados como iniciais (ruído de altitude, tolerância das marchas, raio dos lugares) com os dados reais e registrar no `PROCESSO.md`.

---

## 11. Deploy

### 11.1 `deploy/install.sh`

Bloco novo, depois do bloco "pacotes" (idempotente; precisa de internet só na primeira vez, e o `scripts/deploy.sh` já desliga a proteção do cartão antes de rodar o instalador):

```bash
echo "== GPS: gpsd + relógio pelo GPS (chrony)"
dpkg -s gpsd chrony >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gpsd gpsd-tools chrony || \
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gpsd gpsd-clients chrony; }
install -m 644 deploy/gpsd.default /etc/default/gpsd
install -m 644 deploy/70-obd-gps.rules /etc/udev/rules.d/70-obd-gps.rules
udevadm control --reload && udevadm trigger --subsystem-match=tty || true
install -D -m 644 deploy/chrony-obd-gps.conf /etc/chrony/conf.d/obd-gps.conf
sed -i 's/^makestep .*/makestep 1.0 -1/' /etc/chrony/chrony.conf
install -d /etc/systemd/system/chrony.service.d
printf '[Unit]\nAfter=obd-data.service obd-clock.service\n' > /etc/systemd/system/chrony.service.d/obd.conf
chmod 755 deploy/obd-clock.sh
install -d -o obd -g obd /data/obd/geo
```

E, no bloco "serviços": `obd-clock.service` entra no `systemctl enable`; depois `systemctl enable gpsd.socket gpsd.service chrony.service` e `systemctl restart gpsd chrony || true`.

Arquivos:

```
# deploy/gpsd.default
START_DAEMON="true"
USBAUTO="true"
DEVICES=""
GPSD_OPTIONS="-n"

# deploy/70-obd-gps.rules   (apelido estável; o hotplug em si é das regras do gpsd)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", ATTRS{idProduct}=="01a[78]", SYMLINK+="gps0", GROUP="dialout", MODE="0660"

# deploy/chrony-obd-gps.conf
refclock SHM 0 refid GPS precision 1e-1 offset 0.10 delay 0.4 poll 2 filter 8
```

```ini
# deploy/systemd/obd-clock.service
[Unit]
Description=OBD: relógio do sistema pela última gravação do banco (Pi sem RTC)
DefaultDependencies=no
After=obd-data.service local-fs.target
Before=chrony.service gpsd.service obd-collector.service obd-web.service obd-sync.service

[Service]
Type=oneshot
ExecStart=/opt/obd/deploy/obd-clock.sh

[Install]
WantedBy=multi-user.target
```

```bash
#!/bin/bash
# deploy/obd-clock.sh — sem RTC o relógio volta no boot; a última gravação do banco é a melhor estimativa de "agora".
now=$(date +%s)
best=$(stat -c %Y /data/obd/obd.db-wal /data/obd/obd.db /data/obd/state.json 2>/dev/null | sort -n | tail -1)
[ -n "$best" ] && [ "$best" -gt "$now" ] && [ "$best" -lt $((now + 315360000)) ] && date -s "@$best" >/dev/null
exit 0
```

Pontos de atenção:

- **`chrony` substitui o `systemd-timesyncd`** (os pacotes são conflitantes no Debian; o apt remove o timesyncd). Por isso `clock_synced()` não pode mais depender do arquivo do timesyncd (seção 3.2) e o `obd-clock` assume o papel de devolver a hora no boot. Confirmar no Trixie.
- **Proteção do cartão (`deploy/overlay.sh`):** acrescentar `/var/lib/chrony` à lista `KEEP` (o arquivo de deriva do `chrony` passa a morar no cartão, como já acontece com o Bluetooth e o timesync). O gpsd não grava nada. `/etc/default/gpsd`, as regras de udev e a configuração do chrony são instaladas com a proteção desligada, pelo deploy normal.
- O `obd-collector.service` **não** ganha dependência do gpsd: ele reconecta sozinho, e o GPS não pode atrasar nem impedir a coleta do OBD.
- `obd-doctor`: `SERVICES` ganha `gpsd` e `chrony`, mas só para unidades que existem (`systemctl cat <u>` com código 0), para não registrar falha num Pi sem GPS instalado.
- O usuário `obd` já está no grupo `dialout` (necessário só para a fonte de reserva).

### 11.2 `scripts/deploy.sh`

Depois do `rsync` do código: se existir `data/geo/geo.db`, `rsync -az data/geo/geo.db obd@$HOST:/data/obd/geo/geo.db.tmp` seguido de `ssh … 'mv /data/obd/geo/geo.db.tmp /data/obd/geo/geo.db'`. Só reenvia quando o arquivo mudou (o rsync cuida).

### 11.3 Nuvem

1. `cd cloud && npx wrangler d1 execute civic-obd --remote --file=schema.sql` (cria `share`; os `IF NOT EXISTS` tornam o comando repetível).
2. `npx wrangler secret put LOC_PASSWORD` (escolhida pelo dono; 12 caracteres ou mais; fica em `secrets/cloud.txt`, fora do git).
3. `npm run deploy`. O `build.sh` já copia `*.js` e `*.css` de `static/` (por isso Leaflet e os scripts novos ficam soltos em `static/`, sem subpasta); a F9 acrescenta `cp $S/share.html public/share.html`.
4. Ordem: publicar o Worker **antes** de atualizar o Pi. Pi novo com Worker antigo não envia posição (falta o `loc` no `ping`); Worker novo com Pi antigo só mostra "ainda não sincronizado pelo Pi" nas telas do GPS.

### 11.4 Compatibilidade

- Banco: só acréscimos. Um Pi que volte para a versão anterior ignora as tabelas novas.
- Viagens antigas: sem `trip_gps`, aparecem sem trajeto; `distance_km` não é recalculada.
- `live.json`: campo novo `gps`; painéis antigos em cache ignoram.
- Fila da nuvem: ações de localização só são entregues a quem pede `cap=…,gps`.

---

## 12. Plano de implementação por fases

Regras para quem executa:

- Uma fase por vez, na ordem. Cada fase termina com: testes novos e antigos passando, `docs/PROCESSO.md` com a entrada da fase (o que foi feito, o que ficou para o dia do hardware), `README.md` atualizado se a fase cria recurso visível, e commit.
- **Não fazer deploy no Pi nem na nuvem sem o dono pedir.** O Pi fica desligado com o carro, e o dono está no meio do diagnóstico dos bicos: nenhuma fase pode mudar o comportamento atual sem GPS. O teste `test_hub_without_gps_is_silent` e os testes antigos são a garantia.
- Só tocar nos arquivos listados na fase. Em `hub.py`, cada fase acrescenta as suas linhas em `LIVE_ENGINES`/`FINALIZERS`; em `gps_api.py` e `gps.js`, cada fase acrescenta um bloco próprio com um comentário de cabeçalho (`# ---- F4: rotas e trechos`).
- O esquema inteiro (seção 4) entra na F1; a proteção de localização da nuvem inteira entra na F3. Fases seguintes não mexem em `schema.sql`, nem na regra `isLoc`.

### F1 — Fundação: fonte, gravação e relógio (funcionalidade 2)

| | |
|---|---|
| **Arquivos novos** | `src/obdpi/timesrc.py`; `src/obdpi/gps/{__init__,nmea,source,reader,geo,store,hub,sim}.py`; `src/obdpi/web/gps_api.py`; `deploy/obd-clock.sh`, `deploy/systemd/obd-clock.service`, `deploy/70-obd-gps.rules`, `deploy/chrony-obd-gps.conf`, `deploy/gpsd.default`; `tests/test_gps.py`, `tests/fixtures/gps/{sample.nmea,gpsd-session.jsonl}` |
| **Arquivos alterados** | `config.py` (5.4); `db/schema.sql`, `db/__init__.py` (4.1, 4.2); `collector/main.py` (**todos** os ganchos da seção 6; nesta fase `trip_distance` devolve `None`, `finalize_trip` percorre uma lista vazia e `summary` devolve `{"route": None, "parts": []}`); `web/app.py` (`include_router`, `/api/system`); `maint.py` (retenção); `cli.py` (`obdctl gps status`, `obdctl gps sim`); `doctor.py`; `deploy/install.sh`, `deploy/overlay.sh`; `config/obd.example.toml`; `web/static/index.html` + `app.js` (cartão "GPS" na aba Sistema, lendo `/api/gps/status`; chip "GPS" no cabeçalho só quando `live.gps` existe e não é `desligado`) |
| **Entrega** | Com o simulador servindo como gpsd: posições gravadas a 1 Hz com `ts` coerente com o OBD, decimação parado, `live.gps` e `gps.json`, correção de relógio pelo GPS, "GPS não conectado" sem receptor |
| **Rotas** | `GET /api/gps/status`, `GET /api/gps/live` |
| **Simulador nesta fase** | modelo completo, `to_nmea`, `to_gpsd`, `to_db`, `FakeGpsd`, cenários `commute`, `tunnel`, `towed` |
| **Pronto quando** | `tests/test_gps.py` inteiro passa; os 12 testes atuais passam; com o emulador do ELM + `FakeGpsd` o coletor roda 10 min sem exceção e com uso de CPU do fio < 2 % |
| **[HW]** | itens 1–9 da lista 10.5; nome do pacote `gpsd-tools`; `cycle` aceito pelo gpsd; `adjtimex` com chrony; remoção do timesyncd |

**Notas da implementação da F1 (2026-11-22)** — ajustes ao plano, para as fases seguintes seguirem o código:

- `GpsdSource`: a regra "10 s sem nenhuma linha → reconecta" só vale **com receptor presente**. Sem receptor o gpsd não manda nada depois do `WATCH`, e a regra faria o fio reconectar a cada 10 s à toa.
- `GpsReader(cfg, source=None)`: aceita a fonte pronta (testes). Em qualquer erro fecha e **reabre a mesma fonte** (não recria): o `AutoSource` precisa lembrar as 3 recusas seguidas do gpsd antes de ir para a serial.
- `/run/obd/gps.json` leva também `fix_ts` (hora da posição), `clock` e um bloco `status` (fonte, receptor, driver, satélites, taxa, `ttff_s`, `reconnects`). É de onde `/api/gps/status` e `obdctl gps status` tiram o estado; o bloco não tem posição.
- `GpsHub.clock_offset()` não grava `system_event`: o evento entraria com a hora já corrigida e seria deslocado de novo pelo `apply_clock_offset`. Fica só o `log.warning` (o coletor já registra "relógio sincronizou").
- `gps_cmd` aceita também `{"cmd": "burst", "s": 30}` (abre a janela de 5 Hz; usado a partir da F6).
- `towed()` devolve uma lista com as duas viagens (`[antes, depois]`).
- `Decimator` (em `hub.py`) concentra a regra de gravação (1 Hz andando, 1/30 s parado, tudo na rajada); `sim.to_db` usa a mesma classe.
- `run_finalizers(con, tid, ctx)` (em `hub.py`) é a função que percorre `FINALIZERS`; `finalize_trip`, `sim.to_db(finalize=True)` e o futuro `obdctl gps reanalyze` chamam ela.
- Com `timesrc`, o relógio passa a contar como sincronizado também pelo `adjtimex` do kernel. No PC (WSL) isso costuma dar "sincronizado" mesmo sem o arquivo do timesyncd; no Pi de hoje (timesyncd) o resultado é o mesmo de antes.

### F2 — Diário de bordo, lugares e estacionamento (funcionalidades 1, 7 base, 13, 16 base, distância da 3)

| | |
|---|---|
| **Arquivos novos** | `src/obdpi/gps/{trip,places,geocode}.py`; `tests/test_gps_trip.py` |
| **Arquivos alterados** | `gps/hub.py` (`trip_distance` real, `summary`, `FINALIZERS += [trip.finalize]`, `LIVE_ENGINES += [ParkEngine, GeofenceEngine]`, km sem OBD); `web/gps_api.py` (logbook, trips, track, places, parked, events); `notify.py` (`expire_ts`); `extras.py` (`odometer`); `reset.py` (copia `place`, `segment`, `gps_model` para o banco novo); `cli.py` (`obdctl gps reanalyze [--trip N | --all]`); `gps/sim.py` (cenários) |
| **Entrega** | Cada viagem fecha com origem, destino, distância híbrida, paradas, altitude, custo e trajeto pronto; resumo do ntfy com a rota; lugares com cerca; "onde estacionei"; aviso de "mudou de lugar desligado" |
| **Pronto quando** | critérios de aceite de 9.1, 9.3 (distância), 9.7 (itens 1–3) e 9.13; `test_no_gps_trip_matches_old_distance` prova que nada muda sem GPS |
| **[HW]** | precisão real da origem na partida a frio; raio adequado dos lugares |

**Notas da implementação da F2 (2026-11-22)** — ajustes ao plano, para as fases seguintes seguirem o código:

- **Paradas:** < 3 km/h por **≥ 20 s** (não 60 s: os semáforos do `commute()` param 30–45 s e o aceite pede 6 paradas), sem contar a saída e a chegada (pontas da viagem). `trip.STOP_MIN_S`.
- **Altitude:** histerese de **5 m** (com 3 m o ruído de 4 m do simulador inventava ~70 m de subida em 6 km planos). Recalibrar com o VK-172.
- **Origem pelo estacionamento:** a posição de estacionamento sai do **banco** (`places.parked_before`: média das 3 últimas posições boas antes do início, válida se as 3 estão paradas), não de `gps_model['parked']`, que no fechamento já aponta para o fim desta viagem; assim a reanálise de viagens antigas dá o mesmo resultado. Vale quando está a até `max(150 m, explicada × 1,15 + 50 m)` da primeira posição (a velocidade inteira do PID 0D subestima a distância explicada). Primeira posição mais de 20 s depois do início só marca `flags = origem_tardia`; sozinha não troca a origem (se o carro foi guinchado, o estacionamento está longe e errado).
- **"Mudou de lugar desligado"** só é avaliado com viagem aberta neste boot (sem OBD não há como descontar o que o carro andou) e uma vez por boot. O alerta e o ntfy **não levam link nem coordenada** (a tabela `alert` é pública na nuvem e o ntfy não leva coordenada, seção 7.4); o mapa fica em `/api/gps/parked` e o ponto em `geo_event`.
- **Cerca:** o estado inicial depois do boot é assumido em silêncio (ligar o carro em casa não gera "entrou em Casa").
- **Motores e `gps.json`:** um motor pode ter `private() -> dict`; o hub copia as chaves `place` e `parked` para o `gps.json` (privado). `place` é `{id, name, kind}`. `live()` dos motores da F2 é vazio (nome de lugar é privado).
- **Km sem OBD:** classe `NoObdKm` no `hub.py`, na mesma linha de `LIVE_ENGINES` da F2; o `geo_event` é atualizado a cada minuto e quando uma viagem abre.
- **Resumo do ntfy:** `gps.summary_names` (padrão `true`, decisão do dono) liga os nomes no título. Só entram nomes de **lugares salvos**, nunca coordenada; com uma ponta só: "saindo de Casa" / "chegando em Casa". `parts` traz "R$ 0,52/km" (a nota entra na F5). Viagem sem trajeto: resumo igual ao de antes.
- **`trip_gps`** é gravada também para viagem sem GPS (`n_fix = 0`, `flags = sem_trajeto`). `flags` usa ainda `manobra`, `origem_tardia`, `hora_aproximada`.
- **`track_json`:** `gaps` em segundos desde `t0` (os mesmos valores de `t`, com as pontas sempre mantidas); as colunas `slope` e `limit` só aparecem na F6/F7; campo extra `eps_m` (tolerância final).
- **Fator do velocímetro:** `trip.speedo_k()` lê `gps_model['speedo']['k']` (velocidade verdadeira ÷ a do OBD, aceito entre 0,85 e 1,15). A F5 deve gravar nesta chave.
- **`outbox` vencido:** `sent_ts` negativo (nunca enviado). **`extras.odometer()`** ganha `gps_km`, `obd_km`, `sem_obd_km` e `composicao` (só quando há GPS ou km sem OBD; sem GPS a resposta é a mesma de antes) e soma os km sem OBD em `km`.
- **Arquivos fora da lista da F2:** `config.py` (`gps.summary_names`; `vehicle.tyre = "205/55 R16"` e `vehicle.occupants = 2`, decisões do dono, usadas na F6) e `web/static/extras.js` (uma linha: a tela de manutenção mostra a `composicao` do 9.13). `config/obd.example.toml` com as opções novas.
- **`geocode`:** o ramo do `geo.db` fica para a F7 (`_from_geodb` devolve `None`).
- **Simulador:** cenário `straight(km)`; `add_places()` e `commute_end()` (Casa/Trabalho nas pontas do `commute()`; `obdctl gps sim --to-db` já cria os dois). `to_db(finalize=True)` usa a distância híbrida, como o coletor.
- **API:** `/api/gps/logbook` devolve `{items, total, limit, offset}`; cada item tem `route`, `has_track`, `avg_moving` (km ÷ tempo em movimento) e `clock_synced`. Escrita de lugares avisa o coletor por `{"cmd": "reload_places"}` em `gps_cmd`. Apagar lugar = `deleted = 1` (o diário guarda o nome antigo; o lugar automático apagado não é recriado ali).
- **Reset:** `reset.carry_config()` cria o banco novo (`db.init`) e copia `place`, `segment` e `gps_model` pelas colunas em comum.
- **`obdctl gps reanalyze`** sem opção refaz os últimos 7 dias; só roda `FINALIZERS` (não recalcula `trip.distance_km`).

### F3 — Mapa, exportação e painel online protegido (funcionalidades 4, 15, 22, 24)

| | |
|---|---|
| **Arquivos novos** | `web/static/{leaflet.js,leaflet.css,gpsmap.js,gps.js}`; `src/obdpi/gps/export.py`; `cloud/test/loc.test.mjs` |
| **Arquivos alterados** | `web/gps_api.py` (alerts, heat, export); `web/static/index.html` (aba e seção Mapa, `<link>` e `<script>` novos, `<div id="gpsFuel">` na aba Consumo); `app.js` (os 2 ganchos da seção 8.1 e `LOAD.mapa`); `extras.js` (evento `obdreplay`, seletor de velocidade do replay); `style.css`; `sw.js` (7.3); `sync.py` (documentos da seção 4.6, posição ao vivo junto com `live()`, `KEEP`, `cap=…,gps`, `loc` do `ping`); `cloud/src/index.js` (7.2 inteira, menos os links); `cloud/schema.sql` (tabela `share`, já criada para a F9); `tests/test_gps_trip.py` (alertas com posição, calor, GPX/KML) |
| **Entrega** | Aba Mapa com Diário, Mapa (trajetos, calor por sinal, alfinetes de alertas, lugares, carro estacionado) e Lugares; mapa no detalhe da viagem com "Colorir por" e replay sincronizado; GPX/KML; nuvem servindo tudo isso só com a senha de localização |
| **Leaflet** | baixar a versão 1.9.4 (`leaflet.js` e `leaflet.css` da distribuição oficial), conferir o hash publicado, manter o cabeçalho de licença. Não usar os ícones padrão (exigiriam imagens): só `circleMarker` e `divIcon` |
| **Pronto quando** | aceites de 9.4, 9.15, 9.22 e 9.24; `npm test` na pasta `cloud`; conferência manual no navegador com o banco de 10.4 (item 1), em tela de celular, tema claro e escuro, com e sem rede |
| **[HW]** | nada específico; o mapa real aparece com a primeira viagem |

**Notas da implementação da F3 (2026-11-22)** — ajustes ao plano, para as fases seguintes seguirem o código:

- **Senha de localização obrigatória (decisão do dono):** na nuvem, todo caminho `isLoc` exige o cookie `obd_loc`, criado só com a `LOC_PASSWORD`. A sessão da `DASH_PASSWORD` **não** libera a localização (o plano dizia que valia). Sem `LOC_PASSWORD` a localização fica indisponível para todos (`/api/env.loc = "desativada"`, `ping.loc = false`) mesmo com `DASH_PASSWORD`.
- **`isLoc` normaliza o caminho** antes de testar (minúsculas, `%xx` decodificado, `\` e barras repetidas); erro ao decodificar conta como protegido. A checagem fica antes de qualquer outra rota da API. `POST` em caminho `isLoc` fora de `LOC_QUEUE` → 409; outros métodos → 405. A fila de localização aceita só JSON válido.
- **Cookie:** a assinatura usa um segredo que inclui a própria `LOC_PASSWORD` (trocar a senha invalida todos os cookies) e recusa `exp` além de 31 dias. A senha é comparada pelo HMAC das duas (tempo fixo). `env.LOC_DELAY_MS` existe só para os testes (espera da senha errada); não definir em produção.
- **Defesa extra no Worker:** o estado ao vivo (documento `/api/live` e o canal WebSocket) perde `gps.lat/lon/latitude/longitude/place/parked/dest` se algum dia chegarem. `/api/gps/live` na nuvem soma à `age_s` o tempo desde que a nuvem recebeu e marca `pi_offline` depois de 60 s.
- **`obd-sync`:** `push()` descarta qualquer chave `is_loc` sem a senha confirmada (mesma regra do Worker, em `sync.is_loc`); a posição ao vivo vai no mesmo pedido da cópia do estado; `cap=…,gps` só com a localização liberada. Se o `ping` deixar de trazer `loc: true` (senha removida, Worker antigo), o Pi **apaga da nuvem** os documentos de localização que já tinha enviado (`purge_loc`). Por enquanto sobem só os documentos que existem (F1–F3): `status`, `logbook?limit=1000`, `places`, `parked`, `alerts?days=90`, `events?days=30`, `heat` dos 6 sinais (30 dias), `trips/{id}` e `trips/{id}/track`; cada fase acrescenta os seus em `Sync.gather_gps`.
- **`/api/gps/alerts`:** resposta `{items, hotspots, rules, n, with_pos}`. Posição pela `store.nearest_fix` (±10 s, dos dois lados). `dtc_event` entra só o `apareceu` que não virou alerta no mesmo instante (o coletor já gera um alerta por código novo), com `rule = "dtc:<código>"`. `slope` é nulo até a F6. Pontos repetidos: agrupamento guloso por regra num raio de 100 m (a célula fixa de 100 m partia um mesmo lugar na borda).
- **`/api/gps/heat`:** além de `cells/min/max`, devolve `cell_deg`, `n_points`, `trips`, `days`. No Pi aceita qualquer coluna do trajeto; a nuvem só tem os 6 sinais sincronizados. Viagens ocultas não entram. A célula dobra até caber em 5.000 (teto 0,05°).
- **GPX/KML** (`gps/export.py`): extensões do GPX no espaço `obd:` (`speed_kmh`, `course`, `sats`) e `<hdop>`; o KML leva também as paradas (`stops_json`). `export.gpx` sem datas = últimos 30 dias, até 500 viagens, só as com posição. Viagem sem trajeto → 404.
- **Cores fixas no traçado:** ajuste curto (±10 / ±25 %), λ (0,97–1,03 / 0,93–1,07), marcha (cor por marcha) e temperatura (< 70, 70–100, 100–105, > 105 °C — faixas fixas no `gpsmap.js`, não as de `limits.py` via `/api/live.status`). O resto usa 5 cores pelos quantis do trajeto.
- **Tela:** a aba Mapa mostra só Diário · Mapa · Lugares; Rotas, Trechos e Desempenho entram com as suas fases. O botão "ver no mapa" em Diagnóstico → Alertas ficou para depois (o `app.js` da F3 só recebe os ganchos); o mapa tem a lista de alertas com filtro, linha do tempo e pontos repetidos, e o balão de cada alerta leva ao replay 8 s antes. `<div id="gpsFuel">` na aba Consumo fica vazio até a F8. O mapa principal desenha até 30 trajetos recentes. No tema escuro os ladrilhos são invertidos por filtro CSS. A velocidade do replay (×1, ×10, ×60) fica guardada no aparelho.
- **Leaflet 1.9.4:** baixado do registro do npm (`leaflet-1.9.4.tgz`, integridade `sha512-nxS1ynzJ…FA74PA==` conferida) e copiado byte a byte de `dist/`; SHA-256 conferem com os SRI publicados em leafletjs.com (`leaflet.js` `sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=`, `leaflet.css` `sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=`). O comentário `sourceMappingURL` foi mantido (o `.map` não é copiado; só o DevTools o pede).
- **`sw.js`:** além de `/api/gps/`, `/api/share/`, `/s/` e `/loc/`, também não guarda caminho com `%xx` nem `/API/GPS` em outra grafia. `voice.js` entra no `SHELL` na F6 (um arquivo ausente faria o `addAll` falhar inteiro).
- **Conferência no navegador** (Chromium sem rede para os ladrilhos, 390 × 844, tema claro e escuro, banco simulado de 10.4 e banco vazio): sem erro de JavaScript; sem ladrilhos o traçado aparece com "sem internet: só o traçado"; sem GPS a aba mostra "GPS não conectado…" e telas vazias. Não conferido: ladrilhos reais, Safari do iPhone e `wrangler dev`.

### F4 — Rotas e trecho de referência (funcionalidades 3 e 5)

| | |
|---|---|
| **Arquivos novos** | `src/obdpi/gps/{routes,segments}.py`; `tests/test_gps_routes.py` |
| **Arquivos alterados** | `gps/hub.py` (`FINALIZERS += [routes.assign_trip, segments.scan_trip]`); `web/gps_api.py` (routes, segments); `extras.py` (`impact()` chama `segments.impact_for`); `web/static/gps.js` (Rotas, Trechos, marcar início/fim no traçado); `web/static/extras.js` (linhas "Trecho N" no cartão antes/depois) |
| **Entrega** | km/L, L/100 km e R$/km por rota; trecho de referência com histórico, marcadores de manutenção e veredito por serviço, integrado ao "antes e depois" |
| **Pronto quando** | aceites de 9.3 (rotas) e 9.5 |
| **[HW]** | definir o trecho real (item 10 da lista) |

**Notas da implementação da F4 (2026-11-22)** — ajustes ao plano, para as fases seguintes seguirem o código:

- **Pontas das rotas:** a chave `c<célula de 0,003°>` casa também com as 8 células vizinhas (o ruído do GPS na borda da célula partia a mesma rota). Variante: nome "Casa → Trabalho · via 2". Rota que fica sem viagens (reanálise) é apagada. O nome nunca é trocado sozinho depois de criado (só pelo dono, `POST /api/gps/routes/{id} {name}`).
- **Lugar automático (F2) no 3º dia** muda a ponta de `c…` para `p…`: as duas primeiras viagens ficam numa rota com a ponta em célula até um `obdctl gps reanalyze`, que junta tudo (o teste cobre).
- **Atípica:** parada > 10 min em `stops_json` marca `atipica` em `trip_gps.flags` (escrito pelo `assign_trip`, depois do `trip.finalize`); a viagem entra na rota e fica fora das medianas, do melhor/pior e do perfil. `stats_json`: `n, n_tipicas, n_atipicas, km, dur_s, km_l, l100, cost_km, cost, best, worst, by_fuel{gasolina|etanol|mistura}, by_hour[{faixa,…}], last_ts`; faixas de saída em `routes.HOURS` (hora local). `profile_json = {step_m: 250, t_s: [...], n}` (mediana enquanto ao menos metade das viagens chega à marca).
- **Trecho — criação:** `POST /api/gps/segments {name, trip_id, t_a, t_b}` com `t_a`/`t_b` em ms absolutos (`t0 + t[i]·1000` do trajeto); invertidos são trocados; mínimo 200 m e 10 s. Portões = posições gravadas mais próximas dos instantes; rumo de entrada = do portão A até a primeira posição 25 m adiante.
- **Portões:** a maior aproximação é calculada por pedaço entre duas posições (projeção), não só pelas posições dentro do raio — a 100 km/h pode não haver posição dentro de 35 m. Pedaços com mais de 5 s entre as posições não contam.
- **Validade (`why` com códigos):** `frio` (ECT < 75 °C na última leitura até a entrada), `sem_temp`, `parou` (parada ≥ 5 s, a menos que a passagem da viagem de referência pare), `gps` (buraco > 5 s com o carro andando), `vel` (fora de ±20 % da mediana das passagens sem os outros motivos, só com 3+). `revalidate` refaz `vel` no trecho inteiro a cada busca. A API traduz em `motivos`. A coluna `ect` guarda a temperatura da entrada.
- **Medidas:** sinais lentos com janela de ±10 s em volta do trecho (no Pi o grupo lento é lido a cada ~20 s). `alt_gain` = altitude média (±3 s) em B − em A. `fuel_mix` = `mix_after` do último abastecimento.
- **Antes e depois:** `compare` usa ajuste curto (`IMPACT["stft_run"]`, 4 pontos), ar medido ÷ esperado (`IMPACT["air_idle"]`, 6 pontos) e km/L (`IMPACT["kml"]`, 0,7); exige 2+ passagens válidas de cada lado ("sem dados" senão). `impact_for` só devolve trechos com passagem válida dos dois lados do serviço (janela de 30 dias) e `extras.impact()` só acrescenta a chave `trechos` quando há algo (sem GPS a resposta é idêntica à de antes). Item: `{trecho, antes{n,de,ate}, depois{…}, veredito (melhorou|piorou|misto|igual|sem dados), texto, indicadores}`.
- **Regressão:** só com 3+ passagens válidas; sem ela o resíduo é contra a mediana de L/100 km do trecho.
- **`rescan`:** acha as viagens por `gps_fix` numa caixa de ~500 m em volta do portão A (varre a tabela; só sob demanda). A viagem de referência é processada primeiro.
- **Tela:** os botões "Rotas" e "Trechos" são acrescentados pelo `gps.js` (o `index.html` não estava na lista da F4); telas novas se registram em `EXTRA[data-g]`. Criar trecho fica em Mapa → Trechos → Novo trecho (não há atalho no detalhe da viagem). Gráfico com o `lineChart` do `app.js` (válidas verdes, inválidas cinza, mistura diferente âmbar, linha vermelha por serviço).
- **Nuvem:** `sync.gather_gps` envia `/api/gps/routes`, `/api/gps/routes/{id}`, `/api/gps/segments`, `/api/gps/segments/{id}` (todos `isLoc`, só com a senha). O Worker não mudou (`LOC_QUEUE` já tinha `segments` e `routes/{id}`).

### F5 — Marchas, embreagem, velocímetro e nota de condução (funcionalidades 6, 11, 10)

| | |
|---|---|
| **Arquivos novos** | `src/obdpi/gps/{gears,speedo,score}.py`; `tests/test_gps_drive.py` |
| **Arquivos alterados** | `gps/hub.py` (`LIVE_ENGINES += [GearEngine, SpeedoEngine]`, `FINALIZERS += [score.finalize]`, `gear_table()`, fator em `best_speed`); `collector/main.py` (só `gear()`); `analytics/advice.py` (`gps:clutch_slip`, `gps:moved_off`); `web/gps_api.py` (gears, speedo, score); `web/static/gps.js` (bloco Desempenho: marchas, velocímetro; nota no diário e no detalhe da viagem); `gps/sim.py` (cenário `slip`) |
| **Entrega** | 5 relações aprendidas e usadas na marcha exibida; alerta de patinação; fator do velocímetro aplicado à distância sem GPS; nota de condução por viagem com eventos no mapa |
| **Pronto quando** | aceites de 9.6, 9.11 e 9.10 (sem limites de velocidade) |
| **[HW]** | relações reais; limiar de patinação em uso real (esperado: zero eventos num carro saudável em 2 semanas) |

### F6 — Desempenho: 0–100, dinamômetro, inclinação e altitude (funcionalidades 8, 9, 12, 23)

| | |
|---|---|
| **Arquivos novos** | `src/obdpi/gps/{perf,dyno,terrain}.py`; `web/static/voice.js`; `tests/test_gps_perf.py` |
| **Arquivos alterados** | `gps/hub.py` (`LIVE_ENGINES += [PerfDetector, DynoLive]`, `FINALIZERS += [dyno.finalize, terrain.finalize]`, rajadas, `live.gps.perf`, `slope`, `avail_pct`); `gps/trip.py` (coluna `slope` no trajeto); `collector/main.py` (só `accel_feed`/`accel_poll_cmd`: delegar ao `PerfDetector` quando há GPS bom); `web/gps_api.py` (perf, dyno, terrain); `web/static/drive.html` (carrega `voice.js`, quadros `gpsspd`, `alt`, `power`, 0–100 falado); `web/static/gps.js` (bloco Desempenho completo); `gps/sim.py` (`launch`, `pull`, `coast`, `hill`) |
| **Entrega** | Arrancadas e retomadas automáticas e armadas pelo GPS, com voz; curva de potência e torque por puxada; assistente de coastdown; inclinação no trajeto; análise inclinação/altitude × motor |
| **Pronto quando** | aceites de 9.8, 9.9, 9.12 e 9.23; `/dirigir?demo=1` fala no iPhone (conferência manual do dono) |
| **[HW]** | taxa real de 5 Hz; ruído de altitude; valores absolutos de potência |

### F7 — Vias: radares, limites, cidade × estrada e nomes de rua (funcionalidades 17, 20, complemento da 1 e da 10)

| | |
|---|---|
| **Arquivos novos** | `scripts/geo-build.py`; `src/obdpi/gps/roads.py`; `tests/test_gps_roads.py`, `tests/fixtures/gps/overpass-*.json` |
| **Arquivos alterados** | `gps/hub.py` (`LIVE_ENGINES += [RoadEngine]`, `FINALIZERS += [roads.finalize]`, conexão do `geo.db`); `gps/score.py` (usa os limites); `gps/trip.py` (coluna `limit`); `web/gps_api.py` (roadmix; `geo` no status); `web/static/drive.html` (selo do limite, faixa e voz do radar); `web/static/gps.js` (cidade × estrada; camada de radares no mapa); `scripts/deploy.sh` (envio do `geo.db`); `.gitignore` (`data/`) |
| **Entrega** | `geo.db` gerado no PC; limite e radar no modo direção com voz; consumo e comportamento por tipo de via; origem e destino com rua e bairro |
| **Pronto quando** | aceites de 9.17 e 9.20; `test_reverse_geocode_from_geodb`; o script roda de verdade uma vez no PC para uma área de teste (não a do dono) e gera um arquivo abrível |
| **[HW]** | radar real conhecido (item 13); área que o dono quer cobrir |

### F8 — Rotina: postos, ações dos lugares, chegada, autonomia e trânsito (funcionalidades 14, 16, 18, 19)

| | |
|---|---|
| **Arquivos novos** | `src/obdpi/gps/{fuelstop,traffic,eta}.py`; `tests/test_gps_daily.py` |
| **Arquivos alterados** | `gps/places.py` (execução das ações, `partiu_apos`); `gps/hub.py` (`LIVE_ENGINES += [EtaEngine]`, `FINALIZERS += [traffic.finalize]`, `fuelstop.detect` em `on_trip_start`); `web/app.py` (`fuel_fill` aceita `stop_id`/`place_id` e associa o posto pela posição); `sync.py` (atende `/run/obd/sync_cmd`); `web/gps_api.py` (stations, fuelstop, traffic); `web/static/gps.js` (cartão em Consumo, ranking, trânsito, ações no editor de lugares); `web/static/drive.html` (quadro `eta`, aviso de autonomia); `gps/sim.py` (`refuel`, `commute_alt`) |
| **Entrega** | Parada em posto vira lembrete de registrar; ranking de postos; ações ao chegar/sair; destino e chegada previstos; autonomia por rota; melhor horário de saída |
| **Pronto quando** | aceites de 9.14, 9.16, 9.18 e 9.19 |
| **[HW]** | precisa de semanas de viagens reais para as estatísticas fazerem sentido |

### F9 — Compartilhar posição e modo sempre ligado (funcionalidades 21 e 7 opcional)

| | |
|---|---|
| **Arquivos novos** | `web/static/share.html` |
| **Arquivos alterados** | `cloud/src/index.js` (`/s/<token>`, `/api/share/<token>`, `/api/gps/share*`, `/api/sync/share`); `cloud/build.sh`; `cloud/test/loc.test.mjs`; `sync.py` (envio e revogação de links); `web/gps_api.py` (share); `web/static/gps.js` (criar, listar, encerrar); `gps/places.py` (`ParkGuard`, revogar "até chegar"); `collector/main.py` (nada: o `ecu_silent_loop` já bombeia o GPS desde a F1); `deploy/install.sh` (regra de sudo do desligamento, só com `power.always_on`); `tests/test_gps_daily.py` |
| **Entrega** | Link com validade e revogação mostrando só a posição atual; vigia de estacionamento pronta para quando houver alimentação permanente |
| **Pronto quando** | aceite de 9.21; `test_park_guard_always_on` |
| **[HW]** | o modo sempre ligado depende de instalação elétrica e de internet no carro |

### F10 — Dia do hardware

Sem código novo previsto: executar a lista 10.5, corrigir o que ela mostrar (código USB, taxa, `offset` do chrony, limiares), registrar tudo em `docs/PROCESSO.md` e marcar neste documento cada **[HW]** como conferido ou corrigido. Se o receptor chegar antes de as fases acabarem, a F10 pode ser feita logo depois da F1 e repetida em parte depois da F6 (taxa e voz) e da F7 (radar).

### Ordem e dependências

```
F1 ──► F2 ──► F3 ──► F4
        │      │
        │      └──► F9
        ├──► F5 ──► F6
        └──► F7 ──► F8        (F8 também usa as rotas da F4 e a nota/postos da F5/F7)
```

A ordem numérica respeita as prioridades do dono: alta (diário) e o pré-requisito (relógio) primeiro; depois as de prioridade média na ordem mapa → consumo por rota e trecho → marchas → cerca (já na F2) → 0–100 → dinamômetro → nota; por último a baixa e as sem prioridade. As funcionalidades 15, 22 e 24 entram cedo (F3) porque custam pouco em cima do mapa; a 11 entra com a 6 e a 12/23 com a 9 porque dividem código.

---

## 13. Riscos e perguntas em aberto

### Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| Posição no painel público | Exposição de casa, trabalho e rotina | Regra única que falha fechada (`isLoc`), senha de localização, o Pi não envia sem a nuvem confirmar, teste automático que procura coordenadas no `live.json`, link de compartilhamento só com a posição atual |
| Trocar `timesyncd` por `chrony` | Relógio errado no boot, TLS falhando, `clock_synced` cego | `obd-clock` devolve a hora pela última gravação; `timesrc` funciona com os dois; a correção direta pelo GPS independe do chrony; conferência no dia do hardware antes de confiar |
| Partida a frio do GPS (30–60 s) somada ao boot do Pi | Início da viagem sem posição | Origem herdada do estacionamento; distância completada pelo OBD |
| Queda de tensão na partida já reinicia o Pi | Receptor reinicia junto e perde o sinal | O anel e a retomada de viagem cobrem; o problema elétrico (fonte) continua em aberto no projeto |
| Altitude do GPS ruidosa | Inclinação, dinamômetro em rampa e potência corrigida imprecisos | Janelas longas, marcação de "em subida", preferência por reta plana, coastdown nos dois sentidos |
| Clone ELM327 a 2 Hz | Rotação defasada da velocidade | Regras só em regime; rotação derivada da velocidade na puxada |
| Receptor sem memória: taxa volta a 1 Hz a cada boot | 0–100 menos preciso se a troca falhar | Reenvio a cada conexão, verificação pelo intervalo real, corridas marcadas com `hz` |
| `geo.db` desatualizado ou incompleto | Radar que não existe mais, limite errado | Data do mapa na tela, limite presumido não fala, aviso é auxílio e não substitui a sinalização |
| `full()` do `obd-sync` fica mais longo | Mais CPU e dados no hotspot | Trajeto guardado pronto, envio só do que mudou, posição ao vivo no mesmo pedido da cópia do estado |
| Falso alerta de "mudou de lugar desligado" | Susto à toa | Só com posição de estacionamento válida, descontando o que o OBD e o PID 1F explicam; limiar de 300 m ajustável |
| Voz no iPhone só com a página aberta | Aviso de radar não fala com a tela apagada | Modo direção mantém a tela acesa; limitação declarada |
| Banco maior (+30 %) | Backup diário mais lento | Irrelevante em volume (~170 MB/ano); a retenção já cobre |

### Perguntas para o dono

1. **Senha de localização da nuvem.** Sem ela nada de posição sai do Pi. Pode ser definida já, na F3. Prefere manter o painel público como está e trancar só a localização (proposta), ou colocar senha em tudo (`DASH_PASSWORD`, que já existe)?
2. **Área do mapa offline** para radares e limites: qual região cobrir (cidade, região metropolitana, estradas que costuma pegar)? O tamanho do arquivo depende disso.
3. **Massa e pneus:** confirmar pneu 205/55 R16 e quantas pessoas andam normalmente no carro (entra no dinamômetro).
4. **Alimentação permanente:** existe plano de instalar relé temporizado ou sensor de ignição? Sem isso, a cerca só detecta movimento na partida seguinte.
5. **Resumo no celular** com os nomes dos lugares ("Casa → Trabalho"): o tópico do ntfy é secreto, mas é um serviço de terceiros. Manter os nomes ou usar só a distância?
6. **Cabo extensor USB** para pôr o receptor no painel: vale comprar junto.
7. **Trecho de referência:** qual pedaço do caminho habitual serve de bancada (de preferência uma subida constante de 500 m a 2 km, sem semáforo)?

---

### Arquivos críticos para implementação

- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/gps/hub.py` — única ponte entre o coletor e o GPS; tudo o que roda ao vivo e no fechamento passa por ele. Tem de engolir qualquer erro.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/collector/main.py` — recebe os ganchos da seção 6; é o serviço que não pode parar.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/gps/source.py` e `reader.py` — abstração da fonte e o fio; onde o hardware real pode surpreender.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/gps/sim.py` — o simulador; a qualidade de todos os testes depende dele.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/gps/trip.py` — fechamento da viagem: distância, origem/destino e trajeto, base de quase todas as outras telas.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/timesrc.py` e `/home/cleberson/projects/obd/obd-car-analysis/deploy/obd-clock.sh` — relógio; erro aqui estraga os horários de tudo.
- `/home/cleberson/projects/obd/obd-car-analysis/cloud/src/index.js` — proteção da localização no painel público.
- `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/db/schema.sql` e `/home/cleberson/projects/obd/obd-car-analysis/src/obdpi/db/__init__.py` — esquema e migração da seção 4.
