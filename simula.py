"""
Simulazione SUMO di uno scenario what-if e confronto con la situazione di riferimento.

Uso:
    python simula.py scenari/<id>.json
La GUI (app.py) usa il generatore simula(scenario), che emette eventi di avanzamento:
{"tipo": "fase" | "avanzamento" | "avviso" | "errore" | "fine", "t": secondi dall'inizio, ...}.

Passi:
  1. riferimento, calcolato una sola volta e tenuto in cache in sim/riferimento/: duarouter sui
     viaggi di viaggi.trips.xml, poi sumo con rerouting, seme fisso, tripinfo, statistiche, edgeData;
  2. scenario, in sim/<id>/: patch.edg.xml dagli archi risolti con risolvi.py, netconvert,
     duarouter sugli stessi viaggi (--ignore-errors), sumo con le stesse opzioni e lo stesso seme;
  3. confronto arco per arco e indicatori globali, salvati in sim/<id>/risultati.json.

Nota: anche il riferimento gira sulla rete ripassata da netconvert (senza patch), non su
base.net.xml così com'è. netconvert ricalcola corsie interne e velocità di svolta, e base.net.xml
può venire da un'altra versione di SUMO: così le due simulazioni differiscono solo per la patch.
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import sumolib
from sumolib.net.lane import SUMO_VEHICLE_CLASSES, SUMO_VEHICLE_CLASSES_DEPRECATED

from configurazione import CONFIG
from risolvi import risolvi_scenario

RETE = CONFIG.rete
VIAGGI = CONFIG.viaggi
INDICE = CONFIG.indice
CARTELLA_SIM = CONFIG.simulazioni
RIFERIMENTO = CARTELLA_SIM / "riferimento"
SEME = 42
OPZIONI_SUMO = ["--seed", str(SEME), "--device.rerouting.probability", "1",
                "--ignore-route-errors", "true", "--step-log.period", "100"]
EDGEDATA = '<additional>\n    <edgeData id="dati" file="edgedata.xml" period="3600"/>\n</additional>\n'
OUTPUT_SUMO = ("percorsi.rou.xml", "edgedata.xml", "tripinfo.xml", "statistiche.xml")

# Categorie degli scenari → vClass di SUMO. I nomi deprecati (sinonimi, es. "transport" = truck,
# "lightrail" = tram) restano fuori dai permessi: riscritti in allow riaprirebbero la corsia.
CLASSI = SUMO_VEHICLE_CLASSES - SUMO_VEHICLE_CLASSES_DEPRECATED
VIETATE = {
    "tutti": CLASSI - {"emergency", "authority"},
    "auto_private": {"passenger"},
    "moto": {"motorcycle"},
    "mezzi_pesanti": {"truck", "trailer", "delivery"},
    "bus": {"bus"},
    "taxi": {"taxi"},
    "bici": {"bicycle"},
}

# Soglie del confronto arco per arco: sotto queste variazioni l'arco è "invariato". Tarate
# ripetendo il riferimento con un altro seme: resta colorato circa il 3% degli archi (rumore).
SOGLIE = {"veicoli": 20, "veicoli_rel": 0.25,        # veicoli entrati: almeno 20 e almeno il 25%
          "vel_kmh": 5, "vel_rel": 0.25,             # velocità media: almeno 5 km/h e almeno il 25%
          "min_veicoli_vel": 20}                     # velocità confrontata solo con almeno 20 veicoli
PASSO_SUMO = re.compile(r"Step #(\d+)[.\d]*.*TOT (\d+) ACT (\d+)")


class ErroreSimulazione(Exception):
    """Errore da mostrare nella GUI, con le ultime righe del log del programma che ha fallito."""

    def __init__(self, messaggio, log=""):
        super().__init__(messaggio)
        self.log = log


# --- Esecuzione dei programmi di SUMO ---------------------------------------
def lancia(comando, cartella, nome_log):
    """Esegue un programma di SUMO in cartella e ne genera le righe di output man mano,
    copiandole in <cartella>/<nome_log>. Se fallisce solleva ErroreSimulazione con le ultime righe."""
    ultime = deque(maxlen=25)
    comando = [str(c) for c in comando]
    try:
        proc = subprocess.Popen(comando, cwd=cartella, text=True, errors="replace",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise ErroreSimulazione(f"{comando[0]} non trovato: controlla che SUMO sia nel PATH")
    with proc, open(cartella / nome_log, "w", encoding="utf-8") as log:
        for riga in proc.stdout:            # il log di sumo usa anche \r: diventa una riga a sé
            riga = riga.rstrip()
            if riga:
                log.write(riga + "\n")
                ultime.append(riga)
                yield riga
    if proc.returncode != 0:
        raise ErroreSimulazione(f"{comando[0]} non è riuscito (codice {proc.returncode}); "
                                f"log completo in {cartella / nome_log}", "\n".join(ultime))


def esegui_netconvert(cartella, uscita, patch=None):
    comando = ["netconvert", "-s", RETE.resolve(), "-o", uscita]
    for _ in lancia(comando + (["-e", patch] if patch else []), cartella, "netconvert.log"):
        pass


def calcola_percorsi(cartella, rete):
    """duarouter sui viaggi; restituisce gli id dei veicoli per cui ha trovato un percorso."""
    comando = ["duarouter", "-n", rete.resolve(), "--route-files", VIAGGI.resolve(),
               "-o", "percorsi.rou.xml", "--ignore-errors", "--no-step-log", "--seed", SEME]
    for _ in lancia(comando, cartella, "duarouter.log"):
        pass
    return veicoli_con_percorso(cartella)


def veicoli_con_percorso(cartella):
    return {el.get("id") for _, el in ET.iterparse(cartella / "percorsi.rou.xml") if el.tag == "vehicle"}


def esegui_sumo(cartella, rete, n_veicoli):
    """sumo con le opzioni comuni a riferimento e scenario; genera eventi di avanzamento."""
    (cartella / "dati.add.xml").write_text(EDGEDATA)
    comando = ["sumo", "-n", rete.resolve(), "-r", "percorsi.rou.xml", "-a", "dati.add.xml",
               "--tripinfo-output", "tripinfo.xml", "--statistic-output", "statistiche.xml",
               *OPZIONI_SUMO]
    for riga in lancia(comando, cartella, "sumo.log"):
        passo = PASSO_SUMO.search(riga)
        if passo:
            t, inseriti, attivi = map(int, passo.groups())
            arrivati = inseriti - attivi
            yield {"tipo": "avanzamento", "frazione": round(arrivati / max(n_veicoli, 1), 3),
                   "messaggio": f"tempo simulato {t} s · {arrivati}/{n_veicoli} veicoli arrivati"}


# --- Riferimento -------------------------------------------------------------
def firma_riferimento():
    """Cosa determina il riferimento: se cambia, la cache non vale più."""
    return {"rete": [RETE.stat().st_size, RETE.stat().st_mtime_ns],
            "viaggi": [VIAGGI.stat().st_size, VIAGGI.stat().st_mtime_ns],
            "opzioni": OPZIONI_SUMO, "edgedata": EDGEDATA}


def riferimento():
    firma, file_firma = firma_riferimento(), RIFERIMENTO / "firma.json"
    if (file_firma.exists() and json.loads(file_firma.read_text()) == firma
            and all((RIFERIMENTO / f).exists() for f in OUTPUT_SUMO)):
        yield fase("Riferimento già calcolato (in cache)")
        return
    RIFERIMENTO.mkdir(parents=True, exist_ok=True)
    file_firma.unlink(missing_ok=True)
    yield fase("Riferimento: rete ripassata da netconvert, senza modifiche")
    esegui_netconvert(RIFERIMENTO, "rete.net.xml")
    yield fase("Riferimento: percorsi (duarouter)")
    veicoli = calcola_percorsi(RIFERIMENTO, RIFERIMENTO / "rete.net.xml")
    yield fase("Riferimento: simulazione (sumo)")
    yield from esegui_sumo(RIFERIMENTO, RIFERIMENTO / "rete.net.xml", len(veicoli))
    file_firma.write_text(json.dumps(firma))


# --- Scenario ------------------------------------------------------------------
def controlla(scenario, risolti):
    """Controlli minimi prima di simulare: i file in scenari/ si possono anche scrivere a mano."""
    errori = []
    if not re.fullmatch(r"[\w-]+", str(scenario.get("id", ""))):
        errori.append("id mancante o non valido")
    interventi = scenario.get("interventi") or []
    if not interventi:
        errori.append("nessun intervento")
    for n, (it, ris) in enumerate(zip(interventi, risolti), 1):
        tipo = it.get("tipo")
        if tipo not in ("chiusura_via", "velocita"):
            errori.append(f"intervento {n}: tipo '{tipo}' sconosciuto")
        elif not ris["archi"]:
            errori.append(f"intervento {n}: {ris['avviso'] or 'nessun arco coinvolto'}")
        elif tipo == "velocita" and not (isinstance(it.get("kmh"), (int, float)) and 5 <= it["kmh"] <= 130):
            errori.append(f"intervento {n}: velocità mancante o fuori dall'intervallo 5-130 km/h")
        elif tipo == "chiusura_via" and set(it.get("vietato_a") or ["tutti"]) - set(VIETATE):
            ignote = sorted(set(it["vietato_a"]) - set(VIETATE))
            errori.append(f"intervento {n}: categorie sconosciute {ignote}")
    return errori


def scrivi_patch(net, scenario, risolti, percorso):
    """Scrive la patch degli archi per netconvert. Restituisce, per gli archi con chiusure,
    le classi ammesse dopo la patch su ciascuna corsia."""
    vietate, kmh = defaultdict(set), {}
    for it, ris in zip(scenario["interventi"], risolti):
        for arco in ris["archi"]:
            if it["tipo"] == "chiusura_via":
                for categoria in it.get("vietato_a") or ["tutti"]:
                    vietate[arco] |= VIETATE[categoria]
            else:
                kmh[arco] = it["kmh"]
    radice, permessi = ET.Element("edges"), {}
    for arco in sorted(vietate.keys() | kmh.keys()):
        el = ET.SubElement(radice, "edge", id=arco)
        if arco in kmh:
            el.set("speed", f"{kmh[arco] / 3.6:.2f}")
        if arco in vietate:
            permessi[arco] = []
            for corsia in net.getEdge(arco).getLanes():
                # si parte dai permessi attuali: le corsie riservate (es. bus) restano come sono
                ammesse = corsia.getPermissions() - SUMO_VEHICLE_CLASSES_DEPRECATED - vietate[arco]
                perm = {"allow": " ".join(sorted(ammesse))} if ammesse else {"disallow": "all"}
                ET.SubElement(el, "lane", index=str(corsia.getIndex()), **perm)
                permessi[arco].append(ammesse)
    ET.indent(radice)
    ET.ElementTree(radice).write(percorso, encoding="UTF-8", xml_declaration=True)
    return permessi


def conta_scartati(mancanti, permessi):
    """Divide i viaggi scartati tra chi parte o arriva su un arco chiuso e chi resta senza percorso."""
    classi, viaggi = {}, {}
    for _, el in ET.iterparse(VIAGGI):
        if el.tag == "vType":
            classi[el.get("id")] = el.get("vClass", "passenger")
        elif el.tag == "trip":
            viaggi[el.get("id")] = (el.get("from"), el.get("to"), el.get("type"))
    su_chiusi = 0
    for v in mancanti:
        da, a, tipo = viaggi.get(v, (None, None, None))
        classe = classi.get(tipo, "passenger")
        if any(arco in permessi and not any(classe in p for p in permessi[arco]) for arco in (da, a)):
            su_chiusi += 1
    return {"totale": len(mancanti), "archi_chiusi": su_chiusi, "senza_percorso": len(mancanti) - su_chiusi}


# --- Confronto -----------------------------------------------------------------
def leggi_archi(cartella):
    """edgeData sommati su tutti gli intervalli: {arco: {"veicoli", "vel_kmh", "perso_s"}}."""
    somme = defaultdict(lambda: [0, 0.0, 0.0, 0.0])   # entrati, s campionati, velocità × s, tempo perso
    for _, el in ET.iterparse(cartella / "edgedata.xml"):
        if el.tag == "edge":
            s, secondi = somme[el.get("id")], float(el.get("sampledSeconds", 0))
            s[0] += int(float(el.get("entered", 0)))
            s[1] += secondi
            s[2] += float(el.get("speed", 0)) * secondi
            s[3] += float(el.get("timeLoss", 0))
    return {a: {"veicoli": e, "vel_kmh": round(vs / sec * 3.6, 1) if sec else None, "perso_s": round(p, 1)}
            for a, (e, sec, vs, p) in somme.items()}


def leggi_globali(cartella):
    durate, persi = [], []
    for _, el in ET.iterparse(cartella / "tripinfo.xml"):
        if el.tag == "tripinfo" and not el.get("vaporized"):
            durate.append(float(el.get("duration")))
            persi.append(float(el.get("timeLoss")))
    teletrasporti = ET.parse(cartella / "statistiche.xml").getroot().find("teleports")
    media = lambda valori: round(sum(valori) / len(valori), 1) if valori else None
    return {"viaggi_completati": len(durate), "durata_media_s": media(durate),
            "tempo_perso_medio_s": media(persi),
            "teletrasporti": int(teletrasporti.get("total")) if teletrasporti is not None else None}


def variazione_pct(prima, dopo):
    return round((dopo - prima) / prima * 100, 1) if prima and dopo is not None else None


def classifica(p, d):
    """(livello, motivo) di un arco: +1/+2 peggiora (più traffico o più lento), -1/-2 migliora
    (meno traffico o più veloce), 0 invariato. Il peggioramento prevale."""
    dv = d["veicoli"] - p["veicoli"]
    soglia = max(SOGLIE["veicoli"], SOGLIE["veicoli_rel"] * p["veicoli"])
    traffico = 0 if abs(dv) < soglia else (1 if abs(dv) < 3 * soglia else 2) * (1 if dv > 0 else -1)
    lento = 0
    if (p["vel_kmh"] is not None and d["vel_kmh"] is not None
            and min(p["veicoli"], d["veicoli"]) >= SOGLIE["min_veicoli_vel"]):
        dvel = d["vel_kmh"] - p["vel_kmh"]
        soglia = max(SOGLIE["vel_kmh"], SOGLIE["vel_rel"] * p["vel_kmh"])
        lento = 0 if abs(dvel) < soglia else (1 if abs(dvel) < 2 * soglia else 2) * (1 if dvel < 0 else -1)
    if traffico > 0 or lento > 0:
        motivi = [m for m, c in (("più traffico", traffico > 0), ("più lento", lento > 0)) if c]
        return max(traffico, lento), " e ".join(motivi)
    if traffico < 0 or lento < 0:
        motivi = [m for m, c in (("meno traffico", traffico < 0), ("più veloce", lento < 0)) if c]
        return min(traffico, lento), " e ".join(motivi)
    return 0, "invariato"


def confronta(db, cartella, scenario, avvisi, scartati):
    rif, scen = leggi_archi(RIFERIMENTO), leggi_archi(cartella)
    vuoto = {"veicoli": 0, "vel_kmh": None, "perso_s": 0.0}
    archi = {}
    for a in rif.keys() | scen.keys():
        p, d = rif.get(a, vuoto), scen.get(a, vuoto)
        livello, motivo = classifica(p, d)
        archi[a] = {"veicoli": [p["veicoli"], d["veicoli"]], "vel_kmh": [p["vel_kmh"], d["vel_kmh"]],
                    "perso_s": [p["perso_s"], d["perso_s"]], "livello": livello, "motivo": motivo}

    # Per le vie: veicoli·km (veicoli entrati × lunghezza degli archi) e tempo perso, prima e dopo
    per_via = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for a, nome, lung in db.execute("SELECT id, nome, lunghezza_m FROM archi WHERE nome IS NOT NULL"):
        if a in archi:
            v = per_via[nome]
            v[0] += archi[a]["veicoli"][0] * lung / 1000
            v[1] += archi[a]["veicoli"][1] * lung / 1000
            v[2] += archi[a]["perso_s"][0] / 60
            v[3] += archi[a]["perso_s"][1] / 60
    vie = sorted(({"via": nome, "veicoli_km": [round(v[0], 1), round(v[1], 1)],
                   "variazione_pct": variazione_pct(v[0], v[1]),
                   "perso_min": [round(v[2], 1), round(v[3], 1)]} for nome, v in per_via.items()),
                 key=lambda v: -abs(v["veicoli_km"][1] - v["veicoli_km"][0]))[:10]

    g_rif, g_scen = leggi_globali(RIFERIMENTO), leggi_globali(cartella)
    globali = {k: {"prima": g_rif[k], "dopo": g_scen[k], "variazione_pct": variazione_pct(g_rif[k], g_scen[k])}
               for k in g_rif}
    return {"scenario": scenario, "calcolato": datetime.now().isoformat(timespec="seconds"),
            "avvisi": avvisi, "scartati": scartati, "globali": globali, "soglie": SOGLIE,
            "vie": vie, "archi": archi}


# --- Generatore principale -------------------------------------------------------
def fase(messaggio):
    return {"tipo": "fase", "messaggio": messaggio}


def simula(scenario, indice=INDICE):
    """Simula lo scenario ed emette gli eventi di avanzamento; l'ultimo è "fine" o "errore"."""
    inizio = time.monotonic()
    for ev in _simula(scenario, indice):
        yield {**ev, "t": round(time.monotonic() - inizio, 1)}


