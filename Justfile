
# dtSumo tasks. `just` alone lists them.
# Variables can be changed from the command line, e.g.:
#   just run=prova seeds="36 42" run-all
#   just results_run=sumo-1.28-final results

set dotenv-load := true  # .env: DTSUMO_BASE_PATH, DB_CONNECTION_STRING

python := "uv run python"

# new what-if runs go to output/<run>; the analyses read output/<results_run> (final LTM runs)
run := "prova"
results_run := "sumo-1.28-ltm"
seeds := "36 42 123 2026 99 4 7 12 13 21 48 88 314 777 2002"
scenario_dir := "scenarios/campaign"
scenarios := "baseline francia_montecucco_chiuso speed30 taz604_down taz604_up tls_cycle tls_green tls_off"
variant := "MA_with_TLS"
pasta_anagraphics := "data/sensors/anagraphics_fp.csv"
pasta_flows := "data/sensors/flows_fp.csv"

export DTSUMO_RUN := run

default:
    @just --list

# --- setup ----------------------------------------------------------------------

# Install the environment
init:
    uv sync

# Write requirements.txt from pyproject.toml
requirements:
    uv pip compile pyproject.toml -o requirements.txt

# Create the output folders of the current run
folders:
    {{ python }} -m dtsumo.inputs.config

# Parse the net into nodes/edges XML
parse:
    {{ python }} -m dtsumo.inputs.parser

# CSV with the useful information of each edge
database:
    {{ python }} -m dtsumo.inputs.database

# Detectors on the net
detectors:
    {{ python }} -m dtsumo.tools.detectors

# Sensors data from PASTA/BRIDGE (needs DB_CONNECTION_STRING)
sensors:
    {{ python }} -m dtsumo.analysis.sensors

# Reference (calibrated) assignment, e.g. just assignment --day-only
assignment *args:
    {{ python }} -m dtsumo.operations.assignment {{ args }}

# Network in numbers (nodes, edges, TLS, lengths), with the final state of the reference run
net-stats seed="36":
    DTSUMO_RUN={{ results_run }} {{ python }} -m dtsumo.tools.net_stats \
        --summary output/{{ results_run }}/simulation-out/out_{{ variant }}_seed{{ seed }}/closure_baseline/Summary_marouter_seed{{ seed }}.xml

# --- tests and checks ------------------------------------------------------------

# Tests (pytest options can be added, e.g. just test --e2e -k whatif)
test *args:
    uv run pytest {{ args }}

# Tests of the agent
test-agent:
    uv run pytest src/agent/tests

# Resolve one scenario without simulating it
check scenario:
    {{ python }} -m dtsumo.whatif.run {{ scenario_dir }}/{{ scenario }}.json --check

# Resolve every scenario of the list: stops at the first one not valid
check-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for sc in {{ scenarios }}; do
        echo "== $sc"
        {{ python }} -m dtsumo.whatif.run {{ scenario_dir }}/$sc.json --check
    done

# --- what-if runs (output/<run>) -------------------------------------------------

# One scenario with one seed, in the foreground
run-one scenario seed="36":
    DTSUMO_SEED={{ seed }} {{ python }} -m dtsumo.whatif.run {{ scenario_dir }}/{{ scenario }}.json

# Every scenario with every seed, in parallel and in the background (they go on if the terminal closes)
run-all:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p logs/{{ run }}
    for seed in {{ seeds }}; do
        for sc in {{ scenarios }}; do
            log=logs/{{ run }}/${sc}_seed${seed}.log
            DTSUMO_SEED=$seed nohup sh -c \
                "{{ python }} -m dtsumo.whatif.run {{ scenario_dir }}/$sc.json > $log 2>&1; echo exit \$? >> $log" \
                > /dev/null 2>&1 &
            echo "started $sc seed $seed -> $log"
        done
    done

# Runs still going and exit code of the finished ones (0 = ok)
status:
    #!/usr/bin/env bash
    running=$(pgrep -fc 'bin/python[0-9.]* -m dtsumo.whatif.run' || true)
    echo "running: $running"
    for log in logs/{{ run }}/*.log; do
        [ -e "$log" ] || continue
        last=$(tail -n1 "$log")
        if [[ "$last" == exit* ]]; then state="$last"
        elif [ "$running" -gt 0 ]; then state="running"
        else state="interrupted (no exit code)"; fi
        echo "$(basename "$log" .log): $state"
    done

# Stop all your what-if runs (marouter and sumo included)
stop:
    #!/usr/bin/env bash
    for p in $(pgrep -f 'dtsumo.whatif.run'); do pkill -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done
    echo "stopped"

# --- thesis campaign (run_campaign.sh) -------------------------------------------

# 120 runs of the thesis: assignment + final run (output/sumo-1.28-final)
campaign:
    bash run_campaign.sh start

# Final runs again with the LTM, and output/sumo-1.28-ltm for the analyses
campaign-ltm:
    bash run_campaign.sh ltm

# Progress of the campaign
campaign-status:
    bash run_campaign.sh status

# --- analysis (output/<results_run>) ---------------------------------------------

# Every figure and number of chapter 5 (results.sh), in output/<results_run>/thesis
results:
    RUN={{ results_run }} bash results.sh

# Scenario against the baseline: one seed, or paired over the seeds; optional time window
compare scenario seed="" from="" to="":
    #!/usr/bin/env bash
    set -euo pipefail
    export DTSUMO_RUN={{ results_run }}
    window=(); [ -n "{{ from }}" ] && window=(--from {{ from }} --to {{ to }})
    if [ -n "{{ seed }}" ]; then
        DTSUMO_SEED={{ seed }} {{ python }} -m dtsumo.analysis.compare {{ scenario }} --baseline baseline "${window[@]}"
    else
        {{ python }} -m dtsumo.analysis.compare {{ scenario }} --baseline baseline --seeds "{{ seeds }}" "${window[@]}"
    fi

# Validation of the baseline of one seed against the BRIDGE sensors
validate seed="36":
    DTSUMO_RUN={{ results_run }} {{ python }} -m dtsumo.analysis.sensors_validation --from-sensors \
        --det-out output/{{ results_run }}/additionals/Det_{{ variant }}/DetOut_Day_seed{{ seed }}_baseline \
        --pasta-anagraphics {{ pasta_anagraphics }} --pasta-flows {{ pasta_flows }} \
        --out-dir output/{{ results_run }}/validation_s{{ seed }}

# --- agent -----------------------------------------------------------------------

# Web interface of the agent (llama-server + app)
gui:
    bash gui.sh

# --- cleaning -------------------------------------------------------------------

# Python cache
clean:
    find . -type d -name __pycache__ -exec rm -rf {} +
    find . -type f -name "*.pyc" -delete
