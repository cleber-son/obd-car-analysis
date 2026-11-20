#!/bin/bash
# Liga ou desliga o sistema somente leitura no Pi (deploy/overlay.sh) e reinicia para valer.
# Uso: scripts/protect.sh <host> on|off|status
set -euo pipefail
HOST=${1:?host}; ACT=${2:-status}
cd "$(dirname "$0")/.."
case "$ACT" in
  status) ssh "obd@$HOST" 'sudo /opt/obd/deploy/overlay.sh status' ;;
  on|off)
    ssh "obd@$HOST" "sudo /opt/obd/deploy/overlay.sh $ACT"
    scripts/pi-reboot.sh "$HOST"
    ssh "obd@$HOST" 'sudo /opt/obd/deploy/overlay.sh status; findmnt -no SOURCE,FSTYPE /; findmnt -no SOURCE /data; systemctl is-active obd-collector obd-web obd-sync' ;;
  *) echo "uso: $0 <host> on|off|status"; exit 2 ;;
esac
