"""
Agente interattivo what-if sulla rete reale.

Prerequisiti:
  - llama-server attivo sull'URL salvato in config.local.json
  - indice costruito con: python indice_rete.py costruisci
  - indice_rete.py nella stessa cartella

Uso:
  python agente.py [indice.sqlite]
Scrivi richieste in italiano. "nuovo" azzera la conversazione, "esci" termina.
Gli scenari validi vengono salvati in scenari/<id>.json.

Il ciclo dell'agente è il generatore esegui_turno(messaggi), che emette eventi strutturati:
lo usano sia questa riga di comando (turno) sia la GUI web (app.py).
"""
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from openai import APIConnectionError, APIStatusError, OpenAI

from configurazione import CONFIG
from indice_rete import cerca_via, incroci
from risolvi import archi_via, sovrapposizione

# Da riga di comando l'indice si può indicare come argomento; altrimenti usa la configurazione.
# La connessione è condivisa tra i thread della GUI: solo letture, quindi senza rischi.
PERCORSO_DB = Path(sys.argv[1]).expanduser().resolve() if __name__ == "__main__" and len(sys.argv) > 1 else CONFIG.indice
if not PERCORSO_DB.is_file():
    raise RuntimeError(f"Indice non trovato: {PERCORSO_DB}. Genera l'indice con python indice_rete.py costruisci")
DB = sqlite3.connect(PERCORSO_DB, check_same_thread=False)
client = OpenAI(base_url=CONFIG.llm_base_url, api_key="locale")
CARTELLA_SCENARI = CONFIG.scenari
CARTELLA_SCENARI.mkdir(parents=True, exist_ok=True)

# Categorie comprensibili all'utente; il compilatore le tradurrà nelle vClass di SUMO
CATEGORIE = ["tutti", "auto_private", "moto", "mezzi_pesanti", "bus", "taxi", "bici"]

SYSTEM = """Sei l'assistente di un simulatore di traffico (SUMO). Traduci le richieste what-if dell'utente in uno scenario.
Regole:
- Verifica SEMPRE ogni via con cerca_via. Usa solo nomi che compaiono in "esatte".
- Se "esatte" è vuoto ma c'è un suggerimento plausibile, chiedi conferma all'utente prima di usarlo. Se ci sono più esatte o più suggerimenti plausibili, chiedi quale intende.
- Per i tratti "tra X e Y", verifica con incroci_tra che X e Y incrocino davvero la via.
- Per le chiusure indica a chi è vietato il transito (vietato_a). "Chiudi" senza altre indicazioni significa "tutti". I mezzi di soccorso passano sempre: non indicarli.
- Velocità in km/h, orari HH:MM. Se l'utente chiede di aumentare o ridurre la velocità senza un numero, usa info_via per il limite attuale e chiedi il nuovo valore.
- Quando hai tutte le informazioni chiama crea_scenario. Se restituisce errori, correggi o chiedi all'utente.
- Rispondi in italiano, in modo breve."""

TOOLS = [
    {"type": "function", "function": {
        "name": "cerca_via",
        "description": "Cerca una via nella rete. Restituisce 'esatte' (corrispondenze sicure) e 'suggerimenti' (solo simili).",
        "parameters": {"type": "object", "properties": {"nome": {"type": "string"}}, "required": ["nome"]}}},
    {"type": "function", "function": {
        "name": "incroci_tra",
        "description": "Conta gli incroci tra una via e una trasversale. Usalo per verificare i tratti 'tra X e Y'.",
        "parameters": {"type": "object",
                       "properties": {"via": {"type": "string"}, "trasversale": {"type": "string"}},
                       "required": ["via", "trasversale"]}}},
    {"type": "function", "function": {
        "name": "info_via",
        "description": "Informazioni su una via: numero di archi, lunghezza, limiti di velocità attuali, corsie, tipo OSM.",
        "parameters": {"type": "object", "properties": {"nome": {"type": "string"}}, "required": ["nome"]}}},
    {"type": "function", "function": {
        "name": "crea_scenario",
        "description": "Crea e salva lo scenario what-if. Restituisce l'id oppure un elenco di errori da correggere.",
        "parameters": {"type": "object",
                       "properties": {
                           "descrizione": {"type": "string", "description": "Una frase che riassume lo scenario"},
                           "interventi": {"type": "array", "items": {
                               "type": "object",
                               "properties": {
                                   "tipo": {"type": "string", "enum": ["chiusura_via", "velocita"]},
                                   "via": {"type": "string"},
                                   "tra": {"type": "array", "items": {"type": "string"},
                                           "minItems": 2, "maxItems": 2},
                                   "vietato_a": {"type": "array", "items": {"type": "string", "enum": CATEGORIE}},
                                   "kmh": {"type": "number"},
                               },
                               "required": ["tipo", "via"]}},
                           "fascia_oraria": {"type": "array", "items": {"type": "string"},
                                             "description": "[inizio, fine] in HH:MM, se indicata"},
                       },
                       "required": ["descrizione", "interventi"]}}},
]


# --- Tool ------------------------------------------------------------------
def nomi_vie():
    return {r[0] for r in DB.execute("SELECT nome FROM vie")}


def info_via(nome):
    riga = DB.execute("SELECT archi, lunghezza_m FROM vie WHERE nome = ?", (nome,)).fetchone()
    if not riga:
        return {"errore": f"'{nome}' non è nell'indice: usa cerca_via"}
    ids = json.loads(riga[0])
    segnaposto = ",".join("?" * len(ids))
    archi = DB.execute(f"SELECT kmh, corsie, tipo FROM archi WHERE id IN ({segnaposto})", ids).fetchall()
    return {"archi": len(ids), "lunghezza_totale_m": riga[1],
            "limiti_kmh": sorted({a[0] for a in archi}),
            "corsie": sorted({a[1] for a in archi}),
            "tipi_osm": sorted({a[2] for a in archi if a[2]})}


