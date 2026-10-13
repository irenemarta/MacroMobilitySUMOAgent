"""
Agente interattivo what-if sulla rete reale.

Prerequisiti:
  - llama-server attivo sull'URL salvato in config.local.json
  - indice costruito con: python indice_rete.py costruisci
  - indice_rete.py nella stessa cartella

Uso:
  python agente.py [indice.sqlite]
Scrivi richieste in italiano. "nuovo" azzera la conversazione, "esci" termina.
Gli scenari sono nel formato del what-if di dtSumo (interventions, type, via, between, from, to,
fraction, severity): crea_scenario li fa verificare a dtSumo (run.py --check --json) e salva
quelli validi in scenari/<id>.json, con il resoconto di dtSumo in scenari/controlli/<id>.json.

Il ciclo dell'agente è il generatore esegui_turno(messaggi), che emette eventi strutturati:
lo usano sia questa riga di comando (turno) sia la GUI web (app.py).
"""

import json
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from openai import APIConnectionError, APIStatusError, OpenAI

import agent.simula as simula
from agent.configurazione import CONFIG
from agent.indice_rete import cerca_via, incroci
from agent.risolvi import archi_via
from agent.motore import ErroreDtsumo, verifica

# Da riga di comando l'indice si può indicare come argomento; altrimenti usa la configurazione.
# La connessione è condivisa tra i thread della GUI: solo letture, quindi senza rischi.
PERCORSO_DB = (
    Path(sys.argv[1]).expanduser().resolve()
    if __name__ == "__main__" and len(sys.argv) > 1
    else CONFIG.indice
)
if not PERCORSO_DB.is_file():
    raise RuntimeError(
        f"Indice non trovato: {PERCORSO_DB}. Genera l'indice con python indice_rete.py costruisci"
    )
DB = sqlite3.connect(PERCORSO_DB, check_same_thread=False)
client = OpenAI(base_url=CONFIG.llm_base_url, api_key="locale")
CARTELLA_SCENARI = CONFIG.scenari
CARTELLA_SCENARI.mkdir(parents=True, exist_ok=True)
CARTELLA_CONTROLLI = (
    CARTELLA_SCENARI / "controlli"
)  # resoconti di dtSumo, fuori da scenari/*.json
CARTELLA_CONTROLLI.mkdir(exist_ok=True)

