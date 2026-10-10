# Vigilance feux de forêt — données

Publie sur la branche `data` (toutes les 20 min) :

- `feux.json` : feux actifs détectés par satellite (NASA FIRMS, VIIRS + MODIS, 7 jours) en Occitanie, Catalogne et abords, regroupés en foyers avec la commune la plus proche.
- `fwi-66.json` : indice forêt météo (IFM / FWI canadien) des 226 communes des Pyrénées-Orientales, 7 jours passés + 7 jours de prévision (recalculé toutes les 2 h).
- `state-66.json` : mémoire du calcul : codes d'humidité FFMC / DMC / DC de fin de journée (21 derniers jours) et archive horaire de la pluie (de 13 h UTC la veille à la fin de demain).

Lu par https://app.alertes-meteo.com/vigilance-feux

## Calcul de l'IFM

Aucun service météo extérieur : les entrées viennent de nos propres modèles, lus dans les fichiers par département des dépôts
`arome-meteofrance`, `arpege-meteo-france` et `ICON-GLOBAL-13-km` (branche `data`, `departements/66.json`) par `scripts/modeles.py`.

- **Valeurs de midi (12 h UTC)** : AROME pour les deux premiers jours, ARPEGE jusqu'à 4 jours, ICON au-delà. Chaque commune lit le point
  de grille qui minimise « distance + écart d'altitude / 100 » ; température (6,5 °C/km) et point de rosée (2 °C/km) sont corrigés de l'écart
  entre l'altitude du point et celle du centre de la commune (`static/altitudes-66.json`, IGN RGE ALTI), puis l'humidité relative est recalculée.
- **Pluie de 24 h** (13 h UTC la veille à 12 h UTC) : un modèle qui couvre toute la fenêtre donne son total ; pour le jour en cours, dont le début
  est déjà passé, on additionne l'archive horaire de `state-66.json`. À chaque calcul, les heures à partir du début du meilleur modèle sont
  remplacées par sa prévision, les plus anciennes gardent la valeur archivée (comme les « premières heures de chaque prévision »).
- **Le passé n'est jamais recalculé** : les jours passés sont repris tels quels de la publication précédente. Les codes FFMC / DMC / DC repartent
  de ceux de la veille (`history` de `state-66.json`, à défaut les codes arrondis de la ligne d'hier).
- **Pas de mise en route automatique** : si `state-66.json` et la ligne d'hier de `fwi-66.json` sont perdus (la branche `data` est réécrite à
  chaque publication, sans historique), le calcul s'arrête (plus de 5 % des communes sans codes de départ) et la publication précédente est
  conservée. Après une grosse pluie, on peut repartir des codes standard (85 / 6 / 15) en lançant le workflow à la main avec « codes_standard » :
  les codes longs (DMC, DC) sont alors fiables. En période sèche ils seraient trop bas, ne pas le faire.

Tests : `python -m unittest discover -s tests`.
