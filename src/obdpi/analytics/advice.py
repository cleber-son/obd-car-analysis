"""Base de conhecimento: para cada alerta, o que significa, causas prováveis, o que verificar e o que fazer.

Focada no Honda Civic 8ª geração 1.8 i-VTEC flex (R18A), câmbio manual, corpo de borboleta eletrônico (DBW).
Chaves: id da regra (R1, R4…), regra de limite (sinal:contexto, ex. "ect:running"), código DTC (P2135) ou prefixo.
"""
from __future__ import annotations

from ..obd import dtc as dtcmod

# urgência: "parar" (pare assim que possível), "logo" (resolver em dias), "acompanhar" (observar), "info"
A: dict[str, dict] = {
    # ------------------------------------------------------------------ corpo de borboleta / marcha lenta
    "R1": {
        "o_que": "A borboleta não está indo para a posição que a ECU manda (comando × posição real divergem).",
        "causas": ["Sujeira/carvão no corpo de borboleta (restrição mecânica)", "Motor elétrico da borboleta fraco ou engrenagem gasta",
                   "Mau contato no conector do corpo de borboleta (6 pinos) ou chicote danificado",
                   "Corpo de borboleta paralelo/recondicionado com calibração diferente", "Tensão baixa da bateria/alternador"],
        "verificar": ["Conector do corpo de borboleta bem encaixado e sem oxidação", "Tensão da bateria com motor ligado (13,5–14,7 V)",
                      "Se o alerta aparece mais com motor frio ou quente", "Gráfico pedal × borboleta da viagem (aba Borboleta)"],
        "fazer": ["Limpar o corpo de borboleta com limpa-TBI (motor desligado, sem forçar a borboleta com a mão)",
                  "Fazer o reaprendizado da marcha lenta (ver dica do P2176)", "Se persistir: medir sinais TPS A/B com scanner/oficina; garantia da peça nova"],
        "urgencia": "logo", "rodar": "Pode rodar, mas evite ultrapassagens: a ECU pode entrar em modo de emergência.",
    },
    "R2": {
        "o_que": "Os dois sensores do pedal do acelerador (D e E) não estão concordando entre si.",
        "causas": ["Pedal do acelerador (sensor APP) desgastado — trilha do potenciômetro gasta", "Conector do pedal com mau contato/oxidação",
                   "Chicote do pedal esmagado ou com fio partido", "Tapete prendendo o pedal (falsa leitura)"],
        "verificar": ["Conector atrás do pedal", "Tapete e curso livre do pedal", "Se coincide com P2122/P2123/P2127/P2128/P2138"],
        "fazer": ["Limpar e reencaixar o conector do pedal", "Se repetir: substituir o conjunto do pedal (sensor integrado)"],
        "urgencia": "logo", "rodar": "Pode rodar com cuidado; se a ECU detectar, limita a aceleração (modo de emergência).",
    },
    "R2t": {
        "o_que": "Os dois sensores de posição da própria borboleta (A e B) estão divergindo. É o defeito que gera o P2135.",
        "causas": ["Sensor TPS do corpo de borboleta com trilha gasta (comum em peças paralelas)", "Conector do corpo de borboleta oxidado/frouxo",
                   "Chicote com fio partido próximo ao motor (vibração)", "Aterramento ruim do motor/ECU"],
        "verificar": ["Conector do corpo de borboleta", "Se ocorre em vibração/buracos (mau contato) ou em posição fixa (trilha)",
                      "Garantia da borboleta recém-trocada"],
        "fazer": ["Reencaixar/limpar o conector com limpa-contato", "Checar aterramentos do bloco e da carroceria",
                  "Se persistir: trocar o corpo de borboleta (de preferência original Honda/Hitachi) e fazer o reaprendizado"],
        "urgencia": "logo", "rodar": "Se o P2135 aparecer, o carro entra em modo de emergência (borboleta fixa ~6%). Rode só até a oficina.",
    },
    "R3": {
        "o_que": "Você moveu o pedal mas a borboleta ficou parada. Sinal típico de modo de emergência (limp mode).",
        "causas": ["ECU detectou falha no sistema DBW e travou a borboleta na posição de segurança", "Borboleta presa mecanicamente (carvão/gelo)",
                   "Motor da borboleta queimado ou fusível do ETCS"],
        "verificar": ["Códigos de falha ativos (aba Alertas)", "Se a luz de injeção acendeu", "Fusível do ETCS/FI na caixa do motor"],
        "fazer": ["Parar em local seguro, desligar e religar o motor (às vezes sai do modo de emergência)",
                  "Ler/registrar os DTCs antes de apagar", "Levar para diagnóstico do sistema ETCS"],
        "urgencia": "parar", "rodar": "Desempenho muito limitado. Dirija só o necessário até um local seguro/oficina.",
    },
    "R3b": {"alias": "R3"},
    "R4": {
        "o_que": "A marcha lenta está subindo e descendo sozinha (oscilando).",
        "causas": ["Entrada falsa de ar: junta do corpo de borboleta (muito comum após troca), mangueiras de vácuo, PCV, coletor",
                   "Marcha lenta não reaprendida após troca/limpeza da borboleta ou bateria desconectada",
                   "Corpo de borboleta sujo", "Sensor MAP/MAF sujo", "Vela/bobina falhando", "Válvula EGR/purga do cânister presa aberta"],
        "verificar": ["Assobio de vácuo perto da borboleta com motor ligado", "Ajuste de combustível alto (positivo) em marcha lenta",
                      "Se oscila com ar-condicionado ligado/desligado", "Se melhora com motor bem quente"],
        "fazer": ["Conferir a junta e os parafusos do corpo de borboleta (reaperto em cruz)",
                  "Procedimento de reaprendizado: ignição ON 2 s sem dar partida, ligar o motor sem tocar no pedal e deixar em marcha lenta "
                  "com tudo desligado (A/C, faróis, ventoinha) até a ventoinha do radiador ligar e desligar; depois 10 min de marcha lenta",
                  "Teste de fumaça para achar entrada falsa de ar", "Limpar MAF/MAP com spray próprio"],
        "urgencia": "logo", "rodar": "Pode rodar; risco de o motor morrer em paradas.",
    },
    "R5": {"alias": "R4", "o_que": "A marcha lenta está irregular (vibrando/variando sem ritmo)."},
    "R5b": {
        "o_que": "A marcha lenta está fora do alvo (muito alta ou muito baixa) com o motor quente. Alvo do R18 quente: ~650–800 rpm.",
        "causas": ["Alta: entrada falsa de ar, marcha lenta não reaprendida, borboleta sem fechar totalmente",
                   "Baixa: corpo de borboleta sujo, carga elétrica alta, vela/bobina fraca", "Sensor de temperatura do motor (ECT) lendo errado"],
        "verificar": ["Temperatura do motor no painel (precisa estar acima de 80 °C)", "Ajuste de combustível em marcha lenta"],
        "fazer": ["Fazer o reaprendizado da marcha lenta", "Conferir entradas de ar", "Limpar o corpo de borboleta"],
        "urgencia": "acompanhar", "rodar": "Pode rodar normalmente.",
    },
    "R6": {
        "o_que": "O motor morreu.",
        "causas": ["Em marcha lenta: marcha lenta baixa/instável, borboleta suja, entrada falsa de ar, falha de combustível (bomba, filtro)",
                   "Na arrancada (câmbio manual): soltar a embreagem rápido demais — geralmente não é defeito"],
        "verificar": ["Se aconteceu parado (defeito) ou arrancando (motorista)", "Marcha lenta nos minutos anteriores (aba Borboleta)",
                      "Se houve queda de tensão da bateria junto"],
        "fazer": ["Se repetir parado: reaprendizado da marcha lenta, limpeza da borboleta, checar entrada de ar e pressão de combustível"],
        "urgencia": "logo", "rodar": "Se morre parado com frequência, é risco em cruzamentos. Resolva logo.",
    },
    "R9": {
        "o_que": "Um sensor teve um salto instantâneo e voltou (pico falso). Típico de mau contato.",
        "causas": ["Conector frouxo/oxidado (borboleta ou pedal)", "Fio com isolamento rompido encostando na massa", "Interferência por aterramento ruim"],
        "verificar": ["Se acontece em buracos/lombadas", "Conectores e chicote próximos ao motor"],
        "fazer": ["Limpar conectores com limpa-contato e reencaixar", "Revisar aterramentos (bloco, cabeçote, carroceria)"],
        "urgencia": "acompanhar", "rodar": "Pode rodar; se evoluir, vira DTC e modo de emergência.",
    },

    # ------------------------------------------------------------------ limites absolutos
    "ect:running": {
        "o_que": "Temperatura do motor acima do normal (o normal é 85–98 °C).",
        "causas": ["Nível de líquido de arrefecimento baixo / vazamento", "Ventoinha do radiador não ligando (relé, fusível, sensor, motor)",
                   "Válvula termostática travada fechada", "Radiador entupido/sujo por fora", "Bomba d'água com defeito", "Junta do cabeçote (bolhas no reservatório)"],
        "verificar": ["Nível do reservatório (com motor FRIO)", "Se a ventoinha liga perto de 98–100 °C", "Vazamentos/manchas sob o carro", "Mangueiras quentes/duras"],
        "fazer": ["Desligar o A/C e ligar o aquecedor no máximo (tira calor do motor)", "Se passar de 108 °C: parar e desligar o motor",
                  "Nunca abrir a tampa do radiador quente", "Completar com aditivo Honda Type 2 (azul) diluído conforme o manual"],
        "urgencia": "parar", "rodar": "Superaquecer pode empenar o cabeçote. Acima de 105 °C pare.",
    },
    "batt:running": {
        "o_que": "A tensão com o motor ligado está fora da faixa de carga (13,5–14,7 V).",
        "causas": ["Abaixo: alternador fraco, regulador com defeito, correia do alternador patinando, cabos/terminais oxidados",
                   "Acima: regulador de tensão do alternador com defeito (sobrecarga, ferve a bateria)"],
        "verificar": ["Correia (tensão, trincas, chiado)", "Terminais da bateria limpos e apertados", "Tensão em marcha lenta com faróis e A/C ligados"],
        "fazer": ["Teste de alternador e bateria numa autoelétrica (gratuito em muitas lojas)", "Limpar terminais", "Trocar correia/regulador conforme o teste"],
        "urgencia": "logo", "rodar": "Tensão baixa: a bateria vai descarregar e o carro pode apagar. Tensão alta: risco à bateria e eletrônicos.",
    },
    "ecu_volt:running": {"alias": "batt:running"},
    "batt:off": {
        "o_que": "A bateria está baixa com o motor desligado (saudável: 12,4–12,7 V).",
        "causas": ["Bateria velha (mais de 3–4 anos)", "Consumo parasita (algo ficando ligado: luz, som, o próprio OBD/Pi)",
                   "Carro muito tempo parado", "Alternador não carregando direito"],
        "verificar": ["Idade da bateria", "Se o adaptador OBD/Pi ficam ligados com o carro desligado", "Tensão de carga com o motor ligado"],
        "fazer": ["Teste de bateria (condutância) numa autoelétrica", "Se o carro fica parado dias, desconectar o OBD", "Trocar a bateria se reprovada"],
        "urgencia": "logo", "rodar": "Risco de não dar partida na próxima vez.",
    },
    "stft1:warm": {
        "o_que": "A ECU está corrigindo muito a mistura de combustível (ajuste curto extremo).",
        "causas": ["Positivo (falta combustível/sobra ar): entrada falsa de ar (junta da borboleta, mangueiras, PCV), bico entupido, bomba/filtro fraco, MAF sujo",
                   "Negativo (sobra combustível): bico pingando, pressão alta, sonda lambda ruim, cânister saturado",
                   "Flex: logo após trocar de combustível a ECU ainda está se adaptando (normal por alguns km)"],
        "verificar": ["Se abasteceu recentemente com outro combustível", "Assobio de vácuo", "Sonda de faixa larga (λ) perto de 1,00 em marcha lenta quente"],
        "fazer": ["Procurar entrada falsa de ar: junta do corpo de borboleta, duto de admissão e mangueiras de vácuo (teste com limpa-carburador ou fumaça)",
                  "Se acabou de trocar de combustível (gasolina ↔ etanol), rodar 20–30 km antes de concluir",
                  "Limpeza dos bicos e do MAF", "Medir a pressão de combustível"],
        "urgencia": "logo", "rodar": "Pode rodar; mistura muito pobre por muito tempo aquece o motor e o catalisador.",
    },
    "ltft1:warm": {"alias": "stft1:warm"},
    "fuel_trim_total": {"alias": "stft1:warm"},
    "iat:running": {
        "o_que": "O ar admitido está muito quente.",
        "causas": ["Trânsito parado em dia quente (normal ficar alto)", "Tomada de ar puxando ar do cofre", "Sensor IAT com defeito"],
        "verificar": ["Se baixa ao andar", "Mangueira/caixa do filtro de ar bem encaixadas"],
        "fazer": ["Se só ocorre parado no calor, é normal", "Se ocorre rodando: revisar a admissão e o sensor"],
        "urgencia": "acompanhar", "rodar": "Pode rodar; perde um pouco de potência.",
    },
    "rpm:idle_warm": {"alias": "R5b"},
    "rpm:running": {
        "o_que": "A rotação passou do limite de corte.",
        "causas": ["Redução de marcha errada (ex.: 4ª → 2ª em alta)", "Embreagem patinando"],
        "verificar": ["Se houve redução brusca"], "fazer": ["Evite reduções que levem acima de 6800 rpm"],
        "urgencia": "acompanhar", "rodar": "Pode rodar.",
    },
    "map:idle_warm": {
        "o_que": "Pouco vácuo no coletor em marcha lenta (MAP alto).",
        "causas": ["Entrada falsa de ar", "Borboleta não fechando", "Falha de ignição/compressão baixa", "Sincronismo (corrente de comando)"],
        "verificar": ["Ajuste de combustível", "Marcha lenta irregular", "Falhas de ignição"],
        "fazer": ["Teste de fumaça", "Teste de compressão se persistir"],
        "urgencia": "logo", "rodar": "Pode rodar; investigue.",
    },
    "timing:idle_warm": {
        "o_que": "Avanço de ignição negativo em marcha lenta (a ECU está atrasando muito o ponto).",
        "causas": ["ECU segurando a marcha lenta (compensação)", "Detonação (combustível ruim)", "Sensor de detonação"],
        "verificar": ["Qualidade do combustível", "Marcha lenta oscilando ao mesmo tempo"],
        "fazer": ["Abastecer em posto confiável", "Reaprendizado da marcha lenta"],
        "urgencia": "acompanhar", "rodar": "Pode rodar.",
    },
    "load:idle_warm": {"alias": "map:idle_warm", "o_que": "Carga do motor alta em marcha lenta."},
    "afr_extreme": {
        "o_que": "A sonda de faixa larga mostra a mistura longe do ideal (λ 1,00) com o motor quente em malha fechada.",
        "causas": ["Pobre (λ > 1): entrada falsa de ar, bico entupido, pressão de combustível baixa",
                   "Rica (λ < 1): bico pingando, pressão alta, filtro de ar entupido", "Sonda A/F com defeito ou aquecedor queimado"],
        "verificar": ["Ajuste de combustível ao mesmo tempo", "Se o lambda comandado também está longe de 1"],
        "fazer": ["Teste de fumaça", "Limpeza de bicos", "Testar a sonda A/F"],
        "urgencia": "logo", "rodar": "Pode rodar; consumo e emissões pioram.",
    },
    "o2_stuck": {"alias": "afr_extreme", "o_que": "A sonda lambda não está oscilando como deveria."},
    "cat_b1s1:running": {
        "o_que": "Catalisador muito quente.",
        "causas": ["Falha de ignição jogando combustível no escape", "Mistura muito rica", "Subidas longas em alta carga"],
        "verificar": ["Falhas de ignição (P0300–P0304)", "Ajuste de combustível negativo"],
        "fazer": ["Resolver falhas de ignição imediatamente (danifica o catalisador)"],
        "urgencia": "logo", "rodar": "Reduza a carga.",
    },
    "fuel_level:any": {"o_que": "Combustível na reserva.", "causas": ["Tanque baixo"], "verificar": [], "fazer": ["Abastecer e tocar em ⛽ Tanque cheio no painel"],
                       "urgencia": "info", "rodar": "Rodar com o tanque no fim prejudica a bomba de combustível."},
    "missing": {
        "o_que": "Um sensor que respondia parou de responder pelo OBD.",
        "causas": ["Falha momentânea de comunicação Bluetooth/adaptador", "ECU deixou de informar aquele PID em certa condição", "Sensor/circuito com defeito (aparece DTC)"],
        "verificar": ["Se há DTC relacionado", "Se voltou sozinho"], "fazer": ["Se voltar sozinho, ignore", "Se persistir com DTC, diagnosticar o circuito"],
        "urgencia": "acompanhar", "rodar": "Pode rodar.",
    },
    "z": {
        "o_que": "Este sensor está fora do comportamento habitual do SEU carro (comparado ao histórico aprendido nas mesmas condições).",
        "causas": ["Início de um defeito no sistema relacionado", "Mudança de combustível (gasolina/etanol)", "Condição incomum (calor extremo, subida longa, carga)"],
        "verificar": ["Gráfico do sensor (toque nele) nos últimos 7 dias", "Se coincide com abastecimento ou manutenção"],
        "fazer": ["Se repetir em várias viagens, investigar o sistema do sensor", "Anotar no diário de manutenção qualquer serviço feito"],
        "urgencia": "acompanhar", "rodar": "Pode rodar.",
    },
    "MIL": {
        "o_que": "A luz de injeção (check engine) acendeu: a ECU confirmou uma falha.",
        "causas": ["Veja o código de falha ativo na aba Alertas"],
        "verificar": ["Código e descrição", "Se a luz está piscando (falha de ignição grave: reduzir velocidade)"],
        "fazer": ["Não apague o código antes de diagnosticar", "Se piscando: rodar devagar até a oficina"],
        "urgencia": "logo", "rodar": "Luz fixa: pode rodar com cautela. Piscando: risco ao catalisador.",
    },
}

