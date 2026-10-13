"""
Indice semantico della rete SUMO per l'agente what-if.

Costruzione (una volta, dopo netconvert):
    python indice_rete.py costruisci
    python indice_rete.py costruisci /percorso/rete.net.xml /percorso/indice.sqlite
Prova della ricerca vie:
    python indice_rete.py cerca indice.sqlite corso vittorio
Incroci tra due vie (per i tratti "tra X e Y"):
    python indice_rete.py incroci indice.sqlite "Via Nizza" "Corso Dante"

Richiede: pip install sumolib
La rete deve essere generata con netconvert --output.street-names.
"""

import difflib
import json
import re
import sqlite3
import sys
import unicodedata
import html
from collections import defaultdict

PREFISSI = {
    "via",
    "corso",
    "piazza",
    "piazzale",
    "piazzetta",
    "largo",
    "lungo",
    "viale",
    "vicolo",
    "strada",
    "galleria",
    "ponte",
    "passaggio",
    "rotonda",
    "sottopasso",
    "cavalcavia",
    "borgata",
    "salita",
    "traversa",
}


# --- Normalizzazione dei nomi ----------------------------------------------
def normalizza(testo):
    """Minuscolo, senza accenti né punteggiatura: "Corso Massimo d'Azeglio" -> "corso massimo d azeglio"."""
    t = unicodedata.normalize("NFKD", testo or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def separa_prefisso(norm):
    """ "corso dante" -> ("corso", "dante"); "dante" -> ("", "dante")"""
    parti = norm.split(" ", 1)
    if len(parti) == 2 and parti[0] in PREFISSI:
        return parti[0], parti[1]
    return "", norm


# --- Costruzione dell'indice -----------------------------------------------
def costruisci(percorso_rete, percorso_db):
    import sumolib

    net = sumolib.net.readNet(percorso_rete)
    # Verifica la proiezione prima di modificare l'indice esistente.
    net.getGeoProj()
    db = sqlite3.connect(percorso_db)
    db.executescript("""
        DROP TABLE IF EXISTS archi; DROP TABLE IF EXISTS vie; DROP TABLE IF EXISTS nodi_vie;
        DROP TABLE IF EXISTS nodi;
        CREATE TABLE archi (id TEXT PRIMARY KEY, nome TEXT, da_nodo TEXT, a_nodo TEXT,
                            kmh REAL, corsie INTEGER, lunghezza_m REAL, tipo TEXT,
                            auto INTEGER, geometria TEXT);
        CREATE TABLE vie (nome TEXT PRIMARY KEY, norm TEXT, prefisso TEXT, nucleo TEXT,
                          n_archi INTEGER, lunghezza_m REAL, archi TEXT);
        CREATE TABLE nodi_vie (nodo TEXT, via TEXT, PRIMARY KEY (nodo, via));
        CREATE TABLE nodi (id TEXT PRIMARY KEY, lon REAL, lat REAL);
    """)

    # Coordinate dei nodi: servono alla GUI per i marcatori degli incroci
    db.executemany(
        "INSERT INTO nodi VALUES (?,?,?)",
        [
            (n.getID(), *(round(v, 6) for v in net.convertXY2LonLat(*n.getCoord()[:2])))
            for n in net.getNodes()
        ],
    )

    per_via = defaultdict(list)
    righe = []
    for e in net.getEdges():
        if e.getFunction() in ("internal", "crossing", "walkingarea"):
            continue
        nome = html.unescape((e.getName() or "").strip())
        geom = [
            [round(v, 6) for v in net.convertXY2LonLat(x, y)] for x, y in e.getShape()
        ]
        righe.append(
            (
                e.getID(),
                nome or None,
                e.getFromNode().getID(),
                e.getToNode().getID(),
                round(e.getSpeed() * 3.6, 1),
                e.getLaneNumber(),
                round(e.getLength(), 1),
                e.getType(),
                int(e.allows("passenger")),
                json.dumps(geom),
            )
        )
        if nome:
            per_via[nome].append(e)
    db.executemany("INSERT INTO archi VALUES (?,?,?,?,?,?,?,?,?,?)", righe)

    for nome, archi in per_via.items():
        norm = normalizza(nome)
        prefisso, nucleo = separa_prefisso(norm)
        db.execute(
            "INSERT INTO vie VALUES (?,?,?,?,?,?,?)",
            (
                nome,
                norm,
                prefisso,
                nucleo,
                len(archi),
                round(sum(a.getLength() for a in archi), 1),
                json.dumps([a.getID() for a in archi]),
            ),
        )
        nodi = {a.getFromNode().getID() for a in archi} | {
            a.getToNode().getID() for a in archi
        }
        db.executemany(
            "INSERT OR IGNORE INTO nodi_vie VALUES (?,?)", [(n, nome) for n in nodi]
        )

    db.commit()
    riepilogo(db)


def riepilogo(db):
    (n_archi,) = db.execute("SELECT COUNT(*) FROM archi").fetchone()
    (senza_nome,) = db.execute(
        "SELECT COUNT(*) FROM archi WHERE nome IS NULL"
    ).fetchone()
    (n_vie,) = db.execute("SELECT COUNT(*) FROM vie").fetchone()
    (n_incroci,) = db.execute(
        "SELECT COUNT(*) FROM (SELECT nodo FROM nodi_vie "
        "GROUP BY nodo HAVING COUNT(*) > 1)"
    ).fetchone()
    print(f"Archi:                    {n_archi}  ({senza_nome} senza nome)")
    print(f"Vie con nome:             {n_vie}")
    print(f"Incroci tra vie con nome: {n_incroci}")
    (n_nodi,) = db.execute("SELECT COUNT(*) FROM nodi").fetchone()
    print(f"Nodi con coordinate:      {n_nodi}")
    print("\nLe 15 vie più lunghe (lunghezza sommata sulle due direzioni):")
    for nome, n, lung in db.execute(
        "SELECT nome, n_archi, lunghezza_m FROM vie ORDER BY lunghezza_m DESC LIMIT 15"
    ):
        print(f"  {nome:<40} {n:>4} archi {lung:>8.0f} m")


# --- Ricerca (sarà il tool cerca_via dell'agente) ---------------------------
def cerca_via(db, testo, max_suggerimenti=5):
    """Distingue corrispondenze esatte da semplici suggerimenti.
    Il confronto approssimato avviene sul nucleo del nome (senza via/corso/piazza),
    così "via roma" non viene più scambiata per "via ormea"."""
    q = normalizza(testo)
    q_pref, q_nucleo = separa_prefisso(q)
    esatte, suggerimenti = [], []
    for nome, norm, _pref, nucleo in db.execute(
        "SELECT nome, norm, prefisso, nucleo FROM vie"
    ):
        if norm == q or (not q_pref and nucleo == q_nucleo):
            esatte.append(nome)
            continue
        if nucleo == q_nucleo:  # stesso nome, prefisso diverso
            punteggio = 1.0
        else:
            punteggio = difflib.SequenceMatcher(None, q_nucleo, nucleo).ratio()
            if (
                q_nucleo and f" {q_nucleo} " in f" {nucleo} "
            ):  # "vittorio" in "vittorio emanuele ii"
                punteggio = max(punteggio, 0.9)
        if punteggio >= 0.8:
            suggerimenti.append((punteggio, nome))
    suggerimenti.sort(key=lambda s: -s[0])
    return {
        "esatte": esatte,
        "suggerimenti": [n for _, n in suggerimenti[:max_suggerimenti]],
    }


def incroci(db, via_a, via_b):
    """Nodi in cui due vie si incontrano: servono per risolvere i tratti "tra X e Y"."""
    return [
        r[0]
        for r in db.execute(
            "SELECT a.nodo FROM nodi_vie a JOIN nodi_vie b ON a.nodo = b.nodo "
            "WHERE a.via = ? AND b.via = ?",
            (via_a, via_b),
        )
    ]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["costruisci"]:
        from configurazione import CONFIG

        CONFIG.indice.parent.mkdir(parents=True, exist_ok=True)
        costruisci(str(CONFIG.rete), str(CONFIG.indice))
    elif len(args) == 3 and args[0] == "costruisci":
        costruisci(args[1], args[2])
    elif len(args) >= 3 and args[0] == "cerca":
        risultato = cerca_via(sqlite3.connect(args[1]), " ".join(args[2:]))
        print(json.dumps(risultato, ensure_ascii=False, indent=2))
    elif len(args) == 4 and args[0] == "incroci":
        print(incroci(sqlite3.connect(args[1]), args[2], args[3]))
    else:
        print(__doc__)
