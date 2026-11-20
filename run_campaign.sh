#!/usr/bin/env bash
# Thesis campaign: baseline and 7 scenarios on 15 seeds (120 runs), demand from the 24 case-study sensors,
# then the final simulation of every run repeated with the mesoscopic LTM on the same routes.
# Run from the dtSumo folder.
#   bash run_campaign.sh start     # checks, prepares and launches the 120 runs (assignment + final run)
#   bash run_campaign.sh ltm       # final runs again with --meso-ltm, then output/$LTM_RUN for the analyses
#   bash run_campaign.sh status    # progress and results (also while running)
set -u

RUN=${RUN:-sumo-1.28-final}
LTM_RUN=${LTM_RUN:-sumo-1.28-ltm}
SEEDS="36 42 123 2026 99 4 7 12 13 21 48 88 314 777 2002"
SCEN="baseline francia_montecucco_chiuso speed30 taz604_down taz604_up tls_cycle tls_green tls_off"
OLD=output/sumo-1.26-realtls/marouter-workdir/MA_with_TLS_DAY_seed36/iterative
D=data/derived/od/DAY
CFG=output/$RUN/config

h06() { awk 'NF==3 && $1 ~ /^[0-9]+$/ {s+=$3} END {printf "%.0f", s}' "$1"; }

start() {
    [ -d "output/$RUN" ] && { echo "STOP: output/$RUN already exists (campaign already started?). Use 'status', or RUN=<other name>"; exit 1; }

    # 1. demand: case-study profile in scaled_1.0 (the previous one is kept aside, only once)
    if [ "$(h06 $D/scaled_1.0/h06.mtx)" != "$(h06 $D/candidates/case_study_sensors/h06.mtx)" ]; then
        [ -e $D/scaled_1.0_allsensors_may2026 ] && { echo "STOP: $D/scaled_1.0_allsensors_may2026 already exists, check by hand"; exit 1; }
        mv $D/scaled_1.0 $D/scaled_1.0_allsensors_may2026
        mkdir $D/scaled_1.0
        cp $D/candidates/case_study_sensors/h*.mtx $D/scaled_1.0/
        echo "demand: case-study matrices copied in $D/scaled_1.0"
    else
        echo "demand: case-study matrices already in $D/scaled_1.0"
    fi
    echo "   vehicles at 6h: $(h06 $D/scaled_1.0/h06.mtx) (case study ~7462, all sensors ~10175)"

    # 2. code with time-to-teleport 150
    grep -q "TIME_TO_TELEPORT = 150" src/dtsumo/operations/marouter_cycle.py \
        || { echo "STOP: marouter_cycle.py without TIME_TO_TELEPORT = 150"; exit 1; }
    echo "code: time to teleport 150 s"

    # 3. scenario files (taken once from the realtls campaign) and a quick check of each
    mkdir -p scenarios/campaign logs
    [ -f scenarios/campaign/baseline.json ] || echo '{"id": "baseline", "interventions": []}' > scenarios/campaign/baseline.json
    for sc in $SCEN; do
        [ -f scenarios/campaign/$sc.json ] && continue
        uv run python -c "
import json
info = json.load(open('$OLD/closure_${sc}_seed36/scenario_resolved.json'))
json.dump(info['scenario'], open('scenarios/campaign/${sc}.json', 'w'), ensure_ascii=False, indent=1)" \
            || { echo "STOP: cannot read scenario $sc from $OLD"; exit 1; }
    done
    for sc in $SCEN; do
        uv run python -m dtsumo.whatif.run scenarios/campaign/$sc.json --check > /dev/null 2>&1 \
            || { echo "STOP: $sc fails --check:"; uv run python -m dtsumo.whatif.run scenarios/campaign/$sc.json --check | tail -5; exit 1; }
    done
    echo "scenarios: $(echo $SCEN | wc -w) files checked"

    # 4. launch
    export DTSUMO_RUN=$RUN
    local n=0
    for sc in $SCEN; do for s in $SEEDS; do
        DTSUMO_SEED=$s nohup uv run python -m dtsumo.whatif.run scenarios/campaign/$sc.json > logs/final_${sc}_s$s.log 2>&1 &
        n=$((n + 1))
    done; done
    echo "launched $n runs in output/$RUN (logs in logs/final_*.log)"
}

ltm() {
    [ -d "output/$LTM_RUN" ] && { echo "STOP: output/$LTM_RUN already exists"; exit 1; }
    pgrep -f "dtsumo.whatif.run scenarios/campaign" > /dev/null && { echo "STOP: campaign runs still going"; exit 1; }
    mkdir -p logs

    # 1. final run of every scenario and seed again, same routes, outputs with the prefix ltm_
    ls $CFG/francia_peschiera_MAROUTER_with_TLS_*_DAY_scaled1.0_seed*.sumocfg \
        | xargs -P 10 -I{} sh -c 'sumo -c {} --meso-ltm --output-prefix ltm_ > logs/ltm_$(basename {} .sumocfg).log 2>&1'
    echo "LTM runs done: $(find output/$RUN/simulation-out -name 'ltm_Summary_*.xml' | wc -l)"

    # 2. output/$LTM_RUN: links to output/$RUN, where every ltm_ file takes the place of its original,
    #    so that results.sh and the analysis modules read the LTM outputs with RUN=$LTM_RUN
    cp -rs "$(realpath output/$RUN)"/. "output/$LTM_RUN"/
    rm -rf "output/$LTM_RUN/thesis"
    uv run python - "output/$LTM_RUN" <<'PYEOF'
import os
import sys
n = 0
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        if f.startswith("ltm_"):
            link = os.path.join(root, f)
            new = os.path.join(root, f[4:])
            os.symlink(os.readlink(link), new + ".tmp_ln")
            os.replace(new + ".tmp_ln", new)
            n += 1
print("replaced", n, "outputs with their ltm_ version")
PYEOF
    echo "analyses: RUN=$LTM_RUN bash results.sh"
}

status() {
    local done_n failed
    done_n=$(grep -l "done, recorded" logs/final_*.log 2>/dev/null | wc -l)
    failed=$(grep -li "traceback\|error:" logs/final_*.log 2>/dev/null)
    echo "finished $done_n of $(ls logs/final_*.log 2>/dev/null | wc -l)"
    [ -n "$failed" ] && echo "with errors (see the log): $failed"
    echo "LTM final runs: $(find output/$RUN/simulation-out -name 'ltm_Summary_*.xml' 2>/dev/null | wc -l)"
    printf "%-48s %-17s %s\n" run "time reached" "loaded / waiting / teleports"
    for f in output/$RUN/simulation-out/*/closure_*/Summary_*.xml; do
        [ -f "$f" ] || continue
        head -c 30000 "$f" | grep -q marouter_output_extended || continue   # final run only
        last=$(tail -c 600 "$f" | grep -o '<step [^>]*>' | tail -1)
        printf "%-48s %-17s %s\n" "$(echo "$f" | grep -o 'seed[0-9]*/closure_[^/]*' | sed 's#/closure_# #')" \
            "$(echo "$last" | grep -o 'time="[0-9.]*"' | cut -d'"' -f2)" \
            "$(echo "$last" | grep -o 'loaded="[0-9]*"\|waiting="[0-9]*"\|teleports="[0-9]*"' | cut -d'"' -f2 | paste -sd/)"
    done | sort
}

case "${1:-}" in
    start) start ;;
    ltm) ltm ;;
    status) status ;;
    *) echo "usage: bash run_campaign.sh start|ltm|status" ;;
esac