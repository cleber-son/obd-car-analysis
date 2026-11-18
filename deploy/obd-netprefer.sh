#!/bin/bash
# Prioridade de rede: sempre o hotspot do celular quando estiver ao alcance.
# O NetworkManager não troca de rede sozinho se já está conectado em outra; este laço força a troca.
PROFILE="hotspot-iphone"
SSID="Android 17"
while true; do
  CUR=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="wlan0"{print $1}')
  if [ "$CUR" != "$PROFILE" ]; then
    if nmcli -t -f SSID dev wifi list ifname wlan0 --rescan yes 2>/dev/null | grep -qxF "$SSID"; then
      logger -t obd-netprefer "hotspot \"$SSID\" visível (conectado em: ${CUR:-nenhuma}) — trocando"
      nmcli con up "$PROFILE" ifname wlan0 >/dev/null 2>&1 || logger -t obd-netprefer "falha ao conectar no hotspot"
    fi
  fi
  sleep 30
done
