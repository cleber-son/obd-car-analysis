#!/bin/bash
# Garante que o controlador Bluetooth (BCM43438 do Pi 3) está respondendo.
# Com subtensão o chip às vezes não inicializa ("Reset failed -110"); recarregar o driver costuma resolver.
ok() { timeout 5 bluetoothctl list 2>/dev/null | grep -q Controller; }
ok && exit 0
STAMP=/run/obd-bt-recover
now=$(date +%s); last=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ $((now - last)) -gt 120 ]; then
  echo "$now" > "$STAMP"
  logger -t obd-bt "controlador Bluetooth sem resposta — recarregando o driver hci_uart"
  modprobe -r hci_uart 2>/dev/null; sleep 2; modprobe hci_uart; sleep 6
  systemctl restart bluetooth; sleep 4
fi
ok
