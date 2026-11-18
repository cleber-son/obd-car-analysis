# Diagnóstico — ajuste de combustível após a troca do corpo de borboleta (2026-11-18)

Dados: 131 leituras completas + 1181 rápidas, motor ligado parado (marcha lenta), de 53 °C a 101 °C. Sem rodar.

## O que os dados mostram

| Sinal | Valor (motor quente) | Leitura |
|---|---|---|
| Ajuste curto (PID 06) | **+46,88 % fixo** em 131 leituras, frio e quente, desvio 0 | Byte cru `BC` confirmado na resposta da ECU |
| Sonda A/F, λ (PID 34 A/B) | 1,07–1,16 (média 1,12) | "pobre" |
| Sonda A/F, corrente (PID 34 C/D) | −0,08 a −0,18 mA (≈ zero) | perto do estequiométrico |
| λ comandado (PID 44) | 0,98 | |
| Malha de combustível (PID 03) | 2 = **malha fechada** o tempo todo | |
| MAF | **1,34 g/s** a 750 rpm | baixo |
| MAP / baro | 28 kPa / 90 kPa | bom vácuo (62 kPa abaixo da atmosfera) |
| Borboleta comandada / relativa | **~1 %** / 0,9 % | quase fechada |
| Marcha lenta | 750 rpm, desvio 39 rpm | estável |
| Sonda pós-catalisador (PID 15) | **0,015–0,025 V** fixo | muito baixa |
| Avanço | 5–16° (média 8°) | normal |
| DTCs | nenhum, mas **códigos apagados há 2 km / 2 aquecimentos** | monitores ainda não rodaram |

## Interpretação

1. **O +46,88 % não se comporta como um ajuste real.** Em malha fechada o ajuste curto oscila o tempo todo; aqui ele é
   idêntico do motor frio ao quente. Ou a ECU Keihin/Honda não publica o STFT real no PID genérico 06 (comum em Hondas
   com sonda A/F), ou ele está travado no limite. Sozinho, **não prova** mistura pobre.
2. **A sonda A/F é contraditória:** a palavra de λ diz pobre (1,12) e a corrente de bombeamento diz estequiométrico (≈ 0 mA).
   A corrente é a medida física; a escala da palavra λ neste ECU é suspeita.
3. **O MAF está baixo para o ar que o motor respira.** Pelo cálculo de densidade (MAP 28 kPa, 750 rpm, 1,8 L, ar a 48 °C,
   eficiência volumétrica 60–80 %), o motor admite 2,1–2,8 g/s, mas o MAF mede 1,34 g/s, ou seja **35–50 % a menos**.
   Esse é o sinal mais forte: **ar entrando depois do MAF** (não medido) ou **MAF lendo baixo**.
4. **A borboleta comandada em ~1 % reforça a hipótese:** se entra ar por fora, a ECU fecha a borboleta para segurar a
   marcha lenta.
5. **Pós-catalisador em 0,02 V** indica escape pobre (ou sonda/aquecedor ruim). Com os códigos recém-apagados, a ECU
   ainda não teve ciclos para acusar P0171 / P0137 / P0141.

## Palpite (do mais para o menos provável)

1. **Entrada falsa de ar introduzida na troca do corpo de borboleta**:
   junta ou anel de vedação do corpo reaproveitado, torto ou faltando; parafusos sem aperto em cruz;
   **duto de admissão (entre o MAF e a borboleta) mal encaixado ou com abraçadeira frouxa**; mangueira de
   respiro/PCV ou do servofreio esquecida solta ou trincada durante o serviço.
2. **MAF lendo baixo**: sujo, conector mexido, ou trincado/contaminado durante a desmontagem.
3. **O PID 06 deste ECU não é confiável** (valor fixo). Mesmo assim, as evidências 3–5 continuam apontando para o item 1.

## O que fazer

1. Conferir **junta e parafusos da borboleta** e **todas as abraçadeiras do duto de ar** (MAF → borboleta).
2. Teste rápido com o motor em marcha lenta, quente e com o painel **ao vivo aberto**: borrifar limpa-carburador
   (ou propano apagado) em volta da junta da borboleta, do duto e das mangueiras de vácuo. Se a **rotação subir/variar**
   ou o **MAF/λ mudar** num ponto, é ali. Melhor: **teste de fumaça** em oficina.
3. Limpar o MAF com spray próprio (nunca tocar no fio).
4. Fazer o **reaprendizado da marcha lenta** depois de corrigir.
5. **Rodar 30–50 km** em uso misto: com os monitores completos, a ECU confirma (ou descarta) P0171 / P0137 / P0141, e o
   coletor compara o MAF com o esperado em várias cargas. Vazamento pesa mais em marcha lenta e quase some em carga alta;
   MAF com defeito erra proporcionalmente em todas as cargas.
6. Opcional: comparar com o Car Scanner ou um scanner Honda (HDS), que lê o STFT/LTFT proprietário.
