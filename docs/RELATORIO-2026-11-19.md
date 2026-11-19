## Veredito
🟡 **Atenção: mistura pobre em marcha lenta com assinatura clara de entrada falsa de ar**, muito provavelmente ligada à troca recente do corpo de borboleta. O resto do carro (carga da bateria, arrefecimento, sondas, sensores da borboleta) está saudável. Não há código de falha registrado, mas os autotestes da ECU ainda não terminaram (códigos apagados há só 8 km).

Base: volta de 18/11 (19:06–21:12, 2 h 06, 7,5 km, 70 % do tempo parado com o motor ligado) e hoje em marcha lenta. 6.774 leituras rápidas e 837 varreduras completas.

## Pontos de atenção

### 1. Entrada falsa de ar depois do MAF (prioridade alta)
**O que os dados mostram**
- **MAF ÷ ar esperado pelo motor:** 41 % em marcha lenta; 63 % em carga leve; 80 % em carga média; 86 % em carga alta. O típico fica entre 55 e 100 %. A diferença some conforme a carga aumenta, e é exatamente isso que um vazamento de vácuo faz. Um MAF com defeito erraria igual em todas as cargas.
- **Ajuste curto de combustível:** cravado em **+46,9 % em marcha lenta**, que é o limite máximo de correção da ECU. Rodando ele varia normalmente, de −14 % a +47 %. A ECU está colocando combustível a mais e, parada, não consegue compensar tudo.
- **Sonda de faixa larga em marcha lenta:** λ 1,11–1,14 (pobre) contra 0,98 comandado. Rodando, a média fica a +0,03 do alvo, ou seja, quase normal.
- **Sonda pós-catalisador:** média de 0,18 V, quase sempre "pobre". Ela chegou a 0,92 V, então a sonda funciona; o escape é que está pobre.
- **Borboleta em marcha lenta:** a ECU a mantém quase fechada, com comando de 1,1 %. É o que ela faz quando entra ar por outro lugar.

**Causas mais prováveis, em ordem**
1. Junta ou anel de vedação do corpo de borboleta reaproveitado, torto ou faltando, ou parafusos sem aperto em cruz.
2. Duto de admissão entre o MAF/filtro e a borboleta mal encaixado, com abraçadeira frouxa ou trincado.
3. Mangueira do servofreio, da PCV ou de respiro solta ou trincada na desmontagem.
4. Junta do coletor de admissão. É menos provável.

**Como confirmar:** com o motor quente em marcha lenta e o painel ao vivo aberto, borrife limpa-carburador em volta da junta da borboleta, do duto e das mangueiras de vácuo. Se a **rotação subir** ou o **MAF/λ mudar** num ponto, o vazamento está ali. Um teste de fumaça na oficina resolve se não achar.

**Urgência:** resolver nos próximos dias. Pode rodar, mas o consumo piora, a marcha lenta oscila e a ECU deve acusar **P0171** quando completar os autotestes.

### 2. Ar admitido muito quente (IAT até 77 °C, média 62 °C)
- **Provável causa:** muito tempo parado com o motor ligado (52 min dos 2 h 06), possivelmente em local fechado ou com o capô aquecendo. Rodando, a temperatura cai.
- **O que verificar:** se a tomada de ar do filtro está encaixada e puxando ar de fora do cofre.
- **Urgência:** acompanhar. Rodando normalmente deve ficar perto da temperatura ambiente + 10–20 °C.

### 3. Temperatura do motor no limite alto (pico de 103 °C)
- **O que os dados mostram:** em marcha lenta longa, a temperatura subia até 100–101 °C e caía para 98 °C. Isso mostra a **ventoinha ligando e desligando**, que é normal. O pico foi de 103 °C.
- **O que fazer:** conferir o nível do reservatório **com o motor frio**. Se o pico se repetir rodando, revise ventoinha e radiador.
- **Urgência:** acompanhar.

### 4. Sensores da borboleta A/B: 4 avisos (observar)
- **Situação:** a relação entre os dois sensores mudou em acelerações fortes. A ECU, que mede isso com precisão, **não registrou P2135**, então provavelmente é uma limitação do meu modelo, não defeito.
- **O que fazer:** acompanhar. Se o P2135 aparecer, revise o conector e a garantia da peça nova.

