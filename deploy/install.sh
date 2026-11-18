#!/bin/bash
# Instala/atualiza o OBD Civic no Pi. Idempotente. Rodar como root a partir de /opt/obd.
set -euo pipefail
cd /opt/obd
echo "== pacotes"
dpkg -s bluez-tools python3-venv sqlite3 >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv sqlite3 bluez-tools curl; }
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

echo "== usuário e diretórios"
usermod -aG bluetooth,dialout obd || true
install -d -o obd -g obd /data/obd /data/obd/backup
install -d /etc/obd
[ -f /etc/obd/obd.toml ] || install -m 644 config/obd.example.toml /etc/obd/obd.toml
install -m 600 deploy/bt-pins.txt /etc/obd/bt-pins.txt
if [ ! -f /etc/obd/secrets.toml ]; then
  TOPIC="civic-obd-$(head -c 12 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 12)"
  printf '[notify]\nntfy_topic = "%s"\n' "$TOPIC" > /etc/obd/secrets.toml
fi
chown root:obd /etc/obd/secrets.toml; chmod 640 /etc/obd/secrets.toml

echo "== bluetooth: permitir pareamento legado com PIN (ELM327)"
sed -i 's/^#\?\s*JustWorksRepairing.*/JustWorksRepairing = always/' /etc/bluetooth/main.conf || true

echo "== poupar o cartão SD e acelerar o boot"
install -D -m 644 deploy/journald-volatile.conf /etc/systemd/journald.conf.d/volatile.conf
for u in apt-daily.timer apt-daily-upgrade.timer man-db.timer NetworkManager-wait-online.service ModemManager.service; do
  systemctl disable --now "$u" 2>/dev/null || true
done
touch /etc/cloud/cloud-init.disabled   # cloud-init só era necessário no 1º boot

echo "== mDNS e anúncio de IP"
install -m 644 deploy/avahi-obd.service /etc/avahi/services/obd.service
install -m 755 deploy/90-obd-announce /etc/NetworkManager/dispatcher.d/90-obd-announce

echo "== serviços"
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now obd-btagent.service obd-collector.service obd-web.service obd-maint.timer
systemctl restart obd-collector.service obd-web.service
echo "== OK"
systemctl --no-pager --lines=0 status obd-collector obd-web obd-btagent | grep -E "●|Active"
