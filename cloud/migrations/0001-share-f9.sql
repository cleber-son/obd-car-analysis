-- F9 (docs/GPS-ARQUITETURA.md, seção 9.21): opções de cada link de compartilhamento, em JSON
-- {"m": "tempo" | "viagem", "trip": <viagem do Pi> | null, "dest": 0 | 1, "src": "nuvem" | "pi"}.
-- Rodar UMA vez, depois do schema.sql:
--   npx wrangler d1 execute civic-obd --remote --file=migrations/0001-share-f9.sql
-- Se disser "duplicate column name: opts", já estava aplicada (nada a fazer).
-- Sem esta coluna o compartilhamento fica indisponível (falha fechada: nenhum link vale); o resto do painel não muda.
ALTER TABLE share ADD COLUMN opts TEXT;