SYSTEM = """Sei l'assistente di un simulatore di traffico (SUMO, pipeline dtSumo, Torino). Traduci le richieste what-if dell'utente in uno scenario, lo simuli e ne riferisci i risultati.
Regole:
- Verifica SEMPRE ogni via con cerca_via. Usa solo nomi che compaiono in "esatte".
- Se "esatte" è vuoto ma c'è un suggerimento plausibile, chiedi conferma all'utente prima di usarlo. Se ci sono più esatte o più suggerimenti plausibili, chiedi quale intende.
- Per sapere quali vie incrociano una via usa trasversali.
- Per i tratti "tra X e Y" usa "between", e verifica con incroci_tra che X e Y incrocino davvero la via. Per chiudere un tratto non usare "from"/"to": senza "fraction" vengono ignorati e si chiude tutta la via.
- "Chiudi" una via o un tratto: type "closure" (tutte le corsie, per tutti i veicoli, tutto il giorno).
- Cantiere o restringimento: type "partial_closure" con "severity" = quota di capacità che resta, tra 0 e 1 esclusi.
  Esempi: metà carreggiata -> 0.5; chiusa una corsia su tre -> 0.67. Se l'utente non dice quanto, chiedilo.
- Cantiere lungo una parte della via: "from" (trasversale da cui parte), "fraction" (quota della lunghezza della via, es. 0.1 per il 10%) e, se l'utente indica la direzione, "to". Non usare "between" insieme a "fraction".
- Più o meno traffico: type "demand" con "factor" (1.3 = +30%, 0.8 = -20%). Senza "via" vale per tutta la rete; con "via" per le zone attraversate da quella via.
  "time": ["HH:MM", "HH:MM"] per una fascia oraria. "direction": "to" per chi arriva nella zona (es. una manifestazione o un evento), "from" per chi parte, altrimenti "both".
  Una manifestazione alle 18 si può rendere con più arrivi prima ("to", es. 16:30-18:00) e più partenze dopo ("from", es. 20:00-21:30).
- Limite di velocità: type "speed" con "kmh" in km/h (es. 30 per una zona 30), su una via o su un tratto "between".
- Semafori: type "tls" con "via" e "at" (la trasversale dell'incrocio) e "action":
  "green" con "seconds" = secondi di verde in più (o in meno, negativi) per la via, in ognuna delle sue fasi;
  "cycle" con "factor" = tutto il ciclo più lungo o più corto (1.2 = +20%);
  "off" = semaforo spento, l'incrocio diventa a precedenza.
  Solo alcuni incroci hanno i semafori reali: se il simulatore rifiuta l'incrocio, dillo all'utente.
- Per ora non si possono simulare: chiusure di incroci, categorie di veicoli. Se l'utente li chiede, spiegalo.
- Quando hai tutte le informazioni chiama crea_scenario: lo scenario viene verificato dal simulatore. Se restituisce errori, correggi o chiedi all'utente. Se va bene, riassumi il resoconto e chiedi se simulare.
- simula_scenario avvia la simulazione e torna subito. Modalità "veloce" (meno round di assegnazione, circa la metà del tempo) o "completa" (più precisa, alcune ore): se l'utente non lo dice usa "veloce". Quando l'utente chiede come è andata, usa risultati_scenario e riassumi in poche righe i numeri principali (vie che cambiano di più, tempo perso).
- Alcuni scenari sono già stati simulati in dtSumo: scenari_simulati li elenca, risultati_scenario ne legge i risultati.
- Le variazioni del tempo perso medio sotto il 50% e quelle delle singole vie sotto il 20% possono dipendere dal caso (seme della simulazione): dillo quando le riferisci.
- Per ora non si possono simulare: categorie di veicoli. Se l'utente li chiede, spiegalo.
- Quando hai tutte le informazioni chiama crea_scenario: lo scenario viene verificato dal simulatore. Se restituisce errori, correggi o chiedi all'utente. Se va bene, riassumi il resoconto e chiedi se simulare.
- La simulazione dura circa un'ora: simula_scenario la avvia e torna subito. Quando l'utente chiede come è andata, usa risultati_scenario e riassumi in poche righe i numeri principali (vie che cambiano di più, tempo perso).
- Alcuni scenari sono già stati simulati in dtSumo: scenari_simulati li elenca, risultati_scenario ne legge i risultati.
- Le variazioni del tempo perso medio sotto il 50% e quelle delle singole vie sotto il 20% possono dipendere dal caso (seme della simulazione): dillo quando le riferisci.
- Rispondi in italiano, in modo breve."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cerca_via",
            "description": "Cerca una via nella rete. Restituisce 'esatte' (corrispondenze sicure) e 'suggerimenti' (solo simili).",
            "parameters": {
                "type": "object",
                "properties": {"nome": {"type": "string"}},
                "required": ["nome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "incroci_tra",
            "description": "Conta gli incroci tra una via e una trasversale. Usalo per verificare i tratti 'tra X e Y'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "via": {"type": "string"},
                    "trasversale": {"type": "string"},
                },
                "required": ["via", "trasversale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trasversali",
            "description": "Elenca le vie che incrociano una via, con il numero di incroci.",
            "parameters": {
                "type": "object",
                "properties": {"via": {"type": "string"}},
                "required": ["via"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scenari_simulati",
            "description": "Elenca gli scenari già simulati in dtSumo e confrontabili con la baseline.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_via",
            "description": "Informazioni su una via: numero di archi, lunghezza, limiti di velocità attuali, corsie, tipo OSM.",
            "parameters": {
                "type": "object",
                "properties": {"nome": {"type": "string"}},
                "required": ["nome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crea_scenario",
            "description": "Crea lo scenario what-if e lo fa verificare al simulatore. Restituisce id e resoconto oppure un elenco di errori da correggere.",
            "parameters": {
                "type": "object",
                "properties": {
                    "descrizione": {
                        "type": "string",
                        "description": "Una frase che riassume lo scenario",
                    },
                    "interventions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "closure",
                                        "partial_closure",
                                        "demand",
                                        "speed",
                                        "tls",
                                    ],
                                },
                                "via": {"type": "string"},
                                "at": {
                                    "type": "string",
                                    "description": "la trasversale che incrocia 'via' nell'incrocio semaforizzato",
                                },
                                "tls": {"type": "string"},
                                "action": {"type": "string", "enum": ["green", "cycle", "off"]},
                                "seconds": {"type": "number"},
                                "kmh": {"type": "number", "description": "nuovo limite di velocità in km/h"},
                                "between": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                                "fraction": {"type": "number"},
                                "severity": {"type": "number"},
                                "factor": {"type": "number"},
                                "direction": {
                                    "type": "string",
                                    "enum": ["both", "from", "to"],
                                },
                                "time": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                            },
                            "required": ["type"],
                        },
                    },
                },
                "required": ["descrizione", "interventions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simula_scenario",
            "description": "Avvia la simulazione di uno scenario già creato. Dura ore: torna subito.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "modalita": {"type": "string", "enum": list(simula.MODALITA)},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "risultati_scenario",
            "description": "Stato della simulazione di uno scenario e, se è finita, i risultati principali.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
]


# --- Tool ------------------------------------------------------------------
def nomi_vie():
    return {r[0] for r in DB.execute("SELECT nome FROM vie")}


def info_via(nome):
    riga = DB.execute(
        "SELECT archi, lunghezza_m FROM vie WHERE nome = ?", (nome,)
    ).fetchone()
    if not riga:
        return {"errore": f"'{nome}' non è nell'indice: usa cerca_via"}
    ids = json.loads(riga[0])
    segnaposto = ",".join("?" * len(ids))
    archi = DB.execute(
        f"SELECT kmh, corsie, tipo FROM archi WHERE id IN ({segnaposto})", ids
    ).fetchall()
    return {
        "archi": len(ids),
        "lunghezza_totale_m": riga[1],
        "limiti_kmh": sorted({a[0] for a in archi}),
        "corsie": sorted({a[1] for a in archi}),
        "tipi_osm": sorted({a[2] for a in archi if a[2]}),
    }


def trasversali(via):
    """Vie che hanno almeno un nodo in comune con la via, con il numero di nodi in comune."""
    if via not in nomi_vie():
        return {"errore": f"'{via}' non è nell'indice: usa cerca_via"}
    nodi = set()
    for da, a in DB.execute("SELECT da_nodo, a_nodo FROM archi WHERE nome = ?", (via,)):
        nodi.update((da, a))
    comuni = {}
    for nome, da, a in DB.execute(
        "SELECT nome, da_nodo, a_nodo FROM archi WHERE nome IS NOT NULL AND nome != ?",
        (via,),
    ):
        if da in nodi or a in nodi:
            comuni.setdefault(nome, set()).update(nodi & {da, a})
    return {
        "via": via,
        "trasversali": [
            {"via": nome, "incroci": len(n)} for nome, n in sorted(comuni.items())
        ],
    }


def crea_scenario(args):
    """Il modello propone, il codice decide: prima i nomi delle vie sull'indice, poi dtSumo."""
    errori, vie = [], nomi_vie()
    interventi = args.get("interventions") or []
    if not interventi:
        errori.append("nessun intervento indicato")
    for n, it in enumerate(interventi, 1):
        via = it.get("via", "")
        if it.get("type") == "speed":
            if not isinstance(it.get("kmh"), (int, float)):
                errori.append(
                    f"intervento {n}: per un limite di velocità serve il valore 'kmh "
                )
        if it.get("type") in ("demand", "tls") and not via:
            continue  # tutta la rete o zone TAZ: le controlla dtSumo
        if via not in vie:
            errori.append(f"intervento {n}: '{via}' non è nell'indice, usa cerca_via")
            continue
        trasversali = list(it.get("between") or []) + [
            it[k] for k in ("from", "to", "at") if it.get(k)
        ]
        for t in trasversali:
            if t not in vie:
                errori.append(f"intervento {n}: la trasversale '{t}' non è nell'indice")
            elif not incroci(DB, via, t):
                errori.append(f"intervento {n}: '{t}' non incrocia '{via}'")
    if errori:
        return {"ok": False, "errori": errori}

    # dtSumo e SUMO ragionano in m/s: il modello scrive km/h, la conversione la fa il codice
    interventi = [
        (
            {
                **{key: value for key, value in it.items() if key != "kmh"}, # key = type (ex: speed), value = via (ex: corso Francia)
                "ms": round(it["kmh"] / 3.6, 2),
            } # result = {type, via, ms} 
            if it.get("type") == "speed"
            else it
        )
        for it in interventi
    ]
    ident = datetime.now().strftime("%Y%m%d-%H%M%S")
    scenario = {
        "id": ident,
        "descrizione": args.get("descrizione", ""),
        "interventions": interventi,
    }
    try:
        controllo = verifica(scenario)
    except ErroreDtsumo as e:
        return {"ok": False, "errori": [f"simulatore non raggiungibile: {e}"]}
    if not controllo["ok"]:
        return {"ok": False, "errori": [controllo["error"]]}

    percorso = CARTELLA_SCENARI / f"{ident}.json"
    percorso.write_text(json.dumps(scenario, ensure_ascii=False, indent=2))
    (CARTELLA_CONTROLLI / f"{ident}.json").write_text(
        json.dumps(controllo, ensure_ascii=False)
    )
    resoconto = []
    for it, rep in zip(interventi, controllo["interventions"]):
        if it["type"] == "demand":
            resoconto.append(
                {
                    "zone": rep["taz"] or "tutta la rete",
                    "verso": rep["direction"],
                    "dalle_alle": [ora(rep["begin"]), ora(rep["end"])],
                    "fattore": rep["factor"],
                }
            )
            continue
        if it["type"] == "tls":
            r = {
                "semaforo": rep["tls"],
                "azione": rep["action"],
                "ciclo_s": {
                    "prima": rep["cycle_before_s"],
                    "dopo": rep["cycle_after_s"],
                },
            }
            r.update({k: rep[k] for k in ("seconds", "factor") if k in rep})
            resoconto.append(r)
            continue
        if it["type"] == "speed":
            resoconto.append(
                {
                    "archi": rep["n_edges"],
                    "metri": rep["length_m"],
                    "kmh": kmh(it["ms"]),
                }
            )
            continue
        r = {
            "archi": rep["n_edges"],
            "metri": rep["length_m"],
            "corsie_chiuse": rep["lanes_closed"],
        }
        if rep.get("lanes_kept_for_turns"):
            r["corsie_lasciate_per_le_svolte"] = rep["lanes_kept_for_turns"]
        if rep.get("edges_unchanged"):
            r["archi_invariati_a_una_corsia"] = rep["edges_unchanged"]
        if "fraction_effective" in rep:
            r["quota_effettiva"] = rep["fraction_effective"]
        resoconto.append(r)
    return {"ok": True, "id": ident, "file": str(percorso), "resoconto": resoconto}


