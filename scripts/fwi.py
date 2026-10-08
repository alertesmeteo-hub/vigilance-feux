"""Indice forêt météo canadien (IFM / FWI), équations de Van Wagner (1987).

Entrées quotidiennes « de midi » : température (°C), humidité relative (%), vent à 10 m (km/h),
pluie sur 24 h (mm). Codes d'humidité reportés d'un jour sur l'autre : FFMC, DMC, DC.
"""
from __future__ import annotations

import math

# Facteurs de longueur du jour (hémisphère nord, latitudes moyennes) pour DMC et DC
EL = [6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0]
FL = [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6]

FFMC0, DMC0, DC0 = 85.0, 6.0, 15.0  # valeurs de démarrage standard


def ffmc(t: float, h: float, w: float, ro: float, fo: float) -> float:
    h = min(max(h, 0.0), 100.0)
    w = max(w, 0.0)
    mo = 147.2 * (101.0 - fo) / (59.5 + fo)
    if ro > 0.5:
        rf = ro - 0.5
        mr = mo + 42.5 * rf * math.exp(-100.0 / (251.0 - mo)) * (1.0 - math.exp(-6.93 / rf))
        if mo > 150.0:
            mr += 0.0015 * (mo - 150.0) ** 2 * math.sqrt(rf)
        mo = min(mr, 250.0)
    ed = 0.942 * h**0.679 + 11.0 * math.exp((h - 100.0) / 10.0) + 0.18 * (21.1 - t) * (1.0 - math.exp(-0.115 * h))
    if mo > ed:
        ko = 0.424 * (1.0 - (h / 100.0) ** 1.7) + 0.0694 * math.sqrt(w) * (1.0 - (h / 100.0) ** 8)
        kd = ko * 0.581 * math.exp(0.0365 * t)
        m = ed + (mo - ed) * 10.0 ** (-kd)
    else:
        ew = 0.618 * h**0.753 + 10.0 * math.exp((h - 100.0) / 10.0) + 0.18 * (21.1 - t) * (1.0 - math.exp(-0.115 * h))
        if mo < ew:
            k1 = 0.424 * (1.0 - ((100.0 - h) / 100.0) ** 1.7) + 0.0694 * math.sqrt(w) * (1.0 - ((100.0 - h) / 100.0) ** 8)
            kw = k1 * 0.581 * math.exp(0.0365 * t)
            m = ew - (ew - mo) * 10.0 ** (-kw)
        else:
            m = mo
    return min(max(59.5 * (250.0 - m) / (147.2 + m), 0.0), 101.0)


def dmc(t: float, h: float, ro: float, po: float, month: int) -> float:
    t = max(t, -1.1)
    rk = 1.894 * (t + 1.1) * (100.0 - min(max(h, 0.0), 100.0)) * EL[month - 1] * 1e-4
    if ro > 1.5:
        re = 0.92 * ro - 1.27
        mo = 20.0 + math.exp(5.6348 - po / 43.43)
        if po <= 33.0:
            b = 100.0 / (0.5 + 0.3 * po)
        elif po <= 65.0:
            b = 14.0 - 1.3 * math.log(po)
        else:
            b = 6.2 * math.log(po) - 17.2
        mr = mo + 1000.0 * re / (48.77 + b * re)
        pr = max(244.72 - 43.43 * math.log(mr - 20.0), 0.0)
    else:
        pr = po
    return max(pr + rk, 0.0)


def dc(t: float, ro: float, do: float, month: int) -> float:
    t = max(t, -2.8)
    pe = max((0.36 * (t + 2.8) + FL[month - 1]) / 2.0, 0.0)
    if ro > 2.8:
        rd = 0.83 * ro - 1.27
        qo = 800.0 * math.exp(-do / 400.0)
        qr = qo + 3.937 * rd
        dr = max(400.0 * math.log(800.0 / qr), 0.0)
    else:
        dr = do
    return max(dr + pe, 0.0)


def isi(w: float, f: float) -> float:
    fm = 147.2 * (101.0 - f) / (59.5 + f)
    sf = math.exp(0.05039 * max(w, 0.0))
    ff = 91.9 * math.exp(-0.1386 * fm) * (1.0 + fm**5.31 / 4.93e7)
    return 0.208 * sf * ff


def bui(p: float, d: float) -> float:
    if p == 0.0 and d == 0.0:
        return 0.0
    if p <= 0.4 * d:
        u = 0.8 * p * d / (p + 0.4 * d)
    else:
        u = p - (1.0 - 0.8 * d / (p + 0.4 * d)) * (0.92 + (0.0114 * p) ** 1.7)
    return max(u, 0.0)


def fwi(r: float, u: float) -> float:
    if u <= 80.0:
        bb = 0.1 * r * (0.626 * u**0.809 + 2.0)
    else:
        bb = 0.1 * r * (1000.0 / (25.0 + 108.64 * math.exp(-0.023 * u)))
    if bb <= 1.0:
        return bb
    return math.exp(2.72 * (0.434 * math.log(bb)) ** 0.647)


def step(codes: tuple[float, float, float], t: float, h: float, w: float, ro: float, month: int) -> dict:
    """Un jour : nouveaux codes et indices."""
    f0, p0, d0 = codes
    f = ffmc(t, h, w, ro, f0)
    p = dmc(t, h, ro, p0, month)
    d = dc(t, ro, d0, month)
    i = isi(w, f)
    u = bui(p, d)
    return {"ffmc": f, "dmc": p, "dc": d, "isi": i, "bui": u, "fwi": fwi(i, u)}
