"""
Compares a what-if run with its baseline (same seed), edge by edge and globally.

        uv run python -m dtsumo.analysis.compare <scenario> --baseline <baseline> [--from HH:MM --to HH:MM] [--json]

<scenario> and <baseline> are run.py scenario ids (e.g. "telesio_closure_s36", "baseline_s36"
or, from the agent, "<id>_r3" and "baseline_r3"). Both runs must exist for the same seed
(DTSUMO_SEED) and output folder (DTSUMO_RUN). With --json one JSON line is printed on stdout
(the format read by agent.simula.confronta):
    {"scenario": {...}, "interventions": [...], "lanes_closed": {...},
    "edges": {edge: {"baseline": [vehicles, km/h, time_loss_s], "scenario": [...]}},
    "globals": {"baseline": {...}, "scenario": {...}}, "window": [begin_s, end_s] or None}
With --from/--to only that time window is compared: edge data intervals inside it (the final
simulation dumps one every 30 minutes, so the window must start and end on :00 or :30), trips
that depart inside it, teleports that happen inside it.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

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

def _seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def edge_stats(edgedata: Path, window: Optional[Tuple[int, int]] = None) -> dict:
    """
    {edge: [vehicles entered, mean km/h, time loss s]} over the whole day, or only over the
    edge data intervals inside window = (begin s, end s).
    Speed weighted by sampledSeconds (None if no vehicle passed).
    """
    map_id_to_stats = {} # key = id, value = [entered, speed*seconds, seconds, time loss]
    boundaries, inside = set(), True
    for event, el in ET.iterparse(edgedata, events=("start", "end")):
        if el.tag == "interval" and event == "start":
            begin, end = float(el.get("begin")), float(el.get("end"))
            boundaries.update((begin, end))
            inside = window is None or (window[0] <= begin and end <= window[1])
            continue
        if el.tag == "interval":
            el.clear()  # its edges are already counted
            continue
        if el.tag != "edge" or event != "end":
            continue
        if inside:
            acc = map_id_to_stats.setdefault(
                el.get("id"), [0.0, 0.0, 0.0, 0.0]
            )
            det_sampled_seconds = float(el.get("sampledSeconds", 0))
            acc[0] += float(el.get("entered", 0))
            acc[1] += float(el.get("speed", 0)) * det_sampled_seconds
            acc[2] += det_sampled_seconds
            acc[3] += float(el.get("timeLoss", 0))
        el.clear()
    if window is not None and not set(window) <= boundaries:
        step = sorted(boundaries)[1] - sorted(boundaries)[0] if len(boundaries) > 1 else None
        raise click.ClickException(
            f"the window {[_hhmm(t) for t in window]} must start and end where an edge data interval does"
            + (f" (every {round(step / 60)} minutes)" if step else "")
        )
    return {
        edge: [round(acc[0]), round(acc[1] / acc[2] * 3.6, 1) if acc[2] else None, round(acc[3], 1)]
        for edge, acc in map_id_to_stats.items()
    }


def _hhmm(seconds: float) -> str:
    return f"{int(seconds) // 3600:02d}:{int(seconds) % 3600 // 60:02d}"


def sim_stats(sim_out: Path, scale: float, window: Optional[Tuple[int, int]] = None) -> dict:
    """Trips (all, or those departing inside window) and teleports (inside window)."""
    trips, duration, time_loss = 0, 0.0, 0.0
    for tripinfo in Path(sim_out).glob(f"TripInfo_*_scaled{scale}.xml"):
        for _, el in ET.iterparse(tripinfo):
            if el.tag == "tripinfo":
                if window is None or window[0] <= float(el.get("depart", 0)) < window[1]:
                    trips += 1
                    duration += float(el.get("duration", 0))
                    time_loss += float(el.get("timeLoss", 0))
                el.clear()
    teleports = 0
    for summary in Path(sim_out).glob(f"Summary_*_scaled{scale}.xml"):
        before, last = 0, 0  # teleports are cumulative: count those after the window start
        for _, el in ET.iterparse(summary):
            if el.tag == "step":
                t, n = float(el.get("time", 0)), int(float(el.get("teleports", last)))
                if window is None or t < window[1]:
                    last = n
                if window is not None and t < window[0]:
                    before = n
                el.clear()
        teleports = last - before
    return {
        "trips": trips,
        "mean_duration_s": round(duration / trips, 1) if trips else None,
        "mean_time_loss_s": round(time_loss / trips, 1) if trips else None,
        "teleports": teleports,
    }

def compare_baseline_to_scenario(
    sid: str, baseline: str, variant: str = VARIANT, window: Optional[Tuple[int, int]] = None
) -> dict:
    scen, base = run_info(sid, variant), run_info(baseline, variant)
    e_scen = edge_stats(Path(scen["outputs"]["edgedata"]), window)
    e_base = edge_stats(Path(base["outputs"]["edgedata"]), window)
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
            "baseline": sim_stats(base["outputs"]["sim_out"], base["scale"], window),
            "scenario": sim_stats(scen["outputs"]["sim_out"], scen["scale"], window),
        },
        "window": list(window) if window else None,
    }

@click.command()
@click.argument("scenario")
@click.option("--baseline", required=True)
@click.option("--variant", default=VARIANT, show_default=True)
@click.option("--from", "from_", help="Start of the time window, HH:MM (with --to).")
@click.option("--to", help="End of the time window, HH:MM (with --from).")
@click.option("--json", "as_json", is_flag=True, help="One JSON line on stdout (for the agent).")
def cli(scenario, baseline, variant, from_, to, as_json):
    if bool(from_) != bool(to):
        raise click.UsageError("give both --from and --to, or neither")
    window = None
    if from_:
        window = (_seconds(from_), _seconds(to))
        if not 0 <= window[0] < window[1] <= 24 * 3600:
            raise click.UsageError(f"the window {from_}-{to} must go forward within the day")
    result = compare_baseline_to_scenario(scenario, baseline, variant, window)
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
