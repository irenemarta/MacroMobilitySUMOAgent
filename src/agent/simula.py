"""
Simulazione di uno scenario what-if con dtSumo e confronto con la baseline.

Uso:
    uv run python -m agent.simula <scenario>.json [veloce|completa]
La GUI (app.py) usa il generatore simula(scenario), che emette eventi di avanzamento:
{"tipo": "fase" | "avanzamento" | "avviso" | "errore" | "fine", "t": secondi dall'inizio, ...}.

Il calcolo avviene tutto in dtSumo, sulla stessa macchina (vedi motore.py), con i round della modalità
scelta (MODALITA); scenario e baseline usano sempre gli stessi round:
  1. verifica dello scenario (run_scenario con check_only=True);
  2. baseline: se dtSumo non l'ha ancora simulata, la lancia (una volta sola, poi resta);
  3. scenario: run.py in background (assegnazione iterativa, estensione O'/D', simulazione
     finale), avanzamento letto dal log ogni ATTESA secondi;
  4. confronto con la baseline (compare.py) e classificazione degli archi,
     salvati in sim/<id>/risultati.json.
"""

import json
import sqlite3
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from agent.configurazione import CONFIG
from agent.motore import (
    CARTELLA,
    ErroreDtsumo,
    avvia,
    baseline_pronta,
    confronto,
    invia,
    scenari_simulati,
    stato,
    verifica,
)

INDICE = CONFIG.indice
CARTELLA_SIM = CONFIG.simulazioni
MODALITA = {
    "veloce": 3,
    "completa": 6,
}  # round di assegnazione in dtSumo (completa = N_ROUNDS di run.py)
MODALITA_PREDEFINITA = "veloce"
ATTESA = 60  # secondi tra un controllo del log e il successivo
BASELINE = {"id": "baseline", "interventions": []}
MAX_PARALLELE = (
    4  # simulazioni insieme in dtSumo (GUI e agente): ognuna usa un core e qualche GB
)
IN_CORSO = set()  # id delle simulazioni in corso
BLOCCO = threading.Lock()
BLOCCO_BASELINE = (
    threading.Lock()
)  # la baseline si lancia una volta sola anche con più richieste

# Soglie del confronto arco per arco: sotto queste variazioni l'arco è "invariato".
# Veicoli: dalla baseline dtSumo con 5 seed (seed_noise.py), sugli archi con almeno 200 veicoli
# al giorno il 95% delle differenze tra seed resta sotto il 15-21%.
SOGLIE = {
    "veicoli": 100,
    "veicoli_rel": 0.25,  # veicoli entrati: almeno 100 e almeno il 25%
    "vel_kmh": 5,
    "vel_rel": 0.25,  # velocità media: almeno 5 km/h e almeno il 25%
    "min_veicoli_vel": 20,
}  # velocità confrontata solo con almeno 20 veicoli


class ErroreSimulazione(Exception):
    """Errore da mostrare nella GUI, con le ultime righe del log del programma che ha fallito."""

    def __init__(self, messaggio, log=""):
        super().__init__(messaggio)
        self.log = log


# --- Esecuzione in dtSumo --------------------------------------------------------
def attendi(ident, n_round):
    """Aspetta la fine della simulazione ident in dtSumo; genera eventi di avanzamento."""
    ultimo, finale = None, False
    while True:
        s = stato(ident, n_round)
        if s["uscita"] is not None:
            if s["uscita"] != 0:
                raise ErroreSimulazione(
                    f"dtSumo si è fermato con codice {s['uscita']} "
                    f"(log in {CARTELLA / 'logs' / f'{ident}_r{n_round}.log'})",
                    s["log"],
                )
            return
        if s["round"] is not None and s["round"] != ultimo:
            ultimo = s["round"]
            yield {
                "tipo": "avanzamento",
                "frazione": round(ultimo / n_round, 3),
                "messaggio": f"round {ultimo + 1}/{n_round} (marouter + sumo meso su 24 ore)",
            }
        if s["finale"] and not finale:
            finale = True
            yield fase("Estensione O'/D' fatta, simulazione finale")
        time.sleep(ATTESA)


