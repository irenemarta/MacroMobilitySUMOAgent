"""
How much the results move only because of the random seed.

Same scenario run with different seeds (and different ids, see run.py), compared with the
first one as the chatbot does with the baseline:

    uv run python -m scripts.src.whatif.seed_noise baseline:36 baseline_s42:42 baseline_s123:123 baseline_s7:7 baseline_s99:99

Prints, over the edges with enough traffic, the percentiles of the relative difference
from the reference run (vehicles entered and time loss), and the global trip stats.
"""

import json
from pathlib import Path

import click
import numpy as np

import dtsumo.inputs.config as cfg
from dtsumo.analysis.compare import edge_stats, sim_stats
from dtsumo.whatif.run import VARIANTS

MIN_VEHICLES = 200  # per day: below this the relative differences are meaningless


def run_info(variant: str, sid: str, seed: str) -> dict:
    # work folders have the seed in their name, so look for the run under the marouter workdir
    root = cfg.WORKDIRS[variant]["DAY"].parent
    found = list(
        root.glob(
            f"{variant}_DAY*/iterative/closure_{sid}_seed{seed}/scenario_resolved.json"
        )
    )
    if not found:
        raise click.ClickException(f"no run '{sid}' with seed {seed} under {root}")
    info = json.loads(found[0].read_text())
    if "outputs" not in info:
        raise click.ClickException(f"'{sid}' seed {seed} is still running or failed")
    return info


def relative_diffs(ref: dict, other: dict, index: int) -> np.ndarray:
    diffs_by_seeds = []
    for e, vals in ref.items():
        if vals[0] < MIN_VEHICLES or e not in other:
            continue
        if vals[index]:
            diffs_by_seeds.append(abs(other[e][index] - vals[index]) / vals[index])
    return np.array(diffs_by_seeds)


@click.command()
@click.argument("runs", nargs=-1, required=True)
@click.option(
    "--variant", type=click.Choice(VARIANTS), default="MA_with_TLS", show_default=True
)
def cli(runs, variant):
    """RUNS as id:seed, the first one is the reference."""
    pairs = [r.split(":") for r in runs]
    infos = [run_info(variant, sid, seed) for sid, seed in pairs]
    edges = [edge_stats(Path(i["outputs"]["edgedata"])) for i in infos]

    print("global trip stats")
    for (sid, seed), info in zip(pairs, infos):
        print(f"\t{sid} (seed {seed}): {sim_stats(Path(info['outputs']['sim_out']), info['scale'], seed=info['seed'])}")
    ref_id = f"{pairs[0][0]} (seed {pairs[0][1]})"
    for name, index in (("vehicles entered", 0), ("time loss", 2)):
        print(
            f"\n{name}: relative difference from {ref_id}, edges with >= {MIN_VEHICLES} veh/day"
        )
        for (sid, seed), other in zip(pairs[1:], edges[1:]):
            d = relative_diffs(edges[0], other, index)
            p50, p90, p95 = np.percentile(d, [50, 90, 95]) * 100
            print(
                f"\t{sid} (seed {seed}): {len(d)} edges, p50 {p50:.1f}%  p90 {p90:.1f}%  p95 {p95:.1f}%"
            )


if __name__ == "__main__":
    cli()
