#!/bin/bash
# deploy/obd-clock.sh — sem RTC o relógio volta no boot; a última gravação do banco é a melhor estimativa de "agora".
# Roda como root antes do chrony e do coletor (obd-clock.service). Só avança o relógio, nunca volta, e ignora datas
# mais de 10 anos à frente (arquivo com data absurda).
now=$(date +%s)
best=$(stat -c %Y /data/obd/obd.db-wal /data/obd/obd.db /data/obd/state.json 2>/dev/null | sort -n | tail -1)
[ -n "$best" ] && [ "$best" -gt "$now" ] && [ "$best" -lt $((now + 315360000)) ] && date -s "@$best" >/dev/null
exit 0
