#!/bin/bash
# Sistema somente leitura: protege o cartão SD quando a energia cai de repente (o Pi apaga junto com o carro).
#
# Com a proteção ativa, tudo o que o sistema grava fora de /data vai para a memória RAM e some no próximo boot;
# os arquivos do sistema no cartão nunca são reescritos. Foi a reescrita no meio de um corte de energia que apagou as
# redes WiFi em 20/11. /data (banco, backups, diários) continua gravando direto no cartão, assim como as chaves do
# Bluetooth, o relógio salvo e a deriva do chrony (GPS). É o mesmo mecanismo do raspi-config (pacote overlayroot + "overlayroot=tmpfs" no
# cmdline.txt), sem travar a partição de boot (o install.sh edita o cmdline.txt).
#
# Uso (root):  overlay.sh status | on | off | mount-data | persist <arquivo>...
#   on/off      valem a partir do próximo boot (scripts/protect.sh e scripts/deploy.sh reiniciam o Pi)
#   mount-data  chamado no boot pelo obd-data.service
#   persist     grava no cartão arquivos alterados com a proteção ativa (usado pelo obd-doctor); sem proteção não faz nada
set -euo pipefail
CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || CMD=/boot/cmdline.txt
LOWER=/media/root-ro                 # o cartão de verdade, montado pelo overlayroot no boot
MARK=/etc/obd/protegido              # "a proteção deve ficar ligada": o deploy desliga, atualiza e religa
KEEP=(/data /var/lib/bluetooth /var/lib/systemd/timesync /var/lib/chrony)

active() { grep -q '\boverlayroot=tmpfs\b' /proc/cmdline && mountpoint -q "$LOWER"; }
configured() { grep -q '\boverlayroot=tmpfs\b' "$CMD"; }

case "${1:-status}" in
  status)
    active && echo "ativa" || echo "inativa"
    configured && echo "no próximo boot: ativa" || echo "no próximo boot: inativa"
    ;;
  on)
    active && { echo "já está ativa"; exit 0; }
    dpkg -s overlayroot >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq overlayroot
    # sem o overlayroot dentro do initramfs a marca no cmdline não faria nada: melhor avisar do que fingir
    INITRD=$(ls -t /boot/firmware/initramfs* /boot/initrd.img-"$(uname -r)" 2>/dev/null | head -1 || true)
    if [ -z "$INITRD" ] || ! lsinitramfs "$INITRD" 2>/dev/null | grep overlayroot >/dev/null; then
      update-initramfs -u -k "$(uname -r)" >/dev/null 2>&1 || true
      INITRD=$(ls -t /boot/firmware/initramfs* /boot/initrd.img-"$(uname -r)" 2>/dev/null | head -1 || true)
      if [ -z "$INITRD" ] || ! lsinitramfs "$INITRD" 2>/dev/null | grep overlayroot >/dev/null; then
        echo "ERRO: initramfs sem overlayroot ($INITRD); proteção não ativada"; exit 1
      fi
    fi
    for d in "${KEEP[@]}"; do install -d "$d"; done
    systemctl enable obd-data.service >/dev/null
    configured || sed -i '1 s/^/overlayroot=tmpfs /' "$CMD"
    install -d /etc/obd; touch "$MARK"; sync
    echo "proteção ativa a partir do próximo boot"
    ;;
  off)
    sed -i 's/\boverlayroot=tmpfs \?//g' "$CMD"
    [ "${2:-}" = "--keep-mark" ] || { if active; then rm -f "$LOWER$MARK"; else rm -f "$MARK"; fi; }
    sync
    echo "proteção desligada a partir do próximo boot"
    ;;
  mount-data)
    active || exit 0
    if mount -o remount,rw "$LOWER"; then
      ok=1
      for d in "${KEEP[@]}"; do
        install -d "$LOWER$d" "$d"
        mountpoint -q "$d" || mount --bind "$LOWER$d" "$d" || ok=0
      done
      [ $ok = 1 ] && { echo "dados gravando no cartão: ${KEEP[*]}"; exit 0; }
    fi
    # sem /data no cartão tudo o que o coletor gravar sumiria no próximo boot: desliga a proteção e volta ao normal
    echo "ERRO: não consegui deixar /data gravável; desligando a proteção e reiniciando"
    mount -o remount,rw /boot/firmware 2>/dev/null || true
    sed -i 's/\boverlayroot=tmpfs \?//g' "$CMD"; sync
    systemctl --no-block reboot
    exit 1
    ;;
  persist)
    shift
    active || exit 0
    mount -o remount,rw "$LOWER"
    for f in "$@"; do
      f=$(realpath -m "$f")
      [ -e "$f" ] || continue
      install -d "$LOWER$(dirname "$f")"
      cp -a "$f" "$LOWER$f.obdtmp" && mv -f "$LOWER$f.obdtmp" "$LOWER$f"
    done
    sync
    ;;
  *) echo "uso: $0 status|on|off|mount-data|persist <arquivo>..."; exit 2 ;;
esac
