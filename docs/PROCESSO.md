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

## 2026-11-18 — Arquitetura (Claude Fable)
- Plano completo em `docs/ARQUITETURA.md`: driver ELM327 próprio via socket RFCOMM, coletor
  Python síncrono (varredura completa a cada 20 s + grupo rápido da borboleta a 1–2 Hz),
  SQLite WAL, FastAPI + SSE, SPA Svelte + uPlot compilada no PC, mDNS + ntfy com o IP,
  10 regras para o corpo de borboleta (R1–R10) e roteiro em fases com critérios de aceite.
- Divergência a resolver: a arquitetura sugere partição `/data` separada, mas o cartão já foi
  gravado com expansão automática do root no 1º boot. Opções: aceitar `/data` como diretório
  no root (mais simples) ou encolher o root depois em outra máquina Linux.

## 2026-11-18 — Respostas do dono
- Corpo de borboleta foi **trocado** → linha de base nova; 1ª anotação do diário de manutenção.
- Requisito: **monitorar todos os sensores e alertar qualquer valor fora do padrão** → seção 7A da arquitetura.
- Celular iPhone → notificações via app ntfy. Hotspot "Android 17" adicionado ao `network-config`
  do cartão (3 redes: casa, garagem, hotspot).
- iPhone: ativar **Ajustes → Acesso Pessoal → Maximizar Compatibilidade** (o Pi 3 só enxerga 2,4 GHz).

## 2026-11-18 — Primeiro boot OK
- `obd.local` → 192.168.200.105 (cabo) e 192.168.200.205 (WiFi "Wake Up 2"). SSH por chave funcionando.
- Kernel 6.18.50 aarch64, cloud-init `done`, root expandido (29 GB, 10% usado), 905 MB RAM (764 livres).
- Bluetooth `hci0` B8:27:EB:15:60:6B ligado; avahi, ssh ativos; relógio sincronizado (NTP); Python 3.13.5.
- ⚠️ `vcgencmd get_throttled = 0x50005` → **subtensão agora e CPU limitada**. A fonte atual é fraca;
  trocar por 5,1 V / 2,5 A (oficial) em casa e um conversor 12→5 V de 3 A reais no carro.
- Obs.: o PC está em 192.168.17.x e o Pi em 192.168.200.x (redes diferentes), mas há rota entre elas.
