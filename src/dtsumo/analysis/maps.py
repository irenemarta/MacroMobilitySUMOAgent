"""
Per-edge values drawn on the network over an OSM basemap.
Geometries come from the parsed .edg.xml (lon,lat shapes), one line per edge (its first lane).

Detector heatmap (mean flow or speed per edge over the whole run):

    uv run python -m dtsumo.analysis.maps --det-out DIR --title "MA with TLS - DAY" [--metric speed]
"""

from pathlib import Path

import click
import contextily as cx
import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from shapely.geometry import LineString

import dtsumo.inputs.config as cfg
from dtsumo.tools.detectors import edges_as_lanes, load_all_detectors


def edge_geometries(edg_file: Path = cfg.EDG_PARSE_XML) -> gpd.GeoDataFrame:
    """One LineString per edge (index = edge id), in EPSG:3857 to match the basemap."""
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
    edges: gpd.GeoDataFrame = None,
) -> None:
    """values: Series indexed by edge id. Edges without a value are drawn in grey."""
    edges = edge_geometries() if edges is None else edges
    gdf = edges.join(values.rename("value"), how="inner")

    fig, ax = plt.subplots(figsize=(14, 14), constrained_layout=True)
    edges.plot(ax=ax, color="lightgrey", linewidth=0.5, zorder=1)
    gdf.plot(
        ax=ax,
        column="value",
        cmap=cmap,
        norm=norm,
        linewidth=2,
        legend=True,
        legend_kwds={"label": label, "shrink": 0.5},
        zorder=2,
    )
    cx.add_basemap(ax, source=cx.providers.CartoDB.Positron, zorder=0)
    ax.set_title(title, fontsize=16, fontweight="bold")
    ax.set_axis_off()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


@click.command(name="detector-heatmap")
@click.option("--det-out", "det_out", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True, help="Folder with det_*.xml outputs.")
@click.option("--metric", type=click.Choice(["flow", "speed"]), default="flow", show_default=True)
@click.option("--title", required=True)
@click.option("--out-dir", type=click.Path(path_type=Path), default=None, help="Default: cfg.IMAGES/maps.")
def heatmap_cli(det_out, metric, title, out_dir):
    out_dir = out_dir or (cfg.IMAGES / "maps")
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_all_detectors(det_out)
    column = "flow_veh_h" if metric == "flow" else "speed"
    values = df.groupby("edge_id")[column].mean()
    if metric == "flow":
        kwargs = {"label": "mean flow (veh/h)", "cmap": "Blues"}
    else:
        kwargs = {"label": "mean speed (m/s)", "cmap": "RdYlGn"}

    out_path = out_dir / f"heatmap_{metric}_{title.replace(' ', '_')}.png"
    plot_edge_map(values, f"Heatmap {metric} - {title}", out_path, **kwargs)
    click.echo(f"Heatmap saved in {out_path}")


if __name__ == "__main__":
    heatmap_cli()