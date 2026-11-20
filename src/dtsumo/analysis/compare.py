"""
Compares a what-if run with its baseline (same seed), edge by edge and globally.

        uv run python -m dtsumo.analysis.compare <scenario> --baseline <baseline> [--from HH:MM --to HH:MM] [--json]
        uv run python -m dtsumo.analysis.compare <scenario> --baseline <baseline> --seeds "36 42 123" [--from --to] [--csv f]

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
With --seeds the comparison is paired over several seeds (same id, one run per seed): per-seed
effect on the mean time loss, median, signs and bootstrap 95% CI of the mean; for the edges the
same "solid" rule as seed_bootstrap (CI without 0, same sign in every seed, change >= 20%).
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

import click
import numpy as np
import pandas as pd

import dtsumo.inputs.config as cfg

VARIANT = "MA_with_TLS"
MIN_CHANGE = 0.20  # same thresholds as seed_bootstrap
RESAMPLES = 2000


def resolve_output(path) -> Path:
    """Paths saved in scenario_resolved.json are absolute. 
    The same file is looked for first under the simulation-out / marouter-workdir of the current run (output/<DTSUMO_RUN>), 
    so that a copy of a campaign (e.g. the LTM final runs linked under another run name) reads its own files.
    """
    path = Path(path)
    for folder, root in (
        ("simulation-out", cfg.SUMO_OUTPUT),
        ("marouter-workdir", cfg.MAROUTER_WORKDIR),
    ):
        if folder in path.parts:
            local = root.joinpath(*path.parts[path.parts.index(folder) + 1 :])
            if local.exists():
                return local
    return path


def run_info(sid: str, variant: str = VARIANT, seed: Optional[int] = None) -> dict:
    seed = cfg.SEED if seed is None else seed
    path = (
        cfg.MAROUTER_WORKDIR
        / f"{variant}_DAY_seed{seed}"
        / "iterative"
        / f"closure_{sid}_seed{seed}"
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
    map_id_to_stats = (
        {}
    )  # key = id, value = [entered, speed*seconds, seconds, time loss]
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
            acc = map_id_to_stats.setdefault(el.get("id"), [0.0, 0.0, 0.0, 0.0])
            det_sampled_seconds = float(el.get("sampledSeconds", 0))
            acc[0] += float(el.get("entered", 0))
            acc[1] += float(el.get("speed", 0)) * det_sampled_seconds
            acc[2] += det_sampled_seconds
            acc[3] += float(el.get("timeLoss", 0))
        el.clear()
    if window is not None and not set(window) <= boundaries:
        step = (
            sorted(boundaries)[1] - sorted(boundaries)[0]
            if len(boundaries) > 1
            else None
        )
        raise click.ClickException(
            f"the window {[_hhmm(t) for t in window]} must start and end where an edge data interval does"
            + (f" (every {round(step / 60)} minutes)" if step else "")
        )
    return {
        edge: [
            round(acc[0]),
            round(acc[1] / acc[2] * 3.6, 1) if acc[2] else None,
            round(acc[3], 1),
        ]
        for edge, acc in map_id_to_stats.items()
    }


def _hhmm(seconds: float) -> str:
    return f"{int(seconds) // 3600:02d}:{int(seconds) % 3600 // 60:02d}"


def sim_stats(
    sim_out: Path,
    scale: float,
    window: Optional[Tuple[int, int]] = None,
    seed: Optional[int] = None,
) -> dict:
    """Trips (all, or those departing inside window) and teleports (inside window)."""
    tag = (
        f"seed{seed}" if seed is not None else f"scaled{scale}"
    )  # final runs are named by seed
    trips, duration, time_loss = 0, 0.0, 0.0
    for tripinfo in Path(sim_out).glob(f"TripInfo_*_{tag}.xml"):
        for _, el in ET.iterparse(tripinfo):
            if el.tag == "tripinfo":
                if (
                    window is None
                    or window[0] <= float(el.get("depart", 0)) < window[1]
                ):
                    trips += 1
                    duration += float(el.get("duration", 0))
                    time_loss += float(el.get("timeLoss", 0))
                el.clear()
    teleports = 0
    for summary in Path(sim_out).glob(f"Summary_*_{tag}.xml"):
        before, last = (
            0,
            0,
        )  # teleports are cumulative: count those after the window start
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
    sid: str,
    baseline: str,
    variant: str = VARIANT,
    window: Optional[Tuple[int, int]] = None,
    seed: Optional[int] = None,
) -> dict:
    scen, base = run_info(sid, variant, seed), run_info(baseline, variant, seed)
    e_scen = edge_stats(resolve_output(scen["outputs"]["edgedata"]), window)
    e_base = edge_stats(resolve_output(base["outputs"]["edgedata"]), window)
    empty = [0, None, 0.0]
    edges = {
        e: {"baseline": e_base.get(e, empty), "scenario": e_scen.get(e, empty)}
        for e in set(e_base) | set(e_scen)
    }
    return {
        "scenario": scen["scenario"],
        "interventions": scen["interventions"],
        "lanes_closed": scen["lanes_closed"],
        "seed": cfg.SEED if seed is None else seed,
        "net": base["outputs"].get("net"),
        "edges": edges,
        "globals": {
            "baseline": sim_stats(
                resolve_output(base["outputs"]["sim_out"]),
                base["scale"],
                window,
                base["seed"],
            ),
            "scenario": sim_stats(
                resolve_output(scen["outputs"]["sim_out"]),
                scen["scale"],
                window,
                scen["seed"],
            ),
        },
        "window": list(window) if window else None,
    }


def _bootstrap_ci(values: np.ndarray, rng) -> Tuple[float, float]:
    means = rng.choice(values, size=(RESAMPLES, len(values)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def paired_over_seeds(
    results: list, min_veh: float, csv_path: Optional[Path] = None
) -> None:
    """results: compare_baseline_to_scenario() of the same scenario, one per seed."""
    rng = np.random.default_rng(0)
    g = pd.DataFrame(
        [
            {
                "seed": r["seed"],
                "time_loss_base": r["globals"]["baseline"]["mean_time_loss_s"],
                "time_loss_scen": r["globals"]["scenario"]["mean_time_loss_s"],
                "teleports_base": r["globals"]["baseline"]["teleports"],
                "teleports_scen": r["globals"]["scenario"]["teleports"],
            }
            for r in results
        ]
    )
    g["effect_s"] = g["time_loss_scen"] - g["time_loss_base"]
    lo, hi = _bootstrap_ci(g["effect_s"].to_numpy(), rng)
    print(g.to_string(index=False))
    print(
        f"\nmean time loss: median effect {g['effect_s'].median():+.2f} s, mean {g['effect_s'].mean():+.2f} s "
        f"[{lo:+.2f}, {hi:+.2f}], worse in {(g['effect_s'] > 0).sum()}/{len(g)} seeds"
    )

    net = results[0].get("net")
    names = (
        {
            el.get("id"): el.get("name", "")
            for _, el in ET.iterparse(net)
            if el.tag == "edge" and el.get("function") != "internal"
        }
        if net
        else {}
    )
    rows = []
    for edge in results[0]["edges"]:
        if not all(edge in r["edges"] for r in results):
            continue
        base = np.array([r["edges"][edge]["baseline"][0] for r in results], dtype=float)
        diff = (
            np.array([r["edges"][edge]["scenario"][0] for r in results], dtype=float)
            - base
        )
        if base.mean() < min_veh:
            continue
        e_lo, e_hi = _bootstrap_ci(diff, rng)
        same_sign = int(max((diff > 0).sum(), (diff < 0).sum()))
        rows.append(
            {
                "edge": edge,
                "via": names.get(edge, ""),
                "baseline_veh": round(base.mean()),
                "effect_veh": round(diff.mean()),
                "ci_low": round(e_lo),
                "ci_high": round(e_hi),
                "effect_pct": round(diff.mean() / base.mean() * 100, 1),
                "same_sign": f"{same_sign}/{len(diff)}",
                "solid": (e_lo > 0 or e_hi < 0)
                and same_sign == len(diff)
                and abs(diff.mean()) >= MIN_CHANGE * base.mean(),
            }
        )
    edges = pd.DataFrame(rows)
    solid = edges[edges["solid"]] if len(edges) else edges
    solid = (
        solid.reindex(solid["effect_veh"].abs().sort_values(ascending=False).index)
        if len(solid)
        else solid
    )
    print(
        f"\nedges with >= {min_veh:.0f} veh: {len(edges)}, solid effects: {len(solid)}"
        f" ({len(solid) / max(len(edges), 1) * 100:.1f}%)"
    )
    if len(solid):
        print(solid.head(15).to_string(index=False))
    if csv_path:
        edges.to_csv(csv_path, index=False)
        print(f"all edges in {csv_path}")


@click.command()
@click.argument("scenario")
@click.option("--baseline", required=True)
@click.option("--variant", default=VARIANT, show_default=True)
@click.option("--from", "from_", help="Start of the time window, HH:MM (with --to).")
@click.option("--to", help="End of the time window, HH:MM (with --from).")
@click.option(
    "--json", "as_json", is_flag=True, help="One JSON line on stdout (for the agent)."
)
@click.option(
    "--seeds", help='Paired comparison over these seeds, e.g. "36 42 123 2026 99".'
)
@click.option(
    "--csv",
    "csv_path",
    type=click.Path(path_type=Path),
    help="With --seeds: all edges to this file.",
)
def cli(scenario, baseline, variant, from_, to, as_json, seeds, csv_path):
    if bool(from_) != bool(to):
        raise click.UsageError("give both --from and --to, or neither")
    window = None
    if from_:
        window = (_seconds(from_), _seconds(to))
        if not 0 <= window[0] < window[1] <= 24 * 3600:
            raise click.UsageError(
                f"the window {from_}-{to} must go forward within the day"
            )
    if seeds:
        if as_json:
            raise click.UsageError("--seeds and --json cannot be used together")
        seed_list = [int(x) for x in seeds.split()]
        print(
            f"{scenario} vs {baseline}, seeds {seeds}, window {from_ or '00:00'}-{to or '24:00'}"
        )
        results = [
            compare_baseline_to_scenario(scenario, baseline, variant, window, s)
            for s in seed_list
        ]
        # same traffic threshold as seed_bootstrap (200 veh/day), scaled to the window
        min_veh = 200 * ((window[1] - window[0]) / 86400 if window else 1)
        paired_over_seeds(results, min_veh, csv_path)
        return
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
    for edge, metrics in sorted(
        result["edges"].items(), key=vehicle_change, reverse=True
    )[:15]:
        print(f"  {edge}: {metrics['baseline'][0]} -> {metrics['scenario'][0]}")


if __name__ == "__main__":
    sys.exit(cli())
