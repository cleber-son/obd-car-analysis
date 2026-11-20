#!/bin/bash
# Prioridade de rede: sempre o hotspot do celular. O Pi fica tentando até conseguir; enquanto não acha, segue no WiFi
# que o NetworkManager escolher (em casa, "garagem").
# - O NetworkManager não troca de rede sozinho se já está conectado em outra; este laço força a troca.
# - Nos primeiros 10 min depois de ligar procura a cada ~10 s (é quando o celular acabou de entrar no carro); depois, a cada 30 s.
# - A busca é dirigida ao nome do hotspot: o iPhone para de se anunciar quando fica sem ninguém conectado, mas responde
#   a quem pergunta por ele.
PROFILE="hotspot-iphone"
SSID="Android 17"
FAST_S=600
LOG=/data/obd/net.log    # diário de rede no cartão: o journal fica só na memória e some quando o carro desliga
log() {
  logger -t obd-netprefer "$*"
  echo "$(date '+%F %T') up=$(cut -d. -f1 /proc/uptime)s $*" >> "$LOG" 2>/dev/null
  [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 400 ] && { tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }
}
active() { nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="wlan0"{print $1}'; }
log "iniciado"
LAST="?"
while true; do
  CUR=$(active)
  [ "$CUR" = "$LAST" ] || { log "rede atual: ${CUR:-nenhuma}"; LAST="$CUR"; }
  if [ "$CUR" != "$PROFILE" ]; then
    nmcli dev wifi rescan ifname wlan0 ssid "$SSID" >/dev/null 2>&1
    sleep 3
    if nmcli -t -f SSID dev wifi list ifname wlan0 --rescan no 2>/dev/null | grep -qxF "$SSID"; then
      log "hotspot \"$SSID\" visível (conectado em: ${CUR:-nenhuma}) — trocando"
      if ERR=$(nmcli -w 30 con up "$PROFILE" ifname wlan0 2>&1 >/dev/null); then
        log "conectado no hotspot"
      else
        log "falha ao conectar no hotspot (${ERR:0:160}); volta para a rede anterior e tenta de novo"
        sleep 5
        [ -n "$(active)" ] || nmcli -w 30 dev connect wlan0 >/dev/null 2>&1    # não ficar sem rede nenhuma
      fi
    fi
  fi
  if [ "$SECONDS" -lt "$FAST_S" ]; then sleep 7; else sleep 30; fi
done
