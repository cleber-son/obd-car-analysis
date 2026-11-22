-- Esquema v1. Timestamps em milissegundos UTC.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS session (
  id INTEGER PRIMARY KEY, started_ts INTEGER, last_ts INTEGER, clock_synced INTEGER DEFAULT 0,
  adapter TEXT, elm_version TEXT, protocol TEXT, multi_pid INTEGER, supported_pids TEXT,
  vin TEXT, app_version TEXT);

CREATE TABLE IF NOT EXISTS trip (
  id INTEGER PRIMARY KEY, session_id INTEGER, start_ts INTEGER, end_ts INTEGER, end_reason TEXT,
  finalized INTEGER DEFAULT 0, duration_s INTEGER, distance_km REAL, idle_s INTEGER,
  avg_speed REAL, max_speed REAL, max_rpm REAL, max_ect REAL, min_volt REAL, max_volt REAL,
  avg_ltft REAL, cold_start INTEGER, tb_score INTEGER, n_alerts INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS trip_start ON trip(start_ts);

CREATE TABLE IF NOT EXISTS signal (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, pid INTEGER, name TEXT, unit TEXT, decimals INTEGER);

-- Todos os sinais a cada 20 s (formato estreito)
CREATE TABLE IF NOT EXISTS sample (
  signal_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL,
  PRIMARY KEY (signal_id, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sample_ts ON sample(ts);

-- Grupo rápido (corpo de borboleta) ~1 Hz (formato largo)
CREATE TABLE IF NOT EXISTS fast_sample (
  ts INTEGER PRIMARY KEY, trip_id INTEGER, rpm REAL, speed REAL, map REAL,
  tps REAL, tps_rel REAL, tps_b REAL, tps_cmd REAL, pedal_d REAL, pedal_e REAL);

CREATE TABLE IF NOT EXISTS dtc_state (
  code TEXT, kind TEXT, first_seen_ts INTEGER, last_seen_ts INTEGER, active INTEGER, count INTEGER DEFAULT 1,
  PRIMARY KEY (code, kind));
CREATE TABLE IF NOT EXISTS dtc_event (
  id INTEGER PRIMARY KEY, ts INTEGER, trip_id INTEGER, code TEXT, kind TEXT, event TEXT, mil INTEGER, freeze_json TEXT);

CREATE TABLE IF NOT EXISTS alert (
  id INTEGER PRIMARY KEY, ts INTEGER, ts_end INTEGER, trip_id INTEGER, source TEXT, rule TEXT,
  signal TEXT, severity TEXT, title TEXT, detail TEXT, value REAL, notified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS alert_ts ON alert(ts);

CREATE TABLE IF NOT EXISTS baseline (
  key TEXT PRIMARY KEY, n INTEGER, mean REAL, m2 REAL, updated_ts INTEGER);

CREATE TABLE IF NOT EXISTS system_event (id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, ts INTEGER, title TEXT, body TEXT, priority INTEGER, tags TEXT, sent_ts INTEGER);
CREATE TABLE IF NOT EXISTS annotation (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, text TEXT);

-- Abastecimentos (botão "Tanque cheio")
CREATE TABLE IF NOT EXISTS fuel_fill (
  id INTEGER PRIMARY KEY, ts INTEGER, fuel TEXT, liters REAL, price_l REAL, full INTEGER DEFAULT 1,
  mix_after REAL,          -- fração de etanol hidratado no tanque após abastecer (0 = gasolina C, 1 = etanol)
  km_since_prev REAL, est_l_since_prev REAL, real_kml REAL, calib REAL, note TEXT);

-- Testes de aceleração 0–100 km/h
CREATE TABLE IF NOT EXISTS accel_run (
  id INTEGER PRIMARY KEY, ts INTEGER, trip_id INTEGER, t60 REAL, t80 REAL, t100 REAL,
  max_rpm REAL, samples INTEGER, hz REAL, fuel_mix REAL, note TEXT);

-- ===================================================================== GPS (docs/GPS-ARQUITETURA.md, seção 4)
-- Todas as tabelas entram na Fase 1, mesmo as usadas depois, para as fases seguintes não mexerem no esquema.
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
