-- Documentos (respostas prontas da API do Pi) e fila de ações feitas no painel online
CREATE TABLE IF NOT EXISTS doc (key TEXT PRIMARY KEY, json TEXT NOT NULL, updated_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cmd (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
                                body TEXT, done INTEGER NOT NULL DEFAULT 0, done_ts INTEGER, result TEXT);
CREATE INDEX IF NOT EXISTS cmd_pending ON cmd(done, id);
