#!/bin/bash
# Rede do Pi: (1) prefere sempre o hotspot do celular e (2) se conserta sozinho quando fica sem rede.
#
# 1. Hotspot: o NetworkManager não troca de rede sozinho se já está conectado em outra; este laço força a troca.
#    Nos primeiros 10 min depois de ligar procura a cada ~10 s (o celular acabou de entrar no carro); depois, a cada 30 s.
#    A busca é dirigida ao nome do hotspot: o iPhone para de se anunciar quando fica sem ninguém conectado, mas responde
#    a quem pergunta por ele. Se a conexão falha, espera cada vez mais (20 s … 5 min) antes de tentar de novo: tentar
#    sem parar derrubaria o WiFi de casa a cada tentativa.
# 2. Autoconserto: sem rede e com uma rede conhecida ao alcance, sobe um degrau de cada vez: reconectar → desligar e
#    ligar o rádio → reiniciar o NetworkManager → recarregar o driver do WiFi. Sem nenhuma rede à vista por muito tempo
#    (rádio travado), desliga/liga o rádio e recarrega o driver. Na estrada, com redes à vista mas nenhuma conhecida,
#    não faz nada. Nunca reinicia o Pi: a coleta do carro não depende de rede.
# Tudo fica em /data/obd/net.log (o journal fica só na memória e some quando o carro desliga).
export LC_ALL=C
PROFILE="hotspot-iphone"
SSID="Android 17"
FAST_S=600
LOG=${OBD_NETLOG:-/data/obd/net.log}
log() {
  logger -t obd-netprefer "$*"
  echo "$(date '+%F %T') up=$(now)s $*" >> "$LOG" 2>/dev/null
  [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 400 ] && { tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }
}
now() { cut -d. -f1 /proc/uptime; }
active() { nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="wlan0"{print $1}'; }
cur_ssid() { nmcli -t -f ACTIVE,SSID dev wifi list ifname wlan0 --rescan no 2>/dev/null | sed -n 's/^yes://p' | head -1; }
has_ip() { ip -4 -o addr show dev wlan0 2>/dev/null | grep -q inet; }
visible() { nmcli -t -f SSID dev wifi list ifname wlan0 --rescan no 2>/dev/null | grep -v '^$'; }
known_ssids() {
  nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' | while read -r n; do
    nmcli -g 802-11-wireless.ssid con show "$n" 2>/dev/null
  done | grep -v '^$' | sort -u
}
radio_cycle() { nmcli radio wifi off; sleep 3; nmcli radio wifi on; }
driver_reload() { modprobe -r brcmfmac_wcc 2>/dev/null; modprobe -r brcmfmac 2>/dev/null; sleep 2; modprobe brcmfmac; sleep 8; }

log "iniciado"
LAST="?"; DOWN=0; STAGE=0; ESTAGE=0; FAILS=0; NEXT_TRY=0
KNOWN=$(known_ssids)
while true; do
  T=$(now); CUR=$(active); VIS=""
  [ "$CUR" = "$LAST" ] || { log "rede atual: ${CUR:-nenhuma}"; LAST="$CUR"; }

  # ---- 1. hotspot
  if [ -n "$CUR" ] && [ "$(cur_ssid)" = "$SSID" ]; then
    FAILS=0                                   # já está no hotspot (por qualquer perfil)
  else
    nmcli dev wifi rescan ifname wlan0 ssid "$SSID" >/dev/null 2>&1
    sleep 3
    VIS=$(visible)
    if grep -qxF "$SSID" <<<"$VIS" && [ "$T" -ge "$NEXT_TRY" ]; then
      log "hotspot \"$SSID\" visível (conectado em: ${CUR:-nenhuma}) — trocando"
      if ERR=$(nmcli -w 30 con up "$PROFILE" ifname wlan0 2>&1 >/dev/null); then
        log "conectado no hotspot"; FAILS=0
      else
        [ "$FAILS" -lt 5 ] && FAILS=$((FAILS + 1))
        W=$((20 * 2 ** (FAILS - 1))); [ "$W" -gt 300 ] && W=300
        NEXT_TRY=$(( $(now) + W ))
        log "falha ao conectar no hotspot (${ERR:0:160}); nova tentativa em ${W}s"
        sleep 5
        [ -n "$(active)" ] || nmcli -w 30 dev connect wlan0 >/dev/null 2>&1    # volta para a rede anterior
      fi
    fi
  fi

  # ---- 2. autoconserto
  if [ -n "$(active)" ] && has_ip; then
    [ "$STAGE$ESTAGE" = "00" ] || log "rede de volta: $(active)"
    DOWN=0; STAGE=0; ESTAGE=0
  else
    [ "$DOWN" = 0 ] && DOWN=$T
    D=$(( $(now) - DOWN ))
    if [ -n "$KNOWN" ] && [ -n "$VIS" ] && grep -qxFf <(echo "$KNOWN") <<<"$VIS"; then
      ESTAGE=0
      if   [ "$STAGE" = 0 ] && [ "$D" -ge 45 ];   then STAGE=1; log "sem rede há ${D}s com rede conhecida à vista: reconectando"; nmcli -w 30 dev connect wlan0 >/dev/null 2>&1
      elif [ "$STAGE" = 1 ] && [ "$D" -ge 120 ];  then STAGE=2; log "sem rede há ${D}s: desligando e ligando o rádio WiFi"; radio_cycle
      elif [ "$STAGE" = 2 ] && [ "$D" -ge 240 ];  then STAGE=3; log "sem rede há ${D}s: reiniciando o NetworkManager"; systemctl restart NetworkManager; sleep 10
      elif [ "$STAGE" = 3 ] && [ "$D" -ge 420 ];  then STAGE=4; log "sem rede há ${D}s: recarregando o driver do WiFi"; driver_reload
      elif [ "$STAGE" = 4 ] && [ "$D" -ge 1320 ]; then STAGE=0; DOWN=$(now); log "ainda sem rede: recomeçando o autoconserto"
      fi
    elif [ -z "$VIS" ]; then
      if   [ "$ESTAGE" = 0 ] && [ "$D" -ge 300 ];  then ESTAGE=1; log "nenhuma rede à vista há ${D}s: desligando e ligando o rádio WiFi"; radio_cycle
      elif [ "$ESTAGE" = 1 ] && [ "$D" -ge 600 ];  then ESTAGE=2; log "nenhuma rede à vista há ${D}s: recarregando o driver do WiFi"; driver_reload
      elif [ "$ESTAGE" = 2 ] && [ "$D" -ge 2400 ]; then ESTAGE=0; DOWN=$(now)
      fi
    fi
  fi
  if [ "$SECONDS" -lt "$FAST_S" ]; then sleep 7; else sleep 30; fi
done
