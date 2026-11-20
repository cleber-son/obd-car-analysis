#!/bin/bash
# Grava as redes WiFi do Pi como perfis do NetworkManager (arquivos próprios, fora do netplan) e guarda uma cópia em
# /etc/obd/wifi/, de onde o obd-netprefer restaura se um perfil sumir ou amanhecer vazio (queda de energia no meio
# de uma gravação apagou todas as redes em 2026-11-20).
# Uso (root): linhas "SSID<TAB>senha" na entrada. Chamado pelo scripts/deploy.sh a partir de os/boot/network-config.
set -euo pipefail
BK=/etc/obd/wifi
install -d -m 700 "$BK"
prio_of() { case "$1" in "Android 17") echo 20;; garagem) echo 10;; *) echo 5;; esac; }
while IFS=$'\t' read -r SSID PSK; do
  [ -n "$SSID" ] && [ -n "$PSK" ] || continue
  if [ "$SSID" = "Android 17" ]; then NAME=hotspot-iphone; else NAME="wifi-$(echo "$SSID" | tr -c 'A-Za-z0-9_-' '-' | sed 's/-*$//')"; fi
  # remove perfis antigos (netplan/cloud-init) da mesma rede, para não haver dois concorrendo
  nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' | while read -r n; do
    [ "$n" != "$NAME" ] && [ "$(nmcli -g 802-11-wireless.ssid con show "$n" 2>/dev/null)" = "$SSID" ] && nmcli con delete "$n" >/dev/null 2>&1 || true
  done
  nmcli con show "$NAME" >/dev/null 2>&1 || nmcli con add type wifi ifname wlan0 con-name "$NAME" ssid "$SSID" >/dev/null
  nmcli con modify "$NAME" 802-11-wireless.ssid "$SSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" wifi-sec.psk-flags 0 \
    connection.autoconnect yes connection.autoconnect-priority "$(prio_of "$SSID")" connection.autoconnect-retries 0
  if [ "$NAME" = hotspot-iphone ]; then
    nmcli con modify "$NAME" ipv4.method manual ipv4.addresses 172.20.10.14/28 ipv4.gateway 172.20.10.1 ipv4.dns "172.20.10.1 1.1.1.1"
  else
    nmcli con modify "$NAME" ipv4.method auto
  fi
  F=$(grep -lx "id=$NAME" /etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null | head -1 || true)
  [ -n "$F" ] || F=$(grep -rl "$(nmcli -g connection.uuid con show "$NAME")" /etc/netplan /run/NetworkManager 2>/dev/null | head -1 || true)
  case "$F" in
    /etc/NetworkManager/system-connections/*) install -m 600 "$F" "$BK/$(basename "$F")"; echo "rede \"$SSID\": perfil $NAME (prioridade $(prio_of "$SSID"))";;
    *) echo "AVISO: perfil $NAME foi gravado em $F (esperado em /etc/NetworkManager/system-connections)";;
  esac
done
find /etc/netplan -name '90-NM-*.yaml' -size 0 -delete      # restos vazios deixados pela queda de energia
sync
