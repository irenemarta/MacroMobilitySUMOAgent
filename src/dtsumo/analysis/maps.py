"""
Heatmaps: per-edge values drawn on the network (all edges in grey underneath, no basemap).
Geometries come from the parsed .edg.xml (lon,lat shapes), one line per edge (its first lane).
Detector heatmap (mean flow or speed per edge over the whole run):
    uv run python -m dtsumo.analysis.maps detector-heatmap --det-out DIR --title "MA with TLS - DAY" [--metric speed] [--from 08:00 --to 09:00]
    several runs on the same scale: --det-out DIR1 --label baseline --det-out DIR2 --label tls_off
Effect of a scenario over the seeds (per-edge table of seed_bootstrap effect or compare --seeds):
    uv run python -m dtsumo.analysis.maps effect-map --csv effect_speed30.csv --title "speed30" [--all-edges]
"""

import click
import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
import pandas as pd
from pathlib import Path
from shapely.geometry import LineString

import dtsumo.inputs.config as cfg
from dtsumo.tools.detectors import edges_as_lanes, load_all_detectors


def edge_geometries(edg_file: Path = cfg.EDG_PARSE_XML) -> gpd.GeoDataFrame:
    """One LineString per edge (index = edge id), in EPSG:3857 (metres)."""
    shapes = edges_as_lanes(edg_file).groupby("id_edge")["shape"].first()
    geometry = shapes.apply(
        lambda s: LineString([tuple(map(float, c.split(","))) for c in s.split()])
    )
    return gpd.GeoDataFrame(geometry=geometry, crs="EPSG:4326").to_crs(epsg=3857)


def plot_edge_map(
    values: pd.Series,
    title: str,
    out_path: Path,
    label: str,
    cmap="RdYlGn_r",
    norm=None,
    vmin=None,
    vmax=None,
    widths=None,
    edges: gpd.GeoDataFrame = None,
) -> None:
    """values: Series indexed by edge id; widths: optional Series of line widths (same index).
    Edges without a value are drawn in grey (no basemap: the tile servers are not reachable from the server).
    """
    edges = edge_geometries() if edges is None else edges
    gdf = edges.join(values.rename("value"), how="inner")
    linewidth = (
        gdf.index.map(widths).fillna(0.5).to_numpy() if widths is not None else 2
    )

    fig, ax = plt.subplots(figsize=(14, 14), constrained_layout=True)
    edges.plot(ax=ax, color="lightgrey", linewidth=0.5, zorder=1)
    gdf.plot(
        ax=ax,
        column="value",
        cmap=cmap,
        norm=norm,
        vmin=None if norm else vmin,
        vmax=None if norm else vmax,
        linewidth=linewidth,
        legend=True,
        legend_kwds={"label": label, "shrink": 0.5},
        zorder=2,
    )
    ax.set_title(title, fontsize=16, fontweight="bold")
    ax.set_axis_off()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


# low flow red, high flow blue (purple in between), as in the heatmaps of the assignment chapter
FLOW_CMAP = LinearSegmentedColormap.from_list("red_blue", ["red", "blue"])


@click.command(name="detector-heatmap")
@click.option(
    "--det-out",
    "det_outs",
    multiple=True,
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Folder with det_*.xml outputs. Repeat it (with --label) to draw several maps on the same scale.",
)
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Name of each --det-out, in the same order (default: folder name).",
)
@click.option(
    "--metric", type=click.Choice(["flow", "speed"]), default="flow", show_default=True
)
@click.option("--title", required=True)
@click.option(
    "--from",
    "from_",
    help="Start of the time window, HH:MM (with --to). Default: whole run.",
)
@click.option("--to", help="End of the time window, HH:MM (with --from).")
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Default: cfg.IMAGES/maps.",
)
def heatmap_cli(det_outs, labels, metric, title, from_, to, out_dir):
    if bool(from_) != bool(to):
        raise click.UsageError("give both --from and --to, or neither")
    if labels and len(labels) != len(det_outs):
        raise click.UsageError("give one --label for each --det-out")
    labels = labels or [d.name for d in det_outs]
    out_dir = out_dir or (cfg.IMAGES / "maps")
    out_dir.mkdir(parents=True, exist_ok=True)

    means = {}
    for name, det_out in zip(labels, det_outs):
        df = load_all_detectors(det_out)
        if from_:
            begin, end = (int(t[:2]) * 3600 + int(t[3:]) * 60 for t in (from_, to))
            df = df[(df["begin"] >= begin) & (df["end"] <= end)]
            if df.empty:
                raise click.ClickException(
                    f"no detector interval inside {from_}-{to} in {det_out}"
                )
        means[name] = df.groupby("edge_id")[["flow_veh_h", "speed"]].mean()

    # same scale for every map, so that the maps can be compared by eye
    column = "flow_veh_h" if metric == "flow" else "speed"
    vmax = max(m[column].max() for m in means.values())
    max_flow = max(m["flow_veh_h"].max() for m in means.values())
    for name, m in means.items():
        out_path = out_dir / f"heatmap_{metric}_{title.replace(' ', '_')}_{name}.png"
        plot_edge_map(
            m[column],
            f"Heatmap {metric} - {title} - {name}",
            out_path,
            label="mean flow (veh/h)" if metric == "flow" else "mean speed (m/s)",
            cmap=FLOW_CMAP if metric == "flow" else "RdYlGn",
            vmin=0,
            vmax=vmax,
            widths=m["flow_veh_h"]
            / max_flow
            * 5,  # line width proportional to the flow
        )
        click.echo(f"Heatmap saved in {out_path}")


@click.command(name="effect-map")
@click.option(
    "--csv",
    "csv_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Per-edge table written by seed_bootstrap effect --csv or compare --seeds --csv.",
)
@click.option("--title", required=True)
@click.option(
    "--all-edges",
    is_flag=True,
    help="Draw every edge of the table, not only the solid ones.",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Default: cfg.IMAGES/maps.",
)
def effect_map_cli(csv_path, title, all_edges, out_dir):
    out_dir = out_dir or (cfg.IMAGES / "maps")
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(csv_path, dtype={"edge": str})
    if not all_edges:
        df = df[df["solid"]]
    if df.empty:
        click.echo(f"no solid edge in {csv_path}: no map")
        return
    # same colour scale for every scenario: changes beyond +-100% get the darkest colour
    values = df.set_index("edge")["effect_pct"].clip(-100, 100)
    kind = "all edges" if all_edges else f"{len(df)} solid edges"
    out_path = out_dir / f"effect_{title.replace(' ', '_')}.png"
    plot_edge_map(
        values,
        f"Effect on vehicles - {title} ({kind})",
        out_path,
        label="change in vehicles (%), clipped at +-100%",
        cmap="RdBu_r",
        norm=TwoSlopeNorm(vcenter=0, vmin=-100, vmax=100),
    )
    click.echo(f"Effect map saved in {out_path}")


@click.group()
def cli():
    pass


cli.add_command(heatmap_cli)
cli.add_command(effect_map_cli)

if __name__ == "__main__":
    cli()
