"""
Test del tool calling per l'agente what-if di mobilità (SUMO).

Uso:
    pip install openai
    python test_tool_calling.py                        # server dalla configurazione locale
    python test_tool_calling.py http://host:porta/v1   # altro server

Lo stradario qui è finto e minimale: nel progetto vero i tool interrogheranno
l'indice semantico generato con sumolib. Aggiungi casi a PROVE con le vie reali
del tuo quartiere: diventerà il set di valutazione per confrontare i modelli.
"""
import difflib
import json
import sys
import time

from openai import OpenAI

from configurazione import CONFIG

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else CONFIG.llm_base_url
client = OpenAI(base_url=BASE_URL, api_key="locale")
MODELLO = "locale"  # llama-server usa il modello caricato, il nome è indifferente

# --- Stradario finto (da sostituire con l'indice SQLite) -------------------
STRADARIO = [
    "Via Nizza", "Via Madama Cristina", "Via Sacchi", "Via Ormea",
    "Corso Dante", "Corso Raffaello", "Corso Marconi",
    "Corso Massimo d'Azeglio", "Corso Vittorio Emanuele II", "Corso Re Umberto",
]
ZONE = ["San Salvario", "Crocetta", "Centro"]
ECCEZIONI = ["bus", "emergency", "delivery", "taxi", "bicycle"]

SYSTEM = """Sei l'assistente di un simulatore di traffico (SUMO) per un quartiere di Torino.
Traduci le richieste what-if dell'utente in uno scenario da simulare.
Regole:
- Prima di usare un nome di via o di zona verificalo SEMPRE con cerca_via o cerca_zona e usa il nome ufficiale restituito.
- Se una via o una zona non esiste, dillo all'utente e non creare lo scenario.
- Se la richiesta è ambigua o manca un'informazione indispensabile, fai UNA domanda di chiarimento invece di creare lo scenario.
- Le velocità sono in km/h, gli orari in formato HH:MM.
- Quando hai tutto, chiama crea_scenario una sola volta con tutti gli interventi."""

TOOLS = [
    {"type": "function", "function": {
        "name": "cerca_via",
        "description": "Cerca una via nello stradario del quartiere e restituisce i nomi ufficiali candidati.",
        "parameters": {"type": "object",
                       "properties": {"nome": {"type": "string",
                                               "description": "Nome della via come l'ha scritto l'utente"}},
                       "required": ["nome"]}}},
    {"type": "function", "function": {
        "name": "cerca_zona",
        "description": "Cerca una zona o un quartiere tra quelli definiti e restituisce i nomi ufficiali candidati.",
        "parameters": {"type": "object",
                       "properties": {"nome": {"type": "string"}},
                       "required": ["nome"]}}},
    {"type": "function", "function": {
        "name": "crea_scenario",
        "description": "Crea lo scenario what-if da simulare in SUMO.",
        "parameters": {"type": "object",
                       "properties": {
                           "nome": {"type": "string", "description": "Identificativo breve in snake_case"},
                           "interventi": {"type": "array", "items": {
                               "type": "object",
                               "properties": {
                                   "tipo": {"type": "string",
                                            "enum": ["chiusura_via", "chiusura_zona", "velocita"]},
                                   "via": {"type": "string", "description": "Nome ufficiale della via"},
                                   "tra": {"type": "array", "items": {"type": "string"},
                                           "minItems": 2, "maxItems": 2,
                                           "description": "Le due vie trasversali che delimitano il tratto, se indicate"},
                                   "zona": {"type": "string", "description": "Nome ufficiale della zona"},
                                   "kmh": {"type": "number"},
                                   "eccezioni": {"type": "array",
                                                 "items": {"type": "string", "enum": ECCEZIONI},
                                                 "description": "Categorie di veicoli a cui resta consentito il transito"},
                               },
                               "required": ["tipo"]}},
                           "fascia_oraria": {"type": "array", "items": {"type": "string"},
                                             "description": "[inizio, fine] in formato HH:MM, se indicata"},
                       },
                       "required": ["nome", "interventi"]}}},
]


