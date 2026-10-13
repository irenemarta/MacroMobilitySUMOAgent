# generate_closure_scenarios.py
# Batch of what-if scenarios (see src/whatif/run.py)
#   uv run python generate_closure_scenarios.py  # positions x severities + baseline
#   uv run python generate_closure_scenarios.py scenarios/  # every scenarios/*.json + baseline
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import click
import sumolib
from colorama import init, Fore

import dtsumo.inputs.config as cfg
from dtsumo.operations.marouter_cycle import DEFAULT_DAY_SCALE
from dtsumo.inputs.dayODs import generate_hour_matrices
from dtsumo.whatif.run import N_ROUNDS, SCENARIOS_YAML, record, run_scenario
from dtsumo.whatif.scenario import ScenarioError, disturbances, load_scenario

init(autoreset=True)

# disturb ALL existing connections, incoming AND outgoing
CLOSURE_POSITIONS = {
    "pos9": {  # existing, priority 9
        "edge_id": "125665632#1",
        "connections": [
            ("125665632#0", "125665632#1"),
            ("125665632#1", "135863195#1"),
        ],
    },
    "pos4": {  # existing, priority 7
        "edge_id": "38319544#20",
        "connections": [
            ("-154986395#12", "38319544#20"),
            ("154986395#9", "38319544#20"),
            ("38319544#18", "38319544#20"),
            ("38319544#20", "-39671247"),
            ("38319544#20", "38319544#22"),
        ],
    },
    "pos_prio9b": {  # new, priority 9
        "edge_id": "1307179596",
        "connections": [
            ("-1307179596", "1307179596"),
            ("1307179597#0", "1307179596"),
            ("1307179596", "-1307179596"),
            ("1307179596", "37756577#0"),
        ],
    },
    "pos_prio3": {  # new, priority 3 - the only really new tier
        "edge_id": "1305446509#5",
        "connections": [
            ("1305446509#4", "1305446509#5"),
            ("1305446509#5", "1305446509#7"),
        ],
    },
    "pos_prio7b": {  # new, priority 7
        "edge_id": "121952564#4",
        "connections": [
            ("-110531712#0", "121952564#4"),
            ("121952564#2", "121952564#4"),
            ("121952564#4", "121952564#5"),
        ],
    },
}
# severity = share of capacity left, 0.0 closes every lane (see scenario.lanes_to_close)
# SEVERITY_LEVELS = [0.7, 0.4, 0.0]
SEVERITY_LEVELS = [0.5, 0.2]
TLS_VARIANTS_TO_RUN = ["MA_with_TLS"]
MAX_WORKERS = 2  # depends on the machine (CPU, RAM)


def closure_scenarios() -> list:
    scenarios = [{"id": "baseline", "interventions": []}]
    for pos_name, pos in CLOSURE_POSITIONS.items():
        for sev in SEVERITY_LEVELS:
            intervention = ({"type": "closure", "edges": [pos["edge_id"]]} if sev == 0.0 else
                            {"type": "partial_closure", "edges": [pos["edge_id"]], "severity": sev})
            scenarios.append({"id": f"{pos_name}_sev{sev}", "interventions": [intervention]})
    return scenarios


def check_connections() -> None:
    net = sumolib.net.readNet(str(cfg.NET_FILE))
    for pos_name, pos in CLOSURE_POSITIONS.items():
        computed = {(d["from"], d["to"]) for d in disturbances(net, {pos["edge_id"]: 0.0})}
        listed = set(pos["connections"])
        if computed != listed:
            print(Fore.YELLOW + f"{pos_name}: listed connections differ from the network: "
                f"only listed {sorted(listed - computed)}, only computed {sorted(computed - listed)}")


def scenarios_from_folder(folder: Path) -> list:
    # baseline added if missing
    files = sorted(folder.glob("*.json"))
    if not files:
        raise ScenarioError(f"no *.json scenario files in {folder}")
    scenarios = []
    for f in files:
        try:
            scenarios.append(load_scenario(f))
        except ValueError as e:  # also broken json
            raise ScenarioError(f"{f.name}: {e}")
    ids = [s["id"] for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ScenarioError(f"duplicate scenario ids: {duplicates}")
    if "baseline" not in ids:
        scenarios.insert(0, {"id": "baseline", "interventions": []})
    return scenarios


def run_one(args) -> dict:
    scenario, variant = args
    print(f"\n########## {variant}_{scenario['id']}_seed{cfg.SEED} ##########")
    return run_scenario(scenario, variant, n_rounds=N_ROUNDS, scale=DEFAULT_DAY_SCALE)


@click.command()
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--workers", type=int, default=MAX_WORKERS, show_default=True)
def main(folder: Optional[Path], workers: int):
    """Run the closure batch, or every scenario file in FOLDER."""
    data_dir = cfg.OD_MATRICES["DAY"] / f"scaled_{DEFAULT_DAY_SCALE}"
    if len(list(data_dir.glob("h*.mtx"))) < 24:
        generate_hour_matrices(
            od_morning=cfg.OD_MATRICES["AM"],
            od_evening=cfg.OD_MATRICES["PM"],
            output_dir_data=data_dir,
            input_data=cfg.SENS_DATA_FOLDER / "flows.csv",
            demand_scale=DEFAULT_DAY_SCALE,
        )
    else:
        print(f"Hourly matrices already in {data_dir}, skipping.")

    try:
        if folder:
            scenarios = scenarios_from_folder(folder)
        else:
            check_connections()
            scenarios = closure_scenarios()
        for variant in TLS_VARIANTS_TO_RUN:  # check everything before hours of simulation
            for sc in scenarios:
                print(f"[check] {sc['id']}")
                run_scenario(sc, variant, check_only=True)
    except ScenarioError as e:
        raise click.ClickException(str(e))

    runs = [(sc, variant) for variant in TLS_VARIANTS_TO_RUN for sc in scenarios]
    entries = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, r) for r in runs]
        for future in as_completed(futures):
            entries.append(future.result())

    record(entries)
    print(f"\nWrote {len(entries)} scenarios to {SCENARIOS_YAML}")


if __name__ == "__main__":
    main()
