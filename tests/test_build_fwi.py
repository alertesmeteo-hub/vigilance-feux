"""Tests de la chaîne IFM sur nos modèles : python -m unittest discover -s tests (depuis la racine du dépôt)."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_fwi as b  # noqa: E402
import modeles  # noqa: E402
from modeles import Heure  # noqa: E402

UTC = timezone.utc
ALTITUDES = json.loads((ROOT / "static" / "altitudes-66.json").read_text(encoding="utf-8"))


def h(jour: date, heure: int) -> int:
    return b.heure_utc(jour, heure)


RANGS = {"AROME": 0, "ARPEGE": 1, "ICON": 2}


def sm(nom: str, heures: dict[int, Heure]) -> b.SerieModele:
    """Série d'un modèle pour la commune, initialisée à la première heure de la série."""
    return b.SerieModele(nom, RANGS[nom], min(heures), heures)


def serie(jour0: date, premiere: int, derniere: int, rr=0.0, t=20.0, rh=50.0, ws=10.0) -> dict[int, Heure]:
    """Série horaire factice : du jour0 à `premiere` h, sur `derniere` heures de plus (la pluie du premier pas est inconnue)."""
    base = h(jour0, premiere)
    return {base + k: Heure(t, rh, ws, 270.0, 30.0, None if k == 0 else rr) for k in range(derniere + 1)}


class Physique(unittest.TestCase):
    def test_humidite_et_rosee_sont_reciproques(self):
        for t, rh in [(10.0, 40.0), (25.0, 80.0), (-3.0, 90.0)]:
            self.assertAlmostEqual(modeles.humidite_relative(t, modeles.rosee(t, rh)), rh, delta=0.5)

    def test_humidite_bornee(self):
        self.assertEqual(modeles.humidite_relative(10.0, 15.0), 100.0)

    def test_distance(self):
        self.assertAlmostEqual(modeles.distance_km(42.7, 2.9, 42.7, 2.9), 0.0)
        self.assertAlmostEqual(modeles.distance_km(42.0, 2.0, 43.0, 2.0), 111.2, delta=0.5)


COLONNES = ["temperature_c", "dewpoint_c", "precipitation_mm", "wind_speed_kmh", "wind_direction_deg", "wind_gust_kmh"]


