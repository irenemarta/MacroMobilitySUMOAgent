"""
Bootstrap of the multi-seed runs: is there variability between seeds, and which scenario
effects are larger than it? Runs are found as in seed_noise.py (same DTSUMO_RUN folder).

1. Variability of the baseline between seeds (runs given as id:seed):

    DTSUMO_RUN=multiseed uv run python -m dtsumo.analysis.seed_bootstrap variability baseline:36 baseline:42 baseline:123 baseline:2026 baseline:99

   For each seed, bootstrap of the trips -> 95% CI of mean duration and time loss.
   If the seed means differ more than these CIs, the variability comes from the seed and
   not from the finite number of trips.

2. Effect of a scenario, paired by seed (runs given as scenario:seed:baseline):

    DTSUMO_RUN=multiseed uv run python -m dtsumo.analysis.seed_bootstrap effect telesio_closure:36:baseline telesio_closure:42:baseline \
        telesio_closure:123:baseline telesio_closure:2026:baseline telesio_closure:99:baseline --csv telesio_bootstrap.csv

   For each seed: scenario - baseline. Bootstrap of the seeds -> 95% CI of the mean effect,
   global and per edge. An edge effect is "solid" if the CI does not contain 0, the sign is the
   same in every seed and the change is at least MIN_REL of the baseline.
"""

import csv
import xml.etree.ElementTree as ET
from pathlib import Path

import click
import numpy as np

import dtsumo.inputs.config as cfg
from dtsumo.analysis.compare import edge_stats
from dtsumo.whatif.run import VARIANTS
from dtsumo.whatif.seed_noise import MIN_VEHICLES, run_info

N_BOOT = 2000
MIN_REL = 0.20  # an edge effect below 20% of the baseline is inside the seed noise (seed_noise.py)
rng = np.random.default_rng(0)


def local(path: str) -> Path:
    """Paths saved in scenario_resolved.json are absolute: if the outputs were moved to output/<run>,
    the same file is looked for under the simulation-out / marouter-workdir of the current run."""
    path = Path(path)
    if path.exists():
        return path
    for folder, root in (("simulation-out", cfg.SUMO_OUTPUT), ("marouter-workdir", cfg.MAROUTER_WORKDIR)):
        if folder in path.parts:
            return root.joinpath(*path.parts[path.parts.index(folder) + 1:])
    return path


def trips(info: dict) -> np.ndarray:
    """One row per trip of the final simulation: [duration s, time loss s]."""
    # new runs: TripInfo_*_seed<seed>.xml (as compare.sim_stats); older runs: TripInfo_*_scaled<scale>.xml
    sim_out = local(info["outputs"]["sim_out"])
    patterns = [f"TripInfo_*_seed{info.get('seed')}.xml", f"TripInfo_*scaled{info.get('scale')}.xml"]
    files = next((found for p in patterns if (found := list(sim_out.glob(p)))), [])
    if not files:
        raise click.ClickException(f"no {' or '.join(patterns)} in {sim_out}")
    rows = []
    for tripinfo in files:
        for _, el in ET.iterparse(tripinfo):
            if el.tag == "tripinfo":
                rows.append((float(el.get("duration", 0)), float(el.get("timeLoss", 0))))
                el.clear()
    return np.array(rows)


def boot_ci(values: np.ndarray, n_boot: int = N_BOOT) -> tuple:
    """Mean and 95% percentile CI of the mean, resampling the rows of values."""
    means = []
    for _ in range(n_boot):
        sample = values[rng.integers(0, len(values), len(values))]
        means.append(sample.mean(axis=0))
    low, high = np.percentile(means, [2.5, 97.5], axis=0)
    return values.mean(axis=0), low, high


def edge_names() -> dict:
    names = {}
    for _, el in ET.iterparse(cfg.NET_FILE):
        if el.tag == "edge":
            if el.get("function") != "internal":
                names[el.get("id")] = el.get("name", "")
            el.clear()
    return names


@click.group()
def cli():
    pass


