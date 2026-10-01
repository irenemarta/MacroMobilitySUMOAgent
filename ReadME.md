# Agente what-if SUMO – centro di Torino

Un agente AI traduce richieste in italiano ("chiudi via Cernaia", "30 all'ora in corso Vinzaglio")
in scenari per SUMO, li simula e li confronta con la situazione di riferimento.
Si usa da riga di comando (`agente.py`) o da una GUI web (`app.py`).

| File | Contenuto |
|---|---|
| `base.net.xml` | rete SUMO generata con netconvert da OpenStreetMap |
| `indice_rete.py` | costruzione e ricerca nell'indice locale `indice.sqlite` |
| `agente.py` | agente: prompt, tool, validazione e ciclo `esegui_turno()`, che emette eventi |
| `risolvi.py` | archi coinvolti da ogni intervento (via intera o tratto "tra X e Y") |
| `simula.py` | patch della rete, duarouter, sumo e confronto con il riferimento |
| `app.py`, `static/index.html` | GUI web (FastAPI, Leaflet, Server-Sent Events) |
| `configurazione.py`, `config.example.json` | configurazione condivisa e esempio senza percorsi personali |
| `avvia_llm.py` | avvio di llama-server con i percorsi e le opzioni salvati |
| `requirements.txt` | dipendenze Python |
| `viaggi.trips.xml` | un'ora di traffico di prova (randomTrips.py) |

`llama.cpp` e i modelli sono dipendenze esterne: puoi installarli in qualsiasi cartella.
`config.local.json`, `.venv/`, `indice.sqlite`, `base.rou.xml`, `scenari/`, `sim/`, cache Python,
impostazioni dell'editor e pacchetti `.deb` sono esclusi da Git. Rete e viaggi di esempio
restano nella repo perché permettono di usare la demo senza procurarsi altri dati.
L'esclusione tramite `.gitignore` lascia i file sul disco: non li sposta né li cancella.
L'indice si rigenera dalla rete; `scenari/` contiene invece il lavoro dell'utente e va
conservata se si vogliono mantenere gli scenari già creati.
Il pacchetto `cuda-keyring_1.1-1_all.deb` è l'installer del repository NVIDIA CUDA:
installa la chiave GPG usata da APT per verificare i pacchetti NVIDIA e configura il repository.
Una volta installato `cuda-keyring`, puoi eliminare il file `.deb` scaricato senza
disinstallare CUDA o compromettere llama.cpp. Per verificare l'installazione:

```bash
dpkg-query -W -f='${Status}\n' cuda-keyring
```

Se restituisce `install ok installed`, il file `.deb` non serve più al funzionamento dei tool.
Non serve spostare il virtualenv; se vuoi tenerlo altrove, ricrealo nella destinazione anziché
spostarlo, perché gli script al suo interno possono contenere percorsi assoluti.

## 1. Modello locale (llama.cpp)

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git curl libcurl4-openssl-dev
LLAMA_DIR="/percorso/scelto/llama.cpp"
MODELLI_DIR="/percorso/scelto/modelli"
git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
```

Per compilare serve prima il toolkit nvidia.

```bash
cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=ON
cmake --build "$LLAMA_DIR/build" --config Release -j --target llama-server llama-cli

pip install -U huggingface_hub
hf download unsloth/Qwen3.6-35B-A3B-GGUF \
  --include "*UD-Q4_K_XL*" \
  --local-dir "$MODELLI_DIR/Qwen3.6-35B-A3B-GGUF"
```

Il download è un esempio per il modello usato in questa demo: puoi usare un altro GGUF
compatibile con tool calling. I percorsi scelti si salvano una volta sola, dalla repo:

```bash
python3 configurazione.py \
  --llama-dir "$LLAMA_DIR" \
  --modello "$MODELLI_DIR/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
```

Se hai già spostato tutto in `/home`, basta invece:

```bash
python3 configurazione.py --llama-dir /home/llama.cpp \
  --modello /home/modelli/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
