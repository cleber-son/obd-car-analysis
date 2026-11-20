#!/bin/bash
# Envia o projeto para o Pi, instala e configura o painel online. Uso: scripts/deploy.sh [host]   (padrão obd.local)
set -euo pipefail
HOST=${1:-obd.local}
cd "$(dirname "$0")/.."
# sistema somente leitura ligado: destrava e reinicia antes (senão a atualização some no próximo boot), religa no fim
PROT=$(ssh "obd@$HOST" '[ -f /etc/obd/protegido ] && echo 1 || echo 0')
if [ "$PROT" = 1 ]; then
  echo "== proteção do cartão ligada: destravando para atualizar (o Pi reinicia duas vezes)"
  ssh "obd@$HOST" 'sudo /opt/obd/deploy/overlay.sh off --keep-mark'
  scripts/pi-reboot.sh "$HOST"
  trap 'echo "== religando a proteção do cartão"; ssh "obd@$HOST" "sudo /opt/obd/deploy/overlay.sh on" && scripts/pi-reboot.sh "$HOST"' EXIT
fi
rsync -az --delete --exclude '__pycache__' src deploy config requirements.txt "obd@$HOST:/opt/obd/"
ssh "obd@$HOST" 'sudo bash /opt/obd/deploy/install.sh'
if [ -f secrets/cloud.txt ]; then
  sed -n 's/^SYNC_TOKEN=//p' secrets/cloud.txt | tr -d '\n' | ssh "obd@$HOST" 'sudo /opt/obd/deploy/set-cloud.sh https://obd.cleberson.social.br'
fi
if [ -f os/boot/network-config ]; then           # redes WiFi (senhas fora do git): perfis do NetworkManager + cópia para autorrestauração
  python3 - os/boot/network-config <<'PY' | ssh "obd@$HOST" 'sudo /opt/obd/deploy/set-wifi.sh'
import re, sys
ssid = None
for ln in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r'\s+"(.+)":\s*$', ln)
    if m:
        ssid = m.group(1); continue
    m = re.match(r'\s+password:\s*"(.*)"\s*$', ln)
    if m and ssid:
        print(f"{ssid}\t{m.group(1)}"); ssid = None
PY
fi
