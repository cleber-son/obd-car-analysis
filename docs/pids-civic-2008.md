# O que o Civic 2008 1.8 flex informa pelo OBD

Leitura real em 2026-11-18, adaptador "OBD2" (clone ELM327 v1.5, Bluetooth clássico SPP, PIN 1234).

| Item | Valor |
|---|---|
| Protocolo | **ISO 15765-4 CAN 29 bits / 500 kbps** (ATSP7) |
| Multi-PID (vários sensores por pedido) | Sim |
| PIDs modo 01 suportados | 31 |
| Distância / aquecimentos desde a limpeza de códigos | 2 km / 2 (códigos apagados recentemente, após a troca da borboleta) |

## PIDs suportados

`01 03 04 05 06 0B 0C 0D 0E 0F 10 11 13 15 1F 20 21 2E 30 31 33 34 40 42 43 44 45 47 49 4A 4C`

| PID | Sinal | Observação |
|---|---|---|
| 03 | Estado do sistema de combustível | malha aberta/fechada |
| 04 | Carga do motor | |
| 05 | Temperatura do motor | |
| 06 | Ajuste curto de combustível B1 | |
| 0B | MAP | |
| 0C | Rotação | |
| 0D | Velocidade | |
| 0E | Avanço de ignição | |
| 0F | Temperatura do ar admitido | |
| 10 | Fluxo de ar (MAF) | o R18 tem MAF e MAP |
| 11 | Posição da borboleta (A) | repouso ~18% |
| 13 | Sondas presentes | |
| 15 | Sonda O2 B1S2 (pós-catalisador, convencional) | |
| 1F | Tempo desde a partida | |
| 21 | Distância com luz de injeção acesa | |
| 2E | Purga do cânister comandada | |
| 30 / 31 | Aquecimentos / distância desde a limpeza de códigos | |
| 33 | Pressão barométrica | ~90 kPa |
| 34 | **Sonda de faixa larga B1S1 (λ e corrente)** | sonda A/F antes do catalisador |
| 42 | Tensão do módulo (ECU) | |
| 43 | Carga absoluta | |
| 44 | Lambda comandado | |
| 45 | Borboleta relativa | |
| 47 | Borboleta absoluta B | 2º sensor da borboleta (repouso ~34%) |
| 49 / 4A | **Pedal do acelerador D / E** | repouso 20% / 9,8% (os dois sensores do pedal) |
| 4C | **Atuador da borboleta comandado** | o que a ECU manda a borboleta fazer |

## Não suportados (relevantes)
- **07 ajuste longo de combustível**: só há o curto (06). A regra STFT+LTFT não se aplica; a linha de base aprendida
  do STFT por contexto cobre desvios.
- **52 % de etanol**: a ECU não informa. Usar o diário de abastecimento (anotações).
- **5C temperatura do óleo, 2F nível de combustível, 46 temperatura ambiente**.

## Consequências no projeto
- Corpo de borboleta: dá para rodar R1 (comando × posição), R2 (pedal D × E), R3 (travamento), R4/R5/R6 (marcha lenta e estol)
  e correlação A × B (PID 11 × 47).
- Regra da sonda adaptada para faixa larga: alerta se λ ficar > 1,25 ou < 0,8 em malha fechada com motor quente.