A.update({
    "air_leak": {
        "o_que": "O sensor de fluxo de ar (MAF) mede bem menos ar do que o motor está admitindo, principalmente em marcha lenta. É a assinatura de ar entrando depois do MAF.",
        "causas": ["Junta/anel do corpo de borboleta (muito comum logo após a troca)", "Duto de ar entre o MAF e a borboleta solto, abraçadeira frouxa ou trincado",
                   "Mangueira do servofreio, PCV ou respiro do cárter solta/trincada", "Junta do coletor de admissão", "Válvula de purga do cânister presa aberta"],
        "verificar": ["Abraçadeiras e encaixe do duto de admissão", "Junta e aperto (em cruz) dos parafusos do corpo de borboleta",
                      "Assobio com motor em marcha lenta", "Painel: MAF e λ ao borrifar limpa-carburador em volta das juntas (rotação/λ mudam no ponto do vazamento)"],
        "fazer": ["Corrigir o vazamento (junta nova do corpo de borboleta é barata)", "Teste de fumaça em oficina se não achar",
                  "Depois: reaprendizado da marcha lenta e rodar 30–50 km"],
        "urgencia": "logo", "rodar": "Pode rodar; consumo aumenta, marcha lenta pode oscilar e a ECU vai acusar P0171 com o tempo.",
    },
    "maf_low": {
        "o_que": "O MAF está medindo abaixo do esperado em todas as cargas (erro proporcional, não vazamento).",
        "causas": ["MAF sujo (óleo do filtro esportivo, poeira)", "Conector do MAF com mau contato", "MAF com defeito/paralelo", "Filtro de ar muito sujo"],
        "verificar": ["Filtro de ar", "Conector do MAF"], "fazer": ["Limpar com spray próprio para MAF (sem tocar no elemento)", "Se não melhorar, substituir o MAF"],
        "urgencia": "logo", "rodar": "Pode rodar; consumo e desempenho pioram.",
    },
    "crank_weak": {
        "o_que": "A tensão da bateria caiu muito durante a partida. É o teste mais confiável de bateria fraca.",
        "causas": ["Bateria no fim da vida (3–5 anos)", "Terminais/cabos oxidados ou frouxos", "Motor de partida puxando corrente demais", "Frio (piora a bateria)"],
        "verificar": ["Idade da bateria", "Terminais e cabo massa", "Se a partida está mais lenta"],
        "fazer": ["Teste de bateria (condutância) em autoelétrica", "Limpar e apertar terminais", "Trocar a bateria se reprovada"],
        "urgencia": "logo", "rodar": "Risco de não pegar na próxima partida, principalmente em dia frio.",
    },
    "thermostat": {"alias": "P0128"},
    # ------------------------------------------------------------------ GPS (docs/GPS-ARQUITETURA.md, seções 9.6 e 9.7)
    "gps:clutch_slip": {
        "o_que": "Com o pé no acelerador numa marcha alta, a rotação subiu mais do que a velocidade real (medida pelo GPS) explica, "
                 "e voltou ao normal na mesma marcha. O motor girou mais que as rodas: a embreagem escorregou.",
        "causas": ["Disco de embreagem gasto (o mais comum depois de muitos quilômetros)",
                   "Platô (mola da embreagem) fraco", "Óleo ou graxa no disco (retentor do virabrequim ou do câmbio vazando)",
                   "Embreagem sem folga no pedal ou cabo/hidráulico prendendo (o pedal não volta todo)",
                   "Hábito de dirigir com o pé apoiado no pedal da embreagem"],
        "verificar": ["Se o pedal da embreagem volta inteiro e tem folga no começo do curso",
                      "Se o carro 'perde força' em subida em 4ª ou 5ª com o giro subindo sem o carro acelerar",
                      "Teste parado (com cuidado e freio de mão puxado): 3ª engatada, soltar a embreagem devagar com o motor a ~1.500 rpm — o motor deveria morrer",
                      "Mancha de óleo embaixo, entre o motor e o câmbio", "Quantas vezes o aviso apareceu (aba Mapa → Desempenho)"],
        "fazer": ["Levar a uma oficina de confiança para avaliar o conjunto de embreagem (disco, platô e rolamento)",
                  "Evitar arrancadas fortes e subidas pesadas em marcha alta até o conserto",
                  "Se houver vazamento de óleo, corrigir o retentor junto com a troca, ou o disco novo também patina"],
        "urgencia": "logo", "rodar": "Pode rodar com calma, evitando acelerar fundo em marcha alta; a patinação piora rápido e pode deixar o carro sem tração numa subida.",
    },
    "gps:moved_off": {
        "o_que": "Na partida, o GPS mostrou o carro longe de onde ele ficou estacionado, e o que o carro andou com o motor ligado não explica a diferença.",
        "causas": ["Guincho (estacionamento proibido, pátio)", "Manobra por terceiros (manobrista, oficina, lava-rápido, família)",
                   "Furto ou tentativa de furto", "GPS sem posição boa no último estacionamento (raro: o aviso exige posição válida)"],
        "verificar": ["No mapa do painel, onde o carro foi deixado (Mapa → Estacionado) e onde ele está agora",
                      "Se alguém da família ou uma oficina usou o carro", "Sinais de arrombamento (fechaduras, vidros, ignição)"],
        "fazer": ["Se ninguém conhecido usou o carro: registrar boletim de ocorrência e guardar o horário e as posições do painel",
                  "Se foi guincho: procurar o pátio do órgão de trânsito da cidade"],
        "urgencia": "logo", "rodar": "Confira o carro antes de rodar.",
    },
    # F9: modo sempre ligado (power.always_on), seção 9.7
    "gps:park_guard": {
        "o_que": "Com o motor desligado, o GPS viu o carro sair de onde estava estacionado (mais de 60 m, ou andando acima de 8 km/h por 10 s).",
        "causas": ["Guincho", "Manobra por terceiros (manobrista, oficina, família)", "Furto ou tentativa de furto"],
        "verificar": ["Onde o carro está agora: Mapa → Agora no painel (a posição sobe para a nuvem a cada 5 s enquanto dura o aviso)",
                      "Se alguém da família ou uma oficina está com o carro"],
        "fazer": ["Se ninguém conhecido está com o carro: acionar a polícia com o horário e a posição do painel"],
        "urgencia": "logo", "rodar": "Confira o carro antes de rodar.",
    },
    "power:low_batt": {
        "o_que": "No modo sempre ligado, a bateria ficou abaixo do limite configurado por 10 minutos com o carro desligado; o Pi se desliga para não descarregar a bateria.",
        "causas": ["Carro parado muitos dias com o Pi ligado", "Bateria fraca ou alternador carregando pouco"],
        "verificar": ["Tensão da bateria na partida seguinte (aba Ao vivo)", "Há quantos dias o carro está parado"],
        "fazer": ["Dar uma volta de 20 min para recarregar", "Se repetir com poucos dias parado: testar a bateria"],
        "urgencia": "acompanhar", "rodar": "Pode rodar normalmente.",
    },
})

