"""
GUI web dell'agente what-if: mappa della rete, chat, traccia dell'agente e simulazione SUMO.

Avvio, dalla cartella del progetto e con llama-server già attivo:
    uvicorn app:app --host 0.0.0.0 --port 8000
poi apri http://<indirizzo-di-questo-pc>:8000, anche da un altro computer della rete locale.

I turni dell'agente e le simulazioni girano in un thread (Lavoro) che conserva gli eventi
emessi dai generatori agente.esegui_turno e simula.simula. La pagina li riceve con
Server-Sent Events da GET /api/lavori/<id>/eventi; se la connessione cade, EventSource
riprende dall'ultimo evento ricevuto (Last-Event-ID) e il lavoro intanto prosegue.
"""
import asyncio
import json
import re
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

import agent.agente as agente
import agent.simula as simula
from agent.configurazione import CARTELLA_PROGETTO

DB = agente.DB
if not DB.execute("SELECT name FROM sqlite_master WHERE name = 'nodi'").fetchone():
    raise RuntimeError(f"{agente.PERCORSO_DB} non ha la tabella nodi: rigeneralo con "
                    "python indice_rete.py costruisci")

app = FastAPI(title="Agente what-if SUMO")
ID_VALIDO = re.compile(r"[\w-]+")


# --- Lavori in background ----------------------------------------------------------
LAVORI = {}


class Lavoro:
    """Consuma un generatore di eventi in un thread; gli eventi restano disponibili per la pagina."""

    def __init__(self, generatore):
        self.id = uuid.uuid4().hex[:12]
        self.eventi, self.finito = [], False
        for vecchio in [l.id for l in LAVORI.values() if l.finito][:-50]:
            del LAVORI[vecchio]
        LAVORI[self.id] = self
        threading.Thread(target=self._esegui, args=(generatore,), daemon=True).start()

    def _esegui(self, generatore):
        try:
            for evento in generatore:
                self.eventi.append(evento)
        except Exception as e:           # i generatori gestiscono già i propri errori: è solo una difesa
            self.eventi.append({"tipo": "errore", "messaggio": f"errore interno: {e}"})
        finally:
            self.finito = True


@app.get("/api/lavori/{ident}/eventi")
async def eventi_lavoro(ident: str, request: Request):
    lavoro = LAVORI.get(ident)
    if not lavoro:
        raise HTTPException(404, "lavoro sconosciuto: il server è stato riavviato?")
    try:
        i = int(request.headers.get("last-event-id", -1)) + 1
    except ValueError:
        i = 0

    async def flusso():
        nonlocal i
        ultimo_invio = time.monotonic()
        while True:
            while i < len(lavoro.eventi):
                yield f"id: {i}\ndata: {json.dumps(lavoro.eventi[i], ensure_ascii=False)}\n\n"
                i, ultimo_invio = i + 1, time.monotonic()
            if lavoro.finito and i >= len(lavoro.eventi):
                yield "event: chiuso\ndata: {}\n\n"       # la pagina chiude EventSource
                return
            if await request.is_disconnected():
                return
            if time.monotonic() - ultimo_invio > 15:        # tiene viva la connessione
                yield ": in corso\n\n"
                ultimo_invio = time.monotonic()
            await asyncio.sleep(0.1)

    return StreamingResponse(flusso(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- Agente ------------------------------------------------------------------------
CONVERSAZIONI = {}           # id scelto dalla pagina → {"messaggi": [...], "lavoro": Lavoro o None}
BLOCCO = threading.Lock()


class Richiesta(BaseModel):
    conversazione: str
    testo: str


@app.post("/api/chat")
def chat(richiesta: Richiesta):
    testo = richiesta.testo.strip()
    if not testo:
        raise HTTPException(400, "richiesta vuota")
    with BLOCCO:
        for vecchia in [c for c, v in CONVERSAZIONI.items()                 # ogni pagina aperta ne crea una
                        if not (v["lavoro"] and not v["lavoro"].finito)][:-100]:
            del CONVERSAZIONI[vecchia]
        conv = CONVERSAZIONI.setdefault(richiesta.conversazione, {
            "messaggi": [{"role": "system", "content": agente.SYSTEM}], "lavoro": None})
        if conv["lavoro"] and not conv["lavoro"].finito:
            raise HTTPException(409, "l'agente sta ancora rispondendo alla richiesta precedente")
        conv["messaggi"].append({"role": "user", "content": testo})
        conv["lavoro"] = Lavoro(agente.esegui_turno(conv["messaggi"]))
        return {"lavoro": conv["lavoro"].id}


@app.delete("/api/chat/{conversazione}")
def chiudi_conversazione(conversazione: str):
    with BLOCCO:
        conv = CONVERSAZIONI.get(conversazione)
        if conv and conv["lavoro"] and not conv["lavoro"].finito:
            raise HTTPException(409, "attendi la fine del turno in corso")
        CONVERSAZIONI.pop(conversazione, None)
    return {"ok": True}


@app.get("/api/stato")
def stato():
    try:
        agente.client.with_options(timeout=3, max_retries=0).models.list()
        return {"llm": True, "messaggio": None}
    except Exception:
        return {"llm": False, "messaggio": f"llama-server non raggiungibile su {agente.client.base_url}"}


# --- Rete ----------------------------------------------------------------------------
def rete_geojson():
    righe = DB.execute("SELECT id, nome, da_nodo, a_nodo, kmh, corsie, lunghezza_m, tipo, geometria "
                       "FROM archi").fetchall()
    coppie = {(r[2], r[3]) for r in righe}
    return json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": ident,
        # doppio: esiste l'arco opposto, la pagina affianca le due direzioni
        "properties": {"id": ident, "nome": nome, "kmh": kmh, "corsie": corsie, "lunghezza_m": lung,
                       "tipo": tipo, "doppio": (a, da) in coppie},
        "geometry": {"type": "LineString", "coordinates": json.loads(geom)},
    } for ident, nome, da, a, kmh, corsie, lung, tipo, geom in righe]}, ensure_ascii=False)


