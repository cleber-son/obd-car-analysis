#!/bin/bash
# Envia o projeto para o Pi, instala e configura o painel online. Uso: scripts/deploy.sh [host]   (padrão obd.local)
set -euo pipefail
HOST=${1:-obd.local}
cd "$(dirname "$0")/.."
rsync -az --delete --exclude '__pycache__' src deploy config requirements.txt "obd@$HOST:/opt/obd/"
ssh "obd@$HOST" 'sudo bash /opt/obd/deploy/install.sh'
if [ -f secrets/cloud.txt ]; then
  sed -n 's/^SYNC_TOKEN=//p' secrets/cloud.txt | tr -d '\n' | ssh "obd@$HOST" 'sudo /opt/obd/deploy/set-cloud.sh https://obd.cleberson.social.br'
fi
