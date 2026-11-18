#!/bin/bash
# Envia o projeto para o Pi e instala. Uso: scripts/deploy.sh [host]   (padrão obd.local)
set -euo pipefail
HOST=${1:-obd.local}
cd "$(dirname "$0")/.."
rsync -az --delete --exclude '__pycache__' src deploy config requirements.txt "obd@$HOST:/opt/obd/"
ssh "obd@$HOST" 'sudo bash /opt/obd/deploy/install.sh'