@cli.command()
@click.argument("runs", nargs=-1, required=True)
@click.option("--variant", type=click.Choice(VARIANTS), default="MA_with_TLS", show_default=True)
def variability(runs, variant):
    """RUNS as id:seed, all of the same scenario."""
    print(f"bootstrap of the trips ({N_BOOT} resamples), 95% CI of the mean", flush=True)
    infos, means, half_widths = [], [], []
    for run in runs:
        sid, seed = run.split(":")
        info = run_info(variant, sid, seed)
        infos.append(info)
        values = trips(info)
        mean, low, high = boot_ci(values)
        means.append(mean)
        half_widths.append((high - low) / 2)
        print(f"\tseed {seed}: {len(values)} trips, duration {mean[0]:.1f} s [{low[0]:.1f}, {high[0]:.1f}], "
              f"time loss {mean[1]:.2f} s [{low[1]:.2f}, {high[1]:.2f}]", flush=True)

    means, half_widths = np.array(means), np.array(half_widths)
    print("\nbetween seeds")
    for i, name in enumerate(("duration", "time loss")):
        spread = means[:, i].max() - means[:, i].min()
        ci = half_widths[:, i].mean()
        print(f"\t{name}: seed means from {means[:, i].min():.2f} to {means[:, i].max():.2f} s "
              f"(std {means[:, i].std(ddof=1):.2f}), CI of a single seed +-{ci:.2f} s")
        verdict = "variability due to the seed" if spread > 2 * ci else "no variability beyond the trip sampling"
        print(f"\t\t-> spread {spread:.2f} s vs 2*CI {2 * ci:.2f} s: {verdict}")

    # edges: how much the daily vehicles of an edge change from seed to seed
    edges = [edge_stats(local(i["outputs"]["edgedata"])) for i in infos]
    common = [e for e in edges[0] if all(e in d for d in edges) and edges[0][e][0] >= MIN_VEHICLES]
    counts = np.array([[d[e][0] for d in edges] for e in common], dtype=float)
    cv = counts.std(axis=1, ddof=1) / counts.mean(axis=1)
    print(f"\nedges with >= {MIN_VEHICLES} veh/day: {len(common)}")
    print(f"\tcoefficient of variation between seeds: median {np.median(cv) * 100:.1f}%, "
          f"p90 {np.percentile(cv, 90) * 100:.1f}%, edges above 10%: {(cv > 0.10).sum()}")


@cli.command()
@click.argument("runs", nargs=-1, required=True)
@click.option("--variant", type=click.Choice(VARIANTS), default="MA_with_TLS", show_default=True)
@click.option("--csv", "csv_path", type=click.Path(path_type=Path), default=None, help="Save the per edge table.")
@click.option("--top", default=15, show_default=True, help="Solid edges to print.")
def effect(runs, variant, csv_path, top):
    """RUNS as scenario:seed:baseline, one per seed."""
    global_effects, scen_edges, base_edges = [], [], []
    for run in runs:
        sid, seed, base = run.split(":")
        scen, ref = run_info(variant, sid, seed), run_info(variant, base, seed)
        global_effects.append(trips(scen).mean(axis=0) - trips(ref).mean(axis=0))
        scen_edges.append(edge_stats(local(scen["outputs"]["edgedata"])))
        base_edges.append(edge_stats(local(ref["outputs"]["edgedata"])))
        print(f"\tseed {seed}: duration {global_effects[-1][0]:+.2f} s, time loss {global_effects[-1][1]:+.2f} s",
              flush=True)

    global_effects = np.array(global_effects)
    mean, low, high = boot_ci(global_effects)
    print(f"\nmean effect over {len(runs)} seeds (bootstrap of the seeds, 95% CI)")
    for i, name in enumerate(("duration", "time loss")):
        sure = "solid" if low[i] > 0 or high[i] < 0 else "not distinguishable from 0"
        print(f"\t{name}: {mean[i]:+.2f} s [{low[i]:+.2f}, {high[i]:+.2f}] -> {sure}")

    # per edge: vehicles entered, scenario - baseline in each seed
    names = edge_names()
    rows = []
    for e in base_edges[0]:
        base = np.array([d.get(e, [0])[0] for d in base_edges], dtype=float)
        if base.mean() < MIN_VEHICLES:
            continue
        diff = np.array([d.get(e, [0])[0] for d in scen_edges], dtype=float) - base
        m, lo, hi = boot_ci(diff.reshape(-1, 1), n_boot=1000)
        rel = m[0] / base.mean()
        same_sign = max((diff > 0).sum(), (diff < 0).sum())
        solid = (lo[0] > 0 or hi[0] < 0) and same_sign == len(diff) and abs(rel) >= MIN_REL
        rows.append({"edge": e, "via": names.get(e, ""), "baseline_veh": round(base.mean()),
                     "effect_veh": round(m[0]), "ci_low": round(lo[0]), "ci_high": round(hi[0]),
                     "effect_pct": round(rel * 100, 1), "same_sign": f"{same_sign}/{len(diff)}", "solid": solid})

    solid = sorted((r for r in rows if r["solid"]), key=lambda r: -abs(r["effect_veh"]))
    print(f"\nedges with >= {MIN_VEHICLES} veh/day: {len(rows)}, solid effects: {len(solid)}")
    for r in solid[:top]:
        print(f"\t{r['via'] or r['edge']:<32} {r['edge']:<16} {r['effect_veh']:+7d} veh "
              f"[{r['ci_low']:+d}, {r['ci_high']:+d}]  {r['effect_pct']:+.1f}%  sign {r['same_sign']}")
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda r: -abs(r["effect_veh"])))
        print(f"\nall edges in {csv_path}")


if __name__ == "__main__":
    cli()