def baseline(n_round):
    with (
        BLOCCO_BASELINE
    ):  # se un'altra simulazione la sta già calcolando, qui si aspetta
        if baseline_pronta(BASELINE["id"], n_round):
            yield fase(f"Baseline a {n_round} round già simulata in dtSumo")
            return
        yield fase(
            f"Baseline: assegnazione e simulazione dtSumo ({n_round} round, si fa una volta sola)"
        )
        invia(BASELINE, n_round)
        avvia(BASELINE["id"], n_round)
        yield from attendi(BASELINE["id"], n_round)


def avvisi_verifica(controllo):
    """Avvisi dal resoconto di dtSumo: corsie lasciate aperte e archi non modificati."""
    avvisi = []
    for n, rep in enumerate(controllo["interventions"], 1):
        if "factor" in rep:
            continue
        if rep.get("lanes_kept_for_turns"):
            avvisi.append(
                f"intervento {n}: {rep['lanes_kept_for_turns']} corsie lasciate aperte "
                "perché sono le uniche da cui si svolta"
            )
        if rep.get("edges_unchanged"):
            avvisi.append(
                f"intervento {n}: {rep['edges_unchanged']} archi restano invariati "
                "(a una corsia, la chiusura parziale ne lascia sempre una)"
            )
    return avvisi


# --- Confronto -----------------------------------------------------------------
def variazione_pct(prima, dopo):
    return (
        round((dopo - prima) / prima * 100, 1) if prima and dopo is not None else None
    )


def classifica(p, d):
    """(livello, motivo) di un arco: +1/+2 peggiora (più traffico o più lento), -1/-2 migliora
    (meno traffico o più veloce), 0 invariato. Il peggioramento prevale."""
    dv = d["veicoli"] - p["veicoli"]
    soglia = max(SOGLIE["veicoli"], SOGLIE["veicoli_rel"] * p["veicoli"])
    traffico = (
        0
        if abs(dv) < soglia
        else (1 if abs(dv) < 3 * soglia else 2) * (1 if dv > 0 else -1)
    )
    lento = 0
    if (
        p["vel_kmh"] is not None
        and d["vel_kmh"] is not None
        and min(p["veicoli"], d["veicoli"]) >= SOGLIE["min_veicoli_vel"]
    ):
        dvel = d["vel_kmh"] - p["vel_kmh"]
        soglia = max(SOGLIE["vel_kmh"], SOGLIE["vel_rel"] * p["vel_kmh"])
        lento = (
            0
            if abs(dvel) < soglia
            else (1 if abs(dvel) < 2 * soglia else 2) * (1 if dvel < 0 else -1)
        )
    if traffico > 0 or lento > 0:
        motivi = [
            m
            for m, c in (("più traffico", traffico > 0), ("più lento", lento > 0))
            if c
        ]
        return max(traffico, lento), " e ".join(motivi)
    if traffico < 0 or lento < 0:
        motivi = [
            m
            for m, c in (("meno traffico", traffico < 0), ("più veloce", lento < 0))
            if c
        ]
        return min(traffico, lento), " e ".join(motivi)
    return 0, "invariato"