# --- Conversioni --------------------------------------------------------------
def ora(secondi):
    return f"{secondi // 3600:02d}:{secondi % 3600 // 60:02d}"


def kmh(ms):
    return round(ms * 3.6)


# --- Simulazione dal testo -----------------------------------------------------
SIMULAZIONI = {}  # id -> {"eventi": [...], "finito": bool}


def simula_scenario(ident, modalita=simula.MODALITA_PREDEFINITA):
    if modalita not in simula.MODALITA:
        return {
            "ok": False,
            "errore": f"modalità '{modalita}' sconosciuta: usa {' o '.join(simula.MODALITA)}",
        }
    percorso = CARTELLA_SCENARI / f"{ident}.json"
    if not percorso.exists():
        return {
            "ok": False,
            "errore": f"lo scenario {ident} non esiste: crealo con crea_scenario",
        }
    if ident in SIMULAZIONI and not SIMULAZIONI[ident]["finito"]:
        return {"ok": False, "errore": f"la simulazione di {ident} è già in corso"}
    errore = simula.posto_libero(ident)
    if errore:
        return {"ok": False, "errore": errore}
    stato = SIMULAZIONI[ident] = {"eventi": [], "finito": False}

    def esegui():
        try:
            for ev in simula.simula(
                json.loads(percorso.read_text()), PERCORSO_DB, modalita
            ):
                stato["eventi"].append(ev)
        finally:
            stato["finito"] = True

    threading.Thread(target=esegui, daemon=True).start()
    return {
        "ok": True,
        "messaggio": f"simulazione avviata in modalità {modalita} "
        f"({simula.MODALITA[modalita]} round di assegnazione e simulazione finale)",
    }


