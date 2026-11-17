#!/usr/bin/env python3
"""
Collecte des courses PMU terminées (API JSON publique utilisée par pmu.fr).

Usage :
  python3 collecte.py --courses 500 --min-partants 8      # collecte initiale
  python3 collecte.py --mise-a-jour                        # ajoute les nouvelles courses (depuis la dernière date)

Stockage : SQLite (pmu.sqlite). Cache brut des réponses dans la table `cache`
(une réponse n'est jamais retéléchargée). Requêtes espacées (--pause secondes).
"""
import argparse
import datetime as dt
import json
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = "https://online.turfinfo.api.pmu.fr/rest/client/1"
UA = "pmu-strategie-recherche/1.0"  # générique, sans données personnelles

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS courses (
  course_id TEXT PRIMARY KEY,         -- DDMMYYYY_R{n}C{n}
  date TEXT, reunion INTEGER, course INTEGER,
  hippodrome TEXT, hippodrome_code TEXT, pays TEXT, paris_province TEXT,
  libelle TEXT, discipline TEXT, specialite TEXT, distance INTEGER,
  nb_partants INTEGER, nb_non_partants INTEGER, quinte INTEGER,
  arrivee_definitive INTEGER
);
CREATE TABLE IF NOT EXISTS chevaux (
  course_id TEXT, num INTEGER, nom TEXT, statut TEXT,
  cote_matin REAL,        -- dernierRapportReference (rapport de référence, le matin)
  cote_finale REAL,       -- dernierRapportDirect (dernier rapport avant départ)
  place_arrivee INTEGER, age INTEGER, sexe TEXT, musique TEXT,
  driver TEXT, driver_change INTEGER, entraineur TEXT, deferre TEXT,
  PRIMARY KEY (course_id, num)
);
CREATE TABLE IF NOT EXISTS rapports_places (
  course_id TEXT, num INTEGER,
  dividende_pour_1e REAL,  -- rapport simple placé définitif pour 1 €, EN CENTIMES (ex. 150 = 1,50 €)
  PRIMARY KEY (course_id, num)
);
CREATE TABLE IF NOT EXISTS courses_traitees (course_id TEXT PRIMARY KEY, ok INTEGER, note TEXT);
"""

PARIS_HIPPO = {"VIN", "LON", "AUT", "SCL", "ENG", "MAI"}  # Paris (Vincennes, Longchamp, Auteuil, Saint-Cloud, Enghien, Maisons-Laffitte)


class Client:
    def __init__(self, db, pause):
        self.db = db
        self.pause = pause
        self.last = 0.0
        self.nb_reseau = 0

    def get(self, path):
        """Retourne le JSON (ou None si pas de contenu). Utilise le cache."""
        url = BASE + path
        row = self.db.execute("SELECT status, body FROM cache WHERE url=?", (url,)).fetchone()
        if row:
            return json.loads(row[1]) if row[1] else None
        for essai in range(4):
            # espacement entre requêtes, avec un peu d'aléa
            attente = self.pause + random.uniform(0, self.pause / 2) - (time.time() - self.last)
            if attente > 0:
                time.sleep(attente)
            self.last = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status, body = r.status, r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                status, body = e.code, ""
            except Exception:
                time.sleep(10 * (essai + 1))
                continue
            if status in (429, 503):
                time.sleep(60 * (essai + 1))
                continue
            self.nb_reseau += 1
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                            (url, status, body if status == 200 else "", dt.datetime.now().isoformat()))
            self.db.commit()
            return json.loads(body) if status == 200 and body else None
        return None


def ts_to_date(ms):
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def rapport_ref(cheval, cle):
    r = cheval.get(cle) or {}
    return r.get("rapport")


def traiter_course(client, db, date_ddmmyyyy, reunion, course_info, hippo):
    n_r, n_c = reunion["numOfficiel"], course_info["numOrdre"]
    cid = f"{date_ddmmyyyy}_R{n_r}C{n_c}"
    if db.execute("SELECT 1 FROM courses_traitees WHERE course_id=?", (cid,)).fetchone():
        return None
    parts = client.get(f"/programme/{date_ddmmyyyy}/R{n_r}/C{n_c}/participants")
    if not parts:
        db.execute("INSERT OR REPLACE INTO courses_traitees VALUES (?,?,?)", (cid, 0, "pas de participants"))
        return None
    liste = parts["participants"]
    partants = [p for p in liste if p.get("statut") == "PARTANT"]
    nb_np = len(liste) - len(partants)
    rap = client.get(f"/programme/{date_ddmmyyyy}/R{n_r}/C{n_c}/rapports-definitifs") or []
    types = {bloc.get("typePari") for bloc in rap}
    # Le Quinté+ n'apparaît que dans les rapports définitifs
    quinte = int(any("QUINTE" in str(t) for t in types))
    # Placé national en priorité ; sinon placé international (courses étrangères)
    type_place = "SIMPLE_PLACE" if "SIMPLE_PLACE" in types else "SIMPLE_PLACE_INTERNATIONAL"
    placés = {}
    for bloc in rap:
        if bloc.get("typePari") == type_place:
            for rr in bloc.get("rapports", []):
                for num in str(rr.get("combinaison", "")).split("-"):
                    if num.strip().isdigit():
                        placés[int(num)] = rr.get("dividendePourUnEuro")
    code = hippo["code"]
    db.execute("INSERT OR REPLACE INTO courses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        cid, ts_to_date(course_info["heureDepart"]), n_r, n_c,
        hippo.get("libelleLong"), code, reunion["pays"]["code"],
        "PARIS" if (reunion["pays"]["code"] == "FRA" and code in PARIS_HIPPO) else "PROVINCE",
        course_info.get("libelle"), course_info.get("discipline"), course_info.get("specialite"),
        course_info.get("distance"), len(partants), nb_np, quinte, int(bool(course_info.get("arriveeDefinitive"))),
    ))
    for p in liste:
        ordre = p.get("ordreArrivee")
        place = ordre if isinstance(ordre, int) and ordre > 0 else None
        db.execute("INSERT OR REPLACE INTO chevaux VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            cid, p.get("numPmu"), p.get("nom"), p.get("statut"),
            rapport_ref(p, "dernierRapportReference"), rapport_ref(p, "dernierRapportDirect"),
            place, p.get("age"), p.get("sexe"), p.get("musique"),
            p.get("driver"), int(bool(p.get("driverChange"))), p.get("entraineur"), p.get("deferre"),
        ))
    for num, div in placés.items():
        db.execute("INSERT OR REPLACE INTO rapports_places VALUES (?,?,?)", (cid, num, div))
    db.execute("INSERT OR REPLACE INTO courses_traitees VALUES (?,?,?)", (cid, 1, ""))
    return cid, len(partants)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pmu.sqlite")
    ap.add_argument("--courses", type=int, default=500, help="nombre de courses à viser")
    ap.add_argument("--min-partants", type=int, default=8)
    ap.add_argument("--pause", type=float, default=1.5, help="secondes entre requêtes")
    ap.add_argument("--jours-max", type=int, default=400, help="sécurité : jours à remonter au maximum")
    ap.add_argument("--depuis", help="date de départ AAAA-MM-JJ (défaut : hier)")
    ap.add_argument("--relire-cache", action="store_true",
                    help="vide les tables calculées et les reconstruit depuis le cache (sans réseau pour les pages déjà vues)")
    ap.add_argument("--disciplines", default="PLAT",
                    help="disciplines étudiées, séparées par des virgules (vide = toutes). "
                         "Par défaut : plat uniquement (choix du projet).")
    args = ap.parse_args()
    disciplines = {d.strip() for d in args.disciplines.split(",") if d.strip()}

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    if args.relire_cache:
        for t in ("courses", "chevaux", "rapports_places", "courses_traitees"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
    client = Client(db, args.pause)

    jour = dt.date.fromisoformat(args.depuis) if args.depuis else dt.date.today() - dt.timedelta(days=1)
    ajoutees = 0
    exclues = 0
    for _ in range(args.jours_max):
        if ajoutees >= args.courses:
            break
        d = jour.strftime("%d%m%Y")
        prog = client.get(f"/programme/{d}")
        if prog:
            for reunion in prog["programme"]["reunions"]:
                for c in reunion["courses"]:
                    if not c.get("arriveeDefinitive"):
                        continue
                    if disciplines and c.get("discipline") not in disciplines:
                        continue  # discipline non étudiée : ni téléchargée ni comptée
                    res = traiter_course(client, db, d, reunion, c, reunion["hippodrome"])
                    db.commit()
                    if res is None:
                        continue
                    _, nb = res
                    if nb < args.min_partants:
                        exclues += 1
                        for table in ("courses", "chevaux", "rapports_places"):
                            db.execute(f"DELETE FROM {table} WHERE course_id=?", (res[0],))
                        db.commit()
                        continue
                    ajoutees += 1
                    if ajoutees >= args.courses:
                        break
                if ajoutees >= args.courses:
                    break
        print(f"{jour}: total courses retenues={ajoutees}, exclues(<{args.min_partants} partants)={exclues}, requêtes réseau={client.nb_reseau}", flush=True)
        jour -= dt.timedelta(days=1)
    db.commit()
    print("Terminé.", ajoutees, "courses retenues.")


if __name__ == "__main__":
    sys.exit(main())
