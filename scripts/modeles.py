"""Prévisions des communes calculées avec nos propres modèles : AROME et ARPEGE (Météo-France), ICON (DWD).

Les trois modèles sont publiés par département dans les dépôts alertesmeteo-hub/<modèle> (branche data,
departements/66.json) : une grille de points et, pour chaque heure UTC, une ligne de valeurs par point.
Pour chaque commune on retient le point de grille qui minimise « distance + écart d'altitude / 100 » (au plus
15 km, sinon le plus proche), puis on corrige la température et le point de rosée de l'écart d'altitude entre
ce point et la commune (gradients standard), comme pour la météo randonnée et les prévisions par altitude.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import requests

RAW = "https://raw.githubusercontent.com/alertesmeteo-hub/"
UA = "AlertesMeteo-VigilanceFeux/2.0 (+https://app.alertes-meteo.com)"
# Par priorité décroissante : le modèle le plus fin qui couvre l'heure demandée est celui qu'on lit.
SOURCES = [("AROME", "arome-meteofrance"), ("ARPEGE", "arpege-meteo-france"), ("ICON", "ICON-GLOBAL-13-km")]

GRADIENT = 0.0065  # °C par mètre : température de l'air
GRADIENT_ROSEE = 0.002  # °C par mètre : point de rosée
RAYON_KM = 15.0
RAYON_MAX_KM = 40.0


class Heure(NamedTuple):
    """Valeurs d'une commune à une heure UTC (None quand le modèle ne les donne pas)."""

    t: float | None  # température (°C), corrigée de l'altitude
    rh: float | None  # humidité relative (%), recalculée avec la température et le point de rosée corrigés
    ws: float | None  # vent moyen à 10 m (km/h)
    wd: float | None  # direction du vent (°)
    gust: float | None  # rafale (km/h)
    rr: float | None  # pluie de l'heure précédente (mm)


@dataclass
class Modele:
    nom: str
    genere: str
    heures: list[int]  # heures UTC depuis l'epoch, une par pas
    points: list[tuple[float, float, float]]  # latitude, longitude, altitude du modèle (m)
    colonnes: dict[str, int]
    lignes: list[list[list]]  # lignes[pas][point][colonne]
    rang: int = 0  # 0 = le plus fin (AROME), plus grand = moins fin

    @property
    def init(self) -> int:
        """Heure UTC (depuis l'epoch) du premier pas, c'est-à-dire de l'initialisation du run."""
        return self.heures[0]