# --- Implementazione finta dei tool ----------------------------------------
def cerca(nome, elenco):
    q = nome.lower().strip()
    diretti = [x for x in elenco if q and q in x.lower()]
    simili_lower = difflib.get_close_matches(q, [x.lower() for x in elenco], n=3, cutoff=0.6)
    simili = [x for x in elenco if x.lower() in simili_lower]
    return {"candidati": list(dict.fromkeys(diretti + simili))}


def esegui_tool(nome, argomenti):
    if nome == "cerca_via":
        return cerca(argomenti.get("nome", ""), STRADARIO)
    if nome == "cerca_zona":
        return cerca(argomenti.get("nome", ""), ZONE)
    return {"errore": f"tool sconosciuto: {nome}"}


# --- Ciclo agente ----------------------------------------------------------
def prova(richiesta, max_giri=6):
    messaggi = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": richiesta}]
    for _ in range(max_giri):
        risposta = client.chat.completions.create(model=MODELLO, messages=messaggi, tools=TOOLS)
        msg = risposta.choices[0].message
        if not msg.tool_calls:
            return {"esito": "testo", "contenuto": (msg.content or "").strip()}
        messaggi.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                return {"esito": "json_non_valido", "contenuto": call.function.arguments}
            print(f"   → {call.function.name}({json.dumps(args, ensure_ascii=False)})")
            if call.function.name == "crea_scenario":
                return {"esito": "scenario", "scenario": args}
            messaggi.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(esegui_tool(call.function.name, args),
                                                   ensure_ascii=False)})
    return {"esito": "troppi_giri"}


# --- Casi di prova (aggiungine con le vie vere del tuo quartiere) ----------
def interventi(s, tipo):
    return [i for i in s.get("interventi", []) if i.get("tipo") == tipo]


PROVE = [
    {"richiesta": "Cosa succede se chiudo via Nizza tra corso Dante e corso Raffaello?",
     "atteso": "scenario",
     "controlla": lambda s: any(i.get("via") == "Via Nizza"
                                and set(i.get("tra", [])) == {"Corso Dante", "Corso Raffaello"}
                                for i in interventi(s, "chiusura_via"))},
    {"richiesta": "Porta il limite di corso Massimo d'Azeglio a 50 all'ora",
     "atteso": "scenario",
     "controlla": lambda s: any(i.get("via") == "Corso Massimo d'Azeglio" and i.get("kmh") == 50
                                for i in interventi(s, "velocita"))},
    {"richiesta": "Chiudi tutta San Salvario alle auto dalle 7 alle 9, ma lascia passare i bus",
     "atteso": "scenario",
     "controlla": lambda s: any(i.get("zona") == "San Salvario" and "bus" in i.get("eccezioni", [])
                                for i in interventi(s, "chiusura_zona"))},
    {"richiesta": "Metti il 30 all'ora in via Sacchi e in via Madama Cristina",
     "atteso": "scenario",
     "controlla": lambda s: {i.get("via") for i in interventi(s, "velocita") if i.get("kmh") == 30}
                            == {"Via Sacchi", "Via Madama Cristina"}},
    {"richiesta": "Chiudi via Roma", "atteso": "testo"},        # non esiste nello stradario
    {"richiesta": "Abbassa la velocità", "atteso": "testo"},    # manca dove: deve chiedere
]

if __name__ == "__main__":
    superati = 0
    for p in PROVE:
        print(f"\n▶ {p['richiesta']}")
        t0 = time.time()
        r = prova(p["richiesta"])
        ok = r["esito"] == p["atteso"]
        if ok and r["esito"] == "scenario" and "controlla" in p:
            try:
                ok = bool(p["controlla"](r["scenario"]))
            except Exception:
                ok = False
        superati += ok
        dettaglio = (json.dumps(r["scenario"], ensure_ascii=False) if r["esito"] == "scenario"
                     else r.get("contenuto", ""))
        print(f"   {'OK' if ok else 'NO'} ({time.time() - t0:.1f}s) [{r['esito']}] {dettaglio}")
    print(f"\nSuperati {superati}/{len(PROVE)}")