## Sistema por sistema
- **Motor e marcha lenta:** 774 rpm em média, normal. Houve oscilação pontual, ligada à mistura pobre. O avanço ficou entre −10° e +38°, com média de 9°; os valores negativos aparecem quando a ECU segura a marcha lenta.
- **Mistura e combustível:** pobre em marcha lenta (ponto 1). Rodando, está próxima do ideal.
- **Corpo de borboleta e pedal:**
  - A borboleta e o comando seguem juntos.
  - Os dois sensores do pedal (D e E) estão coerentes.
  - Não houve travamento nem modo de emergência.
  - Houve 2 picos de sinal: vale conferir o conector se isso se repetir.
- **Arrefecimento:** a ventoinha funciona, mas a temperatura fica no limite alto em marcha lenta longa (ponto 3).
- **Bateria e carga:** 13,8–14,4 V com o motor ligado: alternador e regulador OK. A queda na partida não foi medida, porque o Pi liga depois da partida.
- **Sondas e catalisador:** a sonda de faixa larga responde, e a pós-catalisador oscila de 0,00 a 0,92 V, então funciona.
- **Consumo e direção:**
  - **Consumo da volta:** 5,5 km/L, que não é representativo, porque 70 % do tempo foi parado.
  - **Nota de direção econômica:** 84, com penalidade só pelo tempo parado ligado.
  - **Ritmo:** 1 arrancada brusca e nenhuma freada brusca.

## O que comprar e trocar
| Item | Por quê | Prioridade |
|---|---|---|
| **Junta do corpo de borboleta** (original Honda, para R18 1.8) | Principal suspeita do vazamento; é barata e deve ser trocada sempre que o corpo é removido | Alta |
| Abraçadeiras do duto de admissão, e o duto se estiver trincado | Segunda suspeita | Alta (inspecionar) |
| Mangueiras de vácuo, PCV e servofreio que estiverem ressecadas | Possível ponto de vazamento | Média (inspecionar) |
| Spray limpa-MAF | Limpeza preventiva. Os dados **não** indicam MAF com defeito, porque com carga ele mede 86 % | Baixa |
| **Conversor 12 V → 5 V de 3 A** para o Raspberry | O Pi reinicia na partida (subtensão) | Alta, para o sistema |
| Velas: NGK IZFR6K13 ou Denso SKJ20DR-M13 (conferir no manual) | Só se estiverem perto de ~100 mil km desde a última troca; os dados não mostram falha de ignição | Conforme a quilometragem |

**Não troque às cegas:** MAF, sondas lambda, corpo de borboleta (é novo), bateria ou alternador. Os dados mostram esses itens funcionando.

## O que fazer agora
1. Conferir ou trocar a **junta do corpo de borboleta** e o **aperto em cruz** dos parafusos.
2. Conferir **abraçadeiras e duto** entre o filtro/MAF e a borboleta, e as **mangueiras de vácuo**.
3. Fazer o teste com limpa-carburador e o painel ao vivo aberto.
4. Fazer o **reaprendizado da marcha lenta**:
   - ignição ligada 2 s, sem dar partida;
   - dar partida sem tocar no pedal;
   - deixar em marcha lenta, com tudo desligado, até a ventoinha ligar e desligar;
   - deixar mais 10 min em marcha lenta.
5. Rodar de **30 a 50 km** em uso normal, com menos tempo parado ligado. Depois compare o diagnóstico **Ar × MAF**: a marcha lenta deve passar de 55 % e o ajuste curto deve sair do limite.
6. Instalar o conversor 12 V→5 V no Pi.

## Limitações dos dados
- **Volta pouco representativa:** foram 7,5 km, com 70 % parado. Consumo e direção precisam de uso mais normal.
- **Autotestes incompletos:** a ECU só informou 1 monitor. Os códigos foram apagados há 8 km, então ela ainda não confirmou falhas como P0171.
- **Sem ajuste longo:** a ECU não informa o ajuste longo de combustível (PID 07), então não dá para ver a correção acumulada.
- **Bateria na partida:** não foi medida, porque o Pi liga depois da partida. Com alimentação permanente e sensor de ignição isso passa a ser possível.
- **Valores calculados:** o "ar esperado" supõe uma eficiência volumétrica típica. A conclusão vem da **diferença entre as cargas**, não do valor absoluto.

_Relatório gerado a partir dos dados do Pi em 19/11/2026._
