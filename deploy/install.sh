#!/bin/bash
# Instala/atualiza o OBD Civic no Pi. Idempotente. Rodar como root a partir de /opt/obd.
set -euo pipefail
cd /opt/obd
echo "== pacotes"
dpkg -s bluez-tools python3-venv sqlite3 >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv sqlite3 bluez-tools curl; }
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

echo "== GPS: gpsd + relógio pelo GPS (chrony; substitui o systemd-timesyncd)"
dpkg -s gpsd chrony >/dev/null 2>&1 || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gpsd gpsd-tools chrony || \
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gpsd gpsd-clients chrony || echo "AVISO: gpsd/chrony não instalados (sem internet?)"; }
install -m 644 deploy/gpsd.default /etc/default/gpsd
install -m 644 deploy/70-obd-gps.rules /etc/udev/rules.d/70-obd-gps.rules
udevadm control --reload && udevadm trigger --subsystem-match=tty || true
install -D -m 644 deploy/chrony-obd-gps.conf /etc/chrony/conf.d/obd-gps.conf
[ -f /etc/chrony/chrony.conf ] && sed -i 's/^makestep .*/makestep 1.0 -1/' /etc/chrony/chrony.conf || true
install -d /etc/systemd/system/chrony.service.d
printf '[Unit]\nAfter=obd-data.service obd-clock.service\n' > /etc/systemd/system/chrony.service.d/obd.conf
chmod 755 deploy/obd-clock.sh

echo "== usuário e diretórios"
usermod -aG bluetooth,dialout obd || true
install -d -o obd -g obd /data/obd /data/obd/backup /data/obd/repair /data/obd/geo
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

echo "== resistir a travamentos e a quedas de energia (o Pi apaga junto com o carro)"
# cão de guarda do próprio chip: se o sistema travar por 15 s, o Pi reinicia sozinho
install -d /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' > /etc/systemd/system.conf.d/obd-watchdog.conf
# conserta o sistema de arquivos sozinho no boot depois de um desligamento brusco
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  [ -f "$f" ] && ! grep -q 'fsck.repair=yes' "$f" && sed -i '1 s/$/ fsck.repair=yes/' "$f" || true
done
# grava no cartão a cada 5 s no máximo (padrão 30 s): menos dados em risco quando a energia cai
printf 'vm.dirty_expire_centisecs = 500\nvm.dirty_writeback_centisecs = 500\n' > /etc/sysctl.d/90-obd-flush.conf
sysctl -q --system || true

# sistema somente leitura (deploy/overlay.sh): não liga aqui; quem liga é scripts/protect.sh, e o deploy respeita
chmod 755 deploy/overlay.sh

echo "== modo sempre ligado (power.always_on): regra de sudo do desligamento por bateria baixa, só com o modo ligado"
if PYTHONPATH=src venv/bin/python -c "import sys; from obdpi import config; sys.exit(0 if config.load().get('power', {}).get('always_on') is True else 1)" 2>/dev/null; then
  printf 'obd ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff\n' > /etc/sudoers.d/obd-poweroff.tmp
  chmod 440 /etc/sudoers.d/obd-poweroff.tmp
  if visudo -cf /etc/sudoers.d/obd-poweroff.tmp >/dev/null; then mv /etc/sudoers.d/obd-poweroff.tmp /etc/sudoers.d/obd-poweroff; else rm -f /etc/sudoers.d/obd-poweroff.tmp; echo "AVISO: regra de sudo recusada pelo visudo"; fi
else
  rm -f /etc/sudoers.d/obd-poweroff                  # modo desligado (padrão): nenhuma permissão extra
fi

echo "== WiFi sem economia de energia (menos atraso no painel ao vivo e no SSH)"
install -d /etc/NetworkManager/conf.d
printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/obd-wifi-powersave.conf
iw dev wlan0 set power_save off 2>/dev/null || true

echo "== mDNS e anúncio de IP"
install -m 644 deploy/avahi-obd.service /etc/avahi/services/obd.service
install -m 755 deploy/90-obd-announce /etc/NetworkManager/dispatcher.d/90-obd-announce

echo "== redes WiFi: gravadas pelo deploy/set-wifi.sh (chamado pelo scripts/deploy.sh com as senhas de os/boot/network-config)"

echo "== serviços"
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable obd-data.service obd-clock.service obd-btagent.service obd-collector.service obd-web.service obd-maint.timer obd-netprefer.service obd-sync.service obd-doctor.timer
# GPS: o coletor não depende do gpsd (reconecta sozinho); sem receptor o painel mostra "GPS não conectado"
for u in gpsd.socket gpsd.service chrony.service; do systemctl cat "$u" >/dev/null 2>&1 && systemctl enable "$u" || true; done
systemctl restart gpsd chrony 2>/dev/null || true
for u in obd-btagent obd-collector obd-web obd-maint.timer obd-netprefer obd-sync obd-doctor.timer; do
  systemctl restart "$u" || echo "AVISO: $u não iniciou (veja journalctl -u $u)"   # um serviço com problema não impede o resto
done
echo "== OK"
systemctl --no-pager --lines=0 status obd-collector obd-web obd-btagent | grep -E "●|Active"