DTC: dict[str, dict] = {
    "P2135": {"alias": "R2t"}, "P2138": {"alias": "R2"}, "P2122": {"alias": "R2"}, "P2123": {"alias": "R2"}, "P2127": {"alias": "R2"}, "P2128": {"alias": "R2"},
    "P2101": {"alias": "R1"}, "P2118": {"alias": "R1"}, "P2119": {"alias": "R3"}, "P2100": {"alias": "R3"}, "P2111": {"alias": "R3"}, "P2112": {"alias": "R3"},
    "P0505": {"alias": "R4"}, "P0506": {"alias": "R5b"}, "P0507": {"alias": "R5b"},
    "P0121": {"alias": "R2t"}, "P0122": {"alias": "R2t"}, "P0123": {"alias": "R2t"}, "P0221": {"alias": "R2t"}, "P0222": {"alias": "R2t"}, "P0223": {"alias": "R2t"},
    "P2176": {
        "o_que": "A ECU não aprendeu a posição de marcha lenta da borboleta (comum após troca/limpeza da borboleta ou bateria desconectada).",
        "causas": ["Reaprendizado não feito após a troca do corpo de borboleta", "Bateria desconectada"],
        "verificar": ["Se a borboleta foi trocada/limpa recentemente (sim, no seu caso)"],
        "fazer": ["Reaprendizado: ignição ON 2 s sem partida → ligar sem tocar no pedal → marcha lenta com A/C, faróis e cargas desligados "
                  "até a ventoinha ligar e desligar → mais 10 min de marcha lenta → desligar 10 s", "Depois, apagar o código e rodar"],
        "urgencia": "logo", "rodar": "Pode rodar; marcha lenta pode ficar instável até o reaprendizado.",
    },
    "P0171": {"alias": "stft1:warm"}, "P0172": {"alias": "stft1:warm"}, "P2195": {"alias": "afr_extreme"}, "P2196": {"alias": "afr_extreme"},
    "P0116": {"alias": "ect:running"}, "P0128": {
        "o_que": "O motor demora a atingir a temperatura de trabalho.", "causas": ["Válvula termostática travada aberta (muito comum)", "Sensor ECT"],
        "verificar": ["Gráfico de temperatura: quanto tempo leva para passar de 80 °C"], "fazer": ["Trocar a válvula termostática (peça barata)"],
        "urgencia": "acompanhar", "rodar": "Pode rodar; consumo aumenta."},
    "P0300": {"o_que": "Falhas de ignição detectadas.", "causas": ["Vela gasta", "Bobina com defeito", "Bico entupido", "Entrada de ar", "Compressão baixa"],
              "verificar": ["Qual cilindro (P0301–P0304)", "Se a luz de injeção pisca"], "fazer": ["Trocar velas (NGK iridium a cada ~100 mil km)", "Trocar a bobina do cilindro indicado (dá para inverter bobinas para testar)"],
              "urgencia": "logo", "rodar": "Luz piscando: reduza a carga, risco ao catalisador."},
    "P0420": {"o_que": "Catalisador com eficiência baixa.", "causas": ["Catalisador desgastado", "Sonda pós-catalisador com defeito", "Vazamento no escape"],
              "verificar": ["Vazamentos no escape"], "fazer": ["Diagnóstico das sondas antes de trocar o catalisador"], "urgencia": "acompanhar", "rodar": "Pode rodar."},
    "P0562": {"alias": "batt:running"}, "P0563": {"alias": "batt:running"},
}
for _c in ("P0301", "P0302", "P0303", "P0304"):
    DTC[_c] = {"alias": "P0300"}