```

Il comando crea o aggiorna `config.local.json` preservando gli altri parametri. Puoi anche
crearlo copiando `config.example.json` e modificando i valori. Non viene mai chiesto di
reinserire i percorsi all'avvio, anche dopo un riavvio del computer. `LLAMA_DIR` e `MODELLI_DIR`
servono solo per i comandi di installazione mostrati sopra: dopo il salvataggio nel JSON
non occorre definirle di nuovo. Il launcher cerca il binario in `build/bin/llama-server`,
`llama-server` e `bin/llama-server` sotto `LLAMA_CPP_DIR`; con `--llama-server /percorso/binario`
puoi indicare direttamente un eseguibile installato altrove.

Le opzioni di inferenza sono personalizzabili in `LLAMA_SERVER_ARGS`. Per mantenere
le opzioni usate finora con il modello della demo:

```bash
python3 configurazione.py --server-args '["--jinja", "-ngl", "99", "--n-cpu-moe", "20", "-c", "32768", "--temp", "0.7", "--top-p", "0.8", "--top-k", "20", "--min-p", "0.0", "--presence-penalty", "1.5", "--chat-template-kwargs", "{\"enable_thinking\":false}"]'
```

Il valore predefinito è `["--jinja"]`; le opzioni per GPU, contesto e campionamento dipendono
dal modello e dalla macchina. Per il modello della demo, calibra `--n-cpu-moe`, cioè quanti
livelli tengono gli esperti in RAM. Il modello ha 40 livelli,
quindi il valore va da 0 a 40. Tieni aperto `watch -n 1 nvidia-smi` in un altro terminale:

- se l'avvio fallisce per memoria esaurita, alza il valore di 4;
- se restano più di 1,5 GB di VRAM liberi, abbassalo di 2.

Host e porta si salvano con `--host` e `--porta` (predefiniti `127.0.0.1` e `8080`).
L'URL usato da agente, GUI e prova di tool calling è derivato da questi valori.
Per un server già avviato su un'altra macchina, salva `--llm-url http://host:porta/v1`:
i tool possono usarlo anche senza configurare un'installazione locale di llama.cpp.
`LLM_BASE_URL` prevale sull'URL derivato; rimettilo a `null` nel JSON per tornare a usare host e porta.

## 2. Ambiente Python e SUMO

Serve Python 3.10 o successivo.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

SUMO è installato da PPA (`SUMO_HOME=/usr/share/sumo`): `sumo`, `netconvert` e `duarouter`
devono essere nel PATH.

## 3. Indice e traffico di prova (una volta)

```bash
python indice_rete.py costruisci

python $SUMO_HOME/tools/randomTrips.py -n base.net.xml -o viaggi.trips.xml -r base.rou.xml \
  -e 3600 -p 2 --fringe-factor 5 --min-distance 300 --validate
```

L'indice va rigenerato ogni volta che cambia la rete; la GUI richiede la tabella `nodi`.
La rete e i viaggi di esempio sono già inclusi: randomTrips serve solo per rigenerare il traffico.
Il traffico di prova sono 1800 viaggi di auto in un'ora: con `-p 1` (il default di randomTrips)
la rete va quasi in stallo. Se cambiano la rete o i viaggi, il riferimento in `sim/riferimento/`
viene ricalcolato alla simulazione successiva.

## 4. Avvio

Dalla cartella del progetto e con il virtualenv attivo, avvia il modello in un terminale:

```bash
python avvia_llm.py
```

In un secondo terminale, dalla cartella del progetto, attiva lo stesso virtualenv e avvia la GUI:

