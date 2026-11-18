#!/usr/bin/env python3
"""
Compare les disciplines pour choisir celle où se concentrer.

Critères (tous mesurés, aucun présupposé) :
  A. Stratégie de base (2 € placé favori + 2 € placé outsider >= 30) : rendement, cotes matin et finales.
  B. Prévisibilité du placé par les cotes du public (perte logarithmique, plus bas = plus prévisible).
  C. Erreur du public : gain de précision quand on recalibre les cotes, puis quand on ajoute la musique
     (mesuré sur les 30 % de courses les plus récentes, jamais utilisées pour l'apprentissage).
  D. Avantage exploitable : règle « valeur » (jouer placé si proba modèle x rapport placé estimé > 1),
     rendement hors échantillon avec intervalle bootstrap à 90 %.

Usage : python3 analyse_disciplines.py --db pmu.sqlite --cote cote_matin
"""
import argparse
import re
import sqlite3

import numpy as np
import pandas as pd

MISE = 2.0
rng = np.random.default_rng(42)


# ---------- musique ----------
TOKEN = re.compile(r"([0-9DTARN])([a-z])")


def parse_musique(m):
    if not isinstance(m, str) or not m:
        return []
    m = re.sub(r"\(\d+\)", "", m)
    return [t[0] for t in TOKEN.findall(m)]  # plus récent en premier


def feats_musique(m):
    t = parse_musique(m)
    pos = [10 if not c.isdigit() or c == "0" else int(c) for c in t]
    l3, l5 = t[:3], t[:5]
    return {
        "inedit": int(len(t) == 0),
        "top3_der3": sum(1 for c in l3 if c in "123"),
        "disq_der5": sum(1 for c in l5 if c == "D"),
        "pos_moy_der5": float(np.mean(pos[:5])) if pos else 10.0,
        "gagne_der": int(bool(t) and t[0] == "1"),
    }


# ---------- probabilités ----------
def harville_top3(p):
    """Probabilité de finir dans les 3 premiers (approximation de Harville)."""
    n = len(p)
    out = np.array(p, dtype=float)
    for i in range(n):
        s2 = 0.0
        s3 = 0.0
        for j in range(n):
            if j == i:
                continue
            dj = 1 - p[j]
            s2 += p[j] * p[i] / dj
            for k in range(n):
                if k in (i, j):
                    continue
                djk = 1 - p[j] - p[k]
                if djk > 1e-9:
                    s3 += p[j] * p[k] / dj * p[i] / djk
        out[i] += s2 + s3
    return np.clip(out, 1e-4, 1 - 1e-4)


def logit(x):
    return np.log(x / (1 - x))


def fit_logistic(X, y, l2=1e-3, it=50):
    X = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X.shape[1])
    for _ in range(it):
        z = np.clip(X @ w, -30, 30)
        pr = 1 / (1 + np.exp(-z))
        g = X.T @ (pr - y) + l2 * w
        H = (X * (pr * (1 - pr))[:, None]).T @ X + l2 * np.eye(len(w))
        w -= np.linalg.solve(H, g)
    return w


def predict(w, X):
    X = np.column_stack([np.ones(len(X)), X])
    return 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))


