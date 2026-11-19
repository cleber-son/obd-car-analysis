#!/bin/bash
# Grava a URL do painel online e o token de sincronização em /etc/obd/secrets.toml (token lido da entrada padrão).
# Uso (como root): printf '%s' "$TOKEN" | set-cloud.sh https://obd.cleberson.social.br
set -euo pipefail
URL="$1"; TOKEN="$(cat)"
[ -n "$URL" ] && [ ${#TOKEN} -ge 32 ] || { echo "uso: printf token | $0 URL" >&2; exit 1; }
F=/etc/obd/secrets.toml; touch "$F"
python3 - "$F" "$URL" "$TOKEN" <<'PY'
import re, sys
f, url, tok = sys.argv[1:]
s = open(f).read()
s = re.sub(r"\n?\[cloud\][^\[]*", "\n", s).strip() + f'\n\n[cloud]\nurl = "{url}"\ntoken = "{tok}"\n'
open(f, "w").write(s.lstrip())
PY
chown root:obd "$F"; chmod 640 "$F"
systemctl restart obd-sync 2>/dev/null || true
echo "nuvem configurada: $URL"
