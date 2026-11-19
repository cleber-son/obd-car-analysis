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
install -d -o obd -g obd /data/obd /data/obd/backup /data/obd/repair
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

echo "== hotspot do celular com IP fixo e prioridade"
if ! nmcli -t -f NAME con show | grep -qx "hotspot-iphone"; then
  PSK=$(grep -A1 '"Android 17"' /etc/netplan/*.yaml 2>/dev/null | sed -n 's/.*password: "\(.*\)"/\1/p' | head -1)
  [ -n "$PSK" ] && nmcli con add type wifi ifname wlan0 con-name hotspot-iphone ssid "Android 17" wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PSK" ipv4.method manual ipv4.addresses 172.20.10.14/28 ipv4.gateway 172.20.10.1 \
    ipv4.dns "172.20.10.1 1.1.1.1" connection.autoconnect yes connection.autoconnect-priority 20 >/dev/null
fi

echo "== serviços"
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable obd-btagent.service obd-collector.service obd-web.service obd-maint.timer obd-netprefer.service obd-sync.service obd-doctor.timer
for u in obd-btagent obd-collector obd-web obd-maint.timer obd-netprefer obd-sync obd-doctor.timer; do
  systemctl restart "$u" || echo "AVISO: $u não iniciou (veja journalctl -u $u)"   # um serviço com problema não impede o resto
done
echo "== OK"
systemctl --no-pager --lines=0 status obd-collector obd-web obd-btagent | grep -E "●|Active"