def logloss(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def boot_roi(gains, mises, n=2000):
    if len(gains) == 0:
        return (np.nan, np.nan)
    idx = rng.integers(0, len(gains), (n, len(gains)))
    r = gains[idx].sum(1) / mises[idx].sum(1)
    return tuple(np.percentile(r, [5, 95]))


# ---------- chargement ----------
def charger(db, cote):
    con = sqlite3.connect(db)
    df = pd.read_sql_query(f"""
        SELECT h.course_id, c.date, c.reunion, c.course, c.discipline, c.nb_partants,
               h.num, h.{cote} AS cote, h.musique,
               COALESCE(r.dividende_pour_1e, 0) / 100.0 AS div  -- l'API donne des centimes
        FROM chevaux h JOIN courses c USING(course_id)
        LEFT JOIN rapports_places r ON r.course_id=h.course_id AND r.num=h.num
        WHERE h.statut='PARTANT' AND c.nb_partants >= 8
    """, con)
    # courses sans aucun rapport placé : exclues (choix validé)
    ok = df.groupby("course_id")["div"].transform("max") > 0
    df = df[ok]
    # courses où une cote manque : exclues
    ok = df.groupby("course_id")["cote"].transform(lambda s: s.notna().all() and (s > 1).all())
    df = df[ok].copy()
    df["place"] = (df["div"] > 0).astype(int)
    df = df.sort_values(["date", "reunion", "course", "num"])
    # proba de victoire du public (marge retirée par normalisation), puis top 3
    df["p_gagn"] = (1 / df["cote"]) / df.groupby("course_id")["cote"].transform(lambda s: (1 / s).sum())
    df["p_place_marche"] = 0.0
    for cid, g in df.groupby("course_id"):
        df.loc[g.index, "p_place_marche"] = harville_top3(g["p_gagn"].values)
    f = df["musique"].apply(feats_musique).apply(pd.Series)
    return pd.concat([df, f], axis=1)


def strategie(g, seuil=30):
    """Lignes de paris de la règle de base pour un groupe de courses."""
    rows = []
    for cid, c in g.groupby("course_id", sort=False):
        c = c.sort_values(["cote", "num"])
        fav = c.iloc[0]
        rows.append(("favori", cid, MISE, MISE * fav["div"]))
        out = c[c["cote"] >= seuil]
        if len(out):
            o = out.iloc[0]
            rows.append(("outsider", cid, MISE, MISE * o["div"]))
    return pd.DataFrame(rows, columns=["type", "course_id", "mise", "retour"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="pmu.sqlite")
    ap.add_argument("--cote", default="cote_matin", choices=["cote_matin", "cote_finale"])
    args = ap.parse_args()
    df = charger(args.db, args.cote)

    courses = df[["course_id", "date"]].drop_duplicates().sort_values("date")
    coupe = courses["course_id"].iloc[int(len(courses) * 0.7)]
    ordre = {c: i for i, c in enumerate(courses["course_id"])}
    df["test"] = df["course_id"].map(ordre) >= ordre[coupe]

    feats = ["top3_der3", "disq_der5", "pos_moy_der5", "inedit", "gagne_der"]
    lignes = []
    ecartees = []
    for disc, g in df.groupby("discipline"):
        n_c = g["course_id"].nunique()
        tr, te = g[~g["test"]], g[g["test"]]
        n_te = te["course_id"].nunique()
        if n_te < 50 or tr["course_id"].nunique() < 50:
            ecartees.append(f"{disc} ({n_c} courses)")
            continue  # trop peu de courses : non étudiée
        res = {"discipline": disc, "courses": n_c, "courses_test": n_te}

        # A. stratégie de base
        s = strategie(g)
        for t in ("favori", "outsider"):
            st = s[s["type"] == t]
            res[f"roi_{t}"] = st["retour"].sum() / st["mise"].sum() - 1 if len(st) else np.nan
            res[f"reussite_{t}"] = (st["retour"] > 0).mean() if len(st) else np.nan
        res["roi_total"] = s["retour"].sum() / s["mise"].sum() - 1

        y_te = te["place"].values
        # B. prévisibilité brute du public
        res["logloss_marche"] = logloss(y_te, te["p_place_marche"].values)
        # C1. recalibrage (corrige le biais favori/outsider)
        Xtr = logit(tr["p_place_marche"].values)[:, None]
        w1 = fit_logistic(Xtr, tr["place"].values)
        p1 = predict(w1, logit(te["p_place_marche"].values)[:, None])
        res["logloss_calibre"] = logloss(y_te, p1)
        # C2. + musique
        Xtr2 = np.column_stack([Xtr, tr[feats].values])
        w2 = fit_logistic(Xtr2, tr["place"].values)
        p2 = predict(w2, np.column_stack([logit(te["p_place_marche"].values)[:, None], te[feats].values]))
        res["logloss_musique"] = logloss(y_te, p2)
        res["gain_calibrage_%"] = 100 * (res["logloss_marche"] - res["logloss_calibre"]) / res["logloss_marche"]
        res["gain_musique_%"] = 100 * (res["logloss_calibre"] - res["logloss_musique"]) / res["logloss_calibre"]

        # D. règle valeur : rapport placé estimé à partir de la cote (appris sur train, chevaux placés)
        pl = tr[tr["place"] == 1]
        a, b = np.polyfit(np.log(pl["cote"]), np.log(pl["div"]), 1)
        div_est = np.exp(b + a * np.log(te["cote"].values))
        jouer = p2 * div_est > 1.0
        gains = (MISE * te["div"].values - MISE)[jouer]
        mises = np.full(jouer.sum(), MISE)
        res["valeur_paris"] = int(jouer.sum())
        res["valeur_roi"] = gains.sum() / mises.sum() if len(gains) else np.nan
        lo, hi = boot_roi(gains, mises)
        res["valeur_roi_ic90"] = f"[{lo:+.1%} ; {hi:+.1%}]" if len(gains) else ""
        lignes.append(res)

    if ecartees:
        print("Disciplines écartées (moins de 50 courses d'apprentissage ou de vérification) :", ", ".join(ecartees))
    if not lignes:
        print("Aucune discipline n'a assez de courses pour être étudiée.")
        return
    out = pd.DataFrame(lignes).set_index("discipline")
    pd.set_option("display.width", 250, "display.max_columns", 40)
    print(f"Cotes utilisées : {args.cote} | courses : {df['course_id'].nunique()} | "
          f"période : {df['date'].min()} -> {df['date'].max()} | test à partir de {coupe}")
    fmt = out.copy()
    for c in fmt.columns:
        if c.startswith(("roi", "reussite", "valeur_roi")) and c != "valeur_roi_ic90":
            fmt[c] = fmt[c].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "")
        elif c.startswith(("logloss", "gain")):
            fmt[c] = fmt[c].map(lambda v: f"{v:.4f}" if c.startswith("logloss") and pd.notna(v)
                                else (f"{v:+.2f}" if pd.notna(v) else ""))
    print(fmt.T.to_string())


if __name__ == "__main__":
    main()
