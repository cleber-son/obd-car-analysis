#!/bin/bash
# Reinicia o Pi e espera ele voltar (até 5 min). Uso: scripts/pi-reboot.sh <host>
set -uo pipefail
HOST=$1
ssh "obd@$HOST" 'sudo systemctl reboot' 2>/dev/null || true
sleep 20
for _ in $(seq 1 56); do
  if ssh -o ConnectTimeout=4 -o BatchMode=yes "obd@$HOST" true 2>/dev/null; then echo "Pi de volta"; exit 0; fi
  sleep 5
done
echo "ERRO: o Pi não voltou em 5 min"; exit 1