def doc_modele(debut: datetime, pas: int, altitudes=(300.0,), t=20.0, td=10.0, rr=lambda k, quand: 0.5, ws=12.0, grille=False) -> dict:
    """Fichier département factice : des points alignés vers le nord (ou, avec `grille`, une grille sur tout le département), une pluie
    `rr(k, instant)` à chaque pas après le premier."""
    if grille:
        points = [[k, 42.3 + 0.15 * (k // 9), 1.7 + 0.2 * (k % 9), 300.0] for k in range(5 * 9)]
    else:
        points = [[k, 42.5 + 0.01 * k, 2.5, a] for k, a in enumerate(altitudes)]
    return {
        "status": "ok",
        "generated_at": debut.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "columns": {"points": ["model_index", "latitude", "longitude", "altitude_m"], "values": COLONNES},
        "points": points,
        "forecast": [
            [(debut + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), [[t, td, 0.0 if k == 0 else rr(k, debut + timedelta(hours=k)), ws, 270.0, None if k == 0 else 25.0] for _ in points]]
            for k in range(pas)
        ],
    }


def faux_modele(altitudes, **kw) -> modeles.Modele:
    m = modeles.lire_modele("AROME", doc_modele(datetime(2026, 10, 10, 6, tzinfo=UTC), 4, altitudes, **kw))
    assert m is not None
    return m


class Modeles(unittest.TestCase):
    def test_choisit_le_point_a_la_bonne_altitude(self):
        m = faux_modele([100, 1500])  # 0,01° = 1,1 km d'écart entre les deux points
        self.assertEqual(modeles.choisir_point(m, 42.5, 2.5, 1480), 1)  # un peu plus loin mais à la bonne altitude
        self.assertEqual(modeles.choisir_point(m, 42.5, 2.5, 90), 0)

    def test_point_trop_loin(self):
        self.assertIsNone(modeles.choisir_point(faux_modele([100]), 45.0, 2.5, 100))

    def test_correction_altitude(self):
        m = faux_modele([100])
        quand = b.heure_utc(date(2026, 10, 10), 7)
        haut = modeles.serie_commune(m, 0, 1100)[quand]
        bas = modeles.serie_commune(m, 0, 100)[quand]
        self.assertAlmostEqual(haut.t, 20.0 - 6.5, places=6)  # 1 000 m plus haut
        self.assertAlmostEqual(bas.t, 20.0, places=6)
        # plus haut, l'air est plus froid et un peu plus humide : humidité relative supérieure à celle du point de grille
        self.assertGreater(haut.rh, bas.rh)

    def test_pluie_du_pas_initial_ignoree(self):
        s = modeles.serie_commune(faux_modele([100]), 0, 100)
        self.assertIsNone(s[b.heure_utc(date(2026, 10, 10), 6)].rr)
        self.assertEqual(s[b.heure_utc(date(2026, 10, 10), 7)].rr, 0.5)

    def test_fichier_inutilisable(self):
        self.assertIsNone(modeles.lire_modele("AROME", {"status": "error"}))
        self.assertIsNone(modeles.lire_modele("AROME", {"columns": {}}))


class Pluie(unittest.TestCase):
    J = date(2026, 10, 10)

    def test_archive_remplace_a_partir_du_debut_du_meilleur_modele_et_garde_le_passe(self):
        arch = {h(self.J, 3): 1.0, h(self.J, 8): 9.0}
        arome = serie(self.J, 6, 20, rr=0.2)
        icon = serie(self.J, 0, 100, rr=0.7)
        b.mettre_a_jour_archive(arch, [sm("AROME", arome), sm("ICON", icon)])
        self.assertEqual(arch[h(self.J, 3)], 1.0)  # avant AROME : archive conservée
        self.assertEqual(arch[h(self.J, 8)], 0.2)  # après le début d'AROME : AROME
        self.assertEqual(arch[h(self.J, 1)], 0.7)  # heure jamais archivée avant AROME : comblée par ICON
        self.assertEqual(arch[h(self.J, 6)], 0.7)  # pas initial d'AROME sans pluie connue : ICON
        self.assertEqual(arch[h(self.J, 6) + 40], 0.7)  # au-delà d'AROME : ICON

    def test_fenetre_complete_d_un_modele(self):
        demain = self.J + timedelta(days=1)
        total, manque = b.pluie_fenetre(demain, [sm("AROME", serie(self.J, 6, 48, rr=0.1))], {})
        self.assertAlmostEqual(total, 2.4)
        self.assertEqual(manque, 0)

    def test_fenetre_du_jour_en_cours_utilise_l_archive(self):
        arome = serie(self.J, 6, 48, rr=0.1)
        arch = {h(self.J - timedelta(days=1), k): 0.5 for k in range(13, 24)}
        arch.update({h(self.J, k): 1.0 for k in range(0, 6)})
        b.mettre_a_jour_archive(arch, [sm("AROME", arome)])
        total, manque = b.pluie_fenetre(self.J, [sm("AROME", arome)], arch)
        # 11 h à 0,5 (13 h à 23 h hier) + 6 h à 1,0 (0 h à 5 h) + 6 h d'AROME (7 h à 12 h) à 0,1
        self.assertAlmostEqual(total, 11 * 0.5 + 6 * 1.0 + 6 * 0.1)
        self.assertEqual(manque, 1)  # l'heure de 6 h (pas initial) n'a jamais été archivée

    def test_garder_archive(self):
        arch = {h(self.J, 0) - 100: 1.0, h(self.J - timedelta(days=1), 13): 2.0, h(self.J + timedelta(days=2), 0): 3.0, h(self.J, 5): 4.0}
        b.garder_archive(arch, self.J)
        self.assertEqual(sorted(arch.values()), [2.0, 4.0])

    def test_archive_aller_retour(self):
        arch = {"66001": {100: 0.5, 102: 1.25}, "66002": {101: 0.0}}
        self.assertEqual(b.lire_archive(json.loads(json.dumps(b.ecrire_archive(arch)))), arch)
        self.assertEqual(b.lire_archive({"x": 1}), {})
        self.assertEqual(b.lire_archive(None), {})


class Entrees(unittest.TestCase):
    J = date(2026, 10, 10)

    def test_valeurs_de_midi_du_meilleur_modele(self):
        arome = serie(self.J, 6, 48, t=22.0, rh=45.0, ws=28.0)
        icon = serie(self.J, 0, 100, t=10.0, rh=90.0, ws=2.0)
        x = b.entrees_jour(self.J + timedelta(days=1), [sm("AROME", arome), sm("ICON", icon)], {}, None)
        assert x
        self.assertEqual((x["t"], x["rh"], x["ws"], x["source"]), (22.0, 45.0, 28.0, "AROME"))

    def test_jour_hors_de_portee_d_arome_pris_sur_le_modele_suivant(self):
        arome = serie(self.J, 6, 48, t=22.0)
        arpege = serie(self.J, 6, 100, t=15.0)
        x = b.entrees_jour(self.J + timedelta(days=3), [sm("AROME", arome), sm("ARPEGE", arpege)], {}, None)
        assert x
        self.assertEqual((x["t"], x["source"]), (15.0, "ARPEGE"))

    def test_midi_deja_passe_repris_de_la_publication_precedente(self):
        apres_midi = serie(self.J, 15, 40, rr=0.0)  # modèle qui démarre à 15 h : plus de 12 h UTC
        prec = [5.0, 3.0, 20.0, 80.0, 10.0, 100.0, 23.4, 41, 31, 320, 52, 0.0]
        arch = {h(self.J, 0) + k: 0.0 for k in range(-11, 15)}
        x = b.entrees_jour(self.J, [sm("AROME", apres_midi)], arch, prec, jour_en_cours=True)
        assert x
        self.assertEqual((x["t"], x["rh"], x["ws"], x["source"]), (23.4, 41, 31, "précédente"))
        self.assertEqual(x["gust"], 52)  # la rafale de la publication précédente est conservée (maximum de la journée)

    def test_un_jour_a_venir_n_est_jamais_repris_d_une_prevision_plus_ancienne(self):
        apres_midi = serie(self.J, 15, 40, rr=0.0)  # ne couvre pas le midi du jour
        prec = [5.0, 3.0, 20.0, 80.0, 10.0, 100.0, 23.4, 41, 31, 320, 52, 0.0]
        self.assertIsNone(b.entrees_jour(self.J, [sm("AROME", apres_midi)], {}, prec, jour_en_cours=False))
        demain = self.J + timedelta(days=1)
        x = b.entrees_jour(demain, [sm("AROME", serie(self.J, 6, 48, rr=0.0))], {}, prec, jour_en_cours=False)
        assert x
        self.assertEqual(x["gust"], 30.0)  # rafale du modèle seule : les 52 km/h d une ancienne prévision ne restent pas

    def test_sans_aucune_donnee(self):
        self.assertIsNone(b.entrees_jour(self.J, [], {}, None))

    def test_pluie_inconnue_sans_repli_donne_pas_de_ligne(self):
        self.assertIsNone(b.entrees_jour(self.J, [sm("AROME", serie(self.J, 6, 48, rr=0.0))], {}, None))  # 18 h jamais archivées

    def test_publication_precedente_sert_de_plancher_a_la_pluie(self):
        prec = [5.0, 3.0, 20.0, 80.0, 10.0, 100.0, 23.4, 41, 31, 320, 52, 7.5]
        x = b.entrees_jour(self.J, [sm("AROME", serie(self.J, 6, 48, rr=0.0))], {}, prec)
        assert x
        self.assertEqual(x["rr"], 7.5)


class Memoire(unittest.TestCase):
    def frais(self, rang, init=1000, rr=0.5):
        return {"t": 20.0, "rh": 50.0, "ws": 10.0, "wd": 270.0, "gust": 30.0, "rr": rr, "source": b.NOMS[rang], "rang": rang, "init": init, "manque": 0}

    ARPEGE_GARDE = [0, 990, 22.0, 45.0, 12.0, 300.0, 40.0, 1.0]  # une entrée AROME de 10 h plus tôt

    def test_premier_calcul_memorise_le_frais(self):
        x, garde = b.choisir_memoire(self.frais(1), None, 1000, False)
        self.assertEqual(x["source"], "ARPEGE")
        self.assertEqual(garde[:2], [1, 1000])

    def test_modele_plus_fin_en_memoire_l_emporte_et_est_conserve(self):
        x, garde = b.choisir_memoire(self.frais(1), self.ARPEGE_GARDE, 1000, False)
        self.assertEqual((x["source"], x["t"], x["rr"]), ("AROME (mémoire)", 22.0, 1.0))
        self.assertIs(garde, self.ARPEGE_GARDE)

    def test_memoire_trop_ancienne(self):
        x, garde = b.choisir_memoire(self.frais(1), self.ARPEGE_GARDE, 990 + b.MEMOIRE_MAX_H + 1, False)
        self.assertEqual(x["source"], "ARPEGE")
        self.assertEqual(garde[0], 1)

    def test_a_modele_egal_le_frais_remplace_la_memoire(self):
        x, garde = b.choisir_memoire(self.frais(0, init=1000), self.ARPEGE_GARDE, 1000, False)
        self.assertEqual(x["source"], "AROME")
        self.assertEqual(garde[1], 1000)

    def test_sans_calcul_frais_la_memoire_sert(self):
        x, garde = b.choisir_memoire(None, self.ARPEGE_GARDE, 1000, False)
        self.assertEqual(x["t"], 22.0)
        self.assertIsNotNone(garde)

    def test_repli_sur_la_publication_precedente_moins_bon_que_la_memoire(self):
        repli = self.frais(0)
        repli.update({"source": "précédente", "rang": None, "init": None})
        x, _ = b.choisir_memoire(repli, self.ARPEGE_GARDE, 1000, True)
        self.assertEqual(x["source"], "AROME (mémoire)")
        # et sans mémoire, le repli n'est pas mémorisé
        self.assertEqual(b.choisir_memoire(repli, None, 1000, True)[1], None)

    def test_jour_en_cours_garde_la_pluie_fraiche(self):
        x, _ = b.choisir_memoire(self.frais(1, rr=7.5), self.ARPEGE_GARDE, 1000, True)
        self.assertEqual((x["source"], x["t"], x["rr"]), ("AROME (mémoire)", 22.0, 7.5))

    def test_lecture_de_la_memoire_ecarte_le_passe_et_le_mal_forme(self):
        doc = {"66001": {"2026-10-09": [0] * 8, "2026-10-10": [0] * 8, "2026-10-11": [0, 1]}, "66002": "x"}
        self.assertEqual(sorted(b.lire_memoire(doc, date(2026, 10, 10))["66001"]), ["2026-10-10"])
        self.assertEqual(b.lire_memoire(None, date(2026, 10, 10)), {})


class BoutEnBout(unittest.TestCase):
    """Exécutions successives sur de faux fichiers : reprise d'une publication existante, gel du passé, archive de pluie."""

    def preparer(self, d: Path) -> list:
        (d / "modeles").mkdir()
        (d / "pub").mkdir()
        communes = json.loads((ROOT / "static" / "communes-66.json").read_text(encoding="utf-8"))["communes"]
        dates = b.drange(date(2026, 10, 3), date(2026, 10, 16))
        ligne = [5.0, 3.0, 20.0, 80.0, 10.0, 100.0, 20.0, 50, 10, 270, 30, 0.0]
        doc = {"generated_at": "2026-10-10T01:00:00Z", "dates": dates, "communes": communes, "data": [[list(ligne) for _ in dates] for _ in communes]}
        (d / "pub" / "fwi-66.json").write_text(json.dumps(doc), encoding="utf-8")
        hist = {"2026-10-09": {c[0]: [80.0, 5.0, 100.0] for c in communes}}
        (d / "pub" / "state-66.json").write_text(json.dumps({"history": hist}), encoding="utf-8")
        return communes

    def ecrire_modeles(self, d: Path, run: datetime, rr, pas_arome: int = 49, chaud: float = 0.0) -> None:
        """Fichiers modèles factices ; `pas_arome` tronque AROME, `chaud` ajoute des degrés à AROME seul."""
        for depot, pas in [("arome-meteofrance", pas_arome), ("arpege-meteo-france", 103), ("ICON-GLOBAL-13-km", 181)]:
            t = 22.0 + (chaud if depot.startswith("arome") else 0.0)
            (d / "modeles" / f"{depot}.json").write_text(json.dumps(doc_modele(run, pas, t=t, td=8.0, rr=rr, ws=20.0, grille=True)), encoding="utf-8")

    def lancer(self, d: Path, quand: datetime, *options: str):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(b.main(["--out", str(d / "pub"), "--modeles-dir", str(d / "modeles"), *options], maintenant=quand), 0)
        return json.loads((d / "pub" / "fwi-66.json").read_text(encoding="utf-8")), json.loads((d / "pub" / "state-66.json").read_text(encoding="utf-8"))

    def test_trois_executions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            communes = self.preparer(d)
            # exécution 1 : 12 h 30 UTC le 10 octobre, modèles initialisés à 6 h, sans pluie
            self.ecrire_modeles(d, datetime(2026, 10, 10, 6, tzinfo=UTC), lambda k, t: 0.0)
            doc1, st1 = self.lancer(d, datetime(2026, 10, 10, 12, 30, tzinfo=UTC))
            i10, i09 = doc1["dates"].index("2026-10-10"), doc1["dates"].index("2026-10-09")
            self.assertEqual(doc1["data"][0][i09], [5.0, 3.0, 20.0, 80.0, 10.0, 100.0, 20.0, 50, 10, 270, 30, 0.0])  # hier : figé
            ligne10 = doc1["data"][0][i10]
            self.assertIsNotNone(ligne10)
            self.assertEqual(ligne10[b.RR], 0.0)
            self.assertAlmostEqual(ligne10[b.T], 22.0 - 0.0065 * (ALTITUDES[communes[0][0]] - 300.0), places=1)
            self.assertIn("2026-10-10", st1["history"])
            self.assertTrue(st1["pluie"]["valeurs"])
            self.assertEqual(doc1["source"]["origine_par_jour"]["2026-10-10"], "AROME")
            self.assertEqual(doc1["source"]["origine_par_jour"]["2026-10-13"], "ARPEGE")
            self.assertEqual(doc1["source"]["origine_par_jour"]["2026-10-16"], "ICON")
            # exécution 2 : 18 h 30 UTC, nouveau run de 12 h avec une averse de 2 mm/h de 13 h à 15 h UTC
            self.ecrire_modeles(d, datetime(2026, 10, 10, 12, tzinfo=UTC), lambda k, t: 2.0 if t.hour in (13, 14, 15) and t.day == 10 else 0.0)
            doc2, _ = self.lancer(d, datetime(2026, 10, 10, 18, 30, tzinfo=UTC))
            ligne11 = doc2["data"][0][doc2["dates"].index("2026-10-11")]
            self.assertAlmostEqual(ligne11[b.RR], 6.0)  # l'averse tombe dans la fenêtre de demain (13 h UTC d'aujourd'hui à 12 h UTC)
            # exécution 3 : le lendemain à 0 h 30 UTC, le 10 est devenu un jour passé, repris tel que calculé la veille
            self.ecrire_modeles(d, datetime(2026, 10, 11, 0, tzinfo=UTC), lambda k, t: 0.0)
            doc3, st3 = self.lancer(d, datetime(2026, 10, 11, 0, 30, tzinfo=UTC))
            self.assertEqual(doc3["today"], "2026-10-11")
            self.assertEqual(doc3["data"][0][doc3["dates"].index("2026-10-10")], doc2["data"][0][doc2["dates"].index("2026-10-10")])
            ligne11b = doc3["data"][0][doc3["dates"].index("2026-10-11")]
            # la pluie de 13 h à 15 h UTC vient de l'archive : le run de 0 h ne la connaît plus
            self.assertAlmostEqual(ligne11b[b.RR], 6.0)
            self.assertIn("2026-10-10", st3["history"])

    def test_fichier_arome_tronque_ne_fait_pas_repasser_demain_sur_arpege(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.preparer(d)
            # run de 6 h complet : demain est calculé avec AROME
            self.ecrire_modeles(d, datetime(2026, 10, 10, 6, tzinfo=UTC), lambda k, t: 0.0)
            doc1, _ = self.lancer(d, datetime(2026, 10, 10, 11, 30, tzinfo=UTC))
            self.assertEqual(doc1["source"]["origine_par_jour"]["2026-10-11"], "AROME")
            # run de 9 h tronqué à +12 h : demain n'est plus couvert par AROME, la mémoire garde l'AROME de 6 h
            self.ecrire_modeles(d, datetime(2026, 10, 10, 9, tzinfo=UTC), lambda k, t: 0.0, pas_arome=13, chaud=3.0)
            doc2, _ = self.lancer(d, datetime(2026, 10, 10, 13, 30, tzinfo=UTC))
            self.assertEqual(doc2["source"]["origine_par_jour"]["2026-10-11"], "AROME (mémoire)")
            self.assertEqual(doc2["source"]["origine_par_jour"]["2026-10-12"], "ARPEGE")
            i = doc2["dates"].index("2026-10-11")
            self.assertEqual(doc2["data"][0][i][b.T], doc1["data"][0][i][b.T])  # mêmes entrées qu'avant le run tronqué
            # le run de 9 h complet remplace la mémoire
            self.ecrire_modeles(d, datetime(2026, 10, 10, 9, tzinfo=UTC), lambda k, t: 0.0, chaud=3.0)
            doc3, _ = self.lancer(d, datetime(2026, 10, 10, 15, 30, tzinfo=UTC))
            self.assertEqual(doc3["source"]["origine_par_jour"]["2026-10-11"], "AROME")
            self.assertAlmostEqual(doc3["data"][0][i][b.T] - doc1["data"][0][i][b.T], 3.0, places=1)
            # plus de 18 h plus tard, avec un AROME tronqué : la mémoire a expiré
            self.ecrire_modeles(d, datetime(2026, 10, 11, 3, tzinfo=UTC), lambda k, t: 0.0, pas_arome=13)
            doc4, _ = self.lancer(d, datetime(2026, 10, 11, 7, 30, tzinfo=UTC))
            self.assertEqual(doc4["source"]["origine_par_jour"]["2026-10-12"], "ARPEGE")

    def test_pas_de_recalcul_si_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.preparer(d)
            self.ecrire_modeles(d, datetime(2026, 10, 10, 0, tzinfo=UTC), lambda k, t: 0.0)
            avant = (d / "pub" / "fwi-66.json").read_text(encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                b.main(["--out", str(d / "pub"), "--modeles-dir", str(d / "modeles"), "--min-age-hours", "5"], maintenant=datetime(2026, 10, 10, 3, tzinfo=UTC))
            self.assertEqual((d / "pub" / "fwi-66.json").read_text(encoding="utf-8"), avant)

    def test_sans_codes_de_depart_aucune_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.preparer(d)
            (d / "pub" / "state-66.json").write_text("{}", encoding="utf-8")
            doc = json.loads((d / "pub" / "fwi-66.json").read_text(encoding="utf-8"))
            doc["data"] = [[None] * len(doc["dates"]) for _ in doc["communes"]]
            (d / "pub" / "fwi-66.json").write_text(json.dumps(doc), encoding="utf-8")
            self.ecrire_modeles(d, datetime(2026, 10, 10, 6, tzinfo=UTC), lambda k, t: 0.0)
            with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                b.main(["--out", str(d / "pub"), "--modeles-dir", str(d / "modeles")], maintenant=datetime(2026, 10, 10, 12, 30, tzinfo=UTC))

    def test_codes_standard_apres_perte_de_la_memoire(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.preparer(d)
            (d / "pub" / "state-66.json").write_text("{}", encoding="utf-8")
            doc = json.loads((d / "pub" / "fwi-66.json").read_text(encoding="utf-8"))
            doc["data"] = [[None] * len(doc["dates"]) for _ in doc["communes"]]
            (d / "pub" / "fwi-66.json").write_text(json.dumps(doc), encoding="utf-8")
            self.ecrire_modeles(d, datetime(2026, 10, 10, 6, tzinfo=UTC), lambda k, t: 0.0)
            doc, st = self.lancer(d, datetime(2026, 10, 10, 18, 30, tzinfo=UTC), "--codes-standard")
            self.assertTrue(doc["spinup"])
            self.assertIsNotNone(doc["data"][0][doc["dates"].index("2026-10-10")])
            self.assertIn("2026-10-10", st["history"])

    def test_aucun_modele(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.preparer(d)
            with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                b.main(["--out", str(d / "pub"), "--modeles-dir", str(d / "modeles")], maintenant=datetime(2026, 10, 10, 12, 30, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
