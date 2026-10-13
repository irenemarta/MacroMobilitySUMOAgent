"""Avvia llama-server con i percorsi e le opzioni salvati in config.local.json."""
import argparse
import os
import shlex

from agent.configurazione import CONFIG, FILE_CONFIG, ErroreConfigurazione


def comando_server(config=CONFIG):
    if config.modello is None:
        raise ErroreConfigurazione("Modello non configurato: usa python configurazione.py --modello /percorso/modello.gguf")
    if not config.modello.is_file():
        raise ErroreConfigurazione(f"Modello non trovato: {config.modello}")
    if config.llama_server is not None:
        candidati = [config.llama_server]
    elif config.llama_cpp_dir is not None:
        candidati = [config.llama_cpp_dir / p for p in ("build/bin/llama-server", "llama-server", "bin/llama-server")]
    else:
        raise ErroreConfigurazione("llama.cpp non configurato: usa python configurazione.py --llama-dir /percorso/llama.cpp")
    server = next((p for p in candidati if p.is_file() and os.access(p, os.X_OK)), None)
    if server is None:
        raise ErroreConfigurazione("llama-server eseguibile non trovato: " + ", ".join(map(str, candidati))
                                  + ". Compilalo oppure configura --llama-server /percorso/binario.")
    return [str(server), "--model", str(config.modello), *config.server_args,
            "--host", config.host, "--port", str(config.porta)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mostra-comando", action="store_true", help="verifica i percorsi e mostra il comando senza avviare")
    args = parser.parse_args()
    try:
        comando = comando_server()
        print(f"Configurazione: {FILE_CONFIG}\n{shlex.join(comando)}", flush=True)
        if not args.mostra_comando:
            os.execv(comando[0], comando)
    except (ErroreConfigurazione, OSError) as e:
        parser.exit(2, f"Errore: {e}\n")


if __name__ == "__main__":
    main()
