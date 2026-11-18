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