URG = {"parar": "Pare assim que for seguro", "logo": "Resolver nos próximos dias", "acompanhar": "Acompanhar", "info": "Informativo"}


def _resolve(d: dict | None, depth: int = 0) -> dict | None:
    if not d:
        return None
    if "alias" in d and depth < 4:
        base = A.get(d["alias"]) or DTC.get(d["alias"])
        base = _resolve(base, depth + 1) or {}
        return {**base, **{k: v for k, v in d.items() if k != "alias"}}
    return d


def advice_for(rule: str, source: str = "") -> dict | None:
    rule = rule or ""
    d = None
    if rule in DTC:
        d = DTC[rule]
    elif rule in A:
        d = A[rule]
    else:
        head = rule.split(":")[0]
        d = A.get(head) or (A.get(f"{rule.split(':')[0]}:{rule.split(':')[1]}") if ":" in rule else None)
        if not d and rule.startswith("z:"):
            d = A["z"]
        if not d and rule.startswith("missing:"):
            d = A["missing"]
        if not d and rule.startswith("R9"):
            d = A["R9"]
    d = _resolve(d)
    if not d and rule[:1] in "PCBU" and len(rule) == 5:
        d = {"o_que": dtcmod.describe(rule), "causas": ["Consultar o manual de serviço Honda para este código"],
             "verificar": ["Freeze frame do código (condições no momento da falha)"], "fazer": ["Diagnóstico em oficina com scanner Honda (HDS)"],
             "urgencia": "logo", "rodar": "Depende do código; se a luz piscar, reduza a carga."}
    if d:
        d = dict(d)
        d["urgencia_txt"] = URG.get(d.get("urgencia", ""), "")
    return d
