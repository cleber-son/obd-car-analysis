-- Documentos (respostas prontas da API do Pi) e fila de ações feitas no painel online
CREATE TABLE IF NOT EXISTS doc (key TEXT PRIMARY KEY, json TEXT NOT NULL, updated_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cmd (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
                                body TEXT, done INTEGER NOT NULL DEFAULT 0, done_ts INTEGER, result TEXT);
CREATE INDEX IF NOT EXISTS cmd_pending ON cmd(done, id);
-- Links de compartilhamento da posição ao vivo (usados a partir da F9 do GPS; docs/GPS-ARQUITETURA.md, seção 9.21).
-- Só o hash do token fica guardado.
CREATE TABLE IF NOT EXISTS share (
  token_hash TEXT PRIMARY KEY, created_ts INTEGER NOT NULL, expires_ts INTEGER NOT NULL, label TEXT,
  until_place INTEGER, revoked INTEGER NOT NULL DEFAULT 0, views INTEGER NOT NULL DEFAULT 0, last_view_ts INTEGER);
