"""Feux actifs détectés par satellite (NASA FIRMS) autour des Pyrénées-Orientales et de l'Occitanie.

Sources : fichiers publics NRT « Europe 7 jours » de VIIRS (Suomi-NPP, NOAA-20, NOAA-21, pixels de
375 m) et MODIS (Aqua/Terra, 1 km). Les détections proches (< 1,5 km) sont regroupées en un même
foyer ; chaque foyer reçoit la commune la plus proche et un indicateur « foyer fixe probable »
(détections le même endroit sur au moins 3 jours distincts : industrie, torchère, centrale…).
Publie feux.json.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
SOURCES = [
    ("VIIRS_SNPP", "VIIRS Suomi-NPP", "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Europe_7d.csv"),
    ("VIIRS_NOAA20", "VIIRS NOAA-20", "noaa-20-viirs-c2/csv/J1_VIIRS_C2_Europe_7d.csv"),
    ("VIIRS_NOAA21", "VIIRS NOAA-21", "noaa-21-viirs-c2/csv/J2_VIIRS_C2_Europe_7d.csv"),
    ("MODIS", "MODIS Aqua/Terra", "modis-c6.1/csv/MODIS_C6_1_Europe_7d.csv"),
]
UA = "AlertesMeteo-VigilanceFeux/1.0 (+https://app.alertes-meteo.com)"
# Occitanie, Catalogne, Andorre et abords
LAT0, LAT1, LON0, LON1 = 41.3, 45.1, -0.4, 4.9
LIEN_KM = 1.5
FIXE_JOURS = 3
OCCITANIE = {"09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"}


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 111.2
    dx = (lon2 - lon1) * 111.2 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def lire(path: str) -> list[dict]:
    for attempt in range(4):
        try:
            r = requests.get(BASE + path, timeout=(15, 120), headers={"User-Agent": UA})
            r.raise_for_status()
            return list(csv.DictReader(io.StringIO(r.text)))
        except Exception as e:  # noqa: BLE001
            print(f"{path} : {e} (tentative {attempt + 1})", flush=True)
            time.sleep(20 * (attempt + 1))
    return []


def confiance(src: str, v: str) -> str:
    v = (v or "").strip().lower()
    if src == "MODIS":
        try:
            n = int(float(v))
        except ValueError:
            return "nominale"
        return "haute" if n >= 80 else "faible" if n < 30 else "nominale"
    return {"h": "haute", "high": "haute", "l": "faible", "low": "faible"}.get(v, "nominale")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="pub")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    dets = []
    etat_sources = []
    for sid, label, path in SOURCES:
        rows = lire(path)
        n = 0
        last = None
        for r in rows:
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
            except (KeyError, ValueError):
                continue
            hhmm = r.get("acq_time", "0000").zfill(4)
            t = datetime.strptime(f"{r['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
            last = t if last is None or t > last else last
            if not (LAT0 <= lat <= LAT1 and LON0 <= lon <= LON1):
                continue
            try:
                frp = float(r.get("frp") or 0)
            except ValueError:
                frp = 0.0
            dets.append({"lat": lat, "lon": lon, "t": t, "frp": frp, "src": sid, "conf": confiance(sid, r.get("confidence", "")), "jour": r.get("daynight", "")})
            n += 1
        etat_sources.append({"id": sid, "label": label, "ok": bool(rows), "detections_zone": n, "derniere_acquisition": last.isoformat().replace("+00:00", "Z") if last else None})
    if not any(s["ok"] for s in etat_sources):
        print("Aucune source FIRMS lisible", file=sys.stderr)
        return 1

    # regroupement (lien simple à moins de LIEN_KM)
    dets.sort(key=lambda d: d["t"])
    parent = list(range(len(dets)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell: dict[tuple[int, int], list[int]] = {}
    for i, d in enumerate(dets):
        key = (int(d["lat"] / 0.02), int(d["lon"] / 0.02))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in cell.get((key[0] + dy, key[1] + dx), []):
                    if km(d["lat"], d["lon"], dets[j]["lat"], dets[j]["lon"]) <= LIEN_KM:
                        parent[find(i)] = find(j)
        cell.setdefault(key, []).append(i)
    groupes: dict[int, list[dict]] = {}
    for i, d in enumerate(dets):
        groupes.setdefault(find(i), []).append(d)

    communes = json.loads((ROOT / "static" / "communes-sud.json").read_text(encoding="utf-8"))["communes"]
    deps = json.loads((ROOT / "static" / "departements.geojson").read_text(encoding="utf-8"))["features"]
    dep_geoms = [shape(f["geometry"]) for f in deps]
    tree = STRtree(dep_geoms)
    cgrid: dict[tuple[int, int], list[list]] = {}
    for c in communes:
        cgrid.setdefault((int(c[2] / 0.1), int(c[3] / 0.1)), []).append(c)

    def lieu(lat: float, lon: float) -> dict:
        dep = None
        for k in tree.query(Point(lon, lat)):
            if dep_geoms[int(k)].contains(Point(lon, lat)):
                dep = deps[int(k)]["properties"]
                break
        best, bd = None, 1e9
        gy, gx = int(lat / 0.1), int(lon / 0.1)
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                for c in cgrid.get((gy + dy, gx + dx), []):
                    dd = km(lat, lon, c[2], c[3])
                    if dd < bd:
                        best, bd = c, dd
        if dep is None:
            if 42.42 <= lat <= 42.66 and 1.40 <= lon <= 1.79:
                pays = "Andorre"
            elif lat < 43.6 and lon > 3.05 and best and bd > 3:
                pays = "En mer"
            else:
                pays = "Catalogne (Espagne)" if lon >= 0.2 else "Espagne"
            return {"pays": pays, "dep": None, "commune": best[1] if best and bd < 25 else None, "insee": None, "dist_km": round(bd, 1) if best and bd < 25 else None}
        code = str(dep.get("code"))
        return {"pays": "France", "dep": code, "dep_nom": dep.get("nom"), "occitanie": code in OCCITANIE, "commune": best[1] if best else None, "insee": best[0] if best else None, "dist_km": round(bd, 1) if best else None}

    foyers = []
    for g in groupes.values():
        g.sort(key=lambda d: d["t"])
        w = [max(d["frp"], 0.1) for d in g]
        lat = sum(d["lat"] * x for d, x in zip(g, w)) / sum(w)
        lon = sum(d["lon"] * x for d, x in zip(g, w)) / sum(w)
        jours = sorted({d["t"].date().isoformat() for d in g})
        confs = [d["conf"] for d in g]
        conf = "haute" if "haute" in confs or len(g) >= 3 else "faible" if all(c == "faible" for c in confs) else "nominale"
        first, last = g[0]["t"], g[-1]["t"]
        foyers.append({
            "id": f"{round(lat, 3)}_{round(lon, 3)}_{first.strftime('%Y%m%d%H%M')}",
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "premiere": first.isoformat().replace("+00:00", "Z"),
            "derniere": last.isoformat().replace("+00:00", "Z"),
            "age_h": round((now - last).total_seconds() / 3600, 1),
            "n": len(g),
            "frp_max": round(max(d["frp"] for d in g), 1),
            "frp_total": round(sum(d["frp"] for d in g), 1),
            "satellites": sorted({d["src"] for d in g}),
            "confiance": conf,
            "jours": jours,
            "fixe": len(jours) >= FIXE_JOURS,
            "lieu": lieu(lat, lon),
            "detections": [[round(d["lat"], 4), round(d["lon"], 4), d["t"].strftime("%Y-%m-%dT%H:%MZ"), round(d["frp"], 1), d["src"]] for d in g],
        })
    foyers.sort(key=lambda f: f["derniere"], reverse=True)
    doc = {
        "schema_version": 1,
        "status": "ok",
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "zone": {"south": LAT0, "north": LAT1, "west": LON0, "east": LON1, "label": "Occitanie, Catalogne, Andorre et abords"},
        "fenetre_heures": 168,
        "source": {"provider": "NASA FIRMS (LANCE)", "url": "https://firms.modaps.eosdis.nasa.gov/", "license": "Données NASA, libre réutilisation avec mention"},
        "sources": etat_sources,
        "foyers": foyers,
    }
    (out / "feux.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    recents = [f for f in foyers if f["age_h"] <= 24 and not f["fixe"]]
    print(f"FIRMS : {len(dets)} détections, {len(foyers)} foyers ({len(recents)} de moins de 24 h hors foyers fixes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
