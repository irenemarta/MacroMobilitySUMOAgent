#!/usr/bin/env bash
# Figures and numbers of the thesis chapter "Experimental evidences and outcomes", all from one campaign.
#   bash results.sh                                # defaults below
#   RUN=sumo-1.28-final SEED=36 bash results.sh     # same analyses on the classic meso campaign
# Every step writes its printed numbers to $OUT/<step>.txt; a failed step is reported and the others go on.
set -u

RUN=${RUN:-sumo-1.28-ltm} # final runs (LTM), linked from sumo-1.28-final
SEED=${SEED:-36} # reference seed of the thesis
SEEDS=${SEEDS:-"36 42 123 2026 99 4 7 12 13 21 48 88 314 777 2002"}
SCENARIO_SEEDS=${SCENARIO_SEEDS:-$SEEDS}
SCENARIOS=${SCENARIOS:-"francia_montecucco_chiuso speed30 taz604_down taz604_up tls_cycle tls_green tls_off"}
COMPARE_RUNS=${COMPARE_RUNS:-"sumo-1.26-confronto sumo-1.28-final"}   # other models shown next to RUN
# time windows of the scenarios that act on a part of the day ("scenario HH:MM HH:MM", on :00 or :30)
WINDOWS=${WINDOWS:-"tls_cycle 07:00 10:00,tls_cycle 17:00 20:00,tls_green 07:00 10:00,tls_green 17:00 20:00,tls_off 07:00 10:00,tls_off 17:00 20:00"}
VARIANT=MA_with_TLS

export DTSUMO_RUN=$RUN
BASE=output/$RUN
OUT=$BASE/thesis
mkdir -p "$OUT"
PY="uv run python -m"

sim_out() { echo "$BASE/simulation-out/out_${VARIANT}_seed$2/closure_${1}"; }   # id seed
det_out() { echo "$BASE/additionals/Det_$VARIANT/DetOut_Day_seed${2}_${1}"; }     # id seed

step() {  # name command...
    local name=$1; shift
    echo ">> $name"
    if ! "$@" > "$OUT/$name.txt" 2>&1; then
        echo "   FAILED, see $OUT/$name.txt"
    fi
}

B=$(sim_out baseline "$SEED")

# 1. dashboard of the reference run, and TLS on (baseline) vs off
step dashboard $PY dtsumo.analysis.sumoResults "$B/Summary_marouter_seed$SEED.xml" \
    --title "baseline seed $SEED" --label baseline --out-dir "$OUT/dashboard"
step dashboard_tls $PY dtsumo.analysis.sumoResults \
    "$B/Summary_marouter_seed$SEED.xml" "$(sim_out tls_off "$SEED")/Summary_marouter_seed$SEED.xml" \
    --title "TLS seed $SEED" --label tls_on --label tls_off --out-dir "$OUT/dashboard"

# 1b. reference seed of every model on the same plot (e.g. SUMO 1.26, 1.28 classic meso, final)
files=(); labels=()
for r in $COMPARE_RUNS; do
    f="output/$r/simulation-out/out_${VARIANT}_seed$SEED/closure_baseline/Summary_marouter_seed$SEED.xml"
    [ -f "$f" ] && files+=("$f") && labels+=(--label "$r")
done
step dashboard_models $PY dtsumo.analysis.sumoResults "${files[@]}" "$B/Summary_marouter_seed$SEED.xml" \
    "${labels[@]}" --label "$RUN" --title "models seed $SEED" --out-dir "$OUT/dashboard"

# 2. trip level: travel times, time loss by hour, sources/sinks, edge usage
step trips $PY dtsumo.analysis.trip_level_output \
    --tripinfo "$B/TripInfo_marouter_seed$SEED.xml" --vehroutes "$B/VehTraces_marouter_seed$SEED.xml" \
    --out-dir "$OUT/trips"

# 3. heatmaps at the peaks, TLS on vs off (speed and flow)
for sid in baseline tls_off; do
    for peak in "08:00 09:00 AM" "18:00 19:00 PM"; do
        set -- $peak
        for metric in speed flow; do
            step "heatmap_${sid}_${3}_$metric" $PY dtsumo.analysis.maps --det-out "$(det_out $sid "$SEED")" \
                --metric $metric --from $1 --to $2 --title "$sid $3 seed $SEED" --out-dir "$OUT/maps"
        done
    done
done

# 4. validation against the PASTA sensors: either a prepared CSV (DTSUMO_SENSOR_DATA)
#    or the sensor registry + hourly counts (defaults: the files used by analysis/sensors.py)
PASTA_ANAGRAPHICS=${PASTA_ANAGRAPHICS:-data/sensors/anagraphics_fp.csv}
PASTA_FLOWS=${PASTA_FLOWS:-data/sensors/flows_fp.csv}
if [ -n "${DTSUMO_SENSOR_DATA:-}" ]; then
    SENSORS=(--sensor-data "$DTSUMO_SENSOR_DATA")
elif [ -f "$PASTA_ANAGRAPHICS" ] && [ -f "$PASTA_FLOWS" ]; then
    SENSORS=(--pasta-anagraphics "$PASTA_ANAGRAPHICS" --pasta-flows "$PASTA_FLOWS")
else
    SENSORS=()
    echo "   no sensor data: $PASTA_ANAGRAPHICS or $PASTA_FLOWS missing, validation will fail"
fi
for s in $SEEDS; do
    step "validation_s$s" $PY dtsumo.analysis.sensors_validation --det-out "$(det_out baseline "$s")" \
        --from-sensors "${SENSORS[@]}" --out-dir "$OUT/validation_s$s"
done
# affinity of each sensor over the seeds (mean, std, range) and of all sensors
step affinity_seeds uv run python - "$OUT" $SEEDS <<'PYEOF'
import sys
import pandas as pd
out, seeds = sys.argv[1], sys.argv[2:]
df = pd.concat(pd.read_csv(f"{out}/validation_s{s}/affinity_by_sensor.csv").assign(seed=s) for s in seeds)
by_sensor = df.groupby(["Cod_sens", "edge_id"])["affinity_pct"].agg(["mean", "std", "min", "max"])
print(by_sensor.sort_values("mean", ascending=False).round(1).to_string())
m = df.groupby("seed")["affinity_pct"].mean()
print(f"all sensors: {m.mean():.1f} +- {m.std():.1f} (range {m.min():.1f}-{m.max():.1f}) over {len(m)} seeds")
PYEOF

# 5. seed variability of the baseline and effect of each scenario (paired by seed)
runs=""; for s in $SEEDS; do runs="$runs baseline:$s"; done
step variability $PY dtsumo.analysis.seed_bootstrap get-variability $runs
for sc in $SCENARIOS; do
    runs=""; for s in $SCENARIO_SEEDS; do runs="$runs $sc:$s:baseline"; done
    step "effect_$sc" $PY dtsumo.analysis.seed_bootstrap effect --csv "$OUT/effect_$sc.csv" $runs
done

# 6. effect of the scenarios inside their own time window (paired by seed)
IFS=',' read -ra jobs <<< "$WINDOWS"
for job in "${jobs[@]}"; do
    set -- $job
    step "window_${1}_${2/:/}_${3/:/}" $PY dtsumo.analysis.compare "$1" --baseline baseline \
        --seeds "$SCENARIO_SEEDS" --from "$2" --to "$3" --csv "$OUT/window_${1}_${2/:/}_${3/:/}.csv"
done

echo "done: $OUT"