def risultati_scenario(ident):
    file_risultati = simula.CARTELLA_SIM / ident / "risultati.json"
    stato = SIMULAZIONI.get(ident)
    if stato and not stato["finito"]:
        ultimi = [
            e["messaggio"]
            for e in stato["eventi"]
            if e["tipo"] in ("fase", "avanzamento")
        ]
        return {"stato": "in corso", "punto": ultimi[-1] if ultimi else "avvio"}
    if stato and stato["eventi"] and stato["eventi"][-1]["tipo"] == "errore":
        return {"stato": "fallita", "errore": stato["eventi"][-1]["messaggio"]}
    if not file_risultati.exists():
        return {"stato": "non simulato"}
        # forse simulato dal terminale di dtSumo: se non lo è, compare.py fallisce
        # try:
        #     scenario, controllo = simula.importa(ident, PERCORSO_DB)
        # except ErroreDtsumo:
        #     return {"stato": "non simulato"}
        # # salvato come gli scenari del chatbot, così la GUI lo disegna sulla mappa
        # (CARTELLA_SCENARI / f"{ident}.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=2))
        # (CARTELLA_CONTROLLI / f"{ident}.json").write_text(json.dumps(controllo, ensure_ascii=False))
    r = json.loads(file_risultati.read_text())
    return {
        "stato": "finita",
        "globali": r["globali"],
        "avvisi": r["avvisi"],
        "vie_che_cambiano_di_piu": r["vie"][:5],
    }


