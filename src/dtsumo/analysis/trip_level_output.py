"""
Trip-level analysis of one SUMO run, based on tripinfo and vehroute outputs.
- tripinfo output: one row per arrived vehicle (duration, route length, time loss, depart/arrival lane)
- vehroute output: the route actually driven, used for edge usage

    uv run python -m dtsumo.analysis.trip_level_output --tripinfo TripInfo_x.xml [--vehroutes VehTraces_x.xml] [--through EDGE ...]
"""

from collections import Counter
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

import click
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

import dtsumo.inputs.config as cfg
from dtsumo.analysis.maps import edge_geometries, plot_edge_map

TRIPINFO_FIELDS = ["depart", "arrival", "duration", "routeLength", "waitingTime", "timeLoss"]


def load_tripinfo(path: Path) -> pd.DataFrame:
    rows = []
    for _, el in ET.iterparse(path):
        if el.tag != "tripinfo":
            continue
        row = {
            "id": el.get("id"),
            "departLane": el.get("departLane", ""),
            "arrivalLane": el.get("arrivalLane", ""),
        }
        row.update({f: float(el.get(f, "nan")) for f in TRIPINFO_FIELDS})
        rows.append(row)
        el.clear()
    return pd.DataFrame(rows)


def load_vehroutes(path: Path) -> pd.DataFrame:
    """One row per vehicle with the driven route (space separated edge ids)."""
    rows = []
    for _, el in ET.iterparse(path):
        if el.tag != "vehicle":
            continue
        route = el.find("route")
        rows.append({
            "id": el.get("id"),
            "depart": float(el.get("depart", "nan")),
            "arrival": float(el.get("arrival", "nan")),
            "edges": route.get("edges", "") if route is not None else "",
        })
        el.clear()
    return pd.DataFrame(rows)


def edge_counts(df_routes: pd.DataFrame) -> pd.Series:
    """How many vehicles drove on each edge."""
    counter = Counter()
    for edges in df_routes["edges"]:
        counter.update(edges.split())
    return pd.Series(counter).sort_values(ascending=False)


def vehicles_through(df_routes: pd.DataFrame, edges: Iterable[str]) -> pd.DataFrame:
    """Vehicles whose driven route touches at least one of the given edges."""
    edges = set(edges)
    mask = df_routes["edges"].apply(lambda r: not edges.isdisjoint(r.split()))
    return df_routes[mask]


def print_duration_stats(df_trips: pd.DataFrame) -> None:
    click.echo(df_trips["duration"].describe().to_string())
    click.echo("\nLongest 20 trips:")
    longest = df_trips.nlargest(20, "duration")
    click.echo(longest[["id", "depart", "duration", "routeLength", "waitingTime"]].to_string(index=False))


