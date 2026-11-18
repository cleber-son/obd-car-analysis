"""Códigos de falha (modos 03 / 07 / 0A): decodificação e descrições em português."""
from __future__ import annotations

# Lista de vigilância do corpo de borboleta / marcha lenta (alerta crítico imediato)
THROTTLE_WATCH = {
    "P0121", "P0122", "P0123", "P0221", "P0222", "P0223", "P0505", "P0506", "P0507", "P0638",
    "P2100", "P2101", "P2102", "P2103", "P2108", "P2111", "P2112", "P2118", "P2119", "P2122",
    "P2123", "P2127", "P2128", "P2135", "P2138", "P2176", "P1658", "P1659", "P1683", "P1684",
}

DESC = {
    "P0101": "Sensor MAF/MAP fora da faixa", "P0106": "Sensor MAP fora da faixa / desempenho",
    "P0107": "Sensor MAP sinal baixo", "P0108": "Sensor MAP sinal alto",
    "P0111": "Sensor de temperatura do ar fora da faixa", "P0112": "Temp. do ar sinal baixo", "P0113": "Temp. do ar sinal alto",
    "P0116": "Sensor de temperatura do motor fora da faixa", "P0117": "Temp. do motor sinal baixo", "P0118": "Temp. do motor sinal alto",
    "P0121": "Sensor de posição da borboleta A fora da faixa", "P0122": "Sensor da borboleta A sinal baixo",
    "P0123": "Sensor da borboleta A sinal alto", "P0128": "Motor demora a aquecer (válvula termostática)",
    "P0131": "Sonda O2 B1S1 tensão baixa", "P0132": "Sonda O2 B1S1 tensão alta", "P0133": "Sonda O2 B1S1 resposta lenta",
    "P0134": "Sonda O2 B1S1 sem atividade", "P0135": "Aquecedor da sonda O2 B1S1", "P0137": "Sonda O2 B1S2 tensão baixa",
    "P0138": "Sonda O2 B1S2 tensão alta", "P0141": "Aquecedor da sonda O2 B1S2",
    "P0171": "Mistura pobre (banco 1)", "P0172": "Mistura rica (banco 1)",
    "P0221": "Sensor da borboleta B fora da faixa", "P0222": "Sensor da borboleta B sinal baixo", "P0223": "Sensor da borboleta B sinal alto",
    "P0300": "Falhas de ignição aleatórias", "P0301": "Falha de ignição cilindro 1", "P0302": "Falha de ignição cilindro 2",
    "P0303": "Falha de ignição cilindro 3", "P0304": "Falha de ignição cilindro 4",
    "P0325": "Sensor de detonação", "P0335": "Sensor de rotação (CKP)", "P0340": "Sensor de fase (CMP)",
    "P0420": "Eficiência do catalisador abaixo do limite", "P0443": "Válvula de purga do cânister",
    "P0456": "Vazamento pequeno no EVAP", "P0497": "Fluxo baixo de purga EVAP",
    "P0505": "Controle de marcha lenta com falha", "P0506": "Marcha lenta abaixo do esperado", "P0507": "Marcha lenta acima do esperado",
    "P0562": "Tensão do sistema baixa", "P0563": "Tensão do sistema alta", "P0638": "Atuador da borboleta fora da faixa",
    "P0700": "Falha no sistema da transmissão",
    "P2100": "Circuito do motor da borboleta aberto", "P2101": "Motor da borboleta: desempenho / faixa",
    "P2102": "Motor da borboleta: circuito baixo", "P2103": "Motor da borboleta: circuito alto",
    "P2108": "Módulo de controle da borboleta: desempenho", "P2111": "Borboleta presa aberta", "P2112": "Borboleta presa fechada",
    "P2118": "Corrente do motor da borboleta fora da faixa", "P2119": "Borboleta: posição de repouso fora da faixa",
    "P2122": "Pedal D sinal baixo", "P2123": "Pedal D sinal alto", "P2127": "Pedal E sinal baixo", "P2128": "Pedal E sinal alto",
    "P2135": "Sensores da borboleta A/B divergentes", "P2138": "Sensores do pedal D/E divergentes",
    "P2176": "Posição de marcha lenta da borboleta não aprendida (fazer reaprendizado)",
    "P2195": "Sonda O2 B1S1 indica pobre", "P2196": "Sonda O2 B1S1 indica rica",
}

_CAT = {"P": "Motor/transmissão", "C": "Chassi", "B": "Carroceria", "U": "Rede de comunicação"}


def describe(code: str) -> str:
    if code in DESC:
        return DESC[code]
    return f"{_CAT.get(code[:1], 'Código')} — consultar manual ({'genérico' if code[1] in '02' else 'específico Honda'})"


def _code(b1: int, b2: int) -> str:
    return "PCBU"[b1 >> 6] + str((b1 >> 4) & 3) + f"{b1 & 0xF:X}{b2:02X}"


def parse(messages: list[bytes], mode: int, is_can: bool) -> list[str]:
    """Decodifica respostas 43/47/4A em lista de códigos (sem duplicatas)."""
    codes: list[str] = []
    for m in messages:
        if not m or m[0] != mode + 0x40:
            continue
        body = m[1:]
        if is_can and len(body) % 2 == 1:   # CAN: primeiro byte = quantidade
            body = body[1:]
        for i in range(0, len(body) - 1, 2):
            if body[i] == 0 and body[i + 1] == 0:
                continue
            c = _code(body[i], body[i + 1])
            if c not in codes:
                codes.append(c)
    return codes