def _fini(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.pi / 180
    a = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def humidite_relative(t: float, td: float) -> float:
    """Humidité relative (%) d'après la température et le point de rosée (Magnus), bornée à 0-100."""
    e = lambda x: math.exp(17.625 * x / (243.04 + x))  # noqa: E731
    return min(100.0, max(0.0, 100.0 * e(td) / e(t)))


def rosee(t: float, rh: float) -> float:
    """Point de rosée (°C) d'après la température et l'humidité relative (Magnus)."""
    g = math.log(max(rh, 1.0) / 100.0) + 17.625 * t / (243.04 + t)
    return 243.04 * g / (17.625 - g)


def lire_modele(nom: str, doc: dict) -> Modele | None:
    """Fichier département d'un modèle → Modele ; None si le fichier est inutilisable."""
    try:
        if doc.get("status") not in (None, "ok"):
            return None
        cols = {n: i for i, n in enumerate(doc["columns"]["values"])}
        cp = doc["columns"]["points"]
        i_lat, i_lon = cp.index("latitude"), cp.index("longitude")
        i_alt = cp.index("altitude_m") if "altitude_m" in cp else cp.index("model_altitude_m")
        points = [(float(p[i_lat]), float(p[i_lon]), float(p[i_alt]) if _fini(p[i_alt]) else 0.0) for p in doc["points"]]
        heures, lignes = [], []
        for t, rows in doc["forecast"]:
            heures.append(int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() // 3600))
            lignes.append(rows)
        if not heures or "temperature_c" not in cols:
            return None
        return Modele(nom, str(doc.get("generated_at", "")), heures, points, cols, lignes)
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def choisir_point(m: Modele, lat: float, lon: float, alt: float) -> int | None:
    """Point de grille d'une commune : au plus RAYON_KM, celui qui minimise distance (km) + écart d'altitude (m) / 100 ;
    à défaut, le plus proche s'il est à moins de RAYON_MAX_KM."""
    meilleur: tuple[float, int] | None = None
    proche: tuple[float, int] | None = None
    for k, (plat, plon, palt) in enumerate(m.points):
        d = distance_km(lat, lon, plat, plon)
        if proche is None or d < proche[0]:
            proche = (d, k)
        if d <= RAYON_KM:
            score = d + abs(alt - palt) / 100.0
            if meilleur is None or score < meilleur[0]:
                meilleur = (score, k)
    if meilleur:
        return meilleur[1]
    return proche[1] if proche and proche[0] <= RAYON_MAX_KM else None


def serie_commune(m: Modele, point: int, alt: float) -> dict[int, Heure]:
    """Série horaire (heure UTC → Heure) d'une commune d'après un point de grille du modèle.

    Température et point de rosée sont corrigés de l'écart d'altitude entre la commune et le point ; l'humidité relative est
    recalculée avec ces valeurs corrigées. La pluie du premier pas (heure d'initialisation, toujours 0) est ignorée : elle appartient
    à la prévision précédente.
    """
    c = m.colonnes
    ecart = alt - m.points[point][2]
    out: dict[int, Heure] = {}
    for pas, (h, rows) in enumerate(zip(m.heures, m.lignes)):
        r = rows[point] if point < len(rows) else None
        if not r:
            continue

        def v(nom: str) -> float | None:
            i = c.get(nom)
            x = r[i] if i is not None and i < len(r) else None
            return float(x) if _fini(x) else None

        t0 = v("temperature_c")
        t = None if t0 is None else t0 - GRADIENT * ecart
        td = v("dewpoint_c")
        if td is None and t0 is not None and v("humidity_pct") is not None:
            td = rosee(t0, v("humidity_pct"))  # type: ignore[arg-type]
        rh = None
        if t is not None and td is not None:
            rh = humidite_relative(t, min(t, td - GRADIENT_ROSEE * ecart))
        elif v("humidity_pct") is not None:
            rh = v("humidity_pct")
        rr = v("precipitation_mm")
        out[h] = Heure(t, rh, v("wind_speed_kmh"), v("wind_direction_deg"), v("wind_gust_kmh"), None if pas == 0 or rr is None else max(0.0, rr))
    return out


def _telecharger(url: str, tentatives: int = 3) -> dict | None:
    for essai in range(tentatives):
        try:
            r = requests.get(url, timeout=(15, 120), headers={"User-Agent": UA})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            attente = 5 * 3**essai
            print(f"  {url.split('/')[-4]} : {e} — nouvel essai dans {attente} s", flush=True)
            if essai + 1 < tentatives:
                time.sleep(attente)
    return None


def charger_modeles(dossier: str | None = None, departement: str = "66") -> list[Modele]:
    """Les modèles disponibles, par priorité décroissante. `dossier` : lecture locale (<dossier>/<dépôt>.json) pour les tests."""
    out: list[Modele] = []
    for rang, (nom, depot) in enumerate(SOURCES):
        if dossier:
            f = Path(dossier) / f"{depot}.json"
            doc = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        else:
            doc = _telecharger(f"{RAW}{depot}/data/departements/{departement}.json")
        m = lire_modele(nom, doc) if isinstance(doc, dict) else None
        if m:
            m.rang = rang
            print(f"Modèle {nom} : {len(m.points)} points, {len(m.heures)} pas, généré {m.genere}", flush=True)
            out.append(m)
        else:
            print(f"Modèle {nom} indisponible", flush=True)
    return out