def _simula(scenario, indice):
    avvisi = []

    def avviso(messaggio):
        avvisi.append(messaggio)
        return {"tipo": "avviso", "messaggio": messaggio}

    try:
        for file in (RETE, VIAGGI):
            if not file.exists():
                raise ErroreSimulazione(f"manca {file}: vedi il README per generarlo")
        db = sqlite3.connect(indice)
        risolti = risolvi_scenario(db, scenario)
        errori = controlla(scenario, risolti)
        if errori:
            raise ErroreSimulazione("scenario non valido: " + "; ".join(errori))
        for ris in risolti:
            if ris["avviso"]:
                yield avviso(ris["avviso"])
        if scenario.get("fascia_oraria"):
            yield avviso(f"fascia oraria {'–'.join(scenario['fascia_oraria'])} ignorata in questa versione: "
                         "l'intervento vale per tutta la simulazione")

        yield from riferimento()

        cartella = CARTELLA_SIM / scenario["id"]
        cartella.mkdir(parents=True, exist_ok=True)
        (cartella / "risultati.json").unlink(missing_ok=True)
        yield fase("Scenario: patch della rete")
        permessi = scrivi_patch(sumolib.net.readNet(str(RETE)), scenario, risolti, cartella / "patch.edg.xml")
        yield fase("Scenario: rete con la patch (netconvert)")
        esegui_netconvert(cartella, "scenario.net.xml", patch="patch.edg.xml")
        yield fase("Scenario: percorsi sugli stessi viaggi (duarouter)")
        veicoli = calcola_percorsi(cartella, cartella / "scenario.net.xml")
        scartati = conta_scartati(veicoli_con_percorso(RIFERIMENTO) - veicoli, permessi)
        if scartati["totale"]:
            yield avviso(f"{scartati['totale']} viaggi scartati: {scartati['archi_chiusi']} partono o arrivano "
                         f"su archi chiusi, {scartati['senza_percorso']} restano senza percorso")
        yield fase("Scenario: simulazione (sumo)")
        yield from esegui_sumo(cartella, cartella / "scenario.net.xml", len(veicoli))

        yield fase("Confronto con il riferimento")
        risultati = confronta(db, cartella, scenario, avvisi, scartati)
        (cartella / "risultati.json").write_text(json.dumps(risultati, ensure_ascii=False), encoding="utf-8")
        yield {"tipo": "fine", "id": scenario["id"], "globali": risultati["globali"]}
    except ErroreSimulazione as e:
        yield {"tipo": "errore", "messaggio": str(e), "log": e.log}
    except Exception as e:
        yield {"tipo": "errore", "messaggio": f"errore imprevisto: {type(e).__name__}: {e}",
               "log": traceback.format_exc(limit=4)}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    a_capo = False
    for ev in simula(scenario):
        if ev["tipo"] == "avanzamento":
            print(f"\r   {ev['messaggio']}", end="", flush=True)
            a_capo = True
            continue
        if a_capo:
            print()
            a_capo = False
        if ev["tipo"] == "fase":
            print(f"[{ev['t']:6.1f} s] {ev['messaggio']}")
        elif ev["tipo"] == "avviso":
            print(f"   avviso: {ev['messaggio']}")
        elif ev["tipo"] == "errore":
            sys.exit(f"ERRORE: {ev['messaggio']}\n{ev.get('log', '')}")
        elif ev["tipo"] == "fine":
            print(f"\nRisultati in {CARTELLA_SIM / ev['id'] / 'risultati.json'}")
            for nome, g in ev["globali"].items():
                pct = "" if g["variazione_pct"] is None else f"  ({g['variazione_pct']:+.1f}%)"
                print(f"   {nome:<22} {g['prima']!s:>8} → {g['dopo']!s:<8}{pct}")


if __name__ == "__main__":
    main()