def crea_scenario(args):
    """Validazione deterministica: il modello propone, il codice decide."""
    errori, vie = [], nomi_vie()
    interventi = args.get("interventi") or []
    if not interventi:
        errori.append("nessun intervento indicato")
    for n, it in enumerate(interventi, 1):
        via = it.get("via", "")
        if via not in vie:
            errori.append(f"intervento {n}: '{via}' non è nell'indice, usa cerca_via")
            continue
        tra = it.get("tra")
        if tra:
            for t in tra:
                if t not in vie:
                    errori.append(f"intervento {n}: la trasversale '{t}' non è nell'indice")
                elif not incroci(DB, via, t):
                    errori.append(f"intervento {n}: '{t}' non incrocia '{via}'")
        if it.get("tipo") == "velocita":
            kmh = it.get("kmh")
            if not isinstance(kmh, (int, float)) or not 5 <= kmh <= 130:
                errori.append(f"intervento {n}: velocità mancante o fuori dall'intervallo 5-130 km/h")
        if it.get("tipo") == "chiusura_via" and not it.get("vietato_a"):
            it["vietato_a"] = ["tutti"]
    fascia = args.get("fascia_oraria")
    if fascia and (len(fascia) != 2 or not all(re.fullmatch(r"\d{1,2}:\d{2}", f) for f in fascia)):
        errori.append("fascia_oraria deve essere [inizio, fine] in formato HH:MM")
    if errori:
        return {"ok": False, "errori": errori}

    ident = datetime.now().strftime("%Y%m%d-%H%M%S")
    scenario = {"id": ident, "descrizione": args.get("descrizione", ""),
                "interventi": interventi, "fascia_oraria": fascia}
    percorso = CARTELLA_SCENARI / f"{ident}.json"
    percorso.write_text(json.dumps(scenario, ensure_ascii=False, indent=2))
    return {"ok": True, "id": ident, "file": str(percorso)}


def esegui(nome, args):
    if nome == "cerca_via":
        return cerca_via(DB, args.get("nome", ""))
    if nome == "incroci_tra":
        return {"incroci": len(incroci(DB, args.get("via", ""), args.get("trasversale", "")))}
    if nome == "info_via":
        return info_via(args.get("nome", ""))
    if nome == "crea_scenario":
        return crea_scenario(args)
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
        archi = [a for el in sovrapposizione(DB, scenario) for a in el["archi"]]
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
    risposta, errore = "(troppi passaggi senza una risposta: riformula la richiesta)", None
    try:
        for _ in range(max_passi):
            t0 = time.monotonic()
            completamento = client.chat.completions.create(model="locale", messages=messaggi, tools=TOOLS)
            msg = completamento.choices[0].message
            uso = completamento.usage
            yield evento("chiamata_modello", durata=round(time.monotonic() - t0, 2),
                         token_ingresso=uso.prompt_tokens if uso else None,
                         token_uscita=uso.completion_tokens if uso else None)
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
                yield evento("chiamata_tool", nome=nome,
                             argomenti=call.function.arguments if args is None else args)
                t0 = time.monotonic()
                if isinstance(args, dict):
                    risultato = esegui(nome, args)
                else:
                    args, risultato = {}, {"errore": "argomenti non in JSON valido"}
                durata = round(time.monotonic() - t0, 3)
                messaggi.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(risultato, ensure_ascii=False)})
                yield evento("risultato_tool", nome=nome, risultato=risultato,
                             evidenzia=evidenzia(nome, args, risultato), durata=durata)
                if nome == "crea_scenario" and risultato.get("ok"):
                    scenario = json.loads(Path(risultato["file"]).read_text())
                    yield evento("scenario_creato", scenario=scenario,
                                 sovrapposizione=sovrapposizione(DB, scenario))
    except APIConnectionError:
        errore = f"llama-server non raggiungibile su {client.base_url}: avvialo e ripeti la richiesta"
    except APIStatusError as e:
        errore = f"llama-server ha risposto con un errore ({e.status_code}): {e.message}"
    except Exception as e:                    # un guasto imprevisto non deve lasciare la GUI in attesa
        errore = f"errore imprevisto: {type(e).__name__}: {e}"
    if errore:
        del messaggi[n0:]
        if messaggi and messaggi[-1].get("role") == "user":
            messaggi.pop()
        risposta = None
        yield evento("errore", messaggio=errore)
    yield evento("fine_turno", risposta=risposta, durata=round(time.monotonic() - inizio, 2))


def turno(messaggi, max_passi=8):
    """Versione a riga di comando: stampa le chiamate ai tool e restituisce la risposta."""
    args, risposta = {}, ""
    for ev in esegui_turno(messaggi, max_passi):
        if ev["tipo"] == "chiamata_tool":
            args = ev["argomenti"]
        elif ev["tipo"] == "risultato_tool":
            testo = json.dumps(ev["risultato"], ensure_ascii=False)
            print(f"   → {ev['nome']}({json.dumps(args, ensure_ascii=False)}) = {testo[:160]}")
        elif ev["tipo"] == "errore":
            risposta = f"({ev['messaggio']})"
        elif ev["tipo"] == "fine_turno" and ev["risposta"] is not None:
            risposta = ev["risposta"]
    return risposta


def main():
    n_vie, = DB.execute("SELECT COUNT(*) FROM vie").fetchone()
    print(f"Indice caricato: {n_vie} vie. Scrivi una richiesta ('nuovo' azzera, 'esci' termina).")
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
