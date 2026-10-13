"""Configurazione locale persistente, condivisa da tutti i tool.

Una volta: python configurazione.py --llama-dir /percorso/llama.cpp --modello /percorso/modello.gguf
Verifica:  python configurazione.py --mostra
"""
import argparse
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from dtsumo.inputs import config as cfg

CARTELLA_PROGETTO = Path(__file__).resolve().parent
FILE_CONFIG = Path(os.environ.get("MMSA_CONFIG", CARTELLA_PROGETTO / "config.local.json")).expanduser().resolve()
PREDEFINITI = {
    "LLAMA_CPP_DIR": None,
    "LLAMA_SERVER": None,
    "LLAMA_MODEL": None,
    "LLAMA_HOST": "127.0.0.1",
    "LLAMA_PORT": 8080,
    "LLAMA_SERVER_ARGS": ["--jinja"],
    "LLM_BASE_URL": None,
    "RETE": str(cfg.NET_FILE),
    "VIAGGI": str(CARTELLA_PROGETTO / "marouter_extended.rou.xml"),
    "INDICE": str(CARTELLA_PROGETTO / "indice.sqlite"),
    "CARTELLA_SCENARI": str(CARTELLA_PROGETTO / "scenari"),
    "CARTELLA_SIM": str(CARTELLA_PROGETTO / "sim"),
    "DTSUMO_DIR": str(CARTELLA_PROGETTO.parent / "dtSumo"),
    "DTSUMO_PYTHON": ".venv/bin/python",
}
CHIAVI_PERCORSI = {"LLAMA_CPP_DIR", "LLAMA_SERVER", "LLAMA_MODEL", "RETE", "VIAGGI",
                "INDICE", "CARTELLA_SCENARI", "CARTELLA_SIM", "DTSUMO_DIR"}


