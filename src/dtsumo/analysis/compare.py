"""
Compares a what-if run with its baseline (same seed), edge by edge and globally.

    uv run python -m dtsumo.whatif.compare <scenario> --baseline <baseline> [--json]

<scenario> and <baseline> are run.py scenario ids (e.g. "telesio_closure_s36", "baseline_s36"
or, from the agent, "<id>_r3" and "baseline_r3"). Both runs must exist for the same seed
(DTSUMO_SEED) and output folder (DTSUMO_RUN). With --json one JSON line is printed on stdout
(the format read by agent.simula.confronta):
  {"scenario": {...}, "interventions": [...], "lanes_closed": {...},
   "edges": {edge: {"baseline": [vehicles, km/h, time_loss_s], "scenario": [...]}},
   "globals": {"baseline": {...}, "scenario": {...}}}
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import click

import dtsumo.inputs.config as cfg

VARIANT = "MA_with_TLS"


def run_info(sid: str, variant: str = VARIANT) -> dict:
    path = (
        cfg.WORKDIRS[variant]["DAY"]
        / "iterative"
        / f"closure_{sid}_seed{cfg.SEED}"
        / "scenario_resolved.json"
    )
    if not path.exists():
        raise click.ClickException(f"run not found: {path}")
    info = json.loads(path.read_text(encoding="utf-8"))
    if "outputs" not in info:
        raise click.ClickException(f"run {sid} not finished (no outputs in {path})")
    return info


def edge_stats(edgedata: Path) -> dict:
    """{edge: [vehicles entered, mean km/h, time loss s]} over the whole day.
    Speed weighted by sampledSeconds (None if no vehicle passed)."""
    map_id_to_stats = {} # key = id, value = [entered, speed*seconds, seconds, time loss]
    for _, el in ET.iterparse(edgedata):
        if el.tag != "edge":
            continue
        acc = map_id_to_stats.setdefault(
            el.get("id"), [0.0, 0.0, 0.0, 0.0]
        )
        det_sampled_seconds = float(el.get("sampledSeconds", 0))
        acc[0] += float(el.get("entered", 0))
        acc[1] += float(el.get("speed", 0)) * det_sampled_seconds
        acc[2] += det_sampled_seconds
        acc[3] += float(el.get("timeLoss", 0))
        el.clear()
    return {
        edge: [round(acc[0]), round(acc[1] / acc[2] * 3.6, 1) if acc[2] else None, round(acc[3], 1)]
        for edge, acc in map_id_to_stats.items()
    }


def sim_stats(sim_out: Path, scale: float) -> dict:
    trips, duration, time_loss = 0, 0.0, 0.0
    for tripinfo in Path(sim_out).glob(f"TripInfo_*_scaled{scale}.xml"):
        for _, el in ET.iterparse(tripinfo):
            if el.tag == "tripinfo":
                trips += 1
                duration += float(el.get("duration", 0))
                time_loss += float(el.get("timeLoss", 0))
                el.clear()
    teleports = 0
    for summary in Path(sim_out).glob(f"Summary_*_scaled{scale}.xml"):
        for _, el in ET.iterparse(summary):
            if el.tag == "step":
                teleports = int(
                    float(el.get("teleports", teleports))
                )  # cumulative: last step wins
                el.clear()
    return {
        "trips": trips,
        "mean_duration_s": round(duration / trips, 1) if trips else None,
        "mean_time_loss_s": round(time_loss / trips, 1) if trips else None,
        "teleports": teleports,
    }


def compare_baseline_to_scenario(sid: str, baseline: str, variant: str = VARIANT) -> dict:
    scen, base = run_info(sid, variant), run_info(baseline, variant)
    e_scen = edge_stats(Path(scen["outputs"]["edgedata"]))
    e_base = edge_stats(Path(base["outputs"]["edgedata"]))
    empty = [0, None, 0.0]
    edges = {
        e: {"baseline": e_base.get(e, empty), "scenario": e_scen.get(e, empty)}
        for e in set(e_base) | set(e_scen)
    }
    return {
        "scenario": scen["scenario"],
        "interventions": scen["interventions"],
        "lanes_closed": scen["lanes_closed"],
        "seed": cfg.SEED,
        "edges": edges,
        "globals": {
            "baseline": sim_stats(base["outputs"]["sim_out"], base["scale"]),
            "scenario": sim_stats(scen["outputs"]["sim_out"], scen["scale"]),
        },
    }


@click.command()
@click.argument("scenario")
@click.option("--baseline", required=True)
@click.option("--variant", default=VARIANT, show_default=True)
@click.option(
    "--json", "as_json", is_flag=True, help="One JSON line on stdout (for the agent)."
)
def cli(scenario, baseline, variant, as_json):
    result = compare_baseline_to_scenario(scenario, baseline, variant)
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return

    for name, before in result["globals"]["baseline"].items():
        print(f"{name}: {before} -> {result['globals']['scenario'][name]}")

    def vehicle_change(item):
        _, metrics = item
        return abs(metrics["scenario"][0] - metrics["baseline"][0])

    print("\nedges with the largest change in vehicles:")
    for edge, metrics in sorted(result["edges"].items(), key=vehicle_change, reverse=True)[:15]:
        print(f"  {edge}: {metrics['baseline'][0]} -> {metrics['scenario'][0]}")


if __name__ == "__main__":
    sys.exit(cli())
