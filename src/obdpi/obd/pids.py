"""Tabela de PIDs do modo 01 (SAE J1979) com decodificação e nomes em português.

Cada PID gera um ou mais sinais: (chave, nome, unidade, casas decimais, função de decodificação).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Signal:
    key: str
    name: str
    unit: str
    decimals: int
    fn: Callable[[bytes], float | None]
    pid: int = 0


def _a(d): return d[0]
def _pct(d): return d[0] * 100 / 255
def _temp(d): return d[0] - 40
def _trim(d): return (d[0] - 128) * 100 / 128
def _ab(d): return d[0] * 256 + d[1]
def _o2v(d): return d[0] / 200
def _o2trim(d): return None if d[1] == 0xFF else (d[1] - 128) * 100 / 128
def _wide_lambda(d): return (d[0] * 256 + d[1]) * 2 / 65536
def _wide_v(d): return (d[2] * 256 + d[3]) * 8 / 65536
def _wide_ma(d): return (d[2] * 256 + d[3]) / 256 - 128
def _cat(d): return (d[0] * 256 + d[1]) / 10 - 40


T: dict[int, tuple[int, list[tuple]]] = {
    # pid: (bytes, [(key, nome, unidade, decimais, fn)])
    0x03: (2, [("fuel_status", "Estado do sistema de combustível", "", 0, _a)]),
    0x04: (1, [("load", "Carga do motor", "%", 1, _pct)]),
    0x05: (1, [("ect", "Temperatura do motor", "°C", 0, _temp)]),
    0x06: (1, [("stft1", "Ajuste curto combustível B1", "%", 1, _trim)]),
    0x07: (1, [("ltft1", "Ajuste longo combustível B1", "%", 1, _trim)]),
    0x08: (1, [("stft2", "Ajuste curto combustível B2", "%", 1, _trim)]),
    0x09: (1, [("ltft2", "Ajuste longo combustível B2", "%", 1, _trim)]),
    0x0A: (1, [("fuel_press", "Pressão de combustível", "kPa", 0, lambda d: d[0] * 3)]),
    0x0B: (1, [("map", "Pressão do coletor (MAP)", "kPa", 0, _a)]),
    0x0C: (2, [("rpm", "Rotação", "rpm", 0, lambda d: _ab(d) / 4)]),
    0x0D: (1, [("speed", "Velocidade", "km/h", 0, _a)]),
    0x0E: (1, [("timing", "Avanço de ignição", "°", 1, lambda d: d[0] / 2 - 64)]),
    0x0F: (1, [("iat", "Temperatura do ar admitido", "°C", 0, _temp)]),
    0x10: (2, [("maf", "Fluxo de ar (MAF)", "g/s", 2, lambda d: _ab(d) / 100)]),
    0x11: (1, [("tps", "Posição da borboleta", "%", 1, _pct)]),
    0x12: (1, [("sec_air", "Ar secundário", "", 0, _a)]),
    0x13: (1, [("o2_present", "Sondas presentes", "", 0, _a)]),
    **{0x14 + i: (2, [(f"o2s{i+1}_v", f"Sonda O2 S{i+1} tensão", "V", 3, _o2v),
                      (f"o2s{i+1}_trim", f"Sonda O2 S{i+1} ajuste", "%", 1, _o2trim)]) for i in range(8)},
    0x1C: (1, [("obd_std", "Padrão OBD", "", 0, _a)]),
    0x1D: (1, [("o2_present4", "Sondas presentes (4 bancos)", "", 0, _a)]),
    0x1E: (1, [("aux_input", "Entrada auxiliar (PTO)", "", 0, lambda d: d[0] & 1)]),
    0x1F: (2, [("run_time", "Tempo desde a partida", "s", 0, _ab)]),
    0x21: (2, [("dist_mil", "Distância com luz de injeção acesa", "km", 0, _ab)]),
    0x22: (2, [("fuel_rail_rel", "Pressão rail (relativa)", "kPa", 1, lambda d: _ab(d) * 0.079)]),
    0x23: (2, [("fuel_rail", "Pressão rail", "kPa", 0, lambda d: _ab(d) * 10)]),
    **{0x24 + i: (4, [(f"o2w{i+1}_lambda", f"Sonda larga S{i+1} lambda", "λ", 3, _wide_lambda),
                      (f"o2w{i+1}_v", f"Sonda larga S{i+1} tensão", "V", 3, _wide_v)]) for i in range(8)},
    0x2C: (1, [("egr_cmd", "EGR comandada", "%", 1, _pct)]),
    0x2D: (1, [("egr_err", "Erro EGR", "%", 1, _trim)]),
    0x2E: (1, [("evap_cmd", "Purga do cânister comandada", "%", 1, _pct)]),
    0x2F: (1, [("fuel_level", "Nível de combustível", "%", 0, _pct)]),
    0x30: (1, [("warmups", "Aquecimentos desde limpeza de códigos", "", 0, _a)]),
    0x31: (2, [("dist_clr", "Distância desde limpeza de códigos", "km", 0, _ab)]),
    0x32: (2, [("evap_press", "Pressão do sistema EVAP", "Pa", 1,
                lambda d: int.from_bytes(d[:2], "big", signed=True) / 4)]),
    0x33: (1, [("baro", "Pressão barométrica", "kPa", 0, _a)]),
    **{0x34 + i: (4, [(f"o2c{i+1}_lambda", f"Sonda larga S{i+1} lambda (corrente)", "λ", 3, _wide_lambda),
                      (f"o2c{i+1}_ma", f"Sonda larga S{i+1} corrente", "mA", 2, _wide_ma)]) for i in range(8)},
    0x3C: (2, [("cat_b1s1", "Temperatura catalisador B1S1", "°C", 0, _cat)]),
    0x3D: (2, [("cat_b2s1", "Temperatura catalisador B2S1", "°C", 0, _cat)]),
    0x3E: (2, [("cat_b1s2", "Temperatura catalisador B1S2", "°C", 0, _cat)]),
    0x3F: (2, [("cat_b2s2", "Temperatura catalisador B2S2", "°C", 0, _cat)]),
    0x41: (4, [("monitors_cycle", "Monitores neste ciclo", "", 0, lambda d: int.from_bytes(d, "big"))]),
    0x42: (2, [("ecu_volt", "Tensão do módulo (ECU)", "V", 2, lambda d: _ab(d) / 1000)]),
    0x43: (2, [("load_abs", "Carga absoluta", "%", 1, lambda d: _ab(d) * 100 / 255)]),
    0x44: (2, [("lambda_cmd", "Lambda comandado", "λ", 3, lambda d: _ab(d) * 2 / 65536)]),
    0x45: (1, [("tps_rel", "Borboleta relativa", "%", 1, _pct)]),
    0x46: (1, [("ambient", "Temperatura ambiente", "°C", 0, _temp)]),
    0x47: (1, [("tps_b", "Borboleta absoluta B", "%", 1, _pct)]),
    0x48: (1, [("tps_c", "Borboleta absoluta C", "%", 1, _pct)]),
    0x49: (1, [("pedal_d", "Pedal do acelerador D", "%", 1, _pct)]),
    0x4A: (1, [("pedal_e", "Pedal do acelerador E", "%", 1, _pct)]),
    0x4B: (1, [("pedal_f", "Pedal do acelerador F", "%", 1, _pct)]),
    0x4C: (1, [("tps_cmd", "Atuador da borboleta comandado", "%", 1, _pct)]),
    0x4D: (2, [("time_mil", "Tempo com luz de injeção acesa", "min", 0, _ab)]),
    0x4E: (2, [("time_clr", "Tempo desde limpeza de códigos", "min", 0, _ab)]),
    0x51: (1, [("fuel_type", "Tipo de combustível", "", 0, _a)]),
    0x52: (1, [("ethanol", "Etanol no combustível", "%", 0, _pct)]),
    0x53: (2, [("evap_abs", "Pressão absoluta EVAP", "kPa", 2, lambda d: _ab(d) / 200)]),
    0x55: (2, [("st_o2_b1s2", "Ajuste curto O2 secundária B1", "%", 1, _trim)]),
    0x56: (2, [("lt_o2_b1s2", "Ajuste longo O2 secundária B1", "%", 1, _trim)]),
    0x59: (2, [("fuel_rail_abs", "Pressão absoluta rail", "kPa", 0, lambda d: _ab(d) * 10)]),
    0x5A: (1, [("pedal_rel", "Pedal relativo", "%", 1, _pct)]),
    0x5B: (1, [("hybrid_batt", "Bateria híbrida", "%", 1, _pct)]),
    0x5C: (1, [("oil_temp", "Temperatura do óleo", "°C", 0, _temp)]),
    0x5D: (2, [("inj_timing", "Ponto de injeção", "°", 2, lambda d: (_ab(d) - 26880) / 128)]),
    0x5E: (2, [("fuel_rate", "Consumo instantâneo", "L/h", 2, lambda d: _ab(d) / 20)]),
}

# Bytes de dados por PID (inclui PIDs de bitmask e PID 01 com 4 bytes)
PID_LEN: dict[int, int] = {pid: n for pid, (n, _) in T.items()}
PID_LEN.update({0x00: 4, 0x20: 4, 0x40: 4, 0x60: 4, 0x80: 4, 0xA0: 4, 0xC0: 4, 0x01: 4, 0x02: 2})

SIGNALS: dict[str, Signal] = {}
for _pid, (_n, _sigs) in T.items():
    for (_k, _name, _unit, _dec, _fn) in _sigs:
        SIGNALS[_k] = Signal(_k, _name, _unit, _dec, _fn, _pid)

# Sinais extras que não vêm de PIDs
SIGNALS["batt"] = Signal("batt", "Tensão da bateria (ELM)", "V", 1, lambda d: None, -1)

# PIDs que não fazem sentido amostrar continuamente (informativos / bitmasks)
INFO_ONLY = {0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0x01, 0x02, 0x13, 0x1C, 0x1D, 0x41, 0x51}


def decode(pid: int, data: bytes) -> dict[str, float]:
    """Decodifica os bytes de um PID em {chave: valor}. PID desconhecido vira raw_XX."""
    if pid not in T:
        return {f"raw_{pid:02x}": float(int.from_bytes(data[:4], "big"))} if data else {}
    n, sigs = T[pid]
    if len(data) < n:
        return {}
    out = {}
    for (k, _name, _unit, dec, fn) in sigs:
        try:
            v = fn(data)
        except Exception:
            v = None
        if v is not None:
            out[k] = round(float(v), dec + 1)
    return out


def parse_supported(base: int, data: bytes) -> set[int]:
    bits = int.from_bytes(data[:4], "big")
    return {base + i + 1 for i in range(32) if bits & (1 << (31 - i))}


def decode_status(data: bytes) -> dict:
    """PID 01: MIL, número de DTCs e monitores."""
    a, b, c, d = (data + b"\0\0\0\0")[:4]
    return {"mil": bool(a & 0x80), "dtc_count": a & 0x7F, "spark": not bool(b & 0x08),
            "raw": data.hex()}

SIGNALS["fuel_lh"] = Signal("fuel_lh", "Consumo instantâneo (estimado)", "L/h", 2, lambda d: None, -1)
SIGNALS["kml_inst"] = Signal("kml_inst", "Consumo instantâneo (estimado)", "km/L", 1, lambda d: None, -1)
SIGNALS["gear"] = Signal("gear", "Marcha engatada (calculada)", "", 0, lambda d: None, -1)