class ErroreConfigurazione(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    llama_cpp_dir: Path | None
    llama_server: Path | None
    modello: Path | None
    host: str
    porta: int
    server_args: tuple[str, ...]
    llm_base_url: str
    rete: Path
    viaggi: Path
    indice: Path
    scenari: Path
    simulazioni: Path
    dtsumo: Path
    dtsumo_python: str


def leggi_file(percorso=FILE_CONFIG):
    if not percorso.exists():
        return {}
    try:
        dati = json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise ErroreConfigurazione(f"Configurazione non leggibile in {percorso}: {e}") from e
    if not isinstance(dati, dict):
        raise ErroreConfigurazione(f"{percorso}: atteso un oggetto JSON")
    ignote = set(dati) - PREDEFINITI.keys()
    if ignote:
        raise ErroreConfigurazione(f"{percorso}: parametri sconosciuti: {', '.join(sorted(ignote))}")
    return dati


def prepara(dati, cartella):
    valori = {**PREDEFINITI, **dati}

    def percorso(chiave):
        valore = valori[chiave]
        if valore is None and chiave.startswith("LLAMA_"):
            return None
        if not isinstance(valore, str) or not valore.strip():
            raise ErroreConfigurazione(f"{chiave}: serve un percorso non vuoto")
        p = Path(valore).expanduser()
        return (p if p.is_absolute() else cartella / p).resolve()

    host, porta, args = (valori[k] for k in ("LLAMA_HOST", "LLAMA_PORT", "LLAMA_SERVER_ARGS"))
    if not isinstance(host, str) or not host.strip() or "://" in host:
        raise ErroreConfigurazione("LLAMA_HOST: serve un nome host o indirizzo IP")
    if type(porta) is not int or not 1 <= porta <= 65535:
        raise ErroreConfigurazione("LLAMA_PORT: serve un numero intero tra 1 e 65535")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ErroreConfigurazione("LLAMA_SERVER_ARGS: serve una lista JSON di stringhe")
    # Questi parametri devono restare coerenti con i percorsi e l'URL condivisi.
    if any(a.split("=", 1)[0] in {"-m", "--model", "--host", "--port"} for a in args):
        raise ErroreConfigurazione("Imposta modello, host e porta nei rispettivi parametri, non in LLAMA_SERVER_ARGS")
    url = valori["LLM_BASE_URL"]
    if url is None:
        host_client = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        host_url = f"[{host_client}]" if ":" in host_client and not host_client.startswith("[") else host_client
        url = f"http://{host_url}:{porta}/v1"
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ErroreConfigurazione("LLM_BASE_URL: serve un URL http:// o https://")
    else:
        dtsumo = percorso("DTSUMO_DIR")
    return Settings(
        llama_cpp_dir=percorso("LLAMA_CPP_DIR"), llama_server=percorso("LLAMA_SERVER"),
        modello=percorso("LLAMA_MODEL"), host=host, porta=porta, server_args=tuple(args),
        llm_base_url=url.rstrip("/"), rete=percorso("RETE"), viaggi=percorso("VIAGGI"),
        indice=percorso("INDICE"), scenari=percorso("CARTELLA_SCENARI"),
        simulazioni=percorso("CARTELLA_SIM"), dtsumo=percorso("DTSUMO_DIR"),
        dtsumo_python=valori["DTSUMO_PYTHON"])


@lru_cache(maxsize=1)
def carica_configurazione():
    """Legge il file una sola volta per processo; per applicare modifiche riavvia i tool."""
    return prepara(leggi_file(), FILE_CONFIG.parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mostra", action="store_true", help="mostra tutti i valori effettivi")
    opzioni = {"llama-dir": "LLAMA_CPP_DIR", "llama-server": "LLAMA_SERVER", "modello": "LLAMA_MODEL",
               "host": "LLAMA_HOST", "porta": "LLAMA_PORT", "server-args": "LLAMA_SERVER_ARGS",
               "llm-url": "LLM_BASE_URL", "rete": "RETE", "viaggi": "VIAGGI", "indice": "INDICE",
               "scenari": "CARTELLA_SCENARI", "sim": "CARTELLA_SIM", "dtsumo": "DTSUMO_DIR",
               "dtsumo-python": "DTSUMO_PYTHON"}
    for opzione, chiave in opzioni.items():
        parser.add_argument(f"--{opzione}", dest=chiave, type=int if chiave == "LLAMA_PORT" else str)
    args = vars(parser.parse_args())
    mostra = args.pop("mostra")
    aggiornamenti = {k: v for k, v in args.items() if v is not None}
    try:
        dati = leggi_file()
        for chiave, valore in aggiornamenti.items():
            if chiave in CHIAVI_PERCORSI:
                if not valore.strip():
                    raise ErroreConfigurazione(f"{chiave}: serve un percorso non vuoto")
                valore = str(Path(valore).expanduser().resolve())
            elif chiave == "LLAMA_SERVER_ARGS":
                try:
                    valore = json.loads(valore)
                except json.JSONDecodeError as e:
                    raise ErroreConfigurazione(f"--server-args: JSON non valido: {e}") from e
            dati[chiave] = valore
        config = prepara(dati, FILE_CONFIG.parent)
        if aggiornamenti:
            FILE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            FILE_CONFIG.write_text(json.dumps(dati, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Configurazione salvata in {FILE_CONFIG}. Riavvia i tool già in esecuzione.")
        if mostra or not aggiornamenti:
            valori = {**PREDEFINITI, **dati}
            for chiave in CHIAVI_PERCORSI:
                p = valori[chiave]
                if p is not None:
                    valori[chiave] = str((FILE_CONFIG.parent / Path(p).expanduser()).resolve())
            valori["LLM_BASE_URL"] = config.llm_base_url
            print(json.dumps(valori, ensure_ascii=False, indent=2))
    except (ErroreConfigurazione, OSError) as e:
        parser.exit(2, f"Errore: {e}\n")


if __name__ == "__main__":
    main()
else:
    CONFIG = carica_configurazione()