```bash
. .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Apri `http://localhost:8000`, oppure da un altro computer della rete locale
`http://<indirizzo del PC>:8000` (l'indirizzo si legge con `hostname -I`).
Se il firewall è attivo: `sudo ufw allow 8000/tcp`.
La GUI non ha autenticazione: esponila solo su una rete fidata.

Riga di comando, senza GUI:

```bash
python agente.py                        # conversazione con l'agente
python simula.py scenari/<id>.json      # simulazione e confronto di uno scenario
python risolvi.py scenari/<id>.json     # archi coinvolti, con l'indice configurato
```

Per controllare la configurazione e il comando di avvio senza caricare il modello:

```bash
python configurazione.py --mostra
python avvia_llm.py --mostra-comando
```

Tutti i tool condividono `CONFIG` da `configurazione.py`, caricato una sola volta per processo.
Dopo una modifica al file riavvia i tool già aperti. I percorsi relativi scritti nel JSON
sono risolti rispetto alla cartella del file di configurazione; i comandi `--rete`, `--viaggi`,
`--indice`, `--scenari` e `--sim` salvano percorsi assoluti e permettono di tenere anche dati
e risultati fuori dalla repo. Per trasferire l'indice e gli scenari già presenti, copia o
sposta i file nelle destinazioni scelte prima di cambiare questi parametri.
Per usare un file di configurazione in un'altra cartella imposta `MMSA_CONFIG` al suo percorso;
va impostato nello stesso modo nei terminali del server e dei tool.

I test della configurazione non richiedono un modello attivo:

```bash
python -m unittest discover -s tests -v
```

## 5. Uso della GUI

- **Chat** (in alto a destra): scrivi la richiesta e premi Invio; "Nuova conversazione" azzera
  il contesto dell'agente.
- **Traccia dell'agente** (in basso a destra): una riga per evento, mentre accade: chiamate al
  modello con durata e token, chiamate ai tool con gli argomenti, risultati. Un clic su una riga
  mostra il JSON completo.
- **Mappa**: passando sopra un arco compaiono nome, limite e corsie; un clic evidenzia tutta la
  via e ne mostra le informazioni. Le due direzioni di una strada sono affiancate.
  Durante un turno le vie cercate sono evidenziate in giallo e gli incroci con un marcatore;
  queste evidenziazioni si cancellano all'inizio del turno successivo.
- **Scenario attivo** (sopra la mappa): quando l'agente crea uno scenario diventa quello attivo;
  chiusure in rosso tratteggiato, cambi di velocità in viola con l'etichetta "50 → 30 km/h".
  Il riquadro a destra riassume interventi e avvisi.
- **Simula**: mostra l'avanzamento, poi gli indicatori globali (prima, dopo, variazione) e le
  10 vie con la variazione maggiore; un clic su una via la centra sulla mappa.
- **Prima / Dopo**: "Prima" colora gli archi per velocità media nel riferimento, "Dopo" per la
  variazione (rosso: più traffico o più lento; blu: meno traffico o più veloce; grigio: invariato).

Gli errori compaiono nella pagina: llama-server non raggiungibile (banner nella chat),
simulazione fallita (con le ultime righe del log di SUMO), scenario non valido.

## 6. Come funziona la simulazione

1. **Riferimento**, calcolato una volta e tenuto in `sim/riferimento/`: rete ripassata da
   netconvert senza modifiche, `duarouter` sui viaggi, `sumo` con
   `--device.rerouting.probability 1`, seme 42, tripinfo, statistiche ed edgeData (periodo 3600 s).
   La rete viene ripassata da netconvert perché lo fa anche lo scenario: `base.net.xml` è stata
   generata con netconvert 1.23.1 e il netconvert installato (1.26) ricalcola corsie interne e
   velocità di svolta in tutta la rete. Così le due simulazioni differiscono solo per la patch.
2. **Scenario**, in `sim/<id>/`:
   - `patch.edg.xml` dagli archi risolti con `risolvi.py`. Per le chiusure, su ogni corsia si
     parte dai permessi attuali e si tolgono le classi vietate, così le corsie riservate restano
     come sono: `tutti` = tutte le classi tranne emergency e authority, `auto_private` = passenger,
     `moto` = motorcycle, `mezzi_pesanti` = truck, trailer e delivery, `bus`, `taxi`,
     `bici` = bicycle. Le velocità diventano `speed` in m/s;
   - `netconvert -s base.net.xml -e patch.edg.xml -o sim/<id>/scenario.net.xml`;
   - `duarouter --ignore-errors` sugli stessi viaggi: i viaggi scartati sono contati e divisi tra
     quelli che partono o arrivano su archi chiusi e quelli rimasti senza percorso;
   - `sumo` con le stesse opzioni e lo stesso seme del riferimento.
3. **Confronto**: per ogni arco, veicoli entrati, velocità media e tempo perso prima e dopo;
   indicatori globali (viaggi completati, durata media, tempo perso medio, teletrasporti);
   le vie sono ordinate per variazione dei veicoli·km. Risultati in `sim/<id>/risultati.json`.

Limiti di questa versione:

- la fascia oraria degli scenari viene ignorata (la GUI lo segnala): l'intervento vale per
  tutta la simulazione;
- il traffico di prova contiene solo auto: chiudere una via a bus, taxi, moto, bici o mezzi
  pesanti modifica la rete ma non cambia i risultati;
- una singola simulazione ha un rumore non trascurabile. Ripetendo il riferimento con un altro
  seme, circa il 3% degli archi supera le soglie, la durata media cambia di circa il 4% e il
  tempo perso medio di circa il 7%. Le soglie della mappa (in `simula.py`, `SOGLIE`) servono a non colorare questo rumore;
  per conclusioni solide conviene ripetere le simulazioni con più semi.
