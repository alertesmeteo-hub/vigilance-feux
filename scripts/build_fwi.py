"""Danger météorologique de feux de forêt pour chaque commune des Pyrénées-Orientales.

Pour chaque commune (centre géographique), on calcule l'indice forêt météo (IFM / FWI canadien) jour par jour,
à partir des valeurs de 12 h UTC (température, humidité, vent à 10 m) et de la pluie tombée entre 12 h UTC la veille
et 12 h UTC le jour même. Les entrées viennent de nos propres modèles (AROME et ARPEGE de Météo-France, ICON du DWD,
voir modeles.py), sans service extérieur : AROME pour les deux premiers jours, ARPEGE jusqu'à 4 jours, ICON au-delà.

Les codes d'humidité des combustibles (FFMC, DMC, DC) ont une mémoire de plusieurs semaines : ils sont conservés dans
state-66.json (valeurs de fin de journée, 21 derniers jours).

Les modèles ne donnent que le présent et l'avenir ; le passé est donc conservé d'un calcul à l'autre :
 - les jours passés sont repris tels quels de la publication précédente (fwi-66.json) ;
 - la pluie est archivée heure par heure dans state-66.json (« pluie »), à la manière des « premières heures de chaque
   prévision » : à chaque calcul, les heures à partir du début du meilleur modèle sont remplacées par sa prévision ; les
   heures plus anciennes gardent la valeur archivée. La pluie de 24 h du jour en cours s'appuie sur cette archive.
Publie fwi-66.json : 7 jours passés (tendance) + aujourd'hui + 6 jours de prévision.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fwi
import modeles
from modeles import Heure

ROOT = Path(__file__).resolve().parent.parent
PAST_DAYS = 8  # 7 jours affichés + la veille
FORECAST_DAYS = 7
KEEP_STATE_DAYS = 21
HEURES_MANQUANTES_MAX = 6  # au-delà, sans valeur de repli, la pluie du jour est inconnue : pas de ligne

# Classes de danger (seuils EFFIS / Copernicus pour l'Europe du Sud)
CLASSES = [
    (0.0, 1, "Très faible"),
    (5.2, 2, "Faible"),
    (11.2, 3, "Modéré"),
    (21.3, 4, "Élevé"),
    (38.0, 5, "Très élevé"),
    (50.0, 6, "Extrême"),
]

# Colonnes d'une ligne publiée
FWI, ISI, BUI, FFMC, DMC, DC, T, RH, WS, WD, GUST, RR = range(12)

Series = list[tuple[str, dict[int, Heure]]]  # séries horaires d'une commune par modèle, par priorité décroissante


def classe(v: float) -> int:
    n = 1
    for seuil, c, _ in CLASSES:
        if v >= seuil:
            n = c
    return n


def drange(a: date, b: date) -> list[str]:
    return [(a + timedelta(days=k)).isoformat() for k in range((b - a).days + 1)]


def heure_utc(jour: date, h: int) -> int:
    """Heure UTC (depuis l'epoch) d'un jour à h heures."""
    return int(datetime(jour.year, jour.month, jour.day, h, tzinfo=timezone.utc).timestamp() // 3600)


# ———— Pluie : archive horaire ————


def mettre_a_jour_archive(archive: dict[int, float], series: Series) -> None:
    """Range dans l'archive (heure UTC → mm) la pluie des modèles chargés.

    À partir du début du meilleur modèle chargé, chaque heure prend la valeur du meilleur modèle qui la couvre ; avant, les
    valeurs déjà archivées sont conservées et seules les heures absentes sont comblées par un modèle moins fin.
    """
    pluies = [{h: x.rr for h, x in s.items() if x.rr is not None} for _, s in series]
    pluies = [p for p in pluies if p]
    if not pluies:
        return
    debut = min(pluies[0])
    for h in sorted(set().union(*pluies)):
        if h < debut and h in archive:
            continue
        for p in pluies:
            if h in p:
                archive[h] = p[h]
                break


def pluie_fenetre(jour: date, series: Series, archive: dict[int, float]) -> tuple[float, int]:
    """Pluie (mm) de 13 h UTC la veille à 12 h UTC le jour même, et nombre d'heures sans valeur.

    Quand un modèle couvre toute la fenêtre (jours à venir), on prend son total : une même prévision, cohérente. Sinon (jour en
    cours, dont le début est déjà passé) on additionne l'archive horaire.
    """
    h12 = heure_utc(jour, 12)
    fenetre = range(h12 - 23, h12 + 1)
    for _, s in series:
        if all(h in s and s[h].rr is not None for h in fenetre):
            return sum(s[h].rr for h in fenetre), 0  # type: ignore[misc]
    total, manque = 0.0, 0
    for h in fenetre:
        v = archive.get(h)
        if v is None:
            manque += 1
        else:
            total += v
    return total, manque


def garder_archive(archive: dict[int, float], today: date) -> None:
    """Ne garde que ce dont les prochains calculs ont besoin : de 13 h UTC la veille jusqu'à la fin de demain."""
    debut, fin = heure_utc(today - timedelta(days=1), 13), heure_utc(today + timedelta(days=2), 0)
    for h in [h for h in archive if h < debut or h >= fin]:
        del archive[h]


def lire_archive(doc: dict | None) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    if not doc:
        return out
    try:
        h0 = int(doc["h0"])
        for code, vals in doc["valeurs"].items():
            out[code] = {h0 + k: float(v) for k, v in enumerate(vals) if v is not None}
    except (KeyError, TypeError, ValueError):
        return {}
    return out


def ecrire_archive(archive: dict[str, dict[int, float]]) -> dict:
    heures = [h for a in archive.values() for h in a]
    if not heures:
        return {"h0": 0, "valeurs": {}}
    h0, h1 = min(heures), max(heures)
    return {"h0": h0, "valeurs": {code: [round(a[h], 2) if h in a else None for h in range(h0, h1 + 1)] for code, a in sorted(archive.items())}}


# ———— Entrées d'un jour ————


def entrees_jour(jour: date, series: Series, archive: dict[int, float], precedente: list | None, jour_en_cours: bool = False, tolere_manque: bool = False) -> dict | None:
    """Entrées de l'IFM pour un jour : valeurs de 12 h UTC du meilleur modèle qui les donne, pluie sur 24 h.

    Pour le jour en cours (`jour_en_cours`), si aucun modèle ne couvre plus 12 h UTC (journée déjà bien entamée), on reprend les valeurs de
    la publication précédente, et la rafale est le maximum des calculs de la journée. Un jour à venir n'est jamais repris d'une prévision
    plus ancienne : sans modèle, pas de ligne.
    Retourne None quand une entrée manque (ou quand plus de HEURES_MANQUANTES_MAX heures de pluie sont inconnues, sauf `tolere_manque` : elles
    comptent alors pour zéro). `source` dit d'où viennent les valeurs de midi.
    """
    h12 = heure_utc(jour, 12)
    midi: Heure | None = None
    source = ""
    serie_midi: dict[int, Heure] = {}
    for nom, s in series:
        x = s.get(h12)
        if x and x.t is not None and x.rh is not None and x.ws is not None:
            midi, source, serie_midi = x, nom, s
            break
    gust_prec = precedente[GUST] if precedente and jour_en_cours else None
    if midi is None:
        if not jour_en_cours or not precedente or precedente[T] is None or precedente[RH] is None or precedente[WS] is None:
            return None
        midi = Heure(precedente[T], precedente[RH], precedente[WS], precedente[WD], gust_prec, None)
        source = "précédente"
    rr, manque = pluie_fenetre(jour, series, archive)
    if manque:
        # heures jamais archivées (premier calcul, panne) : la publication précédente, qui couvrait la fenêtre, sert de plancher
        if precedente and precedente[RR] is not None:
            rr = max(rr, precedente[RR])
        elif manque > HEURES_MANQUANTES_MAX and not tolere_manque:
            return None
    rafales = [x.gust for h, x in serie_midi.items() if h12 - 12 <= h < h12 + 12 and x.gust is not None]
    if gust_prec is not None:
        rafales.append(gust_prec)
    return {"t": midi.t, "rh": min(100.0, max(0.0, midi.rh)), "ws": max(0.0, midi.ws), "wd": midi.wd, "gust": max(rafales) if rafales else None, "rr": rr, "source": source, "manque": manque}


def ligne(r: dict, x: dict) -> list:
    return [
        round(r["fwi"], 1), round(r["isi"], 1), round(r["bui"], 1), round(r["ffmc"], 1), round(r["dmc"], 1), round(r["dc"], 0),
        round(x["t"], 1), round(x["rh"]), round(x["ws"]), None if x["wd"] is None else round(x["wd"]), None if x["gust"] is None else round(x["gust"]), round(x["rr"], 1),
    ]


def lire_json(chemin: Path) -> dict | None:
    try:
        d = json.loads(chemin.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None, maintenant: datetime | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="pub")
    ap.add_argument("--min-age-hours", type=float, default=0, help="ne recalcule pas si la dernière publication est plus récente")
    ap.add_argument("--modeles-dir", default=None, help="lit les fichiers modèles dans ce dossier au lieu de GitHub (tests)")
    ap.add_argument(
        "--codes-standard",
        action="store_true",
        help="repart des codes standard (85 / 6 / 15) pour les communes sans codes de départ (mémoire perdue) ; à réserver aux lendemains de grosse pluie, sinon DMC et DC sont trop bas",
    )
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    maintenant = maintenant or datetime.now(timezone.utc)

    prev_doc = lire_json(out / "fwi-66.json")
    if args.min_age_hours and prev_doc:
        try:
            gen = datetime.fromisoformat(prev_doc["generated_at"].replace("Z", "+00:00"))
            age = (maintenant - gen).total_seconds() / 3600
            if age < args.min_age_hours:
                print(f"IFM publié il y a {age:.1f} h : pas de recalcul.")
                return 0
        except Exception:  # noqa: BLE001
            pass

    communes = json.loads((ROOT / "static" / "communes-66.json").read_text(encoding="utf-8"))["communes"]
    altitudes = json.loads((ROOT / "static" / "altitudes-66.json").read_text(encoding="utf-8"))
    today = maintenant.date()
    hier = (today - timedelta(days=1)).isoformat()
    first_shown = today - timedelta(days=PAST_DAYS - 1)
    dates_shown = drange(first_shown, today + timedelta(days=FORECAST_DAYS - 1))

    # lignes de la publication précédente, par commune puis par date
    precedent: dict[str, dict[str, list]] = {}
    if prev_doc:
        for c, rows in zip(prev_doc["communes"], prev_doc["data"]):
            precedent[c[0]] = {d: r for d, r in zip(prev_doc["dates"], rows) if r}

    state = lire_json(out / "state-66.json") or {}
    hist: dict[str, dict[str, list[float]]] = state.get("history", {})
    archives = lire_archive(state.get("pluie"))

    mods = modeles.charger_modeles(args.modeles_dir)
    if not mods:
        raise RuntimeError("aucun modèle disponible : IFM non recalculé")

    new_hist: dict[str, dict[str, list[float]]] = {k: v for k, v in hist.items() if k >= (today - timedelta(days=KEEP_STATE_DAYS)).isoformat()}
    per_commune: list[list | None] = []
    sans_codes: list[str] = []
    standard = 0
    origines: dict[str, Counter] = {d: Counter() for d in dates_shown}
    for c in communes:
        code, lat, lon = c[0], c[2], c[3]
        alt = float(altitudes[code])
        series: Series = []
        for m in mods:
            k = modeles.choisir_point(m, lat, lon, alt)
            if k is not None:
                series.append((m.nom, modeles.serie_commune(m, k, alt)))
        archive = archives.setdefault(code, {})
        mettre_a_jour_archive(archive, series)
        garder_archive(archive, today)

        codes = hist.get(hier, {}).get(code)
        if codes is None and precedent.get(code, {}).get(hier):  # repli : codes publiés (arrondis) de la ligne d'hier
            p = precedent[code][hier]
            codes = [p[FFMC], p[DMC], p[DC]]
        if codes is None and args.codes_standard:
            codes = [fwi.FFMC0, fwi.DMC0, fwi.DC0]
            standard += 1
        if codes is None:
            sans_codes.append(code)
        rows: list[list | None] = []
        for ds in dates_shown:
            jour = date.fromisoformat(ds)
            prec = precedent.get(code, {}).get(ds)
            if jour < today:
                rows.append(prec)  # passé : figé
                continue
            if codes is None:
                rows.append(None)
                continue
            x = entrees_jour(jour, series, archive, prec, jour_en_cours=(jour == today), tolere_manque=args.codes_standard)
            if x is None:
                rows.append(None)
                continue
            r = fwi.step((codes[0], codes[1], codes[2]), x["t"], x["rh"], x["ws"], x["rr"], jour.month)
            codes = [r["ffmc"], r["dmc"], r["dc"]]
            rows.append(ligne(r, x))
            origines[ds][x["source"]] += 1
            if jour == today:  # fin de journée provisoire : le calcul de demain repart d'ici
                new_hist.setdefault(ds, {})[code] = [round(r["ffmc"], 3), round(r["dmc"], 3), round(r["dc"], 3)]
        per_commune.append(rows)

    if sans_codes:
        print(f"Attention : pas de codes de départ pour {len(sans_codes)} communes ({', '.join(sans_codes[:5])}…) — lignes vides.")
        if len(sans_codes) > len(communes) // 20:
            raise RuntimeError("codes de départ absents : restaurer state-66.json, ou relancer avec --codes-standard après une grosse pluie")

    ti = dates_shown.index(today.isoformat())
    worst = max(
        ((c, rows[ti]) for c, rows in zip(communes, per_commune) if rows[ti]),
        key=lambda x: x[1][FWI],
        default=None,
    )
    if worst is None or sum(1 for rows in per_commune if rows[ti]) < len(communes) * 0.9:
        raise RuntimeError("trop de communes sans valeur aujourd'hui : IFM non publié")
    now = maintenant.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    doc = {
        "schema_version": 1,
        "status": "ok",
        "generated_at": now,
        "department": "66",
        "method": "Indice forêt météo canadien (Van Wagner 1987), valeurs de 12 h UTC, pluie sur 24 h de 12 h à 12 h UTC",
        "source": {
            "provider": "modèles Météo-France et DWD",
            "models": "AROME jusqu'à 2 jours, ARPEGE jusqu'à 4 jours, ICON au-delà, température corrigée de l'altitude de chaque commune",
            "url": "https://app.alertes-meteo.com/vigilance-feux",
            "license": "Licence ouverte 2.0 (Météo-France), CC BY 4.0 (DWD)",
            "runs": [{"modele": m.nom, "genere": m.genere} for m in mods],
            "origine_par_jour": {d: (o.most_common(1)[0][0] if o else None) for d, o in origines.items()},
        },
        "spinup": standard > 0,
        "classes": [{"min": s, "niveau": n, "nom": nom} for s, n, nom in CLASSES],
        "columns": {
            "communes": ["code", "nom", "lat", "lon", "population"],
            "values": ["fwi", "isi", "bui", "ffmc", "dmc", "dc", "t", "rh", "ws", "wd", "gust", "rr"],
        },
        "dates": dates_shown,
        "today": today.isoformat(),
        "communes": communes,
        "data": per_commune,
    }
    (out / "fwi-66.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "state-66.json").write_text(
        json.dumps({"updated_at": now, "history": dict(sorted(new_hist.items())), "pluie": ecrire_archive(archives)}, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"IFM 66 publié — maxi aujourd'hui : {worst[0][1]} {worst[1][FWI]} ({len(communes)} communes, {len(dates_shown)} jours)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