def sovrapposizione(scenario):
    """Un elemento per intervento, con gli archi risolti da dtSumo e l'etichetta per la mappa."""
    controllo = json.loads((CARTELLA_CONTROLLI / f"{scenario['id']}.json").read_text())
    elementi = []
    for it, rep in zip(scenario["interventions"], controllo["interventions"]):
        if it["type"] == "demand":
            etichetta = f"domanda × {rep['factor']}"
        elif it["type"] == "closure":
            etichetta = "chiusa"
        elif it["type"] == "speed":
            etichetta = f"{kmh(it['ms'])} km/h"
        elif it["type"] == "tls":
            etichetta = {
                "green": f"semaforo: verde {rep.get('seconds', 0):+g} s",
                "cycle": f"semaforo: ciclo × {rep.get('factor')}",
                "off": "semaforo spento",
            }[rep["action"]]
        else:
            etichetta = f"cantiere: {rep['lanes_closed']} corsie chiuse, resta il {round(rep['severity'] * 100)}%"
        elementi.append(
            {
                "tipo": it["type"],
                "via": it.get("via"),
                "tra": it.get("between"),
                "etichetta": etichetta,
                "archi": rep["edge_ids"],
                "avviso": None,
            }
        )
    return elementi


def esegui(nome, args):
    if nome == "cerca_via":
        return cerca_via(DB, args.get("nome", ""))
    if nome == "incroci_tra":
        return {
            "incroci": len(
                incroci(DB, args.get("via", ""), args.get("trasversale", ""))
            )
        }
    if nome == "trasversali":
        return trasversali(args.get("via", ""))
    if nome == "scenari_simulati":
        try:
            return {"scenari": simula.scenari_pronti()}
        except ErroreDtsumo as e:
            return {"errore": f"dtSumo non raggiungibile: {e}"}
    if nome == "info_via":
        return info_via(args.get("nome", ""))
    if nome == "crea_scenario":
        return crea_scenario(args)
    if nome == "simula_scenario":
        return simula_scenario(args.get("id", ""), args.get("modalita", simula.MODALITA_PREDEFINITA))
    if nome == "risultati_scenario":
        return risultati_scenario(args.get("id", ""))
    return {"errore": f"tool sconosciuto: {nome}"}


def evidenzia(nome, args, risultato):
    """Cosa mostrare sulla mappa per il risultato di un tool: {"archi": [...], "nodi": [...]}."""
    archi, nodi = [], []
    if nome == "cerca_via":
        archi = [a for via in risultato.get("esatte", []) for a in archi_via(DB, via)]
    elif nome == "info_via" and "errore" not in risultato:
        archi = archi_via(DB, args.get("nome", ""))
    elif nome == "incroci_tra":
        nodi = incroci(DB, args.get("via", ""), args.get("trasversale", ""))
    elif nome == "crea_scenario" and risultato.get("ok"):
        scenario = json.loads(Path(risultato["file"]).read_text())
        archi = [a for el in sovrapposizione(scenario) for a in el["archi"]]
    return {"archi": archi, "nodi": nodi}


