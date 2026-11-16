"""
Crea uno scenario per ogni tipo di intervento, con la stessa funzione usata dall'agente
(crea_scenario: controllo dei nomi sull'indice e verifica di dtSumo), e scrive l'elenco
dei file in da_simulare.txt, pronto per la simulazione in blocco.

Uso (dalla cartella src):
    uv run python -m agent.crea_scenari_tesi
"""

import time
from pathlib import Path

from agent.agente import CARTELLA_SCENARI, crea_scenario

PUNTA = ["07:00", "10:00"]

SCENARI = [
    {"descrizione": "Chiusura di Corso Francia tra Corso Peschiera e Corso Monte Cucco",
    "interventions": [{"type": "closure", "via": "Corso Francia",
                        "between": ["Corso Peschiera", "Corso Monte Cucco"]}]},
    {"descrizione": "Cantiere a metà carreggiata su Corso Francia, da Corso Monte Cucco verso Via Pozzo Strada",
    "interventions": [{"type": "partial_closure", "via": "Corso Francia", "from": "Corso Monte Cucco",
                        "to": "Via Pozzo Strada", "fraction": 0.5, "severity": 0.5}]},
    {"descrizione": "Limite di 30 km/h su Corso Peschiera tra Via Pozzo Strada e Corso Monte Cucco",
    "interventions": [{"type": "speed", "via": "Corso Peschiera",
                        "between": ["Via Pozzo Strada", "Corso Monte Cucco"], "kmh": 30}]},
    {"descrizione": "Chiusura dell'incrocio tra Corso Francia e Corso Monte Cucco",
    "interventions": [{"type": "junction", "via": "Corso Francia", "at": "Corso Monte Cucco"}]},
    {"descrizione": "Svolta vietata da Corso Francia in Corso Monte Cucco",
    "interventions": [{"type": "junction", "via": "Corso Francia", "at": "Corso Monte Cucco",
                        "from": "Corso Francia", "to": "Corso Monte Cucco"}]},
    {"descrizione": "10 s di verde in più per Corso Peschiera all'incrocio con via Bardonecchia",
    "interventions": [{"type": "tls", "via": "Corso Peschiera", "at": "via Bardonecchia",
                        "action": "green", "seconds": 10}],
    "fascia_oraria": PUNTA},
    {"descrizione": "Ciclo semaforico +20% all'incrocio tra Corso Peschiera e via Bardonecchia",
    "interventions": [{"type": "tls", "via": "Corso Peschiera", "at": "via Bardonecchia",
                        "action": "cycle", "factor": 1.2}],
    "fascia_oraria": PUNTA},
    {"descrizione": "Semaforo spento all'incrocio tra Corso Peschiera e via Bardonecchia",
    "interventions": [{"type": "tls", "via": "Corso Peschiera", "at": "via Bardonecchia",
                        "action": "off"}],
    "fascia_oraria": PUNTA},
    {"descrizione": "Arrivi nella zona 604 +20% tra le 12:30 e le 14:00",
    "interventions": [{"type": "demand", "taz": ["604"], "direction": "to",
                        "time": ["12:30", "14:00"], "factor": 1.2}],
    "fascia_oraria": ["12:30", "14:00"]},
]

if __name__ == "__main__":
    creati = []
    for s in SCENARI:
        esito = crea_scenario(s)
        if esito.get("ok"):
            creati.append(CARTELLA_SCENARI / f"{esito['id']}.json")
            print("ok    ", esito["id"], "-", s["descrizione"])
        else:
            print("ERRORE", s["descrizione"], "->", "; ".join(esito.get("errori", [])))
        time.sleep(1.1)  # l'id è data e ora al secondo: evita due scenari con lo stesso nome
    Path("da_simulare.txt").write_text("".join(f"{p}\n" for p in creati))
    print(f"\n{len(creati)} scenari in da_simulare.txt")