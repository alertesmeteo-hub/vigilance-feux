"""Danger météorologique de feux de forêt pour chaque commune des Pyrénées-Orientales.

Pour chaque commune (centre géographique), on calcule l'indice forêt météo (IFM / FWI canadien)
jour par jour, à partir des valeurs de 12 h UTC (température, humidité, vent à 10 m) et de la pluie
tombée entre 12 h UTC la veille et 12 h UTC le jour même.

Les codes d'humidité des combustibles (FFMC, DMC, DC) ont une mémoire de plusieurs semaines : ils
sont conservés dans state-66.json (valeurs de fin de journée, 21 derniers jours). Sans état utilisable,
on refait la mise en route depuis le 1er mars (démarrage standard 85 / 6 / 15) avec l'historique
Open-Meteo. Publie fwi-66.json : 7 jours passés (tendance) + aujourd'hui + 6 jours de prévision.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import fwi

ROOT = Path(__file__).resolve().parent.parent
FORECAST = "https://api.open-meteo.com/v1/forecast"
HISTORY = "https://historical-forecast-api.open-meteo.com/v1/forecast"
UA = "AlertesMeteo-VigilanceFeux/1.0 (+https://app.alertes-meteo.com)"
VARS = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation"
PAST_DAYS = 8  # 7 jours affichés + la veille pour la pluie du premier jour
FORECAST_DAYS = 7
BATCH = 40
KEEP_STATE_DAYS = 21

# Classes de danger (seuils EFFIS / Copernicus pour l'Europe du Sud)
CLASSES = [
    (0.0, 1, "Très faible"),
    (5.2, 2, "Faible"),
    (11.2, 3, "Modéré"),
    (21.3, 4, "Élevé"),
    (38.0, 5, "Très élevé"),
    (50.0, 6, "Extrême"),
]


def classe(v: float) -> int:
    n = 1
    for seuil, c, _ in CLASSES:
        if v >= seuil:
            n = c
    return n


def fetch(url: str, communes: list[list], params: dict) -> list[dict]:
    """Séries horaires (UTC) pour toutes les communes, par paquets, avec reprise sur erreur."""
    out: list[dict] = []
    for k in range(0, len(communes), BATCH):
        part = communes[k : k + BATCH]
        q = {
            "latitude": ",".join(str(c[2]) for c in part),
            "longitude": ",".join(str(c[3]) for c in part),
            "hourly": VARS,
            "timezone": "GMT",
            "wind_speed_unit": "kmh",
            **params,
        }
        for attempt in range(6):
            try:
                r = requests.get(url, params=q, timeout=(15, 180), headers={"User-Agent": UA})
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:200]}")
                r.raise_for_status()
                js = r.json()
                out.extend(js if isinstance(js, list) else [js])
                break
            except Exception as e:  # noqa: BLE001
                wait = min(120, 15 * 2**attempt)
                print(f"Open-Meteo paquet {k // BATCH + 1} : {e} — reprise dans {wait} s", flush=True)
                time.sleep(wait)
        else:
            raise RuntimeError("Open-Meteo indisponible")
        time.sleep(params.get("_pause", 1))
    return out


def daily_inputs(series: dict) -> dict[str, dict]:
    """Entrées « de midi » par jour (clé AAAA-MM-JJ) à partir des séries horaires UTC."""
    h = series["hourly"]
    times = h["time"]
    days: dict[str, dict] = {}
    for i, t in enumerate(times):
        if not t.endswith("T12:00"):
            continue
        d = t[:10]
        temp, rh, ws = h["temperature_2m"][i], h["relative_humidity_2m"][i], h["wind_speed_10m"][i]
        if temp is None or rh is None or ws is None:
            continue
        # pluie de 13 h UTC la veille à 12 h UTC (cumuls horaires « de l'heure précédente »)
        rain, ok = 0.0, True
        for k in range(i - 23, i + 1):
            if k < 0:
                ok = False
                break
            v = h["precipitation"][k]
            rain += v or 0.0
        if not ok:
            continue
        gusts = [g for g in h["wind_gusts_10m"][max(0, i - 12) : i + 12] if g is not None]
        days[d] = {
            "t": temp,
            "rh": rh,
            "ws": ws,
            "wd": h["wind_direction_10m"][i],
            "gust": max(gusts) if gusts else None,
            "rr": rain,
        }
    return days


def run_days(codes: tuple[float, float, float], days: dict[str, dict], dates: list[str]) -> tuple[dict[str, dict], tuple[float, float, float]]:
    res: dict[str, dict] = {}
    for d in dates:
        x = days.get(d)
        if not x:
            continue
        r = fwi.step(codes, x["t"], x["rh"], x["ws"], x["rr"], int(d[5:7]))
        codes = (r["ffmc"], r["dmc"], r["dc"])
        res[d] = {**r, **x}
    return res, codes


def drange(a: date, b: date) -> list[str]:
    return [(a + timedelta(days=k)).isoformat() for k in range((b - a).days + 1)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="pub")
    ap.add_argument("--min-age-hours", type=float, default=0, help="ne recalcule pas si la dernière publication est plus récente")
    ap.add_argument("--spinup", action="store_true", help="force la mise en route depuis le 1er mars")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    prev_path = out / "fwi-66.json"
    if args.min_age_hours and prev_path.exists():
        try:
            gen = datetime.fromisoformat(json.loads(prev_path.read_text(encoding="utf-8"))["generated_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
            if age < args.min_age_hours:
                print(f"IFM publié il y a {age:.1f} h : pas de recalcul.")
                return 0
        except Exception:  # noqa: BLE001
            pass

    communes = json.loads((ROOT / "static" / "communes-66.json").read_text(encoding="utf-8"))["communes"]
    today = datetime.now(timezone.utc).date()
    first_shown = today - timedelta(days=PAST_DAYS - 1)
    last = today + timedelta(days=FORECAST_DAYS - 1)
    dates_shown = drange(first_shown, last)
    start_key = (first_shown - timedelta(days=1)).isoformat()  # codes de fin de journée nécessaires

    state_path = out / "state-66.json"
    state: dict = {}
    if state_path.exists() and not args.spinup:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    hist: dict[str, dict[str, list[float]]] = state.get("history", {})
    start_codes = hist.get(start_key)
    spin = start_codes is None or any(c[0] not in start_codes for c in communes)

    series = fetch(FORECAST, communes, {"past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS})
    inputs = [daily_inputs(s) for s in series]

    if spin:
        # mise en route : du 1er mars (de l'année, ou de l'année précédente avant mars) à la veille du premier jour affiché
        y = today.year if today.month >= 3 else today.year - 1
        a = date(y, 3, 1)
        b = first_shown - timedelta(days=1)
        print(f"Mise en route des codes depuis le {a} ({(b - a).days + 1} jours)", flush=True)
        hs = fetch(HISTORY, communes, {"start_date": a.isoformat(), "end_date": b.isoformat(), "_pause": 20})
        start_codes = {}
        for c, s in zip(communes, hs):
            _, codes = run_days((fwi.FFMC0, fwi.DMC0, fwi.DC0), daily_inputs(s), drange(a, b))
            start_codes[c[0]] = list(codes)

    per_commune = []
    new_hist: dict[str, dict[str, list[float]]] = {k: v for k, v in hist.items() if k >= (today - timedelta(days=KEEP_STATE_DAYS)).isoformat()}
    new_hist[start_key] = start_codes
    for c, d in zip(communes, inputs):
        codes = tuple(start_codes[c[0]])
        res, _ = run_days(codes, d, dates_shown)  # type: ignore[arg-type]
        rows = []
        for ds in dates_shown:
            r = res.get(ds)
            if not r:
                rows.append(None)
                continue
            rows.append([
                round(r["fwi"], 1), round(r["isi"], 1), round(r["bui"], 1), round(r["ffmc"], 1), round(r["dmc"], 1), round(r["dc"], 0),
                round(r["t"], 1), round(r["rh"]), round(r["ws"]), None if r["wd"] is None else round(r["wd"]), None if r["gust"] is None else round(r["gust"]), round(r["rr"], 1),
            ])
            if ds < today.isoformat():
                new_hist.setdefault(ds, {})[c[0]] = [round(r["ffmc"], 3), round(r["dmc"], 3), round(r["dc"], 3)]
        per_commune.append(rows)

    ti = dates_shown.index(today.isoformat())
    worst = max(
        ((c, rows[ti]) for c, rows in zip(communes, per_commune) if rows[ti]),
        key=lambda x: x[1][0],
        default=None,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    doc = {
        "schema_version": 1,
        "status": "ok",
        "generated_at": now,
        "department": "66",
        "method": "Indice forêt météo canadien (Van Wagner 1987), valeurs de 12 h UTC, pluie sur 24 h de 12 h à 12 h UTC",
        "source": {
            "provider": "Open-Meteo",
            "models": "meilleure combinaison disponible (Météo-France AROME / ARPEGE, ECMWF IFS…)",
            "url": "https://open-meteo.com/",
            "license": "CC BY 4.0",
        },
        "spinup": bool(spin),
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
    state_path.write_text(json.dumps({"updated_at": now, "history": dict(sorted(new_hist.items()))}, separators=(",", ":")), encoding="utf-8")
    if worst:
        print(f"IFM 66 publié — maxi aujourd'hui : {worst[0][1]} {worst[1][0]} ({len(communes)} communes, {len(dates_shown)} jours)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
