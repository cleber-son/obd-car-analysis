"""Consumo de combustível estimado pelo MAF, calibrado pelos abastecimentos (tanque a tanque).

Combustível = ar / (AFR estequiométrico × λ comandado). O AFR depende da mistura no tanque:
gasolina C (E27) ≈ 13,0:1, densidade 0,754 kg/L; etanol hidratado ≈ 8,4:1, 0,809 kg/L.
A mistura é estimada a cada abastecimento (tanque de 50 L do Civic 2008).
"""
from __future__ import annotations

import statistics

TANK_L = 50.0
GAS = {"afr": 13.0, "dens": 0.754}
ETH = {"afr": 8.4, "dens": 0.809}


def props(mix: float) -> tuple[float, float]:
    m = min(1.0, max(0.0, mix))
    return GAS["afr"] + (ETH["afr"] - GAS["afr"]) * m, GAS["dens"] + (ETH["dens"] - GAS["dens"]) * m


def fuel_lh(maf_gs: float | None, lam: float | None, mix: float, calib: float = 1.0, *,
            rpm: float | None = None, load: float | None = None, closed_loop: bool = True) -> float | None:
    if maf_gs is None:
        return None
    if rpm is not None and rpm < 300:
        return 0.0
    # corte de combustível em desaceleração (pé fora, giro alto, malha aberta)
    if not closed_loop and rpm and rpm > 1300 and load is not None and load < 5:
        return 0.0
    afr, dens = props(mix)
    lam = lam if lam and 0.6 < lam < 1.4 else 1.0
    g_s = maf_gs / (afr * lam)
    return g_s / (dens * 1000) * 3600 * calib


def mix_after_fill(prev_mix: float | None, fuel: str, liters: float | None) -> float:
    new = 1.0 if fuel == "etanol" else 0.0
    if prev_mix is None or not liters:
        return new
    liters = min(liters, TANK_L)
    return (prev_mix * (TANK_L - liters) + new * liters) / TANK_L


def calib_from(fills: list) -> float:
    ks = [f["calib"] for f in fills if f["calib"] and 0.6 < f["calib"] < 1.6][-3:]
    return statistics.median(ks) if ks else 1.0