# --- Ciclo di conversazione ------------------------------------------------
def esegui_turno(messaggi, max_passi=8):
    """Esegue un turno sull'ultima richiesta in messaggi ed emette gli eventi man mano che accadono:
    dizionari con "tipo" (inizio_turno, chiamata_modello, testo_modello, chiamata_tool, risultato_tool,
    scenario_creato, errore, fine_turno) e "t", i secondi dall'inizio del turno.
    messaggi viene aggiornato; se il turno fallisce torna com'era prima della richiesta,
    così la conversazione resta valida e la richiesta si può ripetere."""
    inizio, n0 = time.monotonic(), len(messaggi)

    def evento(tipo, **campi):
        return {"tipo": tipo, "t": round(time.monotonic() - inizio, 2), **campi}

    yield evento("inizio_turno")
    risposta, errore = (
        "(troppi passaggi senza una risposta: riformula la richiesta)",
        None,
    )
    try:
        for _ in range(max_passi):
            t0 = time.monotonic()
            completamento = client.chat.completions.create(
                model="locale", messages=messaggi, tools=TOOLS
            )
            msg = completamento.choices[0].message
            uso = completamento.usage
            yield evento(
                "chiamata_modello",
                durata=round(time.monotonic() - t0, 2),
                token_ingresso=uso.prompt_tokens if uso else None,
                token_uscita=uso.completion_tokens if uso else None,
            )
            messaggi.append(msg.model_dump(exclude_none=True))
            testo = (msg.content or "").strip()
            if testo:
                yield evento("testo_modello", testo=testo)
            if not msg.tool_calls:
                risposta = testo
                break
            for call in msg.tool_calls:
                nome = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = None
                yield evento(
                    "chiamata_tool",
                    nome=nome,
                    argomenti=call.function.arguments if args is None else args,
                )
                t0 = time.monotonic()
                if isinstance(args, dict):
                    risultato = esegui(nome, args)
                else:
                    args, risultato = {}, {"errore": "argomenti non in JSON valido"}
                durata = round(time.monotonic() - t0, 3)
                messaggi.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(risultato, ensure_ascii=False),
                    }
                )
                yield evento(
                    "risultato_tool",
                    nome=nome,
                    risultato=risultato,
                    evidenzia=evidenzia(nome, args, risultato),
                    durata=durata,
                )
                if nome == "crea_scenario" and risultato.get("ok"):
                    scenario = json.loads(Path(risultato["file"]).read_text())
                    yield evento(
                        "scenario_creato",
                        scenario=scenario,
                        sovrapposizione=sovrapposizione(scenario),
                    )
    except APIConnectionError:
        errore = f"llama-server non raggiungibile su {client.base_url}: avvialo e ripeti la richiesta"
    except APIStatusError as e:
        errore = (
            f"llama-server ha risposto con un errore ({e.status_code}): {e.message}"
        )
    except Exception as e:  # un guasto imprevisto non deve lasciare la GUI in attesa
        errore = f"errore imprevisto: {type(e).__name__}: {e}"
    if errore:
        del messaggi[n0:]
        if messaggi and messaggi[-1].get("role") == "user":
            messaggi.pop()
        risposta = None
        yield evento("errore", messaggio=errore)
    yield evento(
        "fine_turno", risposta=risposta, durata=round(time.monotonic() - inizio, 2)
    )


def turno(messaggi, max_passi=8):
    """Versione a riga di comando: stampa le chiamate ai tool e restituisce la risposta."""
    args, risposta = {}, ""
    for ev in esegui_turno(messaggi, max_passi):
        if ev["tipo"] == "chiamata_tool":
            args = ev["argomenti"]
        elif ev["tipo"] == "risultato_tool":
            testo = json.dumps(ev["risultato"], ensure_ascii=False)
            print(
                f"   → {ev['nome']}({json.dumps(args, ensure_ascii=False)}) = {testo[:160]}"
            )
        elif ev["tipo"] == "errore":
            risposta = f"({ev['messaggio']})"
        elif ev["tipo"] == "fine_turno" and ev["risposta"] is not None:
            risposta = ev["risposta"]
    return risposta


def main():
    (n_vie,) = DB.execute("SELECT COUNT(*) FROM vie").fetchone()
    print(
        f"Indice caricato: {n_vie} vie. Scrivi una richiesta ('nuovo' azzera, 'esci' termina)."
    )
    messaggi = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            testo = input("\ntu> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not testo:
            continue
        if testo.lower() in ("esci", "exit", "quit"):
            break
        if testo.lower() == "nuovo":
            messaggi = [{"role": "system", "content": SYSTEM}]
            print("(conversazione azzerata)")
            continue
        messaggi.append({"role": "user", "content": testo})
        print(f"agente> {turno(messaggi)}")


if __name__ == "__main__":
    main()