def plot_travel_time_hist(df_trips: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    sns.histplot(df_trips["duration"] / 60, bins=30, ax=ax)
    ax.set_yscale("log")
    ax.set_title("Travel time distribution")
    ax.set_xlabel("Travel time (min)")
    ax.set_ylabel("N° vehicles (log scale)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_length_vs_travel_time(df_trips: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 6))
    sns.regplot(
        x=df_trips["routeLength"],
        y=df_trips["duration"] / 60,
        color="coral",
        scatter_kws={"alpha": 0.3, "s": 10},
        line_kws={"color": "red", "linewidth": 2},
        ax=ax,
    )
    ax.set_title("Route length vs travel time")
    ax.set_xlabel("Route length (m)")
    ax.set_ylabel("Travel time (min)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_trips_by_departure(df_trips: pd.DataFrame, out_path: Path, bin_min: int = 10, window: int = 3) -> None:
    """Mean route length and travel time of the trips departing in each bin (rolling-smoothed)."""
    bins = (df_trips["depart"] // (bin_min * 60)) * bin_min * 60
    binned = df_trips.groupby(bins).agg(length=("routeLength", "mean"), duration=("duration", "mean"))
    binned = binned.rolling(window, center=True, min_periods=1).mean()
    hours = binned.index / 3600

    fig, ax1 = plt.subplots(figsize=(8, 5))
    ax2 = ax1.twinx()
    ax1.plot(hours, binned["length"], color="#4472C4", linewidth=1.2)
    ax2.plot(hours, binned["duration"], color="#C0504D", linewidth=1.2)
    ax1.set_xlabel("Departure time (h)")
    ax1.set_ylabel("Average route length (m)", color="#4472C4")
    ax2.set_ylabel("Average travel time (s)", color="#C0504D")
    ax1.tick_params(axis="y", labelcolor="#4472C4")
    ax2.tick_params(axis="y", labelcolor="#C0504D")
    ax1.set_xlim(0, 24)
    ax1.set_xticks(range(0, 25, 2))
    ax1.grid(True, linestyle="--", alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_time_loss_by_hour(df_trips: pd.DataFrame, out_path: Path) -> None:
    hour = (df_trips["depart"] // 3600).astype(int)
    stats = df_trips.groupby(hour)["timeLoss"].agg(
        mean="mean", median="median", p90=lambda x: x.quantile(0.9)
    )
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(stats.index, stats["mean"], label="mean", color="orange")
    ax.plot(stats.index, stats["median"], label="median", color="green")
    ax.plot(stats.index, stats["p90"], label="90th percentile", color="red")
    ax.set_xlabel("Departure hour")
    ax.set_ylabel("timeLoss (s)")
    ax.set_title("Time loss by departure hour")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_source_sink_maps(df_trips: pd.DataFrame, out_dir: Path) -> None:
    """Where vehicles are inserted (source) and where they leave (sink), per edge, log scale."""
    edges = edge_geometries()
    counts = {
        name: df_trips[col].str.rsplit("_", n=1).str[0].value_counts()
        for name, col in [("Source", "departLane"), ("Sink", "arrivalLane")]
    }
    norm = mcolors.LogNorm(vmin=1, vmax=max(c.max() for c in counts.values()))
    cmap = sns.blend_palette(["red", "blue"], as_cmap=True)
    for name, values in counts.items():
        plot_edge_map(values, name, out_dir / f"{name.lower()}_map.png", "vehicles", cmap=cmap, norm=norm, edges=edges)


def plot_top_edges(counts: pd.Series, out_path: Path, n: int = 20) -> None:
    top = counts.head(n)
    fig, ax = plt.subplots(figsize=(12, 10))
    sns.barplot(x=top.values, y=top.index, hue=top.index, palette="viridis", legend=False, ax=ax)
    ax.set_title(f"The {n} most driven edges")
    ax.set_xlabel("Vehicles")
    ax.set_ylabel("SUMO edge id")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_edge_usage_distribution(counts: pd.Series, duration_min: float, out_path: Path) -> None:
    """Edges sorted by vehicles per minute: how concentrated the traffic is on the network."""
    per_min = (counts / duration_min).sort_values(ascending=False).values
    streets_pct = np.linspace(0, 100, len(per_min))

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.fill_between(streets_pct, per_min, color="#2b7bba")
    ax.plot(streets_pct, per_min, color="#1d5a8a", linewidth=1)
    ax.set_title("Distribution of vehicle passages on map edges")
    ax.set_xlabel("Streets [%]")
    ax.set_ylabel("Passages per minute [veh/min]")
    ax.set_xlim(-5, 105)
    ax.set_xticks(np.arange(0, 101, 5))
    ax.tick_params(axis="x", rotation=90)
    ax.grid(True, alpha=0.5, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


@click.command(name="trips")
@click.option("--tripinfo", "tripinfo_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--vehroutes", "vehroutes_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--through", "through_edges", multiple=True, help="Edge id (repeatable): count the vehicles driving on it. Needs --vehroutes.")
@click.option("--out-dir", type=click.Path(path_type=Path), default=None, help="Default: cfg.IMAGES/trips.")
def trips_cli(tripinfo_path, vehroutes_path, through_edges, out_dir):
    if through_edges and not vehroutes_path:
        raise click.UsageError("--through needs --vehroutes.")
    out_dir = out_dir or (cfg.IMAGES / "trips")
    out_dir.mkdir(parents=True, exist_ok=True)

    df_trips = load_tripinfo(tripinfo_path)
    click.echo(f"[trips] {len(df_trips)} trips in {tripinfo_path.name}")
    print_duration_stats(df_trips)
    plot_travel_time_hist(df_trips, out_dir / "travel_time_hist.png")
    plot_length_vs_travel_time(df_trips, out_dir / "length_vs_travel_time.png")
    plot_trips_by_departure(df_trips, out_dir / "length_and_time_by_departure.png")
    plot_time_loss_by_hour(df_trips, out_dir / "time_loss_by_hour.png")
    plot_source_sink_maps(df_trips, out_dir)

    if vehroutes_path:
        df_routes = load_vehroutes(vehroutes_path)
        counts = edge_counts(df_routes)
        duration_min = (df_routes["arrival"].max() - df_routes["depart"].min()) / 60
        plot_top_edges(counts, out_dir / "top_edges.png")
        plot_edge_usage_distribution(counts, duration_min, out_dir / "edge_usage_distribution.png")

        if through_edges:
            through = vehicles_through(df_routes, through_edges)
            click.echo(f"\n{len(through)} / {len(df_routes)} vehicles drive on {', '.join(through_edges)}")
            per_hour = (through["depart"] // 3600).astype(int).value_counts().sort_index()
            click.echo("by departure hour:\n" + per_hour.to_string())

    click.echo(f"\nPlots saved in {out_dir}")


if __name__ == "__main__":
    trips_cli()