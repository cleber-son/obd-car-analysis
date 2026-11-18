# Diário do processo — OBD Civic 2008

Registro cronológico de tudo que foi feito no projeto.

## Objetivo

Raspberry Pi 3 Model B v1.2 ligado no carro (Honda Civic 2008 1.8 flex, manual), conectado via
Bluetooth a um adaptador OBD-II ELM327. A cada 20 s ele coleta todos os sensores suportados pela
ECU e grava localmente. Um painel web servido pelo próprio Pi (acesso por IP / `obd.local`) mostra
dados em tempo real, relatórios e uma página dedicada à saúde do **corpo de borboleta** (TBI/DBW).
Substitui o app Car Scanner, que só registra com o app aberto.

## 2026-11-18 — Fase 0: sistema operacional no cartão SD

### Decisões
| Item | Escolha | Motivo |
|---|---|---|
| SO | Raspberry Pi OS Lite 64-bit **Trixie** (2026-10-06) | Sem desktop → sobra RAM (1 GB); BlueZ e NetworkManager oficiais |
| Hostname | `obd` → `obd.local` via avahi (mDNS) | Acessar sem saber o IP |
| Usuário | `obd` (senha aleatória em `secrets/credenciais.txt`, fora do git) + chaves SSH ed25519 deste PC | SSH sem senha |
| WiFi | "Wake Up 2" (principal) e "garagem" (alternativa), país BR, via `network-config` (cloud-init/netplan → NetworkManager) | Pi entra sozinho no WiFi |
| Fuso / teclado | America/Sao_Paulo / br | — |

### Como foi gravado (WSL2 + Windows)
O WSL não enxerga leitores USB, então a gravação foi feita pelo lado Windows:

1. Baixada `2026-10-06-raspios-trixie-arm64-lite.img.xz` de `downloads.raspberrypi.com`
   e conferido o SHA256 oficial (OK). Descompactada (3,08 GB).
2. Gerados `os/boot/user-data` e `os/boot/network-config` (cloud-init — o Trixie não usa mais
   `wpa_supplicant.conf` nem `userconf.txt`). Versões sem segredo: `*.example`.
3. Script `os/flash-sd.ps1`, executado elevado (UAC), que:
   - valida o alvo (disco 3, USB, 32 010 928 128 bytes, não é disco de sistema);
   - limpa o disco e grava a imagem raw em `\\.\PhysicalDrive3`;
   - lê de volta e compara SHA256 → **Verificação OK**;
   - monta a partição boot e copia `user-data`, `network-config`, cria `ssh` e acrescenta
     `cfg80211.ieee80211_regdom=BR` no `cmdline.txt`.
4. Arquivos temporários com senha apagados de `D:\SDs\obd`.

> Problemas encontrados: a 1ª execução gravou certo mas falhou na verificação
> (`[math]::Min` com Int32 > 2 GB). Corrigido e criado `-SkipWrite` para refazer só verificação + config.

### Primeiro boot (próximo passo — com o Pi)
1. Ejetar o cartão no Windows, colocar no Pi, ligar (fonte 5 V 2,5 A).
2. Esperar ~3–5 min (cloud-init cria usuário, expande partição, instala avahi, reinicia).
3. No PC: `ssh obd@obd.local` (ou pelo IP que aparecer no roteador).
4. Conferir: `cloud-init status`, `nmcli dev`, `bluetoothctl show`.

## Próximas fases
Ver `docs/ARQUITETURA.md`.

## 2026-11-18 — Prévia da interface
- Mockup navegável (dados simulados) em `web/mockup/painel.html`, publicado como artefato:
  https://claude.ai/artifact/1NkrbnkLgDTzTbuu9sXMSY
- Telas: Ao vivo (rotação, velocidade, temperatura, bateria; gráfico pedal × borboleta; nota de
  saúde da borboleta; grade de todos os PIDs), Corpo de borboleta (tendência), Viagens e alertas (DTCs).
- WiFi alternativo "garagem" adicionado ao `network-config` do cartão.