def confronta(db, dati, scenario, avvisi):
    """dati: il JSON di compare.py ({arco: {"baseline": [veicoli, km/h, perso s], "scenario": [...]}})."""
    archi = {}
    for a, valori in dati["edges"].items():
        p, d = (
            {"veicoli": v[0], "vel_kmh": v[1], "perso_s": v[2]}
            for v in (valori["baseline"], valori["scenario"])
        )
        livello, motivo = classifica(p, d)
        archi[a] = {
            "veicoli": [p["veicoli"], d["veicoli"]],
            "vel_kmh": [p["vel_kmh"], d["vel_kmh"]],
            "perso_s": [p["perso_s"], d["perso_s"]],
            "livello": livello,
            "motivo": motivo,
        }

    # Per le vie: veicoli·km (veicoli entrati × lunghezza degli archi) e tempo perso, prima e dopo
    per_via = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for a, nome, lung in db.execute(
        "SELECT id, nome, lunghezza_m FROM archi WHERE nome IS NOT NULL"
    ):
        if a in archi:
            v = per_via[nome]
            v[0] += archi[a]["veicoli"][0] * lung / 1000
            v[1] += archi[a]["veicoli"][1] * lung / 1000
            v[2] += archi[a]["perso_s"][0] / 60
            v[3] += archi[a]["perso_s"][1] / 60
    vie = sorted(
        (
            {
                "via": nome,
                "veicoli_km": [round(v[0], 1), round(v[1], 1)],
                "variazione_pct": variazione_pct(v[0], v[1]),
                "perso_min": [round(v[2], 1), round(v[3], 1)],
            }
            for nome, v in per_via.items()
        ),
        key=lambda v: -abs(v["veicoli_km"][1] - v["veicoli_km"][0]),
    )[:10]

    nomi = {
        "viaggi_completati": "trips",
        "durata_media_s": "mean_duration_s",
        "tempo_perso_medio_s": "mean_time_loss_s",
        "teletrasporti": "teleports",
    }
    g_rif, g_scen = dati["globals"]["baseline"], dati["globals"]["scenario"]
    globali = {
        k: {
            "prima": g_rif[c],
            "dopo": g_scen[c],
            "variazione_pct": variazione_pct(g_rif[c], g_scen[c]),
        }
        for k, c in nomi.items()
    }
    return {
        "scenario": scenario,
        "calcolato": datetime.now().isoformat(timespec="seconds"),
        "avvisi": avvisi,
        "scartati": None,
        "globali": globali,
        "soglie": SOGLIE,
        "corsie_chiuse": dati["lanes_closed"],
        "vie": vie,
        "archi": archi,
    }


def scenari_pronti():
    """Scenari simulati in dtSumo con una baseline degli stessi round e dello stesso seme, quindi
    confrontabili: [{"id": ..., "modalita": ...}]."""
    simulati = scenari_simulati()
    baseline = {(r, seme) for ident, r, seme in simulati if ident == BASELINE["id"]}
    nomi = {r: m for m, r in MODALITA.items()}
    return sorted(
        (
            {"id": ident, "modalita": nomi.get(r, f"{r} round")}
            for ident, r, seme in simulati
            if (r, seme) in baseline and ident != BASELINE["id"]
        ),
        key=lambda s: s["id"],
    )


def importa(ident, n_round, indice=INDICE, dati=None):
    """Risultati di uno scenario simulato in dtSumo ma non seguito dalla GUI (per esempio dopo un
    riavvio), salvati come se l'avesse appena simulato. Restituisce (scenario, controllo).
    """
    if dati is None:
        dati = confronto(ident, BASELINE["id"], n_round)
    scenario = dati["scenario"]
    controllo = {"ok": True, "interventions": dati["interventions"]}
    risultati = confronta(
        sqlite3.connect(indice), dati, scenario, avvisi_verifica(controllo)
    )
    cartella = CARTELLA_SIM / ident
    cartella.mkdir(parents=True, exist_ok=True)
    (cartella / "risultati.json").write_text(
        json.dumps(risultati, ensure_ascii=False), encoding="utf-8"
    )
    return scenario, controllo


# --- Generatore principale -------------------------------------------------------
def posto_libero(ident):
    """None se lo scenario si può simulare adesso, altrimenti il motivo."""
    if ident in IN_CORSO:
        return f"la simulazione di {ident} è già in corso"
    if len(IN_CORSO) >= MAX_PARALLELE:
        return f"già {len(IN_CORSO)} simulazioni in corso ({', '.join(sorted(IN_CORSO))}): riprova quando una finisce"
    return None


