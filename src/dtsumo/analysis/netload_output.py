"""
Marouter diagnostic based on netload output.
- netload output (one per incremental round): edges over capacity (flowCapacityRatio > 100)
- SUMO log: where teleports happen and whether they fall on edges marouter already saw as saturated
- route file: how many vehicles got more than one route alternative

    uv run python -m dtsumo.analysis.netload WORKDIR/incremental/netload_r*.xml [--log sumo_run.log] [--routes marouter_output.rou.xml]

The saturation table and the capacity map use the last round.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Tuple
import xml.etree.ElementTree as ET

import click
import pandas as pd
import sumolib
from matplotlib.colors import Normalize

import dtsumo.inputs.config as cfg
from dtsumo.analysis.maps import plot_edge_map

TELEPORT_PATTERN = re.compile(r"Teleporting vehicle '([^']+)'.*?lane='([^']+)'")


def load_netload(path: Path) -> pd.DataFrame:
    """One row per (interval, edge), every numeric attribute of the edge as a column."""
    rows = []
    for interval in ET.parse(path).getroot().findall("interval"):
        for edge in interval.findall("edge"):
            row = {k: float(v) for k, v in edge.attrib.items() if k != "id"}
            row.update(id=edge.get("id"), begin=float(interval.get("begin")), end=float(interval.get("end")))
            rows.append(row)
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str) -> None:
    ratio = df["flowCapacityRatio"]
    excess = (ratio - 100).clip(lower=0).sum()
    click.echo(f"--- {label} ---")
    click.echo(f"\tedges >100%: {(ratio > 100).sum()} / {len(df)}")
    click.echo(f"\tedges >150%: {(ratio > 150).sum()}")
    click.echo(f"\ttotal excess (sum of ratio-100 on saturated edges): {excess:.0f}")
    click.echo(f"\tmedian flowCapacityRatio: {ratio.median():.1f}")
    click.echo(f"\tmean speed weighted by entered vehicles: {(df['speed'] * df['entered']).sum() / df['entered'].sum():.1f} m/s")


def saturated_edges(df: pd.DataFrame, net, threshold: float = 100) -> pd.DataFrame:
    """Edges whose max flowCapacityRatio is above threshold, with their network attributes."""
    ratio = df.groupby("id")["flowCapacityRatio"].max()
    rows = []
    for eid, value in ratio[ratio > threshold].items():
        if not net.hasEdge(eid):
            continue
        e = net.getEdge(eid)
        rows.append({
            "edge": eid,
            "flowCapacityRatio": value,
            "priority": e.getPriority(),
            "numLanes": e.getLaneNumber(),
            "speed": e.getSpeed(),
        })
    columns = ["edge", "flowCapacityRatio", "priority", "numLanes", "speed"]
    return pd.DataFrame(rows, columns=columns).sort_values("flowCapacityRatio", ascending=False)


def teleports_from_log(log_path: Path) -> Tuple[Counter, Counter]:
    """Teleport events per vehicle and per edge, read from the SUMO log."""
    by_vehicle, by_edge = Counter(), Counter()
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = TELEPORT_PATTERN.search(line)
            if m:
                veh_id, lane_id = m.groups()
                by_vehicle[veh_id] += 1
                by_edge[lane_id.rsplit("_", 1)[0]] += 1
    return by_vehicle, by_edge


def base_street_id(edge_id: str) -> str:
    """'-121952561#2' -> '121952561' (the OSM way the edge comes from)."""
    m = re.match(r"-?(\d+)", edge_id)
    return m.group(1) if m else edge_id


def route_alternatives(rou_path: Path) -> None:
    single, multi, no_dist = 0, 0, 0
    root = ET.parse(rou_path).getroot()
    for veh in root.findall("vehicle") + root.findall("trip"):
        dist = veh.find("routeDistribution")
        if dist is None:
            no_dist += veh.find("route") is not None
        elif len(dist.findall("route")) <= 1:
            single += 1
        else:
            multi += 1
    total = single + multi + no_dist
    click.echo(f"\nRoute alternatives in {rou_path.name} ({total} vehicles with a route):")
    click.echo(f"\t1 alternative: {single} ({single / total:.1%})")
    click.echo(f"\t>1 alternatives: {multi} ({multi / total:.1%})")
    click.echo(f"\tno routeDistribution: {no_dist}")


def _round_order(path: Path):
    # netload_r10 after netload_r9
    return [int(n) for n in re.findall(r"\d+", path.stem)]


@click.command(name="netload")
@click.argument("netloads", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--log", "log_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="SUMO log of the run, for teleports.")
@click.option("--routes", "rou_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="marouter route file, for route alternatives.")
@click.option("--out-dir", type=click.Path(path_type=Path), default=None, help="Default: cfg.IMAGES/netload.")
def netload_cli(netloads, log_path, rou_path, out_dir):
    out_dir = out_dir or (cfg.IMAGES / "netload")
    out_dir.mkdir(parents=True, exist_ok=True)

    netloads = sorted(netloads, key=_round_order)
    for path in netloads:
        df = load_netload(path)
        summarize(df, path.name)

    # df is the last round
    net = sumolib.net.readNet(str(cfg.NET_FILE))
    saturated = saturated_edges(df, net)
    click.echo(f"\nSaturated edges in {netloads[-1].name} (top 30):")
    click.echo(saturated.head(30).to_string(index=False))
    network_priority = sum(e.getPriority() for e in net.getEdges()) / len(net.getEdges())
    click.echo(f"mean priority: saturated {saturated['priority'].mean():.1f}, whole network {network_priority:.1f}")

    out_path = out_dir / f"capacity_map_{netloads[-1].stem}.png"
    plot_edge_map(
        df.groupby("id")["flowCapacityRatio"].max(),
        f"Max flowCapacityRatio - {netloads[-1].name}",
        out_path,
        "flowCapacityRatio (%)",
        norm=Normalize(vmin=0, vmax=100),
    )
    click.echo(f"Capacity map saved in {out_path}")

    if log_path:
        by_vehicle, by_edge = teleports_from_log(log_path)
        total = sum(by_edge.values())
        click.echo(f"\nTeleports in {log_path.name}: {total}")
        click.echo("top 20 edges:")
        for edge, count in by_edge.most_common(20):
            click.echo(f"\t{edge}: {count}")
        streets = Counter()
        for edge, count in by_edge.items():
            streets[base_street_id(edge)] += count
        click.echo("top 15 streets (OSM way):")
        for street, count in streets.most_common(15):
            click.echo(f"\t{street}: {count}")
        click.echo("vehicles teleported most often:")
        for veh, count in by_vehicle.most_common(10):
            click.echo(f"\t{veh}: {count}")

        overlap = set(saturated["edge"]) & set(by_edge)
        on_saturated = sum(by_edge[e] for e in overlap)
        click.echo(f"saturated edges with teleports: {len(overlap)}/{len(saturated)}")
        if total:
            click.echo(f"share of teleports on saturated edges: {on_saturated / total:.1%}")

    if rou_path:
        route_alternatives(rou_path)


if __name__ == "__main__":
    netload_cli()