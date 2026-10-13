"""
Risoluzione degli interventi di uno scenario negli archi della rete.

Per ogni intervento trova gli id degli archi coinvolti:
  - via intera: tutti gli archi della via;
  - tratto "tra X e Y": sul grafo orientato formato dagli archi della via, il cammino più breve
    (per lunghezza) da un incrocio con X a uno con Y, più quello da Y a X, così da coprire
    entrambe le direzioni. Se non esiste alcun cammino si usa la via intera, con un avviso.
Lo usano la GUI (sovrapposizione sulla mappa) e simula.py (patch della rete).

Prova:
    python risolvi.py scenari/<id>.json
    python risolvi.py /percorso/indice.sqlite scenari/<id>.json
"""
import heapq
import json
import sqlite3
import sys
from collections import defaultdict

from indice_rete import incroci


def archi_via(db, via):
    riga = db.execute("SELECT archi FROM vie WHERE nome = ?", (via,)).fetchone()
    return json.loads(riga[0]) if riga else []


def grafo_via(db, via):
    """Grafo orientato degli archi della via: {nodo: [(arco, nodo_arrivo, lunghezza_m)]}."""
    grafo = defaultdict(list)
    for arco, da, a, lung in db.execute(
            "SELECT id, da_nodo, a_nodo, lunghezza_m FROM archi WHERE nome = ?", (via,)):
        grafo[da].append((arco, a, lung))
    return grafo


def cammino_minimo(grafo, sorgenti, destinazioni):
    """Archi del cammino più breve da una sorgente a una destinazione diversa dalla sorgente
    (Dijkstra da ciascuna sorgente), oppure None se non esiste."""
    migliore = None                                   # (lunghezza, archi)
    for s in sorgenti:
        dist, prec, coda = {s: 0.0}, {}, [(0.0, s)]
        while coda:
            d, n = heapq.heappop(coda)
            if d > dist[n]:
                continue
            if n != s and n in destinazioni:          # la prima destinazione estratta è la più vicina
                if migliore is None or d < migliore[0]:
                    archi = []
                    while n != s:
                        n, arco = prec[n]
                        archi.append(arco)
                    migliore = (d, archi[::-1])
                break
            for arco, m, lung in grafo.get(n, ()):
                if d + lung < dist.get(m, float("inf")):
                    dist[m], prec[m] = d + lung, (n, arco)
                    heapq.heappush(coda, (d + lung, m))
    return migliore[1] if migliore else None


def risolvi_intervento(db, intervento):
    """{"archi": [id, ...], "avviso": testo o None} per un singolo intervento."""
    via = intervento.get("via", "")
    tutti = archi_via(db, via)
    if not tutti:
        return {"archi": [], "avviso": f"'{via}' non è nell'indice"}
    tra = intervento.get("tra")
    if not tra:
        return {"archi": tutti, "avviso": None}
    if len(tra) != 2:
        return {"archi": tutti, "avviso": f"tratto di {via} non valido ({tra}): uso la via intera"}
    x, y = tra
    grafo = grafo_via(db, via)
    nodi_x, nodi_y = set(incroci(db, via, x)), set(incroci(db, via, y))
    andata = cammino_minimo(grafo, nodi_x, nodi_y)
    ritorno = cammino_minimo(grafo, nodi_y, nodi_x)
    if not andata and not ritorno:
        return {"archi": tutti,
                "avviso": f"nessun percorso lungo {via} tra {x} e {y}: uso la via intera"}
    return {"archi": list(dict.fromkeys((andata or []) + (ritorno or []))), "avviso": None}


def risolvi_scenario(db, scenario):
    """Per ogni intervento dello scenario, nello stesso ordine: {"archi": [...], "avviso": ...}."""
    return [risolvi_intervento(db, it) for it in scenario.get("interventi") or []]


# --- Sovrapposizione per la mappa -----------------------------------------
def numero(v):
    return f"{v:g}" if isinstance(v, (int, float)) else "?"


def sovrapposizione(db, scenario):
    """Un elemento per intervento, con gli archi risolti e l'etichetta da mostrare sulla mappa."""
    elementi = []
    for it, ris in zip(scenario.get("interventi") or [], risolvi_scenario(db, scenario)):
        if it.get("tipo") == "velocita":
            segnaposto = ",".join("?" * len(ris["archi"]))
            prima = sorted({r[0] for r in db.execute(
                f"SELECT kmh FROM archi WHERE id IN ({segnaposto})", ris["archi"])})
            etichetta = f"{'/'.join(numero(k) for k in prima)} → {numero(it.get('kmh'))} km/h"
        else:
            etichetta = "chiusa a: " + ", ".join(it.get("vietato_a") or ["tutti"])
        elementi.append({"tipo": it.get("tipo"), "via": it.get("via"), "tra": it.get("tra"),
                         "etichetta": etichetta, "archi": ris["archi"], "avviso": ris["avviso"]})
    return elementi


if __name__ == "__main__":
    from configurazione import CONFIG

    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    indice = sys.argv[1] if len(sys.argv) == 3 else CONFIG.indice
    with open(sys.argv[-1], encoding="utf-8") as f:
        scen = json.load(f)
    for el in sovrapposizione(sqlite3.connect(indice), scen):
        print(f"{el['via']} {el['tra'] or ''} [{el['etichetta']}]: {len(el['archi'])} archi"
              + (f"  ⚠ {el['avviso']}" if el["avviso"] else ""))
        print("   ", " ".join(el["archi"]))