def fase(messaggio):
    return {"tipo": "fase", "messaggio": messaggio}


def simula(scenario, indice=INDICE, modalita=MODALITA_PREDEFINITA):
    """Simula lo scenario ed emette gli eventi di avanzamento; l'ultimo è "fine" o "errore"."""
    inizio = time.monotonic()
    for ev in _simula(scenario, indice, modalita):
        yield {**ev, "t": round(time.monotonic() - inizio, 1)}


def _simula(scenario, indice, modalita):
    with BLOCCO:
        errore = posto_libero(scenario["id"])
        if not errore:
            IN_CORSO.add(scenario["id"])
    if errore:
        yield {"tipo": "errore", "messaggio": errore, "log": ""}
        return
    try:
        if modalita not in MODALITA:
            raise ErroreSimulazione(
                f"modalità '{modalita}' sconosciuta: {', '.join(MODALITA)}"
            )
        n_round = MODALITA[modalita]
        db = sqlite3.connect(indice)
        yield fase("Verifica dello scenario in dtSumo")
        controllo = verifica(scenario)
        if not controllo["ok"]:
            raise ErroreSimulazione("scenario non valido: " + controllo["error"])
        avvisi = avvisi_verifica(controllo)
        for a in avvisi:
            yield {"tipo": "avviso", "messaggio": a}

        yield from baseline(n_round)

        cartella = CARTELLA_SIM / scenario["id"]
        cartella.mkdir(parents=True, exist_ok=True)
        (cartella / "risultati.json").unlink(missing_ok=True)
        yield fase(
            f"Scenario: assegnazione, estensione e simulazione dtSumo ({n_round} round, modalità {modalita})"
        )
        invia(scenario, n_round)
        avvia(scenario["id"], n_round)
        yield from attendi(scenario["id"], n_round)

        yield fase("Confronto con la baseline")
        risultati = confronta(
            db, confronto(scenario["id"], BASELINE["id"], n_round), scenario, avvisi
        )
        (cartella / "risultati.json").write_text(
            json.dumps(risultati, ensure_ascii=False), encoding="utf-8"
        )
        yield {"tipo": "fine", "id": scenario["id"], "globali": risultati["globali"]}
    except (ErroreSimulazione, ErroreDtsumo) as e:
        yield {"tipo": "errore", "messaggio": str(e), "log": e.log}
    except Exception as e:
        yield {
            "tipo": "errore",
            "messaggio": f"errore imprevisto: {type(e).__name__}: {e}",
            "log": traceback.format_exc(limit=4),
        }
    finally:
        IN_CORSO.discard(scenario["id"])


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    modalita = sys.argv[2] if len(sys.argv) == 3 else MODALITA_PREDEFINITA
    a_capo = False
    for ev in simula(scenario, modalita=modalita):
        if ev["tipo"] == "avanzamento":
            print(f"\r   {ev['messaggio']}", end="", flush=True)
            a_capo = True
            continue
        if a_capo:
            print()
            a_capo = False
        if ev["tipo"] == "fase":
            print(f"[{ev['t']:8.1f} s] {ev['messaggio']}")
        elif ev["tipo"] == "avviso":
            print(f"   avviso: {ev['messaggio']}")
        elif ev["tipo"] == "errore":
            sys.exit(f"ERRORE: {ev['messaggio']}\n{ev.get('log', '')}")
        elif ev["tipo"] == "fine":
            print(f"\nRisultati in {CARTELLA_SIM / ev['id'] / 'risultati.json'}")
            for nome, g in ev["globali"].items():
                pct = (
                    ""
                    if g["variazione_pct"] is None
                    else f"  ({g['variazione_pct']:+.1f}%)"
                )
                print(f"   {nome:<22} {g['prima']!s:>8} → {g['dopo']!s:<8}{pct}")


if __name__ == "__main__":
    main()
