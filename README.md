# TurfAI – stratégie PMU « 2 € placé favori + 2 € placé outsider »

Projet d'analyse honnête d'une stratégie de paris simple placé sur PMU.fr.
Rien ici ne garantit un gain : le rapport d'analyse dit clairement ce qui marche et ce qui ne marche pas.

## Contenu

| Fichier | Rôle |
|---|---|
| `collecte.py` | Récupère les courses terminées depuis l'API JSON publique de pmu.fr, avec cache et requêtes espacées. |
| `pmu.sqlite` | Base de données (courses, chevaux, rapports placés, cache brut). |
| `analyse_disciplines.py` | Compare les disciplines pour choisir celle où se concentrer. |

## Données

Source : `https://online.turfinfo.api.pmu.fr/rest/client/1/...`
- `programme/JJMMAAAA` : réunions et courses du jour ;
- `programme/JJMMAAAA/R{n}/C{n}/participants` : partants, cotes, musique, driver/jockey, entraîneur, déferrage ;
- `programme/JJMMAAAA/R{n}/C{n}/rapports-definitifs` : rapports définitifs.

Points à connaître :
- **Cotes** : `cote_matin` = rapport de référence du matin ; `cote_finale` = dernier rapport avant le départ.
  L'API ne publie pas de cote « après course » pour chaque cheval.
- **Rapports placés** : stockés **en centimes** pour 1 € (150 = 1,50 €). Placé national en priorité,
  sinon placé international (courses étrangères).
- **Quinté** : détecté par la présence d'un rapport Quinté+ dans les rapports définitifs.
- Courses de moins de 8 partants et courses sans rapport placé : exclues.
- **Disciplines étudiées : plat et trot attelé uniquement** (option `--disciplines`, par défaut `ATTELE,PLAT`).
  Trot monté, haies, steeple et cross sont trop rares (moins de 50 courses de vérification) :
  ils ne sont ni téléchargés ni comptés.

## Mise à jour chaque semaine

Une seule commande, depuis le dossier du projet :

```bash
python3 collecte.py --db pmu.sqlite --courses 400 --jours-max 10
```

- Les pages déjà téléchargées sont lues dans le cache : rien n'est retéléchargé.
- `--jours-max 10` limite la remontée aux 10 derniers jours (une semaine + marge).
- Le script attend environ 1 seconde entre deux requêtes pour ne pas surcharger le site.

Après une modification du script, pour reconstruire les tables sans refaire de requêtes :

```bash
python3 collecte.py --db pmu.sqlite --relire-cache --depuis AAAA-MM-JJ
```