RETE = rete_geojson()
NODI = {n: [lon, lat] for n, lon, lat in DB.execute("SELECT id, lon, lat FROM nodi")}


@app.get("/api/rete")
def rete():
    return Response(RETE, media_type="application/geo+json")


@app.get("/api/nodi")
def nodi():
    return NODI


@app.get("/api/via")
def via(nome: str):
    info = agente.info_via(nome)
    if "errore" in info:
        raise HTTPException(404, info["errore"])
    return {"nome": nome, **info}


# --- Scenari e simulazione -----------------------------------------------------------
def leggi_scenario(ident):
    if not ID_VALIDO.fullmatch(ident):
        raise HTTPException(400, "id di scenario non valido")
    percorso = agente.CARTELLA_SCENARI / f"{ident}.json"
    if not percorso.exists():
        raise HTTPException(404, f"lo scenario {ident} non esiste")
    try:
        scenario = json.loads(percorso.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"scenario {ident} non valido: il file non è JSON valido ({e})")
    if not isinstance(scenario, dict):
        raise HTTPException(422, f"scenario {ident} non valido: atteso un oggetto JSON")
    scenario["id"] = ident                  # i risultati vanno in sim/<nome del file>
    return scenario


@app.get("/api/scenari")
def elenco_scenari():
    elenco = []
    for percorso in sorted(agente.CARTELLA_SCENARI.glob("*.json"), reverse=True):
        try:
            descrizione = json.loads(percorso.read_text(encoding="utf-8")).get("descrizione", "")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            descrizione = "(file non valido)"
        elenco.append({"id": percorso.stem, "descrizione": descrizione,
                       "simulato": (simula.CARTELLA_SIM / percorso.stem / "risultati.json").exists()})
    return elenco


@app.get("/api/scenari/{ident}")
def dettaglio_scenario(ident: str):
    scenario = leggi_scenario(ident)
    try:
        # archi risolti da dtSumo quando l'agente ha creato lo scenario (scenari/controlli/<id>.json)
        return {"scenario": scenario, "sovrapposizione": agente.sovrapposizione(scenario), "errori": []}
    except FileNotFoundError:
        return {"scenario": scenario, "sovrapposizione": [],
                "errori": ["scenario non verificato da dtSumo: crealo con l'agente, la simulazione lo verifica comunque"]}
    except (AttributeError, TypeError, ValueError, KeyError) as e:
        raise HTTPException(422, f"scenario {ident} non valido: {type(e).__name__}: {e}")


@app.get("/api/scenari/{ident}/risultati")
def risultati(ident: str):
    if not ID_VALIDO.fullmatch(ident):
        raise HTTPException(400, "id di scenario non valido")
    percorso = simula.CARTELLA_SIM / ident / "risultati.json"
    if not percorso.exists():
        raise HTTPException(404, "scenario non ancora simulato")
    return FileResponse(percorso, media_type="application/json")


SIMULAZIONE = {"lavoro": None, "scenario": None}      # la GUI ne segue una alla volta; l'agente può lanciarne altre


@app.post("/api/scenari/{ident}/simula")
def avvia_simulazione(ident: str, modalita: str=simula.MODALITA_PREDEFINITA):
    if modalita not in simula.MODALITA:
        raise HTTPException(400, f"modalità '{modalita}' sconosciuta: usa {' o '.join(simula.MODALITA)}")
    scenario = leggi_scenario(ident)
    with BLOCCO:
        corrente = SIMULAZIONE["lavoro"]
        if corrente and not corrente.finito:
            raise HTTPException(409, f"è già in corso la simulazione dello scenario {SIMULAZIONE['scenario']}")
        errore = simula.posto_libero(ident)            # anche quelle avviate dall'agente
        if errore:
            raise HTTPException(409, errore)
        SIMULAZIONE.update(lavoro=Lavoro(simula.simula(scenario, agente.PERCORSO_DB, modalita)), scenario=ident)
        return {"lavoro": SIMULAZIONE["lavoro"].id, "scenario": ident}


@app.get("/api/simulazione")
def simulazione_in_corso():
    lavoro = SIMULAZIONE["lavoro"]
    if lavoro and not lavoro.finito:
        return {"lavoro": lavoro.id, "scenario": SIMULAZIONE["scenario"]}
    return {"lavoro": None, "scenario": None}


# --- Pagina ------------------------------------------------------------------------
@app.get("/")
def pagina():
    return FileResponse(CARTELLA_PROGETTO / "static/index.html", headers={"Cache-Control": "no-cache"})
