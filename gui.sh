#!/usr/bin/env bash
# Modello (llama-server) e GUI (uvicorn), ciascuno nella sua sessione tmux.
#   ./gui.sh start     accende il modello, aspetta che sia pronto, poi accende la GUI
#   ./gui.sh stop      spegne GUI e modello e aspetta che le porte siano libere
#   ./gui.sh restart   stop + start
#   ./gui.sh status    cosa è acceso e chi occupa le porte
cd "$(dirname "$0")" || exit 1

LLM_PORT=8081   # come LLAMA_PORT in src/agent/config.local.json
GUI_PORT=8000
LLM_CMD=".venv/bin/python -m agent.avvia_llm"
# 127.0.0.1: la GUI si apre solo da questa macchina (o con l'inoltro di porta di VS Code);
# --timeout-graceful-shutdown: senza, uvicorn aspetta che la pagina chiuda le connessioni
# degli eventi e la porta resta occupata per minuti
GUI_CMD=".venv/bin/uvicorn agent.app:app --host 127.0.0.1 --port $GUI_PORT --timeout-graceful-shutdown 3"
ATTESA_MODELLO=600   # secondi massimi per caricare il modello
ATTESA_GUI=60

# codice HTTP della pagina, 000 se non risponde nessuno
http() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$1"; }
# PID di chi ascolta sulla porta (solo i processi tuoi: degli altri utenti ss non lo mostra)
pid_porta() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1; }
porta_occupata() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

chi_occupa() {
  local pid; pid=$(pid_porta "$1")
  if [ -n "$pid" ]; then ps -o pid=,user=,etime=,cmd= -p "$pid"
  else echo "    (processo di un altro utente: scegli un'altra porta)"; fi
}

# aspetta fino a $2 secondi che la condizione $1 sia vera, con un punto ogni 5 s
aspetta() {
  local cond=$1 max=$2 t=0
  until eval "$cond"; do
    [ "$t" -ge "$max" ] && { [ "$t" -ge 5 ] && echo; return 1; }
    sleep 1; t=$((t + 1)); [ $((t % 5)) -eq 0 ] && printf "."
  done
  [ "$t" -ge 5 ] && echo; return 0
}

# spegne quello che ascolta sulla porta: chiude la sessione tmux e aspetta fino a 10 s;
# se la porta è ancora occupata da un processo tuo (es. avviato a mano o sospeso con Ctrl+Z)
# manda TERM, poi KILL dopo altri 10 s
libera_porta() {
  local nome=$1 porta=$2 sessione=$3 pid
  tmux kill-session -t "$sessione:" 2>/dev/null
  if porta_occupata "$porta" && ! aspetta "! porta_occupata $porta" 10; then
    pid=$(pid_porta "$porta")
    if [ -n "$pid" ] && [ "$(ps -o user= -p "$pid" | tr -d ' ')" = "$(id -un)" ]; then
      kill -CONT "$pid" 2>/dev/null; kill "$pid" 2>/dev/null   # CONT: un processo sospeso non riceve TERM
      if ! aspetta "! porta_occupata $porta" 10; then
        echo "$nome: non si chiude, lo forzo (kill -9 $pid)"
        kill -9 "$pid" 2>/dev/null
        aspetta "! porta_occupata $porta" 5
      fi
    fi
  fi
  if porta_occupata "$porta"; then
    echo "$nome: la porta $porta è ancora occupata da:"; chi_occupa "$porta"; return 1
  fi
  echo "$nome: spento, porta $porta libera"
}

avvia_modello() {
  case $(http "http://127.0.0.1:$LLM_PORT/health") in
    200) echo "modello: già acceso"; return 0 ;;
    503) echo "modello: già in caricamento" ;;
    000)
      if porta_occupata "$LLM_PORT"; then
        echo "modello: la porta $LLM_PORT è occupata ma non risponde:"; chi_occupa "$LLM_PORT"
        echo "  spegnilo con ./gui.sh stop (se è tuo) o cambia LLAMA_PORT"; return 1
      fi
      tmux kill-session -t llm: 2>/dev/null   # sessione rimasta da un avvio fallito
      tmux new-session -d -s llm "$LLM_CMD; echo; echo 'llama-server terminato: premi Invio'; read"
      echo "modello: avviato, caricamento del modello in corso" ;;
    *) echo "modello: sulla porta $LLM_PORT risponde qualcos'altro"; chi_occupa "$LLM_PORT"; return 1 ;;
  esac
  if aspetta '[ "$(http http://127.0.0.1:$LLM_PORT/health)" = 200 ]' "$ATTESA_MODELLO"; then
    echo "modello: pronto"
  else
    echo "modello: non pronto dopo $ATTESA_MODELLO s. Log: tmux attach -t llm (esci con Ctrl+B poi D)"; return 1
  fi
}

avvia_gui() {
  if [ "$(http "http://127.0.0.1:$GUI_PORT/")" = 200 ]; then echo "GUI: già accesa"; return 0; fi
  if porta_occupata "$GUI_PORT"; then
    echo "GUI: la porta $GUI_PORT è occupata:"; chi_occupa "$GUI_PORT"
    echo "  se è una GUI vecchia che non si è chiusa: ./gui.sh stop"; return 1
  fi
  tmux kill-session -t gui: 2>/dev/null
  tmux new-session -d -s gui "$GUI_CMD; echo; echo 'GUI terminata: premi Invio'; read"
  echo "GUI: avvio"
  if aspetta '[ "$(http http://127.0.0.1:$GUI_PORT/)" = 200 ]' "$ATTESA_GUI"; then
    echo "GUI: pronta su http://127.0.0.1:$GUI_PORT"
  else
    echo "GUI: non risponde dopo $ATTESA_GUI s. Log: tmux attach -t gui"; return 1
  fi
}

stato() {
  case $(http "http://127.0.0.1:$LLM_PORT/health") in
    200) echo "modello: pronto (porta $LLM_PORT)" ;;
    503) echo "modello: in caricamento (porta $LLM_PORT)" ;;
    000) porta_occupata "$LLM_PORT" && { echo "modello: porta $LLM_PORT occupata ma non risponde:"; chi_occupa "$LLM_PORT"; } \
        || echo "modello: spento" ;;
    *) echo "modello: sulla porta $LLM_PORT risponde qualcos'altro" ;;
  esac
  if [ "$(http "http://127.0.0.1:$GUI_PORT/")" = 200 ]; then echo "GUI: accesa (porta $GUI_PORT)"
  elif porta_occupata "$GUI_PORT"; then echo "GUI: porta $GUI_PORT occupata ma non risponde:"; chi_occupa "$GUI_PORT"
  else echo "GUI: spenta"; fi
}

case "$1" in
  start)   avvia_modello && avvia_gui ;;
  stop)    ok=0
          libera_porta GUI "$GUI_PORT" gui || ok=1
          libera_porta modello "$LLM_PORT" llm || ok=1
          exit $ok ;;
  restart) "$0" stop && "$0" start ;;
  status)  stato ;;
  *)       echo "uso: $0 start|stop|restart|status"; exit 2 ;;
esac