/* Diagnóstico geral: um passo a passo do carro inteiro (conversa + inspeção visual + medições ao vivo pelo OBD) que
   termina num relatório: o que provavelmente está ruim, o que trocar ou verificar, o motivo e a urgência.
   Três partes neste arquivo:
     1. catálogo de perguntas e de achados (dados);
     2. avaliar(): regras puras, sem tela (rodam também no Node: cloud/test/checkup.test.mjs);
     3. o passo a passo, que usa as peças dos testes guiados (window.obdTestKit, de tests.js), e o relatório.
   Nada aqui afirma defeito: todo achado sai como "provável", com as evidências que levaram a ele. */
(() => {
  "use strict";
  const nf = (v, d = 0) => (v == null || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sg = (v, d = 0) => v == null ? "—" : (v > 0 ? "+" : "") + nf(v, d);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const mean = (a) => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const sd = (a) => { if (a.length < 2) return 0; const m = mean(a); return Math.sqrt(mean(a.map((x) => (x - m) ** 2))); };
  const r1 = (x, n = 1) => x == null || Number.isNaN(x) ? null : Math.round(x * 10 ** n) / 10 ** n;

  // =================================================================================================================
  // 1. Perguntas. Cada opção: [valor, texto, achado]. A PRIMEIRA opção é sempre a "sem problema".
  //    g: "insp" = olhar o carro (motor frio e desligado) · "sint" = como o carro anda (respondido enquanto o motor aquece)
  // =================================================================================================================
  const Q = [
    { id: "oleo_nivel", g: "insp", sis: "Motor", q: "Nível do óleo do motor", how: "Carro no plano, motor desligado há pelo menos 5 minutos. Puxe a <b>vareta</b> (argola amarela ou laranja), limpe com papel, enfie até o fim e puxe de novo. O óleo deve ficar <b>entre as duas marcas</b> da ponta.",
      o: [["ok", "Entre as duas marcas"], ["baixo", "Abaixo da marca de baixo", "oleo_baixo"], ["alto", "Acima da marca de cima", "oleo_alto"]] },
    { id: "oleo_asp", g: "insp", sis: "Motor", q: "Como está o óleo na vareta?", how: "Esfregue uma gota entre os dedos e cheire.",
      o: [["ok", "Cor de mel até preto, liso"], ["leitoso", "Cor de café com leite ou maionese", "oleo_leitoso"], ["gas", "Bem fino, com cheiro de gasolina", "oleo_gasolina"], ["areia", "Áspero, com grãos ou borra", "oleo_borra"]] },
    { id: "agua", g: "insp", sis: "Arrefecimento", q: "Líquido de arrefecimento", how: "⚠️ <b>Nunca abra a tampa do radiador com o motor quente.</b> Olhe só o <b>reservatório plástico</b> translúcido ao lado do radiador: o nível deve ficar entre <b>MIN e MAX</b>.",
      o: [["ok", "Entre MIN e MAX"], ["baixo", "Abaixo do MIN", "agua_baixa"], ["vazio", "Reservatório vazio", "agua_vazia"]] },
    { id: "agua_asp", g: "insp", sis: "Arrefecimento", q: "Cor do líquido de arrefecimento", how: "Olhe através do plástico do reservatório, com a lanterna.",
      o: [["ok", "Limpo (verde, azul ou rosa)"], ["ferrugem", "Marrom, cor de ferrugem", "agua_suja"], ["oleo", "Com óleo boiando ou borra", "agua_oleo"]] },
    { id: "freio_fluido", g: "insp", sis: "Freios", q: "Fluido de freio", how: "Reservatório pequeno no fundo do cofre, do lado do motorista, com o símbolo de freio na tampa. Não precisa abrir: olhe pelo plástico.",
      o: [["ok", "Entre MIN e MAX, cor clara"], ["baixo", "Abaixo do MIN", "fluido_baixo"], ["escuro", "Escuro, cor de café", "fluido_velho"]] },
    { id: "bat_vis", g: "insp", sis: "Elétrica", q: "Terminais da bateria", how: "Olhe os dois polos da bateria e tente girar cada terminal com a mão (motor desligado).",
      o: [["ok", "Limpos e firmes"], ["zinabre", "Com pó branco ou esverdeado", "bat_zinabre"], ["solto", "Algum terminal gira ou está frouxo", "bat_solto"]] },
    { id: "bat_idade", g: "insp", sis: "Elétrica", q: "Idade da bateria", how: "Costuma ter uma etiqueta com mês e ano em cima ou na lateral.",
      o: [["nova", "Menos de 3 anos"], ["media", "De 3 a 5 anos"], ["velha", "Mais de 5 anos", "bat_velha"], ["ns", "Não sei"]] },
    { id: "correia", g: "insp", sis: "Motor", q: "Correia do alternador", how: "Correia de borracha na lateral do motor (lado direito do carro). Com o motor <b>desligado</b>, olhe o lado com frisos usando a lanterna.",
      o: [["ok", "Inteira, sem trincas"], ["trinca", "Com trincas, fiapos ou pedaços faltando", "correia_ruim"], ["brilho", "Lisa e brilhante (vitrificada)", "correia_vitrificada"]] },
    { id: "chao", g: "insp", sis: "Motor", q: "Manchas no chão onde o carro dorme", how: "Olhe o piso embaixo do motor e perto das rodas.",
      o: [["ok", "Seco (ou só água limpa do ar-condicionado)"], ["oleo", "Mancha escura de óleo", "vaz_oleo"], ["agua", "Mancha verde ou rosa, meio doce", "vaz_agua"], ["fluido", "Mancha clara e fina perto de uma roda", "vaz_freio"], ["verm", "Mancha avermelhada embaixo do motor", "vaz_direcao"]] },
    { id: "pneu_sulco", g: "insp", sis: "Pneus", q: "Sulco dos pneus", how: "Dentro dos sulcos principais existem ressaltos de borracha (<b>TWI</b>, 1,6 mm). Se a banda já está na altura deles, o pneu acabou. Olhe os 4.",
      o: [["ok", "Sulco bem acima do TWI"], ["meia", "Chegando perto do TWI", "pneu_meia"], ["careca", "Na altura do TWI ou careca", "pneu_careca"]] },
    { id: "pneu_desg", g: "insp", sis: "Pneus", q: "Desgaste e laterais dos pneus", how: "Passe a mão pela banda e olhe as laterais por dentro e por fora.",
      o: [["ok", "Desgaste igual em toda a banda"], ["bordas", "Mais gasto nas duas bordas", "pneu_murcho"], ["centro", "Mais gasto no centro", "pneu_cheio"], ["lado", "Gasto só por dentro ou só por fora", "alinhamento"], ["escama", "Em ondas ou escamas", "pneu_escama"], ["bolha", "Bolha ou corte na lateral", "pneu_bolha"]] },
    { id: "amort", g: "insp", sis: "Direção e suspensão", q: "Amortecedores", how: "Empurre com força um canto do carro para baixo e solte. Repita nos 4 cantos e olhe o corpo de cada amortecedor.",
      o: [["ok", "Sobe e para logo"], ["balanca", "Fica balançando duas vezes ou mais", "amortecedor"], ["vaza", "Amortecedor molhado de óleo", "amortecedor_vaza"]] },
    { id: "luzes_ext", g: "insp", sis: "Elétrica", q: "Luzes de fora", how: "Farol baixo e alto, lanternas, setas, freio (peça ajuda ou veja o reflexo numa parede) e ré.",
      o: [["ok", "Todas acendem"], ["queimada", "Alguma não acende", "lampada"]] },

    { id: "luz_painel", g: "sint", sis: "Central", multi: true, q: "Alguma luz de aviso acesa no painel?", how: "Com o motor ligado, só deve ficar aceso o que você ligou (farol, freio de mão). Marque todas as que estiverem acesas.",
      o: [["nao", "Nenhuma"], ["inj", "Injeção (motor amarelo)", "luz_injecao"], ["oleo", "Óleo (lamparina vermelha)", "luz_oleo"], ["bat", "Bateria", "luz_bateria"], ["temp", "Temperatura", "luz_temp"], ["abs", "ABS", "luz_abs"], ["srs", "Airbag (SRS)", "luz_srs"], ["freio", "Freio (! vermelho)", "luz_freio"]] },
    { id: "partida", g: "sint", sis: "Elétrica", q: "Como o carro pega?", how: "Pense nas últimas semanas.",
      o: [["ok", "Pega na hora"], ["frio", "Demora com o motor frio", "partida_frio"], ["quente", "Demora com o motor quente", "partida_quente"], ["lenta", "O motor gira devagar, cansado", "partida_lenta"], ["clique", "Às vezes só faz clique", "partida_clique"]] },
    { id: "lenta_rel", g: "sint", sis: "Mistura e injeção", q: "Parado no semáforo, o motor…", how: "",
      o: [["ok", "Fica firme"], ["oscila", "Oscila ou treme", "rel_oscila"], ["morre", "Já morreu ou quase morre", "rel_morre"]] },
    { id: "forca", g: "sint", sis: "Mistura e injeção", q: "Força do motor", how: "",
      o: [["ok", "Normal"], ["fraco", "Fraco em subida ou ultrapassagem", "fraco"], ["engasga", "Engasga ou dá trancos ao acelerar", "engasga"]] },
    { id: "consumo", g: "sint", sis: "Mistura e injeção", q: "Consumo de combustível", how: "",
      o: [["ok", "Normal"], ["alto", "Aumentou sem motivo", "consumo_alto"]] },
    { id: "fumaca", g: "sint", sis: "Motor", q: "Fumaça no escapamento", how: "Vapor branco que some nos primeiros minutos do motor frio é normal.",
      o: [["ok", "Nenhuma (ou só vapor no frio)"], ["branca", "Branca o tempo todo", "fum_branca"], ["azul", "Azulada", "fum_azul"], ["preta", "Preta", "fum_preta"]] },
    { id: "cheiro", g: "sint", sis: "Motor", multi: true, q: "Algum cheiro diferente?", how: "Marque todos os que você sente.",
      o: [["nao", "Nenhum"], ["gas", "Gasolina ou etanol", "ch_combustivel"], ["queimado", "Queimado", "ch_queimado"], ["doce", "Adocicado (xarope)", "ch_doce"], ["ovo", "Ovo podre", "ch_ovo"]] },
    { id: "barulho", g: "sint", sis: "Direção e suspensão", multi: true, q: "Algum barulho?", how: "Marque todos os que você ouve.",
      o: [["nao", "Nenhum"], ["chiado", "Chiado na partida ou ao ligar o ar", "correia_chia"], ["tec", "Tec-tec manobrando com o volante virado", "homocinetica"], ["ronco", "Ronco que cresce com a velocidade", "rolamento"], ["buraco", "Batida seca em buraco ou lombada", "suspensao_folga"],
        ["ferro", "Ferro raspando ao frear", "pastilha_fim"], ["motor", "Batida metálica no motor, junto com o giro", "batida_motor"], ["escape", "Escapamento roncando ou estourando", "escape_furo"]] },
    { id: "vibra", g: "sint", sis: "Direção e suspensão", q: "Vibração", how: "",
      o: [["ok", "Nenhuma"], ["volante", "Volante treme entre 80 e 110 km/h", "balanceamento"], ["frear", "Treme ao frear", "disco_empenado"], ["parado", "O carro treme parado, em marcha lenta", "coxim"]] },
    { id: "freio", g: "sint", sis: "Freios", q: "Freio", how: "",
      o: [["ok", "Freia firme e reto"], ["esponja", "Pedal baixo ou esponjoso", "freio_esponjoso"], ["puxa", "Puxa para um lado ao frear", "freio_puxa"], ["chia", "Chia fino ao frear", "pastilha_chia"]] },
    { id: "freio_mao", g: "sint", sis: "Freios", q: "Freio de mão", how: "Numa rampa, puxe e solte o pedal de freio.",
      o: [["ok", "Segura com até 7 cliques"], ["fraco", "Preciso puxar até o fim ou não segura", "freio_mao"]] },
    { id: "embreagem", g: "sint", sis: "Embreagem e câmbio", q: "Embreagem e marchas", how: "",
      o: [["ok", "Normal"], ["patina", "O giro sobe e o carro não acompanha", "emb_patina"], ["alta", "Só pega com o pedal quase todo solto", "emb_alta"], ["arranha", "Arranha ou é duro para engatar", "emb_arranha"], ["trepida", "Trepida ao sair", "emb_trepida"]] },
    { id: "direcao", g: "sint", sis: "Direção e suspensão", q: "Direção", how: "Numa reta plana, afrouxe as mãos no volante por um instante.",
      o: [["ok", "Reta e leve"], ["puxa", "Puxa para um lado", "alinhamento"], ["folga", "Folga ou estalo ao virar", "dir_folga"], ["pesada", "Pesada ou gemendo ao esterçar", "dir_pesada"]] },
    { id: "ar", g: "sint", sis: "Ar-condicionado", q: "Ar-condicionado", how: "",
      o: [["ok", "Gela bem"], ["fraco", "Gela pouco", "ar_fraco"], ["nao", "Não gela", "ar_nao"], ["mofo", "Cheiro de mofo", "ar_filtro"]] },
    { id: "temp_rel", g: "sint", sis: "Arrefecimento", q: "O ponteiro da temperatura já passou do meio?", how: "",
      o: [["ok", "Nunca"], ["subiu", "Já subiu", "rel_temp"]] },
  ];

  // Achados que nascem de uma resposta: [sistema, urgência 1–3, confiança (A alta, M média, B baixa), ação, título, por quê, o que fazer, teste guiado]
  // urgência: 1 pode rodar · 2 agende · 3 pare      ação: trocar | verificar | fazer (serviço ou ajuste)
  const F = {
    oleo_baixo: ["Motor", 2, "A", "fazer", "Óleo do motor abaixo do mínimo", "Com pouco óleo o motor perde lubrificação e pode travar. Motor que baixa óleo ou queima (fumaça azulada) ou vaza (mancha no chão).", "Complete hoje com o óleo do manual (0W-20 ou 5W-30) até a marca de cima. Anote os km e confira de novo em 500 km para saber quanto ele consome."],
    oleo_alto: ["Motor", 1, "M", "verificar", "Óleo acima do máximo", "Óleo demais espuma e força os retentores. Se ninguém colocou a mais, pode ser combustível descendo para o cárter.", "Peça para retirarem o excesso. Se o nível subiu sozinho, verificar bicos e partidas a frio repetidas."],
    oleo_leitoso: ["Motor", 3, "M", "verificar", "Óleo com aspecto leitoso", "Óleo cor de café com leite costuma ser líquido de arrefecimento misturado: o sinal clássico de junta do cabeçote queimada. Em carro que só faz trajeto curto pode ser condensação (aparece mais na tampa do que na vareta).", "Não rode longe. Peça teste de pressão do arrefecimento e teste de gases no radiador antes de autorizar qualquer conserto."],
    oleo_gasolina: ["Motor", 2, "M", "verificar", "Óleo fino e com cheiro de combustível", "Combustível descendo para o cárter afina o óleo e gasta o motor. Vem de bico vazando, mistura rica ou muitas partidas a frio.", "Trocar o óleo e o filtro e investigar a causa (bicos e mistura)."],
    oleo_borra: ["Motor", 2, "M", "fazer", "Óleo com borra ou grãos", "Óleo vencido vira borra e entope as passagens de lubrificação.", "Trocar óleo e filtro agora e encurtar o intervalo das próximas trocas."],
    agua_baixa: ["Arrefecimento", 2, "A", "verificar", "Líquido de arrefecimento abaixo do mínimo", "O sistema é fechado: se baixou, está vazando (mangueira, bomba d'água, radiador, tampa) ou sendo consumido pelo motor.", "Complete com aditivo já diluído, com o motor frio, e marque o nível com uma caneta. Se baixar de novo em uma semana, peça teste de pressão do sistema."],
    agua_vazia: ["Arrefecimento", 3, "A", "verificar", "Reservatório de arrefecimento vazio", "Sem líquido o motor superaquece em poucos minutos, e superaquecimento empena o cabeçote.", "Não rode. Com o motor frio, complete e procure o vazamento; leve para teste de pressão."],
    agua_suja: ["Arrefecimento", 1, "A", "fazer", "Líquido de arrefecimento cor de ferrugem", "Aditivo vencido ou só água: enferruja por dentro e entope o radiador.", "Limpeza do sistema e troca do líquido com aditivo na proporção certa."],
    agua_oleo: ["Arrefecimento", 3, "M", "verificar", "Óleo no líquido de arrefecimento", "Óleo boiando no reservatório indica passagem entre óleo e água: junta do cabeçote ou trocador de calor.", "Não rode longe. Teste de pressão e teste de gases no radiador."],
    fluido_baixo: ["Freios", 2, "A", "verificar", "Fluido de freio abaixo do mínimo", "O nível baixa devagar conforme as pastilhas gastam. Se baixou rápido, há vazamento.", "Medir as pastilhas e procurar vazamento nas rodas e no cilindro mestre. Só completar não resolve."],
    fluido_velho: ["Freios", 1, "A", "fazer", "Fluido de freio escuro", "O fluido absorve umidade com o tempo, ferve mais fácil e o pedal fica mole em descidas longas.", "Trocar o fluido (DOT 3 ou DOT 4) com sangria. O normal é a cada 2 a 3 anos."],
    bat_zinabre: ["Elétrica", 1, "A", "fazer", "Terminais da bateria com zinabre", "A crosta aumenta a resistência: a partida fica fraca e o alternador carrega mal.", "Soltar os terminais (negativo primeiro), limpar com água quente e bicarbonato, secar e reapertar."],
    bat_solto: ["Elétrica", 2, "A", "fazer", "Terminal da bateria frouxo", "Mau contato na bateria apaga o carro, zera a memória da central e pode queimar componentes.", "Reapertar hoje. Depois de mexer na bateria, faça o reaprendizado da marcha lenta.", "relearn"],
    bat_velha: ["Elétrica", 1, "B", "verificar", "Bateria com mais de 5 anos", "Bateria costuma durar de 3 a 5 anos. Pode falhar sem aviso, principalmente no frio.", "Pedir teste de carga numa autoelétrica (é rápido e costuma ser grátis)."],
    correia_ruim: ["Motor", 2, "A", "trocar", "Correia do alternador com trincas", "Se a correia arrebenta, o carro fica sem alternador e sem bomba d'água na hora (a direção do Civic 2008 é elétrica e continua funcionando enquanto houver bateria).", "Trocar a correia e conferir o tensionador."],
    correia_vitrificada: ["Motor", 1, "M", "verificar", "Correia do alternador vitrificada", "Correia lisa e brilhante patina: chia e o alternador carrega menos.", "Conferir o tensionador; costuma pedir correia nova."],
    correia_chia: ["Motor", 1, "M", "verificar", "Chiado de correia", "Chiado na partida ou ao ligar o ar é correia patinando: gasta, frouxa ou com o tensionador cansado.", "Conferir a correia e o tensionador."],
    vaz_oleo: ["Motor", 1, "M", "verificar", "Vazamento de óleo", "Mancha escura sob o motor: junta da tampa de válvulas, retentor ou bujão do cárter são os mais comuns.", "Lavar o motor e localizar a origem. Acompanhar o nível na vareta toda semana."],
    vaz_agua: ["Arrefecimento", 2, "M", "verificar", "Vazamento de líquido de arrefecimento", "Mancha verde ou rosa, adocicada, é líquido do radiador.", "Teste de pressão do sistema para achar o ponto (mangueiras, bomba d'água, radiador)."],
    vaz_freio: ["Freios", 3, "M", "verificar", "Possível vazamento de fluido de freio", "Mancha clara e fina perto de uma roda pode ser fluido de freio. Vazando, o pedal afunda.", "Não rode até um mecânico olhar os flexíveis e as pinças dessa roda."],
    vaz_direcao: ["Direção e suspensão", 2, "B", "verificar", "Vazamento de fluido avermelhado", "O Civic 2008 tem direção elétrica (sem fluido) e câmbio manual: fluido avermelhado não é comum nele. Pode ser fluido de freio/embreagem muito velho ou algo de fora do carro.", "Conferir os níveis do freio e da embreagem e localizar de onde pinga."],
    pneu_meia: ["Pneus", 1, "A", "trocar", "Pneus chegando ao limite", "Perto do TWI o pneu já perdeu boa parte da aderência no molhado.", "Programar a troca; evitar chuva forte em velocidade."],
    pneu_careca: ["Pneus", 3, "A", "trocar", "Pneu no limite (TWI) ou careca", "Abaixo de 1,6 mm o carro aquaplana e a frenagem no molhado alonga muito. Também dá multa.", "Trocar antes de rodar em pista molhada ou estrada."],
    pneu_murcho: ["Pneus", 1, "M", "fazer", "Desgaste nas bordas: pneus rodando murchos", "Pouca pressão gasta as bordas, aumenta o consumo e esquenta o pneu.", "Calibrar a frio com a pressão da etiqueta na porta do motorista e repetir a cada 15 dias."],
    pneu_cheio: ["Pneus", 1, "M", "fazer", "Desgaste no centro: pressão alta demais", "Pressão acima da recomendada gasta o meio da banda e reduz a aderência.", "Calibrar a frio com a pressão da etiqueta na porta do motorista."],
    pneu_escama: ["Direção e suspensão", 2, "M", "verificar", "Pneu gasto em ondas ou escamas", "Desgaste em escamas vem de amortecedor fraco ou folga na suspensão: a roda quica.", "Revisar amortecedores, buchas e terminais; depois alinhar e balancear."],
    pneu_bolha: ["Pneus", 3, "A", "trocar", "Bolha ou corte na lateral do pneu", "Bolha é a carcaça rompida: o pneu pode estourar de uma vez.", "Colocar o estepe e trocar esse pneu. Não tem conserto."],
    alinhamento: ["Direção e suspensão", 1, "M", "fazer", "Sinais de desalinhamento", "Carro que puxa para um lado ou gasta o pneu só de um lado está fora de geometria.", "Alinhar e balancear; pedir para conferirem buchas e terminais antes."],
    amortecedor: ["Direção e suspensão", 2, "M", "trocar", "Amortecedor fraco", "Amortecedor que deixa o carro balançar alonga a frenagem e tira estabilidade em curva.", "Avaliar os amortecedores; trocam-se sempre os dois do mesmo eixo."],
    amortecedor_vaza: ["Direção e suspensão", 2, "A", "trocar", "Amortecedor vazando óleo", "Amortecedor molhado de óleo perdeu a carga.", "Trocar o par do mesmo eixo, com batentes e coifas."],
    lampada: ["Elétrica", 1, "A", "trocar", "Lâmpada queimada", "Luz de freio ou seta queimada é risco de batida e multa.", "Trocar a lâmpada; se queimar de novo, conferir o soquete e o fusível."],
    luz_injecao: ["Central", 2, "A", "verificar", "Luz da injeção acesa", "A central registrou uma falha. Se a luz <b>piscar</b>, é falha de queima forte: alivie o pé, porque isso derrete o catalisador.", "Ver os códigos de falha neste relatório e seguir por eles."],
    luz_oleo: ["Motor", 3, "A", "verificar", "Luz do óleo acesa", "Luz vermelha do óleo com o motor ligado é falta de pressão de óleo: o motor pode travar em minutos.", "Desligue o motor. Confira o nível; se estiver certo, não ligue: chame o guincho."],
    luz_bateria: ["Elétrica", 2, "A", "verificar", "Luz da bateria acesa", "Com o motor ligado, essa luz diz que o alternador não está carregando. O carro anda só enquanto a bateria durar.", "Conferir correia, conector e alternador numa autoelétrica."],
    luz_temp: ["Arrefecimento", 3, "A", "verificar", "Luz de temperatura acesa", "Motor superaquecendo. Continuar rodando empena o cabeçote.", "Pare e desligue. Só abra o reservatório com o motor frio."],
    luz_abs: ["Freios", 2, "A", "verificar", "Luz do ABS acesa", "O freio comum continua funcionando, mas sem o antitravamento. O mais comum é sensor de roda sujo ou com fio rompido.", "Ler os códigos do ABS com um scanner que acesse esse módulo (o OBD genérico não lê)."],
    luz_srs: ["Central", 2, "A", "verificar", "Luz do airbag (SRS) acesa", "Com a luz acesa o airbag pode não abrir numa batida.", "Diagnóstico com scanner que leia o SRS. Não mexa nos conectores amarelos."],
    luz_freio: ["Freios", 3, "A", "verificar", "Luz vermelha de freio acesa", "Com o freio de mão solto, essa luz é fluido de freio baixo ou falha no sistema.", "Conferir o fluido agora. Se estiver baixo, não rode até acharem o motivo."],
    partida_frio: ["Mistura e injeção", 1, "B", "verificar", "Demora para pegar com o motor frio", "Em carro flex com etanol, o mais comum é o sistema de partida a frio (reservatório de gasolina vazio ou bomba), depois velas e bateria.", "Conferir o reservatório de partida a frio e as velas."],
    partida_quente: ["Mistura e injeção", 1, "B", "verificar", "Demora para pegar com o motor quente", "Pode ser bico vazando (afoga o motor), sensor de temperatura lendo errado ou pressão de combustível caindo com o carro parado.", "Medir a pressão de combustível e ver se ela se mantém com o motor desligado."],
    partida_lenta: ["Elétrica", 2, "M", "verificar", "Motor de partida gira devagar", "Giro lento na partida é bateria fraca, terminal ruim ou motor de partida cansado.", "Teste de carga da bateria e limpeza dos terminais.", "crank"],
    partida_clique: ["Elétrica", 2, "M", "verificar", "Às vezes só faz clique na partida", "Um clique sem girar é pouca corrente chegando ao motor de partida: bateria, terminais, cabo massa ou o próprio motor de partida.", "Limpar e apertar terminais e massa; testar a bateria. Se continuar, revisar o motor de partida.", "crank"],
    rel_oscila: ["Mistura e injeção", 1, "B", "verificar", "Marcha lenta oscilando (relato)", "Oscilação parado vem de entrada de ar, corpo de borboleta sujo ou sem reaprendizado, vela ou bobina.", "Veja a medição da marcha lenta neste relatório.", "snap"],
    rel_morre: ["Mistura e injeção", 2, "M", "verificar", "Motor morre ou quase morre parado", "Morrer em marcha lenta é risco em cruzamento. Causas comuns: entrada de ar, corpo de borboleta, reaprendizado da marcha lenta.", "Veja a medição da marcha lenta e da volta à marcha lenta neste relatório.", "snap"],
    fraco: ["Mistura e injeção", 1, "B", "verificar", "Motor fraco em subida", "Falta de força com carga costuma ser falta de combustível (bomba, filtro), catalisador entupido, velas ou embreagem patinando.", "Veja as medições da mistura neste relatório.", "mix"],
    engasga: ["Mistura e injeção", 2, "B", "verificar", "Engasgos ou trancos ao acelerar", "Engasgo é falha de queima: vela, bobina, bico ou falta de combustível quando o motor pede mais.", "Conferir velas e bobinas; medir a pressão de combustível.", "mix"],
    consumo_alto: ["Mistura e injeção", 1, "B", "verificar", "Consumo aumentou", "Antes de culpar o motor: pneus murchos, etanol no lugar de gasolina e trajetos curtos. No motor: mistura rica, sonda, termostato aberto.", "Calibrar os pneus e comparar com as medições da mistura neste relatório."],
    fum_branca: ["Motor", 2, "M", "verificar", "Fumaça branca o tempo todo", "Fumaça branca com o motor já quente é líquido de arrefecimento queimando: junta do cabeçote.", "Conferir se o nível do reservatório baixa; teste de gases no radiador."],
    fum_azul: ["Motor", 2, "M", "verificar", "Fumaça azulada", "Fumaça azulada é óleo queimando: retentores de válvula (na partida) ou anéis (ao acelerar).", "Medir quanto óleo baixa a cada 1.000 km; teste de compressão se passar de 1 litro."],
    fum_preta: ["Mistura e injeção", 2, "M", "verificar", "Fumaça preta", "Fumaça preta é combustível demais: bico vazando, sensor de temperatura ou sonda.", "Veja as medições da mistura neste relatório.", "mix"],
    ch_combustivel: ["Mistura e injeção", 3, "M", "verificar", "Cheiro de combustível", "Cheiro forte de gasolina ou etanol é vazamento (mangueira, bicos, tampa do tanque) e risco de incêndio.", "Com o motor frio, olhe a flauta dos bicos e as mangueiras. Se achar algo molhado, não ligue o motor."],
    ch_queimado: ["Motor", 2, "B", "verificar", "Cheiro de queimado", "Pode ser óleo pingando no escapamento, embreagem patinando, freio preso ou fio derretendo.", "Localizar de onde vem: do motor, das rodas (freio) ou de dentro do carro (elétrica)."],
    ch_doce: ["Arrefecimento", 2, "M", "verificar", "Cheiro adocicado", "Cheiro de xarope é líquido de arrefecimento vazando e evaporando no motor quente.", "Conferir o nível e procurar o vazamento (teste de pressão)."],
    ch_ovo: ["Escapamento", 1, "B", "verificar", "Cheiro de ovo podre", "Enxofre no escapamento: catalisador sobrecarregado por mistura rica, ou combustível ruim.", "Trocar de posto e ver as medições da mistura e do catalisador neste relatório.", "cat"],
    homocinetica: ["Direção e suspensão", 2, "M", "trocar", "Tec-tec com o volante virado: junta homocinética", "Estalo repetido em manobra é homocinética gasta, quase sempre porque a coifa rasgou e perdeu a graxa.", "Trocar a junta e a coifa do lado que estala antes que quebre e o carro pare de tracionar."],
    rolamento: ["Direção e suspensão", 2, "M", "trocar", "Ronco que cresce com a velocidade: rolamento de roda", "Rolamento gasto ronca mais quanto mais rápido, e muda de tom nas curvas.", "Identificar a roda (o ronco aumenta virando para o lado oposto) e trocar o rolamento."],
    suspensao_folga: ["Direção e suspensão", 2, "M", "verificar", "Batida seca em buracos", "Batida seca é folga: bieletas, buchas da bandeja, pivôs ou coxim do amortecedor.", "Revisão da suspensão no elevador."],
    pastilha_fim: ["Freios", 3, "M", "trocar", "Ferro raspando ao frear: pastilhas no fim", "Som de ferro é o metal da pastilha no disco: freia menos e estraga os discos.", "Trocar as pastilhas já; os discos provavelmente também. Evite rodar."],
    pastilha_chia: ["Freios", 1, "M", "verificar", "Chiado fino ao frear", "Pode ser só poeira ou o avisador de desgaste encostando.", "Medir a espessura das pastilhas na próxima parada."],
    batida_motor: ["Motor", 3, "B", "verificar", "Batida metálica no motor", "Batida que acompanha o giro pode ser bronzina ou pino (grave), ou só folga de válvulas e detonação.", "Confira o nível do óleo agora. Não force o motor; leve para um mecânico ouvir."],
    escape_furo: ["Escapamento", 1, "M", "verificar", "Escapamento roncando", "Furo ou junta do escapamento solta. Antes da sonda, o furo também engana a leitura da mistura.", "Soldar ou trocar o trecho; conferir os coxins do escapamento."],
    balanceamento: ["Pneus", 1, "M", "fazer", "Volante treme entre 80 e 110 km/h", "Vibração só nessa faixa é roda desbalanceada ou torta.", "Balancear as 4 rodas e conferir se alguma está amassada."],
    disco_empenado: ["Freios", 2, "M", "trocar", "Trepidação ao frear: discos empenados", "Disco empenado faz o pedal e o volante pulsarem.", "Trocar ou retificar os discos dianteiros, junto com as pastilhas."],
    coxim: ["Motor", 1, "B", "verificar", "Carro treme parado", "Se a rotação estiver firme, o tremor é coxim do motor ou do câmbio cansado. Se a rotação oscilar junto, é falha de queima.", "Compare com a medição da marcha lenta neste relatório."],
    freio_esponjoso: ["Freios", 3, "M", "verificar", "Pedal de freio baixo ou esponjoso", "Pedal mole é ar no sistema, fluido velho ou vazamento. Pode faltar freio numa emergência.", "Não adie: sangria e revisão do sistema, com o cilindro mestre."],
    freio_puxa: ["Freios", 2, "M", "verificar", "Puxa para um lado ao frear", "Uma pinça está freando mais que a outra (pistão travado ou flexível entupido).", "Revisar as pinças e os flexíveis dianteiros."],
    freio_mao: ["Freios", 1, "A", "fazer", "Freio de mão fraco", "Cabo esticado ou lonas e pastilhas traseiras gastas.", "Regular o freio de mão e conferir o freio traseiro."],
    emb_patina: ["Embreagem e câmbio", 2, "M", "trocar", "Embreagem patinando", "Giro que sobe sem o carro acompanhar é disco de embreagem no fim.", "Programar a troca do kit (platô, disco e rolamento). Evitar arrancadas e subidas carregado."],
    emb_alta: ["Embreagem e câmbio", 1, "M", "verificar", "Embreagem pegando no alto", "O disco está gastando; ainda funciona, mas a troca se aproxima.", "Pedir avaliação e se programar para o kit de embreagem."],
    emb_arranha: ["Embreagem e câmbio", 2, "B", "verificar", "Marchas arranhando", "A embreagem não solta por completo (fluido baixo ou com ar, cilindro) ou o óleo do câmbio está vencido.", "Conferir o fluido da embreagem e o óleo do câmbio (MTF Honda)."],
    emb_trepida: ["Embreagem e câmbio", 1, "B", "verificar", "Trepidação ao sair", "Disco contaminado, platô empenado ou coxim do motor cansado.", "Avaliar junto com os coxins."],
    dir_folga: ["Direção e suspensão", 2, "M", "verificar", "Folga ou estalo na direção", "Terminais, barra axial ou cruzeta da coluna com folga.", "Revisão da direção no elevador."],
    dir_pesada: ["Direção e suspensão", 2, "M", "verificar", "Direção pesada ou gemendo", "A direção do Civic 2008 é elétrica (EPS): fica pesada com bateria fraca ou tensão baixa, falha no sistema (luz EPS no painel), pneus murchos ou alinhamento ruim.", "Ver se a luz EPS acende, conferir a calibragem e a bateria; persistindo, scanner com o módulo EPS."],
    ar_fraco: ["Ar-condicionado", 1, "B", "verificar", "Ar-condicionado gelando pouco", "Pouco gás (vazamento pequeno), filtro de cabine entupido ou condensador sujo.", "Trocar o filtro de cabine e pedir teste de pressão do gás, com contraste para achar vazamento."],
    ar_nao: ["Ar-condicionado", 1, "M", "verificar", "Ar-condicionado não gela", "Sem gás, embreagem do compressor, fusível ou relé.", "Diagnóstico numa oficina de ar-condicionado."],
    ar_filtro: ["Ar-condicionado", 1, "M", "fazer", "Cheiro de mofo no ar-condicionado", "Filtro de cabine vencido e fungo no evaporador.", "Trocar o filtro de cabine e higienizar o sistema."],
    rel_temp: ["Arrefecimento", 2, "M", "verificar", "Temperatura já subiu além do normal", "Ponteiro acima do meio é aviso: nível baixo, ventoinha, termostato ou radiador.", "Conferir o nível com o motor frio e ver as medições de arrefecimento neste relatório.", "fan"],
  };

  // Sistemas, na ordem em que aparecem no relatório
  const SIS = ["Motor", "Mistura e injeção", "Arrefecimento", "Elétrica", "Central", "Freios", "Embreagem e câmbio", "Direção e suspensão", "Pneus", "Escapamento", "Ar-condicionado", "Manutenção"];
  const CONF = { A: "alta", M: "média", B: "baixa" }, CORD = { alta: 0, "média": 1, baixa: 2 };
  const URG = ["Informação", "Pode rodar", "Agende", "Pare"];
  const ROT = ["✅ Tudo certo no que foi avaliado", "✅ Pode rodar, com pontos para acompanhar", "🗓️ Agende a oficina", "🛑 Pare: resolva antes de rodar"];
  const TNAME = { leak: "Caça-vazamento de ar", mix: "Raio-X da mistura", cat: "Sondas e catalisador", snap: "Acelerada e volta à marcha lenta", sweep: "Varredura do pedal e da borboleta",
    load: "Marcha lenta com tudo ligado", crank: "Bateria na partida", fan: "Ventoinha do radiador", relearn: "Reaprendizado da marcha lenta" };
  // de qual sistema do "Como está o carro" (insight) e de qual sinal de alerta vem cada sistema daqui, e qual medida de hoje o cobre
  const MIX = ["ar_falso", "falta_comb", "maf", "pobre", "rica"];
  const HSIS = { mistura: ["Mistura e injeção", "idleStft", MIX], lenta: ["Mistura e injeção", "idleRpm", ["lenta", "retorno", "carga"]], borboleta: ["Mistura e injeção", "tpsAB", ["borboleta"]],
    arrefecimento: ["Arrefecimento", "ectWarm", ["superaquece", "ventoinha", "termostato"]], admissao: ["Mistura e injeção", "idleStft", MIX], eletrica: ["Elétrica", "vCharge", ["bateria", "alternador"]], consumo: ["Mistura e injeção", null, []] };

  // =================================================================================================================
  // 2. avaliar(estado, histórico) → relatório. Função pura.
  //    estado:    { ans: {pergunta: valor | [valores] | "-"}, m: {medidas}, skip: ["texto do que foi pulado"] }
  //    histórico: { dtc: {confirmed: [{code, desc}], pending: [...]}, insight: {sistemas, hipoteses}, alerts: [{title, severity, n, signal}],
  //                 maint: [{nome, status, texto}], tests: [{id, title, verdict, ts}] }      (tudo opcional)
  // =================================================================================================================
  function avaliar(st, hist = {}, now = Date.now()) {
    const ans = st.ans || {}, m = st.m || {}, A = new Map(), bons = [], pulados = [...(st.skip || [])], obs = [];
    const has = (id) => A.has(id);
    const put = (id, a) => { const o = A.get(id); if (o) { o.sev = Math.max(o.sev, a.sev ?? 0); if (a.ev) o.ev.push(...a.ev); if (a.conf && CORD[a.conf] < CORD[o.conf]) o.conf = a.conf; return o; }
      const n = { id, sis: a.sis, sev: a.sev, conf: a.conf || "média", acao: a.acao || "verificar", t: a.t, pq: a.pq || "", ev: [...(a.ev || [])], fz: a.fz || "", teste: a.teste || null }; A.set(id, n); return n; };
    const fromF = (key, ev) => { const f = F[key]; return put(key, { sis: f[0], sev: f[1], conf: CONF[f[2]], acao: f[3], t: f[4], pq: f[5], fz: f[6], teste: f[7], ev: ev ? [ev] : [] }); };
    const bump = (id, o) => { const a = A.get(id); if (!a) return; if (o.sev) a.sev = Math.max(a.sev, o.sev); if (o.conf && CORD[o.conf] < CORD[a.conf]) a.conf = o.conf; if (o.ev) a.ev.push(o.ev); if (o.acao) a.acao = o.acao; };
    const said = (q, v) => Array.isArray(ans[q]) ? ans[q].includes(v) : ans[q] === v;

    // ---- respostas do dono
    const answered = {};
    for (const q of Q) {
      const v = ans[q.id]; if (v == null) continue;
      if (v === "-") { pulados.push(`${q.q} (pergunta pulada)`); continue; }
      (answered[q.sis] ||= []).push(q.id);
      for (const x of Array.isArray(v) ? v : [v]) { const o = q.o.find((o) => o[0] === x); if (o && o[2]) fromF(o[2], `Você respondeu em "${q.q}": ${o[1].toLowerCase()}`); }
    }

    // ---- códigos e luz de injeção
    const desc = {}; for (const c of [...(hist.dtc?.confirmed || []), ...(hist.dtc?.pending || [])]) if (c && c.code) desc[c.code] = c.desc || "";
    const conf = [...new Set([...(m.dtc || []), ...(hist.dtc?.confirmed || []).map((c) => c.code)])].filter(Boolean);
    const pend = [...new Set([...(m.pend || []), ...(hist.dtc?.pending || []).map((c) => c.code)])].filter((c) => c && !conf.includes(c));
    const code = (re) => [...conf, ...pend].filter((c) => re.test(c));
    for (const c of conf) put("dtc_" + c, { sis: "Central", sev: /^P030/.test(c) ? 3 : 2, conf: "alta", acao: "verificar", t: `Código de falha ${c}${desc[c] ? ": " + desc[c] : ""}`,
      pq: /^P030/.test(c) ? "A central confirmou falha de queima. Rodar assim joga combustível cru no catalisador e pode derretê-lo." : "A central confirmou esta falha em mais de um ciclo de uso.",
      ev: [`Código confirmado na central${m.mil ? ", com a luz da injeção acesa" : ""}`], fz: "Toque no código na aba Alertas para ver causas e o que verificar. Não apague o código antes de consertar: ele guia o diagnóstico." });
    for (const c of pend) put("dtc_" + c, { sis: "Central", sev: 1, conf: "média", acao: "verificar", t: `Código pendente ${c}${desc[c] ? ": " + desc[c] : ""}`,
      pq: "A central viu a falha uma vez e ainda está confirmando. Se repetir, acende a luz da injeção.", ev: ["Código pendente (ainda não confirmado)"], fz: "Acompanhar. Se virar confirmado, seguir a orientação do código." });
    if (m.mil && !conf.length) put("mil", { sis: "Central", sev: 2, conf: "alta", acao: "verificar", t: "Luz da injeção acesa, sem código lido", pq: "A central indica falha, mas o código não veio pela leitura genérica.", ev: ["MIL acesa na leitura do OBD"], fz: "Ler os códigos com um scanner completo." });
    if (m.dtc && !conf.length && !pend.length && !m.mil) bons.push("Central: nenhum código de falha, luz da injeção apagada");
    if (m.readyTotal && m.readyOk < m.readyTotal) obs.push(`A central concluiu ${m.readyOk} de ${m.readyTotal} autotestes. Depois de desligar a bateria ou apagar códigos ela leva alguns dias de uso para terminar; até lá, falhas de catalisador e de mistura podem ainda não ter virado código.`);

    // ---- bateria e alternador
    if (m.noStart) put("nao_pega", { sis: "Elétrica", sev: 3, conf: "alta", acao: "verificar", t: "O motor não pegou durante o diagnóstico", pq: m.vCrank != null && m.vCrank < 9.6 ? "A tensão afundou na partida: a bateria não tem força para girar o motor." : "A bateria segurou a tensão; a causa provável está em combustível, ignição ou imobilizador.",
      ev: [m.vRest != null ? `Bateria em repouso: ${nf(m.vRest, 1)} V` : "", m.vCrank != null ? `Mínima na tentativa: ${nf(m.vCrank, 1)} V` : ""].filter(Boolean), fz: m.vCrank != null && m.vCrank < 9.6 ? "Carga lenta ou chupeta e teste da bateria." : "Conferir combustível no tanque, fusíveis da bomba e da injeção e se a luz da chave (imobilizador) pisca." });
    if (m.vRest != null) {
      const hot = m.vRestHot ? " (medida logo depois de desligar o motor: o valor real em repouso pode ser um pouco menor)" : "";
      if (m.vRest < 12.4) put("bateria", { sis: "Elétrica", sev: m.vRest < 12.0 ? 2 : 1, conf: "média", acao: "verificar", t: m.vRest < 12.0 ? "Bateria descarregada" : "Bateria com meia carga",
        pq: "Bateria cheia marca 12,6 V com tudo desligado. Abaixo de 12,4 V ela está cansada, o carro roda pouco, ou algo consome com o carro parado.", ev: [`Em repouso: ${nf(m.vRest, 1)} V (bom: 12,4 V ou mais)${hot}`],
        fz: "Limpar e apertar os terminais e pedir um teste de carga numa autoelétrica antes de comprar bateria.", teste: "crank" });
      else if (m.vCrank == null || m.vCrank >= 9.6) bons.push(`Bateria: ${nf(m.vRest, 1)} V em repouso${m.vCrank != null ? `, ${nf(m.vCrank, 1)} V na partida` : ""}`);
    }
    if (m.vCrank != null && m.vCrank < 9.6) put("bateria", { sis: "Elétrica", sev: m.vCrank < 9.0 ? 2 : 1, conf: "média", acao: "verificar", t: "Bateria afunda na partida", pq: "Na partida a tensão não deveria cair abaixo de 9,6 V. Cair mais que isso é bateria fraca ou terminal com mau contato.",
      ev: [`Mínima na partida: ${nf(m.vCrank, 1)} V (bom: 9,6 V ou mais; o leitor mede 2 vezes por segundo, a queda real pode ser maior)`], fz: "Limpar e apertar os terminais e pedir um teste de carga numa autoelétrica antes de comprar bateria.", teste: "crank" });
    if (has("bateria")) {
      const sinais = [m.vRest != null && m.vRest < 12.2, m.vCrank != null && m.vCrank < 9.6, has("partida_lenta") || has("partida_clique"), has("bat_velha")].filter(Boolean).length;
      if (has("bat_velha")) bump("bateria", { ev: "Bateria com mais de 5 anos" });
      if (has("partida_lenta") || has("partida_clique")) bump("bateria", { ev: "Você relatou partida fraca" });
      if (has("bat_zinabre") || has("bat_solto")) bump("bateria", { ev: "Terminais com zinabre ou frouxos: resolva isso primeiro e meça de novo" });
      else if (sinais >= 3 || (m.vCrank != null && m.vCrank < 9.0 && m.vRest != null && m.vRest < 12.2)) { bump("bateria", { sev: 2, conf: sinais >= 3 ? "alta" : "média", acao: "trocar", ev: "Vários sinais juntos. Bateria só descarregada (luz esquecida acesa) dá a mesma leitura: recarregue e faça o teste de carga antes de comprar" }); A.get("bateria").t = "Bateria no fim: provável troca"; }
    }
    if (m.vCharge != null) {
      if (m.vCharge < 13.2) put("alternador", { sis: "Elétrica", sev: m.vCharge < 12.8 ? 3 : 2, conf: has("luz_bateria") ? "alta" : "média", acao: "verificar", t: "Alternador carregando pouco", pq: "Com o motor ligado a tensão deve ficar entre 13,5 e 14,8 V. Abaixo de 13,2 V a bateria não recarrega e o carro para quando ela acabar.",
        ev: [`Com o motor ligado: ${nf(m.vCharge, 1)} V`], fz: "Conferir a correia e o conector do alternador; teste de alternador numa autoelétrica.", teste: "load" });
      else if (m.vCharge > 15.0) put("alternador", { sis: "Elétrica", sev: 2, conf: "média", acao: "verificar", t: "Alternador carregando demais", pq: "Acima de 15 V o regulador de tensão falhou: cozinha a bateria e queima lâmpadas e módulos.", ev: [`Com o motor ligado: ${nf(m.vCharge, 1)} V`], fz: "Trocar o regulador de tensão (fica no alternador)." });
      else if (m.vLoad == null || m.vLoad >= 13.0) bons.push(`Alternador: ${nf(m.vCharge, 1)} V com o motor ligado${m.vLoad != null ? `, ${nf(m.vLoad, 1)} V com tudo ligado` : ""}`);
    }
    if (m.vLoad != null && m.vLoad < 13.0 && !(m.vCharge != null && m.vCharge < 13.2)) put("alternador", { sis: "Elétrica", sev: m.vLoad < 12.6 ? 2 : 1, conf: "média", acao: "verificar", t: "Alternador não dá conta com tudo ligado",
      pq: "Com ar, farol alto e desembaçador ligados a tensão deveria ficar acima de 13 V. Cair abaixo disso é alternador fraco ou correia patinando.", ev: [`Com tudo ligado: ${nf(m.vLoad, 1)} V (sem carga: ${nf(m.vLoad0 ?? m.vCharge, 1)} V)`], fz: "Conferir a correia e o tensionador; teste de alternador numa autoelétrica.", teste: "load" });
    if (has("alternador") && (has("correia_ruim") || has("correia_chia") || has("correia_vitrificada"))) bump("alternador", { ev: "A correia do alternador também deu sinal: comece por ela" });
    if (has("alternador") && has("luz_bateria")) bump("alternador", { conf: "alta", ev: "A luz da bateria está acesa no painel" });
    if (code(/^P056[23]$/).length) bump("alternador", { conf: "alta", ev: `Código de tensão do sistema: ${code(/^P056[23]$/).join(", ")}` });

    // ---- arrefecimento
    const emax = Math.max(m.ectMax ?? 0, m.drv?.ectMax ?? 0) || null;
    if (emax != null && emax >= 106) put("superaquece", { sis: "Arrefecimento", sev: 3, conf: "alta", acao: "verificar", t: "Motor superaquecendo", pq: "A temperatura passou do ponto em que a ventoinha deveria segurar. Rodar assim empena o cabeçote.",
      ev: [`Temperatura máxima no diagnóstico: ${nf(emax)} °C (a ventoinha deve segurar abaixo de 102 °C)`], fz: "Não rode. Com o motor frio, confira o nível. Depois: fusível e relé da ventoinha, ventoinha, termostato e radiador.", teste: "fan" });
    else if (m.fanOn != null && m.fanOn > 102) put("ventoinha", { sis: "Arrefecimento", sev: 2, conf: "média", acao: "verificar", t: "Ventoinha ligando tarde", pq: "A ventoinha deveria ligar entre 93 e 101 °C. Ligando mais tarde, o motor trabalha no limite no trânsito.",
      ev: [`A ventoinha ligou em ${nf(m.fanOn)} °C`], fz: "Conferir o sensor de temperatura, o relé e a própria ventoinha.", teste: "fan" });
    else if (m.fanOn != null && m.fanDone === false) put("ventoinha", { sis: "Arrefecimento", sev: 2, conf: "média", acao: "verificar", t: "Ventoinha não consegue baixar a temperatura", pq: "A ventoinha ligou mas a temperatura não caiu: radiador sujo ou entupido, pouco líquido ou ventoinha fraca.",
      ev: [`Ligou em ${nf(m.fanOn)} °C e a temperatura só baixou até ${nf(m.fanLow)} °C`], fz: "Conferir o nível com o motor frio e lavar a colmeia do radiador por fora.", teste: "fan" });
    else if (emax != null && emax >= 103) put("ventoinha", { sis: "Arrefecimento", sev: 1, conf: "baixa", acao: "verificar", t: "Temperatura chegou perto do limite", pq: "Não cheguei a ver a ventoinha derrubar a temperatura.", ev: [`Máxima no diagnóstico: ${nf(emax)} °C`], fz: "Rode o teste da ventoinha para ver um ciclo completo.", teste: "fan" });
    else if (m.fanOn != null) bons.push(`Arrefecimento: a ventoinha ligou em ${nf(m.fanOn)} °C e baixou até ${nf(m.fanLow)} °C`);
    else if (emax != null && emax >= 80) bons.push(`Arrefecimento: motor chegou a ${nf(emax)} °C sem passar do limite`);
    const slowWarm = m.warmRate != null && m.warmRate < 2.5, coldDrive = m.drv && m.drv.min >= 8 && m.drv.ectEnd != null && m.drv.ectEnd < 75;
    if (slowWarm || coldDrive || code(/^P0128$/).length) put("termostato", { sis: "Arrefecimento", sev: 1, conf: code(/^P0128$/).length || (slowWarm && coldDrive) ? "alta" : coldDrive ? "média" : "baixa", acao: code(/^P0128$/).length ? "trocar" : "verificar", t: "Motor demora a aquecer: termostato provável",
      pq: "Termostato travado aberto deixa o motor frio: gasta mais combustível e o óleo não chega na temperatura de trabalho.",
      ev: [slowWarm ? `Parado, aqueceu ${nf(m.warmRate, 1)} °C por minuto, de ${nf(m.warmFrom)} a ${nf(m.warmTo)} °C (esperado: mais de 2,5; em dia frio aquece mais devagar)` : "", coldDrive ? `Depois de ${nf(m.drv.min)} min rodando, o motor estava em ${nf(m.drv.ectEnd)} °C` : "", code(/^P0128$/).length ? "Código P0128 na central" : ""].filter(Boolean),
      fz: "Ver na aba Diagnóstico o gráfico de aquecimento das últimas viagens. Se confirmar, trocar o termostato (peça barata)." });
    else if (m.warmRate != null) bons.push(`Aquecimento: ${nf(m.warmRate, 1)} °C por minuto em marcha lenta`);
    // junta do cabeçote: só com sinais que se confirmam entre si
    const junta = [["oleo_leitoso", "óleo leitoso"], ["agua_oleo", "óleo no líquido de arrefecimento"], ["fum_branca", "fumaça branca constante"], ["agua_baixa", "líquido baixando"], ["agua_vazia", "reservatório vazio"], ["ch_doce", "cheiro adocicado"], ["rel_temp", "temperatura já subiu"], ["superaquece", "superaqueceu no diagnóstico"]].filter(([k]) => has(k));
    const forte = junta.filter(([k]) => ["oleo_leitoso", "agua_oleo", "fum_branca"].includes(k)).length;
    if (forte >= 2 || (forte >= 1 && junta.length >= 3)) put("junta", { sis: "Motor", sev: 3, conf: forte >= 2 ? "alta" : "média", acao: "verificar", t: "Junta do cabeçote: suspeita", pq: "Vários sinais juntos apontam para passagem entre a água e os cilindros ou o óleo. Só o teste confirma; não autorize a troca sem ele.",
      ev: [`Sinais que se somam: ${junta.map((x) => x[1]).join(", ")}`], fz: "Não rode longe nem deixe aquecer. Peça teste de gases (CO₂) no radiador e teste de pressão do arrefecimento." });
    if ((has("agua_baixa") || has("agua_vazia")) && (has("vaz_agua") || has("ch_doce"))) bump(has("agua_vazia") ? "agua_vazia" : "agua_baixa", { conf: "alta", ev: "Mancha ou cheiro de líquido de arrefecimento: o vazamento é externo (o caso mais simples)" });
    if (has("fum_azul") && has("oleo_baixo")) bump("fum_azul", { conf: "alta", sev: 2, ev: "O nível do óleo também está baixo: o motor está queimando óleo" });
    if (has("freio_esponjoso") && (has("fluido_baixo") || has("vaz_freio"))) bump("freio_esponjoso", { conf: "alta", ev: "Fluido baixo ou vazando junto com pedal mole: vazamento no sistema de freio" });
    if (has("fluido_baixo") && has("luz_freio")) bump("fluido_baixo", { sev: 3, ev: "A luz vermelha de freio está acesa" });
    if (has("pastilha_fim") && has("fluido_baixo")) bump("pastilha_fim", { conf: "alta", ev: "Fluido de freio baixo combina com pastilhas gastas" });

    // ---- mistura (só com o motor quente: frio, a central ainda não corrige pela sonda)
    const s0 = m.idleStft, s1 = m.hiStft, sdv = m.drv?.stft, warm = m.idleEct != null && m.idleEct >= 70;
    if (s0 != null && !warm) pulados.push("Mistura e marcha lenta (o motor não chegou a 70 °C: medição sem valor de diagnóstico)");
    if (s0 != null && warm) {
      const nums = `Ajuste de combustível: ${sg(s0, 0)} % na marcha lenta${s1 != null ? `, ${sg(s1, 0)} % a 2.500 rpm` : ""}${sdv != null ? `, ${sg(sdv, 0)} % rodando` : ""} (normal: entre −10 e +10 %)${m.idleLam != null ? ` · sonda λ ${nf(m.idleLam, 2)}` : ""}`;
      const lean = (id, t, pq, fz, teste, sev, c = "média") => put(id, { sis: "Mistura e injeção", sev, conf: c, acao: "verificar", t, pq, ev: [nums], fz, teste });
      if (s0 > 10 && s1 != null && s0 - s1 > 8) lean("ar_falso", "Mistura pobre parado: provável entrada falsa de ar", "A correção é alta em marcha lenta e some quando a rotação sobe. É a assinatura de ar entrando depois do medidor: junta do corpo de borboleta, mangueira de vácuo, anel de bico, PCV.", "Faça o Caça-vazamento de ar (spray em cada ponto). Se nada reagir, peça teste de fumaça.", "leak", s0 > 25 ? 2 : 1);
      else if ((s1 != null && s1 > 10 && s1 - s0 > 8) || (s0 <= 10 && s1 != null && s1 > 12)) lean("falta_comb", "Mistura pobre com rotação: provável falta de combustível", "A correção cresce quando o motor pede mais: o combustível não acompanha. Bomba cansada, filtro entupido, regulador ou bicos.", "Medir a pressão e a vazão de combustível na oficina; trocar o filtro se estiver vencido.", "mix", 2);
      else if (s0 > 10 && s1 != null) lean("maf", "Mistura pobre em todas as rotações: provável sensor MAF sujo", "O erro é parecido parado e com rotação: algo mede ar a menos o tempo todo. O mais comum é o MAF sujo; também pode ser combustível fora do padrão.", "Limpar o MAF com spray próprio (nunca com pano) e repetir o Raio-X da mistura.", "mix", s0 > 25 ? 2 : 1);
      else if (s0 > 10) lean("pobre", "Mistura pobre em marcha lenta", "A central está somando combustível para compensar. Sem a medição a 2.500 rpm não dá para separar entrada de ar de falta de combustível.", "Rode o Raio-X da mistura: ele separa as causas em 1 minuto.", "mix", s0 > 25 ? 2 : 1, "baixa");
      else if (s0 < -10 || (s1 != null && s1 < -12)) put("rica", { sis: "Mistura e injeção", sev: Math.min(s0, s1 ?? 0) < -20 ? 2 : 1, conf: "média", acao: "verificar", t: "Mistura rica: a central está tirando combustível",
        pq: "Sobra combustível: bico pingando, filtro de ar entupido, pressão alta ou sensor de temperatura lendo frio. Se os bicos foram trocados ou a bateria religada há poucos dias, pode ser só a central reaprendendo.", ev: [nums], fz: "Conferir o filtro de ar e rodar o Raio-X da mistura. Se houve serviço recente, rode 50 km e repita.", teste: "mix" });
      else bons.push(`Mistura: ajuste ${sg(s0, 0)} % na lenta${s1 != null ? ` e ${sg(s1, 0)} % a 2.500 rpm` : ""}${m.idleLam != null ? `, sonda λ ${nf(m.idleLam, 2)}` : ""}`);
      const mix = ["ar_falso", "falta_comb", "maf", "pobre"].find(has);
      if (mix && m.idleLam != null && m.idleLam > 1.08 && s0 >= 20) bump(mix, { sev: 2, ev: `Mesmo corrigindo, a sonda ainda lê pobre (λ ${nf(m.idleLam, 2)}): a central está perto do limite` });
      if (mix && code(/^P017[14]$/).length) bump(mix, { conf: "alta", ev: `Código de mistura pobre na central: ${code(/^P017[14]$/).join(", ")}` });
      if (mix && (has("rel_oscila") || has("rel_morre") || has("fraco") || has("engasga"))) bump(mix, { ev: "Combina com o que você relatou ao dirigir" });
      if (has("rica") && (has("fum_preta") || has("consumo_alto") || has("oleo_gasolina") || has("ch_ovo"))) bump("rica", { conf: "alta", ev: "Combina com o que você relatou (fumaça preta, consumo, cheiro ou óleo com combustível)" });
      if (has("rica") && code(/^P017[25]$/).length) bump("rica", { conf: "alta", ev: `Código de mistura rica: ${code(/^P017[25]$/).join(", ")}` });
    }
    if (sdv != null && sdv > 12 && warm && s0 != null && s0 <= 10 && !has("falta_comb")) put("falta_comb", { sis: "Mistura e injeção", sev: 2, conf: "média", acao: "verificar", t: "Mistura pobre rodando: provável falta de combustível", pq: "Parado a mistura está boa, mas na rua a central precisa somar combustível: bomba, filtro ou bicos não acompanham a carga.",
      ev: [`Ajuste ${sg(sdv, 0)} % rodando, contra ${sg(s0, 0)} % em marcha lenta`], fz: "Medir a pressão e a vazão de combustível; trocar o filtro se estiver vencido.", teste: "mix" });

    // ---- marcha lenta, volta à lenta, carga
    if (m.idleRpm != null && warm) {
      const dips = m.idleDips || 0, isd = m.idleSd || 0, ev = [`Marcha lenta: ${nf(m.idleRpm)} rpm, variando ±${nf(isd)} rpm${dips ? `, com ${dips} queda(s) de mais de 100 rpm` : ""} (bom: 650 a 900 rpm, variação abaixo de ±40)`];
      const mix = ["ar_falso", "falta_comb", "maf", "pobre", "rica"].find(has), mis = code(/^P030\d$/);
      if (isd >= 40 || dips >= 2) {
        const queima = mis.length || (dips >= 2 && !mix);
        put("lenta", { sis: "Mistura e injeção", sev: isd >= 75 || dips >= 4 || has("rel_morre") ? 2 : 1, conf: mis.length ? "alta" : "média", acao: "verificar", t: queima ? "Marcha lenta com falhas: provável falha de queima" : "Marcha lenta instável",
          pq: queima ? "A rotação dá quedas rápidas com a mistura normal: um cilindro falha de vez em quando. Vela gasta, bobina fraca ou bico." : mix ? "A instabilidade é consequência provável da mistura fora do ponto. Resolva a mistura primeiro." : "A rotação não fica parada. Corpo de borboleta sujo ou sem reaprendizado é o mais comum neste carro.",
          ev: mis.length ? [...ev, `Código de falha de queima: ${mis.join(", ")}`] : ev, fz: queima ? "Tirar as velas e olhar (cor e folga); trocar as bobinas de lugar para ver se a falha acompanha." : mix ? "Depois de corrigir a mistura, faça o reaprendizado da marcha lenta." : "Fazer o reaprendizado da marcha lenta; se não firmar, limpar o corpo de borboleta.", teste: queima ? null : "relearn" });
        if (has("coxim")) bump("lenta", { ev: "Você sente o carro tremer parado: é a rotação oscilando, não o coxim" });
      } else if (m.idleRpm < 600 || m.idleRpm > 950) put("lenta", { sis: "Mistura e injeção", sev: 1, conf: "média", acao: "fazer", t: m.idleRpm > 950 ? "Marcha lenta alta" : "Marcha lenta baixa", pq: m.idleRpm > 950 ? "Com o motor quente e tudo desligado a rotação deveria baixar para 700 a 800 rpm. Alta assim, a central compensa ar a mais ou não refez o aprendizado." : "Rotação baixa demais deixa o motor a ponto de morrer ao ligar o ar ou esterçar.",
        ev, fz: "Fazer o reaprendizado da marcha lenta. Se continuar, procurar entrada de ar.", teste: "relearn" });
      else bons.push(`Marcha lenta firme: ${nf(m.idleRpm)} rpm, variação de ±${nf(isd)} rpm`);
      if (!has("lenta") && has("coxim")) bump("coxim", { conf: "média", ev: `A rotação medida está firme (±${nf(isd)} rpm): o tremor vem dos coxins, não do motor` });
      if (!has("lenta") && has("rel_oscila")) bump("rel_oscila", { ev: `Hoje a marcha lenta mediu firme (${nf(m.idleRpm)} ±${nf(isd)} rpm): o sintoma pode ser intermitente` });
    }
    if (m.retLow != null && m.idleRpm != null && (m.idleRpm - m.retLow >= 200 || m.retLow <= 500)) put("retorno", { sis: "Mistura e injeção", sev: m.retLow <= 450 || has("rel_morre") ? 2 : 1, conf: "média", acao: "fazer", t: "Rotação afunda ao soltar o acelerador",
      pq: "Ao soltar o pedal a rotação cai abaixo da marcha lenta antes de voltar. É o que faz o carro morrer ao parar no semáforo.", ev: [`Depois de 2.500 rpm caiu até ${nf(m.retLow)} rpm (marcha lenta: ${nf(m.idleRpm)} rpm)`], fz: "Reaprendizado da marcha lenta; se não resolver, limpar o corpo de borboleta e procurar entrada de ar.", teste: "snap" });
    if (m.loadRpm0 != null && m.loadRpm1 != null && m.loadRpm0 - m.loadRpm1 > 100) put("carga", { sis: "Mistura e injeção", sev: 1, conf: "média", acao: "fazer", t: "Rotação cai com ar e faróis ligados", pq: "A central deveria abrir um pouco a borboleta para segurar a rotação com carga elétrica.", ev: [`Rotação: ${nf(m.loadRpm0)} rpm sem carga, ${nf(m.loadRpm1)} rpm com tudo ligado`], fz: "Reaprendizado da marcha lenta.", teste: "load" });

    // ---- pedal e borboleta
    const tb = code(/^P(012[0-3]|022[0-3]|2135|2138|212[0-8]|2101|2118|2176)$/);
    if ((m.tpsAB != null && m.tpsAB > 8) || (m.pedalDE != null && m.pedalDE > 8)) put("borboleta", { sis: "Mistura e injeção", sev: 2, conf: tb.length ? "alta" : "baixa", acao: "verificar", t: m.tpsAB > 8 ? "Sensores da borboleta discordando" : "Sensores do pedal discordando",
      pq: "O acelerador eletrônico usa dois sensores em cada ponta, que precisam andar juntos. Quando discordam, a central corta a potência por segurança. A medida com o motor ligado é aproximada.",
      ev: [m.tpsAB != null ? `Borboleta A×B: maior diferença ${nf(m.tpsAB, 1)} pontos` : "", m.pedalDE != null ? `Pedal D×E: maior diferença ${nf(m.pedalDE, 1)} pontos` : "", tb.length ? `Código na central: ${tb.join(", ")}` : ""].filter(Boolean),
      fz: "Confirme com a Varredura do pedal (motor desligado). Se repetir: desligar a chave, limpar e reencaixar os conectores do pedal e do corpo de borboleta.", teste: "sweep" });
    else if (m.tpsAB != null || m.pedalDE != null) bons.push(`Acelerador eletrônico: sensores em dupla concordando${m.tpsAB != null ? ` (borboleta ${nf(m.tpsAB, 1)}` : " ("}${m.pedalDE != null ? `${m.tpsAB != null ? ", " : ""}pedal ${nf(m.pedalDE, 1)}` : ""} pontos de diferença)`);

    // ---- sondas e catalisador
    if (m.o2Range != null) {
      const mixBad = ["ar_falso", "falta_comb", "maf", "pobre", "rica"].some(has), p420 = code(/^P04[23]0$/);
      if (m.o2Range > 0.5 || p420.length) put("catalisador", { sis: "Escapamento", sev: 1, conf: p420.length ? "alta" : mixBad ? "baixa" : "média", acao: "verificar", t: "Catalisador: sinal de desgaste", pq: "A sonda depois do catalisador deveria ficar quase parada. Oscilando junto com a da frente, ele não está segurando o oxigênio." + (mixBad ? " Atenção: mistura fora do ponto engana esta medição." : ""),
        ev: [m.o2Range != null ? `Sonda de trás variou ${nf(m.o2Range, 2)} V a 2.500 rpm (bom: menos de 0,5 V)` : "", p420.length ? `Código na central: ${p420.join(", ")}` : ""].filter(Boolean), fz: "Antes de pensar em trocar: corrigir a mistura, procurar furo no escapamento e repetir o teste Sondas e catalisador.", teste: "cat" });
      else if (m.o2Mean != null && m.o2Mean < 0.2) put("sonda_tras", { sis: "Escapamento", sev: 1, conf: "baixa", acao: "verificar", t: "Sonda de trás lendo escape pobre", pq: "Tensão baixa o tempo todo na sonda de trás: mistura pobre, furo no escapamento antes dela ou sonda fraca.", ev: [`Sonda de trás: ${nf(m.o2Mean, 2)} V em média a 2.500 rpm`], fz: "Procurar furo no escapamento; repetir depois de acertar a mistura.", teste: "cat" });
      else bons.push(`Catalisador: sonda de trás estável em ${nf(m.o2Mean, 2)} V (variou ${nf(m.o2Range, 2)} V)`);
      if (has("catalisador") && has("ch_ovo")) bump("catalisador", { ev: "Você relatou cheiro de ovo podre" });
      if (has("catalisador") && has("fraco")) bump("catalisador", { ev: "Motor fraco em subida também pode ser catalisador entupido: peça medição de contrapressão" });
    }

    // ---- histórico do carro (30 dias): confirma, acrescenta ou é desmentido pela medição de hoje
    const hl = [];
    for (const s of (hist.insight?.sistemas || [])) {
      if (!["bad", "warn"].includes(s.status) || s.id === "codigos") continue;
      const [sis, key, ids] = HSIS[s.id] || ["Motor", null, []], today = ids.filter(has).map((i) => A.get(i));
      const measured = key === "ectWarm" ? emax != null && emax >= 80 : key && m[key] != null && (key !== "idleStft" && key !== "idleRpm" || warm);
      if (today.length) { today[0].ev.push(`O histórico dos últimos 30 dias mostra o mesmo: ${s.resumo}`); if (CORD[today[0].conf] > 0) today[0].conf = today[0].conf === "baixa" ? "média" : "alta"; }
      else if (measured) hl.push(`${s.nome}: o histórico acusava problema ("${s.resumo}"), mas a medição de hoje saiu normal. Se houve conserto recente, é o esperado; acompanhe.`);
      else put("hist_" + s.id, { sis, sev: s.status === "bad" ? 2 : 1, conf: "média", acao: "verificar", t: `${s.nome}: o histórico do carro pede atenção`, pq: "Vem dos dados gravados nas viagens dos últimos 30 dias, não de uma medição de hoje.", ev: [s.resumo], fz: s.fazer || "Ver o item em Diagnóstico → Como está o carro." });
    }
    const al = (hist.alerts || []).filter((a) => a.severity !== "info").sort((a, b) => (b.severity === "crit") - (a.severity === "crit") || b.n - a.n).slice(0, 5);
    for (const a of al) hl.push(`Alerta nos últimos 14 dias: ${a.title} (${a.n} vez${a.n > 1 ? "es" : ""}${a.severity === "crit" ? ", crítico" : ""})`);
    const venc = (hist.maint || []).filter((i) => i.status === "bad"), perto = (hist.maint || []).filter((i) => i.status === "warn");
    if (venc.length) put("manutencao", { sis: "Manutenção", sev: 1, conf: "alta", acao: "fazer", t: `Manutenção vencida: ${venc.map((i) => i.nome).join(", ")}`.slice(0, 160), pq: "Itens do plano de manutenção que passaram do prazo ou da quilometragem.", ev: venc.slice(0, 6).map((i) => `${i.nome}: ${i.texto}`), fz: "Fazer na próxima ida à oficina e marcar como feito na aba Manutenção." });
    for (const i of perto.slice(0, 4)) hl.push(`Manutenção chegando: ${i.nome} (${i.texto})`);
    for (const t of (hist.tests || [])) if (t.id !== "geral" && now - t.ts < 14 * 864e5 && !/certo|incompleto/.test(t.verdict || "")) hl.push(`Teste guiado "${t.title}" de ${new Date(t.ts).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })}: ${t.verdict}`);

    // ---- sistemas respondidos sem nenhum achado viram ponto positivo
    for (const sis of SIS) if (answered[sis] && ![...A.values()].some((a) => a.sis === sis) && !bons.some((b) => b.startsWith(sis.split(" ")[0])))
      bons.push(`${sis}: nada de errado no que você conferiu (${answered[sis].length} ${answered[sis].length > 1 ? "itens" : "item"})`);

    const achados = [...A.values()].sort((a, b) => b.sev - a.sev || CORD[a.conf] - CORD[b.conf] || SIS.indexOf(a.sis) - SIS.indexOf(b.sis));
    for (const a of achados) { a.ev = [...new Set(a.ev.filter(Boolean))].slice(0, 6); a.urg = URG[a.sev]; }
    const nivel = achados.reduce((x, a) => Math.max(x, a.sev), 0), nAns = Object.values(ans).filter((v) => v !== "-").length, nMed = Object.keys(m).filter((k) => typeof m[k] === "number").length;
    const parcial = nAns < 10 || nMed < 6;
    const top = achados.filter((a) => a.sev === nivel).slice(0, 3).map((a) => strip(a.t)).map((t) => /^[A-ZÀ-Ú][a-zà-ú]/.test(t) ? t[0].toLowerCase() + t.slice(1) : t);
    const resumo = (!achados.length ? "Nenhum ponto de atenção nas respostas e nas medições." : `${achados.length} ponto${achados.length > 1 ? "s" : ""} de atenção. ${nivel >= 3 ? "Resolva antes de rodar" : nivel === 2 ? "O mais importante" : "Para acompanhar"}: ${top.join("; ")}.`)
      + (parcial ? " Diagnóstico parcial: várias etapas ficaram de fora, então o que não foi avaliado pode esconder algo." : "");
    const rep = { v: 1, ts: st.ts || now, nivel, rotulo: ROT[nivel], resumo, parcial, achados, bons, pulados: [...new Set(pulados)], obs, hist: hl.slice(0, 12), m: compact(m) };
    rep.mecanico = texto(rep);
    return rep;
  }
  // só os números que valem a pena guardar junto do relatório
  function compact(m) { const o = {}; for (const [k, v] of Object.entries(m || {})) { if (typeof v === "number") o[k] = r1(v, 2); else if (typeof v === "boolean") o[k] = v; else if (Array.isArray(v)) o[k] = v.slice(0, 12).map(String); else if (v && typeof v === "object") o[k] = compact(v); } return o; }
  const strip = (s) => String(s ?? "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();

  // texto corrido para copiar, mandar ao mecânico ou entregar à IA
  function texto(rep) {
    const d = new Date(rep.ts).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
    const L = [`DIAGNÓSTICO GERAL · Honda Civic 2008 1.8 flex (R18A), manual · ${d}`, `Feito pelo dono com o Delfos (leitor OBD do carro): respostas dele + medições ao vivo. Os achados são hipóteses a confirmar.`, "", `RESULTADO: ${strip(rep.rotulo).replace(/^\W+/, "")}. ${rep.resumo}`];
    const grp = [["PROVÁVEL TROCA", (a) => a.acao === "trocar"], ["VERIFICAR / INSPECIONAR", (a) => a.acao === "verificar"], ["SERVIÇO OU AJUSTE", (a) => a.acao === "fazer"]];
    for (const [t, f] of grp) { const xs = rep.achados.filter(f); if (!xs.length) continue; L.push("", t + ":");
      for (const a of xs) { L.push(`• [${a.urg}] ${strip(a.t)} (${a.sis}; confiança ${a.conf})`); for (const e of a.ev) L.push(`    - ${strip(e)}`); if (a.fz) L.push(`    → ${strip(a.fz)}`); } }
    if (rep.bons.length) L.push("", "SEM PROBLEMA NO QUE FOI AVALIADO:", ...rep.bons.map((b) => "• " + strip(b)));
    if (rep.hist.length) L.push("", "HISTÓRICO RECENTE DO CARRO:", ...rep.hist.map((b) => "• " + strip(b)));
    if (rep.obs.length) L.push("", "OBSERVAÇÕES:", ...rep.obs.map((b) => "• " + strip(b)));
    if (rep.pulados.length) L.push("", "NÃO AVALIADO:", ...rep.pulados.map((b) => "• " + strip(b)));
    return L.join("\n");
  }

  // relatório em HTML (o mesmo no painel e na página da oficina). Só usa variáveis de cor que as duas páginas têm.
  const COR = ["var(--ok)", "var(--speed, var(--info, #2ec8ff))", "var(--warn)", "var(--bad)"];
  const ACAO = { trocar: "🔧 Provável troca", verificar: "🔎 Verificar", fazer: "🛠️ Serviço ou ajuste" };
  function html(rep, o = {}) {
    const rich = (s) => esc(s).replace(/&lt;(\/?)b&gt;/g, "<$1b>");
    const chip = (t, c) => `<span style="display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin:0 4px 4px 0;color:${c};border:1px solid ${c}">${esc(t)}</span>`;
    const item = (a) => `<details style="border-left:4px solid ${COR[a.sev] || COR[0]};padding:10px 12px;border-radius:10px;background:color-mix(in srgb, ${COR[a.sev] || COR[0]} 7%, transparent);margin-top:8px"${o.open ? " open" : ""}>
      <summary style="cursor:pointer;font-weight:700;line-height:1.35">${rich(a.t)}<div style="margin-top:6px;font-weight:400">${chip(a.urg, COR[a.sev] || COR[0])}${chip(ACAO[a.acao] || a.acao, "var(--muted)")}${chip("confiança " + a.conf, "var(--muted)")}${chip(a.sis, "var(--muted)")}</div></summary>
      <div style="font-size:14px;line-height:1.5;margin-top:6px"><div><b>Por quê:</b> ${rich(a.pq)}</div>
      ${(a.ev || []).length ? `<div style="margin-top:6px"><b>Evidências:</b><ul style="margin:4px 0 0;padding-left:18px">${a.ev.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>` : ""}
      ${a.fz ? `<div style="margin-top:6px"><b>O que fazer:</b> ${rich(a.fz)}</div>` : ""}${a.teste && TNAME[a.teste] ? `<div style="margin-top:6px;color:var(--muted)">Teste guiado para confirmar: <b>${TNAME[a.teste]}</b></div>` : ""}</div></details>`;
    const list = (t, xs, ic) => xs.length ? `<details style="margin-top:12px"${o.open ? " open" : ""}><summary style="cursor:pointer;font-weight:700">${t} (${xs.length})</summary><ul style="margin:6px 0 0;padding-left:4px;list-style:none;font-size:14px;line-height:1.5">${xs.map((x) => `<li>${ic} ${esc(x)}</li>`).join("")}</ul></details>` : "";
    const d = new Date(rep.ts).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    return `<div class="ckrep"><div style="border-left:5px solid ${COR[rep.nivel] || COR[0]};padding:4px 0 4px 12px"><b style="font-size:17px">${esc(rep.rotulo)}</b><div style="font-size:14px;color:var(--muted);margin-top:4px;line-height:1.45">${esc(rep.resumo)}<br>Feito em ${d}. São hipóteses a partir das suas respostas e das medições: confirme antes de trocar peça.</div></div>
      ${(rep.achados || []).map(item).join("")}${list("✅ Sem problema no que foi avaliado", rep.bons || [], "✓")}${list("🕘 Histórico recente do carro", rep.hist || [], "•")}${list("ℹ️ Observações", rep.obs || [], "•")}${list("⏭️ Não avaliado", rep.pulados || [], "○")}</div>`;
  }

  const API = { Q, F, SIS, avaliar, texto, html, TNAME };
  if (typeof module !== "undefined" && module.exports) module.exports = API;       // Node: testes das regras
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // =================================================================================================================
  // 3. Passo a passo
  // =================================================================================================================
  const $ = (s) => document.querySelector(s);
  const KEY = "obdCheckupRun", LAST = "obdCheckupLast", RESUME_H = 12;
  const load = (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } };
  const store = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch { } };
  let K = null, live = null, st = null, ser = [], drive = null, active = false;
  const V = () => live?.values || {};
  const col = (s, k) => s.map((x) => x.v[k]).filter((x) => x != null);
  const lam = (s) => col(s, "o2c1_lambda").filter((x) => x < 1.9);
  const volts = (s) => { const e = col(s, "ecu_volt"); return e.length ? e : col(s, "batt"); };

  window.addEventListener("obdlive", (e) => {
    live = e.detail; if (!active || !st) return;
    const v = V(), now = Date.now();
    if (live.engine_on && v.ect != null) st.m.ectMax = Math.max(st.m.ectMax ?? 0, v.ect);
    // segurança em qualquer etapa: passou de 106 °C, manda desligar na hora e não pede mais nada com o motor ligado
    if (live.engine_on && v.ect >= 106 && !st.m.hot) { st.m.hot = true; store(KEY, st); K?.beep(440, 300, 2); K?.speak("Atenção. Temperatura alta demais. Desligue o motor agora."); }
    if (!ser.length || now - ser[ser.length - 1].t >= 2000) { ser.push({ t: now, ect: v.ect, on: !!live.engine_on }); if (ser.length > 2400) ser.shift(); }
    if (drive && live.engine_on) {                      // volta curta: guarda só o resumo, amostra por amostra
      const dt = drive.last ? Math.min(5, (now - drive.last) / 1000) : 0; drive.last = now;
      const sp = v.speed || 0; drive.km += sp * dt / 3600; if (sp > drive.vmax) drive.vmax = sp;
      if (v.ect != null) { drive.ectMax = Math.max(drive.ectMax, v.ect); drive.ectEnd = v.ect; }
      const u = v.ecu_volt ?? v.batt; if (u != null) drive.vMin = Math.min(drive.vMin, u);
      if (sp > 20) { drive.n++; if (v.stft1 != null && (v.o2c1_lambda ?? 1) < 1.9) drive.st.push(v.stft1); if (v.o2c1_lambda != null && v.o2c1_lambda < 1.9) drive.la.push(v.o2c1_lambda); }
    }
  });

  const save = () => { st.ts = Date.now(); store(KEY, st); };
  const skip = (txt) => { if (!st.skip.includes(txt)) st.skip.push(txt); };
  const title = (t) => { const e = $("#tstTitle"); if (e) e.textContent = t; };
  const clearBtns = () => { const b = $("#tstBtns"); if (b) { b.innerHTML = ""; b.onclick = null; } };
  // etapa cronometrada com botão de pular
  async function timed(sec, big, small, tone = "", fn = null, talk = big, skipLabel = null) {
    let skipped = false;
    if (skipLabel) K.buttons([{ t: skipLabel, v: 1 }]).then(() => { skipped = true; });
    const s = await K.phase(sec, big, small, tone, (w, el) => skipped ? "stop" : fn ? fn(w, el) : null, talk);
    if (skipLabel) clearBtns();
    return { s, skipped };
  }
  // espera uma condição (ex.: "ligue o motor"); false se o dono pulou ou o tempo acabou
  async function waitFor(pred, sec, big, small, talk, skipLabel = "Pular esta etapa") {
    if (pred()) return true;
    K.beep(880, 150);
    const r = await timed(sec, big, small, "go", () => pred() ? "stop" : null, talk, skipLabel);
    return !r.skipped && pred();
  }
  const engineOn = () => waitFor(() => !!live?.engine_on, 120, "Ligue o motor", "Esta etapa mede com o motor ligado, carro parado e em ponto morto.", "Ligue o motor para continuar.");
  function readCodes() {
    if (!live || !live.dtc) return;
    const u = (a, b) => [...new Set([...(a || []), ...(b || [])])];
    st.m.dtc = u(st.m.dtc, live.dtc.confirmed); st.m.pend = u(st.m.pend, live.dtc.pending); st.m.mil = !!(st.m.mil || live.mil);
    if (live.readiness?.total) { st.m.readyOk = live.readiness.ready; st.m.readyTotal = live.readiness.total; }
  }
  // pergunta com opções; multi = marca várias e toca em Continuar. Devolve o valor, a lista, "-" (pulou) ou "<" (voltar)
  function pick(q, back) {
    return new Promise((res) => {
      const box = $("#tstBtns"), sel = new Set();
      const paint = () => { box.innerHTML = q.o.map((o, i) => `<button class="btn${!q.multi && i === 0 ? " big" : ""}" type="button" data-i="${i}"${sel.has(i) ? ' style="outline:2px solid var(--ok)"' : ""}>${q.multi && i ? (sel.has(i) ? "☑ " : "☐ ") : ""}${esc(o[1])}</button>`).join("")
        + (q.multi ? `<button class="btn big" type="button" data-i="ok"${sel.size ? "" : " disabled"}>Continuar com ${sel.size} marcado${sel.size === 1 ? "" : "s"}</button>` : "")
        + `<button class="btn" type="button" data-i="skip" style="opacity:.75">Não sei · pular</button>` + (back ? `<button class="btn" type="button" data-i="back" style="opacity:.75">← Voltar à pergunta anterior</button>` : ""); };
      const end = (v) => { box.innerHTML = ""; box.onclick = null; res(v); };
      box.onclick = (e) => { const b = e.target.closest("[data-i]"); if (!b) return; const i = b.dataset.i;
        if (i === "skip") return end("-"); if (i === "back") return end("<"); if (i === "ok") return sel.size ? end([...sel].map((j) => q.o[j][0])) : null;
        if (!q.multi) return end(q.o[+i][0]);
        if (+i === 0) return end([q.o[0][0]]);
        sel.has(+i) ? sel.delete(+i) : sel.add(+i); paint(); };
      paint();
    });
  }
  async function questions(g, head) {
    const qs = Q.filter((q) => q.g === g);
    for (let i = 0; i < qs.length; i++) {
      const q = qs[i]; if (st.ans[q.id] != null) continue;
      title(`Diagnóstico geral · ${head} ${i + 1}/${qs.length}`);
      K.say(esc(q.q), q.how || "Toque na resposta que mais parece com o seu carro."); K.speak(q.q);
      const v = await pick(q, i > 0);
      if (v === "<") { delete st.ans[qs[i - 1].id]; i -= 2; continue; }
      st.ans[q.id] = v; save();
    }
  }

  // ------------------------------------------------------------------------------------------------ etapas
  const STEPS = [
    { id: "insp", n: "Olhar o carro", async run() {
      if (live?.engine_on || (V().ect ?? 0) > 50) { K.say("Inspeção visual", "O motor está ligado ou quente. Óleo e líquido de arrefecimento se conferem com o motor <b>frio e desligado</b>: responda pelo que você viu da última vez, ou toque em <b>Não sei · pular</b>.<br>⚠️ Não abra a tampa do radiador nem encoste no escapamento."); K.speak("O motor está quente. Não abra a tampa do radiador. Responda o que souber e pule o resto."); await K.buttons([{ t: "Entendi", v: 1, main: true }]); }
      else { K.say("Abra o capô", "Motor desligado e frio. Tenha papel e uma lanterna (a do celular serve). São 13 itens para olhar, um por vez."); K.speak("Abra o capô. Vou pedir para você olhar alguns itens, um por vez."); await K.buttons([{ t: "Capô aberto, vamos", v: 1, main: true }]); }
      await questions("insp", "olhar");
    } },
    { id: "koeo", n: "Bateria e central", async run() {
      title("Diagnóstico geral · bateria");
      if (live?.engine_on) {
        const r = await K.ask("O motor está ligado", "Para medir a bateria em repouso e na partida, <b>desligue o motor</b> e deixe só a ignição ligada (painel aceso, sem dar a partida).", [{ t: "Vou desligar e medir", v: 1, main: true }, { t: "Pular bateria e partida", v: 0 }], "O motor está ligado. Desligue e deixe só a ignição ligada, ou pule esta etapa.");
        if (!r) { skip("Bateria em repouso e na partida (o motor já estava ligado)"); readCodes(); st.m.ect0 = V().ect; st.m.tOn = Date.now(); return; }
        st.m.vRestHot = true;
      }
      if (!await waitFor(() => K.C.koeo.ok(), 180, "Ligue só a ignição", "Gire a chave até o painel acender, <b>sem dar a partida</b>. Faróis e ar desligados.", "Ligue só a ignição, sem dar a partida.")) { skip("Bateria em repouso e na partida (sem leitura com a ignição ligada)"); return; }
      const a = (await timed(8, "Não dê a partida ainda", "Medindo a bateria em repouso e lendo a central", "", null, "Medindo a bateria. Não dê a partida ainda.")).s;
      st.m.vRest = r1(mean(col(a, "batt")) ?? mean(volts(a)), 2); readCodes();
      if (st.m.vRest != null) K.log(st.m.vRest >= 12.4, `Bateria em repouso: <b>${nf(st.m.vRest, 1)} V</b>`);
      const cs = [...(st.m.dtc || []), ...(st.m.pend || [])]; K.log(!cs.length && !st.m.mil, cs.length ? `Códigos na central: <b>${cs.map(esc).join(", ")}</b>` : st.m.mil ? "Luz da injeção acesa" : "Central sem códigos de falha");
    } },
    { id: "crank", n: "Partida", async run() {
      title("Diagnóstico geral · partida");
      if (live?.engine_on) { if (st.m.tOn == null) { st.m.ect0 = V().ect; st.m.tOn = Date.now(); } return; }
      for (;;) {
        K.beep(880, 200); let started = 0, minV = 99; const t0 = Date.now(), ect0 = V().ect;
        const b = (await timed(30, "Dê a partida agora", "", "go", (w) => { const x = col(w, "batt"); if (x.length) minV = Math.min(minV, ...x);
          if (live?.engine_on && !started) started = Date.now(); if (started && Date.now() - started > 10000) return "stop";
          return started ? { big: "Motor pegou", small: "Medindo a carga do alternador. Não acelere.", tone: "go" } : { small: `Carro em ponto morto, freio de mão puxado · mínima até agora ${minV < 99 ? nf(minV, 1) + " V" : "—"}` }; }, "Pode dar a partida.")).s;
        if (minV < 99) st.m.vCrank = r1(minV, 2);
        if (started) {
          const after = b.filter((x) => x.t > started + 3000);
          st.m.noStart = false; st.m.tOn = started; st.m.ect0 = ect0; st.m.tStart = r1((started - t0) / 1000); st.m.vCharge = r1(mean(volts(after)), 2); st.m.coldRpm = r1(mean(col(after, "rpm")), 0);
          K.log(minV >= 9.6, `Partida: mínima de <b>${nf(minV, 1)} V</b>`); if (st.m.vCharge != null) K.log(st.m.vCharge >= 13.2 && st.m.vCharge <= 15, `Alternador: <b>${nf(st.m.vCharge, 1)} V</b> com o motor ligado`);
          return;
        }
        const r = await K.ask("O motor não pegou", "Se ele girou e não pegou, espere 10 segundos antes de tentar de novo.", [{ t: "Tentar de novo", v: 1, main: true }, { t: "Ele não pega: terminar com o que já tenho", v: 0 }, { t: "Pular esta etapa", v: 2 }], "O motor não pegou. Quer tentar de novo?");
        if (r === 0) { st.m.noStart = true; return; }
        if (r === 2) { skip("Partida (pulada)"); return; }
      }
    } },
    { id: "sint", n: "Como o carro anda", async run() {
      if (!st.m.noStart) { K.say("Enquanto o motor aquece", "Deixe o motor em marcha lenta, sem acelerar. Agora são perguntas sobre como o carro anda no dia a dia."); K.speak("Deixe o motor em marcha lenta. Enquanto ele aquece, responda algumas perguntas sobre o carro."); await K.buttons([{ t: "Vamos", v: 1, main: true }]); }
      await questions("sint", "perguntas");
    } },
    { id: "aquece", n: "Aquecimento", on: true, async run() {
      title("Diagnóstico geral · aquecimento");
      let said = 0;
      if ((V().ect ?? 0) < 80) {
        const r = await timed(900, "Aquecendo", "", "", () => { const e = V().ect; if (e == null) return {}; if (e >= 80) return "stop";
          if (e - said >= 10) { said = Math.floor(e / 10) * 10; K.speak(`Motor em ${nf(e)} graus.`, false); }
          return { big: `${nf(e)} °C`, small: "Esperando chegar a 80 °C para medir a mistura. Não acelere; pode deixar o celular apoiado." }; }, "Agora é só esperar o motor aquecer. Eu aviso.", "Pular: medir com o motor como está");
        if (r.skipped) skip("Espera do aquecimento até 80 °C (pulada)");
      }
      // quanto aqueceu por minuto, parado, desde a partida (só vale se começou frio)
      const w = ser.filter((x) => x.on && x.ect != null && (st.m.tOn == null || x.t >= st.m.tOn)), cut = w.findIndex((x) => x.ect >= 80), u = cut > 0 ? w.slice(0, cut + 1) : w;
      if (u.length > 5) { const a = u[0], b = u[u.length - 1], min = (b.t - a.t) / 60000; if (a.ect < 60 && b.ect - a.ect >= 15 && min >= 3) { st.m.warmRate = r1((b.ect - a.ect) / min); st.m.warmFrom = a.ect; st.m.warmTo = b.ect; K.log(st.m.warmRate >= 2.5, `Aquecimento: ${nf(st.m.warmRate, 1)} °C por minuto, de ${nf(a.ect)} a ${nf(b.ect)} °C`); } }
    } },
    { id: "lenta", n: "Marcha lenta", on: true, async run() {
      title("Diagnóstico geral · marcha lenta"); K.beep(880);
      const all = (await timed(45, "Marcha lenta", "Pé fora do pedal, ar e faróis desligados. Medindo 45 segundos.", "", null, "Marcha lenta. Tire o pé do pedal e desligue o ar.")).s, s = all.filter((x) => x.t > (all[0]?.t ?? 0) + 10000);
      const rp = col(s, "rpm"), r0 = mean(rp); if (r0 == null) { skip("Marcha lenta (sem leitura da rotação)"); return; }
      Object.assign(st.m, { idleRpm: r1(r0, 0), idleSd: r1(sd(rp), 0), idleDips: rp.filter((x) => x < r0 - 100).length, idleStft: r1(mean(col(s, "stft1"))), idleLam: r1(mean(lam(s)), 3), idleEct: r1(mean(col(s, "ect")), 0), idleMap: r1(mean(col(s, "map")), 0) });
      st.keep = { p: s.filter((x) => x.v.pedal_d != null).slice(-20).map((x) => [x.v.pedal_d, x.v.pedal_e, x.v.tps, x.v.tps_b]) };
      K.log(st.m.idleSd < 40 && st.m.idleDips < 2, `Marcha lenta: <b>${nf(r0)} rpm</b>, variação ±${nf(st.m.idleSd)} rpm`);
      if (st.m.idleStft != null) K.log(Math.abs(st.m.idleStft) <= 10, `Ajuste de combustível na lenta: <b>${sg(st.m.idleStft, 1)} %</b>${st.m.idleLam != null ? ` · λ ${nf(st.m.idleLam, 2)}` : ""}`);
    } },
    { id: "giro", n: "2.500 rpm", on: true, async run() {
      title("Diagnóstico geral · 2.500 rpm"); K.beep(880);
      const raw = await K.phase(30, "", "", "", K.hold(2500, 300), "Agora acelere devagar e segure em dois mil e quinhentos giros, por trinta segundos.");
      const hi = K.steady(raw, 2500, 450).slice(3);
      K.beep(660); const ret = await K.phase(8, "Pode soltar", "Medindo a volta à marcha lenta", "", null, "Pode soltar.");
      if (hi.length < 10) { skip("Mistura e catalisador a 2.500 rpm (a rotação não ficou parada)"); return; }
      const post = col(hi, "o2s2_v"), la = lam(hi);
      Object.assign(st.m, { hiStft: r1(mean(col(hi, "stft1"))), hiLam: r1(mean(la), 3) });
      if (post.length >= 5) Object.assign(st.m, { o2Mean: r1(mean(post), 2), o2Range: r1(Math.max(...post) - Math.min(...post), 2) });
      const rr = col(ret.filter((x) => x.t > (ret[0]?.t ?? 0) + 1500), "rpm"); if (rr.length > 3) st.m.retLow = r1(Math.min(...rr), 0);
      // sensores em dupla: só leituras em que o pedal estava parado (as duas leituras não são do mesmo instante)
      const pts = [...(st.keep?.p || []), ...raw.filter((x, i) => i && x.v.pedal_d != null && raw[i - 1].v.pedal_d != null && Math.abs(x.v.pedal_d - raw[i - 1].v.pedal_d) < 1.5).map((x) => [x.v.pedal_d, x.v.pedal_e, x.v.tps, x.v.tps_b])];
      const res = (ix, iy) => { const p = pts.filter((x) => x[ix] != null && x[iy] != null), xs = p.map((x) => x[ix]); if (p.length < 12 || Math.max(...xs) - Math.min(...xs) < 4) return null; const f = K.fit(xs, p.map((x) => x[iy])); return f ? r1(f.res) : null; };
      const pd = res(0, 1), tb = res(2, 3); if (pd != null) st.m.pedalDE = pd; if (tb != null) st.m.tpsAB = tb; delete st.keep;
      if (st.m.hiStft != null) K.log(Math.abs(st.m.hiStft) <= 10, `Ajuste de combustível a 2.500 rpm: <b>${sg(st.m.hiStft, 1)} %</b>`);
      if (st.m.o2Range != null) K.log(st.m.o2Range <= 0.5, `Sonda depois do catalisador: ${nf(st.m.o2Mean, 2)} V, variou ${nf(st.m.o2Range, 2)} V`);
    } },
    { id: "carga", n: "Tudo ligado", on: true, async run() {
      title("Diagnóstico geral · alternador com carga");
      const a = (await timed(10, "Tudo desligado", "Medindo sem carga", "", null, "Deixe tudo desligado.")).s; K.beep(880, 150);
      const b = (await timed(25, "Ligue tudo agora", "Ar-condicionado no máximo + farol alto + desembaçador traseiro", "go", null, "Ligue o ar no máximo, o farol alto e o desembaçador traseiro.")).s; K.beep(660, 150);
      await timed(5, "Desligue tudo", "", "", null, "Pode desligar tudo.");
      const late = b.filter((x) => x.t > (b[0]?.t ?? 0) + 8000), r0 = mean(col(a, "rpm")), r1v = mean(col(late, "rpm"));
      if (r0 == null || r1v == null) { skip("Alternador com carga (sem leitura)"); return; }
      Object.assign(st.m, { loadRpm0: r1(r0, 0), loadRpm1: r1(r1v, 0), vLoad0: r1(mean(volts(a)), 2), vLoad: r1(mean(volts(late)), 2) });
      if (st.m.vCharge == null && st.m.vLoad0 != null) st.m.vCharge = st.m.vLoad0;
      if (st.m.vLoad != null) K.log(st.m.vLoad >= 13, `Com tudo ligado: <b>${nf(st.m.vLoad, 1)} V</b> e ${nf(r1v)} rpm (sem carga: ${nf(st.m.vLoad0, 1)} V e ${nf(r0)} rpm)`);
    } },
    { id: "fan", n: "Ventoinha", on: true, async run() {
      title("Diagnóstico geral · ventoinha");
      const go = await K.ask("Acompanhar a ventoinha?", "Parado em marcha lenta, a temperatura sobe até a ventoinha ligar e depois cai. Leva <b>até 10 minutos</b> e você pode pular a qualquer momento.", [{ t: "Acompanhar a ventoinha", v: 1, main: true }, { t: "Pular", v: 0 }], "Quer acompanhar um ciclo da ventoinha? Leva até dez minutos.");
      if (!go) { skip("Ciclo da ventoinha (pulado)"); return; }
      let peak = 0, onAt = null, low = null, done = false;
      const r = await timed(600, "Aguardando a ventoinha", "", "", () => { const e = V().ect; if (e == null) return {};
        if (e > peak) peak = e;
        if (onAt == null && peak >= 94 && e <= peak - 2) { onAt = peak; K.beep(990, 160, 2); K.speak("A ventoinha ligou."); }
        if (onAt != null) { low = low == null ? e : Math.min(low, e); if (e >= low + 1 && low <= onAt - 3) { done = true; return "stop"; } }
        if (peak >= 106) return "stop";
        return { big: `${nf(e)} °C`, small: onAt == null ? `Subindo. A ventoinha costuma ligar entre 93 e 101 °C` : `Ligou em ${nf(onAt)} °C · baixando (mínimo ${nf(low)} °C)`, tone: onAt != null ? "go" : e >= 103 ? "bad" : "" }; }, "Deixe em marcha lenta. Eu aviso quando a ventoinha ligar.", "Pular a ventoinha");
      st.m.ectMax = Math.max(st.m.ectMax ?? 0, peak);
      if (peak >= 106 && !done) { K.say("🛑 Desligue o motor", `A temperatura chegou a ${nf(peak)} °C sem a ventoinha segurar.`, "bad"); K.beep(440, 300, 2); await K.buttons([{ t: "Desliguei", v: 1, main: true }]); st.m.hot = true; return; }
      if (onAt != null) { Object.assign(st.m, { fanOn: onAt, fanLow: low, fanDone: done || r.skipped ? (done ? true : null) : false }); if (st.m.fanDone == null) delete st.m.fanDone; K.log(onAt <= 102 && done, `Ventoinha: ligou em ${nf(onAt)} °C${done ? ` e baixou até ${nf(low)} °C` : ""}`); }
      else skip(`Ciclo da ventoinha (${r.skipped ? "pulado" : "não ligou em 10 min"}; chegou a ${nf(peak)} °C)`);
    } },
    { id: "volta", n: "Volta curta", on: true, async run() {
      title("Diagnóstico geral · volta curta");
      if (st.m.hot) { skip("Volta curta (o motor superaqueceu parado)"); return; }
      const go = await K.ask("Dar uma volta curta?", "Opcional: uns <b>5 a 10 minutos</b>, com um trecho perto de 60 km/h e, se der, uma subida. Mostra o que só aparece com o carro andando (mistura com carga, temperatura e alternador).<br><br>⚠️ <b>Não mexa no celular dirigindo.</b> Deixe-o guardado; ao voltar, pare e toque em Terminei.",
        [{ t: "Vou dar a volta", v: 1, main: true }, { t: "Pular e ver o resultado", v: 0 }], "Quer dar uma volta curta para completar o diagnóstico? É opcional.");
      if (!go) { skip("Volta curta (pulada)"); return; }
      drive = { t0: Date.now(), last: 0, km: 0, vmax: 0, n: 0, st: [], la: [], ectMax: 0, ectEnd: null, vMin: 99 };
      K.speak("Pode sair. Guarde o celular e dirija normalmente. Quando voltar e parar, toque em Terminei.");
      await timed(1800, "Boa volta", "", "go", () => ({ big: (V().speed || 0) > 3 ? "Dirigindo" : "Parado", small: `${nf(drive.km, 1)} km · ${nf((Date.now() - drive.t0) / 60000)} min · não mexa no celular dirigindo` }), "", "Terminei a volta (carro parado)");
      const d = drive; drive = null;
      if (d.n < 60 || d.st.length < 30) { skip(`Volta curta (curta demais para avaliar: ${nf(d.km, 1)} km)`); return; }
      st.m.drv = { km: r1(d.km), min: r1((Date.now() - d.t0) / 60000, 0), vmax: r1(d.vmax, 0), stft: r1(mean(d.st)), lam: r1(mean(d.la), 3), ectMax: d.ectMax || null, ectEnd: d.ectEnd, vMin: d.vMin < 99 ? r1(d.vMin, 2) : null };
      K.log(Math.abs(st.m.drv.stft) <= 12, `Rodando ${nf(d.km, 1)} km: ajuste de combustível ${sg(st.m.drv.stft, 1)} %, temperatura máxima ${nf(d.ectMax)} °C`);
    } },
  ];

  async function history() {
    const get = (u) => Promise.race([fetch(u).then((r) => r.ok ? r.json() : null), new Promise((r) => setTimeout(() => r(null), 7000))]).catch(() => null);
    const [dtc, diag, alerts, maint] = await Promise.all([get("/api/dtc"), get("/api/diagnostics?days=60"), get("/api/alerts?days=90"), get("/api/maintenance")]);
    const h = { tests: (load("obdTests") || []).slice(0, 20) };
    if (dtc) { const d = {}; for (const s of dtc.state || []) d[s.code] = s.description; const mk = (c) => ({ code: c, desc: d[c] || "" }); h.dtc = { confirmed: (dtc.active?.confirmed || []).map(mk), pending: (dtc.active?.pending || []).map(mk) }; }
    if (diag?.insight?.sistemas) h.insight = { sistemas: diag.insight.sistemas.map((s) => ({ id: s.id, nome: s.nome, status: s.status, resumo: s.resumo, fazer: s.fazer })) };
    if (Array.isArray(alerts)) { const by = {}; for (const a of alerts) { if (Date.now() - (a.ts_end || a.ts) > 14 * 864e5) continue; const k = a.title; by[k] ||= { title: a.title, severity: a.severity, n: 0 }; by[k].n++; if (a.severity === "crit") by[k].severity = "crit"; } h.alerts = Object.values(by); }
    if (maint?.itens) h.maint = maint.itens.map((i) => ({ nome: i.nome, status: i.status, texto: i.texto }));
    return h;
  }

  async function finishRun() {
    title("Diagnóstico geral · resultado"); readCodes();
    K.say("Montando o relatório", "Juntando as respostas, as medições e o histórico do carro…"); K.speak("Montando o relatório.");
    const rep = avaliar(st, await history());
    store(LAST, rep); store(KEY, null);
    const lines = rep.achados.slice(0, 5).map((a) => `${strip(a.t)} (${a.urg.toLowerCase()})`);
    const rec = { id: "geral", title: "Diagnóstico geral", ts: rep.ts, verdict: rep.achados.length ? `${rep.achados.length} ponto(s) de atenção` : "tudo certo", lines: lines.length ? lines : rep.bons.slice(0, 4) };
    try { const all = load("obdTests") || []; all.unshift(rec); localStorage.setItem("obdTests", JSON.stringify(all.slice(0, 40))); } catch { }
    // no painel do Pi o relatório inteiro fica guardado (página da oficina) e vira uma linha no diário; se a rota não existir, vai só a linha
    const note = () => fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "teste", ts: rep.ts, text: `Diagnóstico geral: ${rec.verdict}. ${lines.join("; ")}`.slice(0, 480) }) }).catch(() => { });
    fetch("/api/checkup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rep) }).then((r) => { if (!r.ok) note(); }).catch(note);
    K.beep(rep.nivel >= 2 ? 440 : 990, 200, rep.nivel >= 2 ? 2 : 1);
    K.speak(`Diagnóstico concluído. ${strip(rep.rotulo).replace(/^\W+/, "")}. ${rep.achados.length ? `Encontrei ${rep.achados.length} ponto${rep.achados.length > 1 ? "s" : ""} de atenção. O principal: ${strip(rep.achados[0].t)}.` : "Não encontrei nada de errado no que foi avaliado."}`);
    await show(rep);
  }

  // tela do relatório (também serve para rever o último)
  async function show(rep) {
    K.say(esc(rep.rotulo), "", rep.nivel >= 3 ? "bad" : rep.nivel <= 1 ? "go" : "");
    $("#tstLog").innerHTML = html(rep);
    const tests = [...new Set(rep.achados.map((a) => a.teste).filter((t) => t && TNAME[t]))].slice(0, 3);
    // parecer da IA: só no painel do Pi e só com a chave configurada
    const ai = !window.OBD?.cloud?.() && await fetch("/api/ai/settings").then((r) => r.ok ? r.json() : null).then((c) => !!c && !!(c.provider === "openai" ? c.openai_key : c.anthropic_key)).catch(() => false);
    for (;;) {
      const v = await K.buttons([{ t: "📋 Copiar para mandar ao mecânico", v: "copy", main: true }, { t: "👨‍🔧 Abrir a página da oficina (imprimir ou compartilhar)", v: "page" }, ...(ai ? [{ t: "🤖 Pedir o parecer da IA", v: "ai" }] : []),
        ...tests.map((t) => ({ t: `▶ Confirmar com o teste: ${TNAME[t]}`, v: "t:" + t })), { t: "Fechar", v: "x" }]);
      if (v === "x") break;
      if (v === "copy") { let ok = false; try { await navigator.clipboard.writeText(rep.mecanico); ok = true; } catch { } K.say(esc(rep.rotulo), ok ? "Texto copiado. Cole no WhatsApp do mecânico." : "Não consegui copiar sozinho: abra a página da oficina e compartilhe por lá."); }
      if (v === "page") window.open("/oficina#geral", "_blank", "noopener");
      if (v === "ai") await opinion(rep);
      if (v.startsWith("t:")) { active = false; K.close(); render(); return K.launch(v.slice(2)); }
    }
    active = false; K.close(); render();
  }
  async function opinion(rep) {
    const box = (h) => { let e = $("#ckAi"); if (!e) { $("#tstLog").insertAdjacentHTML("afterbegin", `<div id="ckAi" class="alert" style="--s:var(--speed);display:block"></div>`); e = $("#ckAi"); } e.innerHTML = h; };
    box("🤖 Enviando as evidências para a IA…");
    try {
      const r = await fetch("/api/checkup/ai", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rep) }), j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || "indisponível neste painel");
      for (let i = 0; i < 90; i++) {
        box(`🤖 A IA está analisando as evidências… (${i * 4} s)`); await new Promise((res) => setTimeout(res, 4000));
        if ($("#tstModal").hidden) return;
        const c = ((await fetch("/api/ai/chat").then((x) => x.json()).catch(() => [])) || []).find((x) => x.id === j.id);
        if (c && c.status !== "running") { box(`<b>🤖 Parecer da IA</b> <span class="muted">(segunda opinião; também é hipótese)</span><div style="margin-top:6px;line-height:1.5">${window.OBD?.md ? window.OBD.md(c.answer || "") : esc(c.answer || "").replace(/\n/g, "<br>")}</div>`); return; }
      }
      box("A IA demorou demais. A resposta aparece depois em Diagnóstico → Laudo da IA.");
    } catch (e) { box(`Parecer da IA indisponível: ${esc(e.message)}. O relatório acima não depende dela.`); }
  }

  async function start() {
    K = window.obdTestKit; if (!K) return;
    const old = load(KEY), fresh = old && old.v === 1 && Date.now() - old.ts < RESUME_H * 3600e3 && (old.done || []).length;
    K.open("Diagnóstico geral", ["rpm", "ect", "ecu_volt", "stft1", "o2c1_lambda", "batt"]);
    st = null; ser = []; drive = null; active = true;
    try {
      if (fresh) {
        const nx = STEPS.find((s) => !old.done.includes(s.id));
        const r = await K.ask("Continuar de onde parou?", `Você começou um diagnóstico ${new Date(old.t0).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} e parou ${nx ? `antes de <b>${nx.n}</b>` : "no fim"}.`, [{ t: "Continuar", v: 1, main: true }, { t: "Começar de novo", v: 0 }, { t: "Fechar", v: -1 }], "Quer continuar o diagnóstico de onde parou?");
        if (r === -1) { active = false; return K.close(); }
        if (r) st = old;
      }
      if (!st) {
        await K.ready(`Um passo a passo do <b>carro inteiro</b>, em uns 25 minutos. O celular <b>fala</b> cada etapa e mede pelo leitor do carro:<br>
          1. <b>olhar</b> óleo, líquidos, bateria, correia e pneus (motor frio e desligado é o ideal);<br>2. <b>bateria e partida</b>;<br>3. <b>perguntas</b> sobre como o carro anda, enquanto o motor aquece;<br>
          4. <b>medições</b> com o motor quente: marcha lenta, mistura, catalisador, alternador e ventoinha;<br>5. uma <b>volta curta</b>, se você quiser.<br><br>
          No fim sai o relatório: o que provavelmente está ruim, o que trocar ou verificar e a urgência. Dá para <b>pular</b> qualquer etapa e, se fechar, <b>continuar depois</b> de onde parou.<br><br>
          ⚠️ Carro no plano, em ponto morto, freio de mão puxado, em lugar aberto (nunca em garagem fechada com o motor ligado).`, [K.C.stopped]);
        st = { v: 1, t0: Date.now(), ts: Date.now(), ans: {}, m: {}, skip: [], done: [] }; save();
      }
      for (const s of STEPS) {
        if (st.done.includes(s.id)) continue;
        if (s.on && st.m.noStart) { skip(`${s.n} (o motor não pegou)`); st.done.push(s.id); save(); continue; }
        if (s.on && st.m.hot && s.id !== "volta") { skip(`${s.n} (motor superaquecido)`); st.done.push(s.id); save(); continue; }
        if (s.on && !await engineOn()) { skip(`${s.n} (motor desligado)`); st.done.push(s.id); save(); continue; }
        await s.run(); st.done.push(s.id); save();
      }
      await finishRun();
    } catch (e) {
      active = false; drive = null;
      if (e instanceof K.Cancel) { render(); return; }               // fechou no meio: o progresso ficou guardado
      console.error(e); K.say("O diagnóstico parou", esc(e.message) + "<br>O que já foi feito ficou guardado: abra de novo para continuar.", "bad"); K.buttons([{ t: "Fechar", v: 1 }]).then(() => { K.close(); render(); });
    }
  }
  async function review() {
    K = window.obdTestKit; const rep = load(LAST) || (await fetch("/api/checkup").then((r) => r.ok ? r.json() : null).catch(() => null))?.last;
    if (!K || !rep) return;
    K.open("Diagnóstico geral · último resultado", ["rpm", "ect", "ecu_volt"]); active = false;
    try { await show(rep); } catch (e) { if (!(e instanceof K.Cancel)) console.error(e); }
  }

  // cartão na aba Diagnóstico
  async function render() {
    const box = $("#ckBox"), btn = $("#ckBtn"); if (!box) return;
    const run = load(KEY), going = run && run.v === 1 && Date.now() - run.ts < RESUME_H * 3600e3 && (run.done || []).length;
    let rep = load(LAST);
    if (!rep) { rep = (await fetch("/api/checkup").then((r) => r.ok ? r.json() : null).catch(() => null))?.last || null; if (rep) store(LAST, rep); }
    if (btn) btn.textContent = going ? "Continuar" : rep ? "Fazer de novo" : "Começar";
    const d = (ts) => new Date(ts).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    box.innerHTML = (going ? `<div class="alert" style="--s:var(--warn)"><span class="ic">…</span><div><div class="t">Diagnóstico em andamento</div><div class="d">Começou ${d(run.t0)} · ${run.done.length} de ${STEPS.length} etapas feitas. Toque em Continuar.</div></div><span></span></div>` : "")
      + (rep ? `<div class="alert" style="--s:${COR[rep.nivel]};cursor:pointer" id="ckSee"><span class="ic">${rep.nivel >= 2 ? "!" : "✓"}</span><div><div class="t">${esc(rep.rotulo)}</div><div class="d">${d(rep.ts)} · ${esc(rep.resumo)}</div></div><span>›</span></div>`
        : going ? "" : `<div class="empty">Ainda não foi feito. Leva uns 25 minutos, com o celular ao lado do carro.</div>`);
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest?.("#ckBtn")) { start(); return; }
    if (e.target.closest?.("#ckSee")) { review(); return; }
    if (e.target.closest?.("#tstClose") && active) { active = false; drive = null; setTimeout(render, 0); }      // fechou no meio: o progresso ficou guardado
  });
  window.obdCheckup = Object.assign(API, { start, review, render });
  render();
})();
