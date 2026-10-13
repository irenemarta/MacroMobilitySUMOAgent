"""
Validates simulation's edge output against real sensor data using:
- GEH statistic: standard transportation-engineering measure for
    comparing modeled vs observed flow (GEH<=5 good, 5-10 caution, >10 poor)
- Validation using an "affinity index" (as in TuST - Rapelli et al. 2018) per sensor.

Real sensor data is the ground truth used to validate both meso or micro simulations.
"""

import math
from pathlib import Path
from typing import List, Tuple, Optional
import xml.etree.ElementTree as ET

import click
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.dtsumo.analysis.sensors import DATE_FORMAT, pasta_db_merge, standardize_datetime
from dtsumo.modules.entities import DataFrameSchemaPasta

GEH_OK = 5.0
GEH_BAD = 10.0

MIN_GOOD_HOURS_FOR_THRESHOLD = 10  # sotto questa soglia il p90 interno non è affidabile


def prepare_pasta_data(anagraphics: pd.DataFrame, flows: pd.DataFrame) -> pd.DataFrame:
    df = pasta_db_merge(anagraphics, flows)

    already_iso = df["daytime"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$").all()
    if not already_iso:
        df = standardize_datetime(df, "daytime", DATE_FORMAT)

    if "count_all" in df.columns and "PASTA_count" not in df.columns:
        df = df.rename(columns={"count_all": "PASTA_count"})

    schema_columns = list(DataFrameSchemaPasta.to_schema().columns)
    missing = [c for c in schema_columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"Missing columns after pasta_db_merge(): {missing}. "
            f"Available columns: {sorted(df.columns)}"
        )
    df = df[schema_columns]

    return DataFrameSchemaPasta.validate(df)


def validate_sensor_df(df: pd.DataFrame) -> pd.DataFrame:
    return DataFrameSchemaPasta.validate(df)


def load_validated_sensor_data(path: Path) -> pd.DataFrame:
    return validate_sensor_df(pd.read_csv(path))


def load_edgedata(edgedata_file: Path, edge_ids: Optional[List[str]] = None) -> pd.DataFrame:
    """
    Parses a SUMO edgeData dump into one row per (edge, interval): density
    (veh/km), flow (veh/h, derived from the `entered` vehicle count over
    the interval duration) and speed.
    """
    tree = ET.parse(edgedata_file)
    rows = []
    for interval in tree.getroot().findall("interval"):
        begin = float(interval.get("begin"))
        end = float(interval.get("end"))
        duration_h = (end - begin) / 3600.0
        for edge in interval.findall("edge"):
            eid = edge.get("id")
            if edge_ids is not None and eid not in edge_ids:
                continue
            entered = edge.get("entered", "0")
            rows.append({
                "edge_id": eid,
                "begin": begin,
                "end": end,
                "density": float(edge.get("density", "nan")),
                "flow_veh_h": (float(entered) / duration_h) if duration_h > 0 else float("nan"),
                "speed": float(edge.get("speed", "nan")),
            })
    return pd.DataFrame(rows)


def compute_geh(sim_flow: float, real_flow: float) -> float:
    """
    GEH statistic between one simulated and one observed flow value (veh/h).
    0 = perfect match.
    """
    total = sim_flow + real_flow
    if total <= 0:
        return 0.0
    return float(np.sqrt(2 * (sim_flow - real_flow) ** 2 / total))


def hourly_sumo_flow(edgedata_df: pd.DataFrame, edge_id: str) -> Tuple[pd.DataFrame, pd.Series]:
    """
    Aggregates fine-grained (e.g. 5-min) SUMO flow samples for one edge
    into one mean value per hour of day (0-23).
    Keeping the raw samples for the scatter-cloud comparison plot.
    """
    sub = edgedata_df[edgedata_df["edge_id"] == edge_id].copy()
    sub["hour"] = (sub["begin"] // 3600).astype(int)
    hourly_mean = sub.groupby("hour")["flow_veh_h"].mean()
    return sub, hourly_mean


def hourly_real_flow(sensor_df: pd.DataFrame, cod_sens: int) -> pd.Series:
    """
    Real PASTA flow for one sensor, averaged across every considered day in sensor_df.
    One value per hour of day (0-23).
    sensor_df is the output of wave_speed_calibration.prepare_pasta_data() / load_validated_sensor_data().
    """
    sub = sensor_df[sensor_df["Cod_sens"] == cod_sens].copy()
    sub["hour_int"] = sub["hour"].str.slice(0, 2).astype(int)
    return sub.groupby("hour_int")["PASTA_count"].mean()


def real_samples_by_hour(sensor_df: pd.DataFrame, cod_sens: int) -> pd.DataFrame:
    """
    Same filtering as hourly_real_flow, but WITHOUT averaging across days —
    one row per (day, hour) sample. Used only by plot_flows_grid for the
    mean±band plot, never for GEH/affinity (che restano su hourly_real_flow).
    """
    sub = sensor_df[sensor_df["Cod_sens"] == cod_sens].copy()
    sub["hour_int"] = sub["hour"].str.slice(0, 2).astype(int)
    return sub[["hour_int", "PASTA_count"]]


def geh_by_hour(sim_hourly: pd.Series, real_hourly: pd.Series) -> pd.Series:
    """GEH for every hour present on both sides (hours missing on either side are skipped)."""
    common_hours = sorted(set(sim_hourly.index) & set(real_hourly.index))
    return pd.Series(
        {h: compute_geh(sim_hourly[h], real_hourly[h]) for h in common_hours}
    ).sort_index()


def affinity_by_hour(sim_hourly: pd.Series, real_hourly: pd.Series) -> pd.Series:
    """
    Affinity index, computed as in Rapelli et al. paper, Sec. 5.2.
    """
    common_hours = sorted(
        h for h in set(sim_hourly.index) & set(real_hourly.index) if real_hourly[h] > 0
    )
    return pd.Series(
        {
            h: max(0.0, min(100.0, 100 * (1 - abs(sim_hourly[h] - real_hourly[h]) / real_hourly[h])))
            for h in common_hours
        }
    ).sort_index()


def _sensor_edge_pairs(
    edgedata_df: pd.DataFrame, sensor_df: pd.DataFrame, lookup_path: Path
) -> List[Tuple[int, str]]:
    """
    Check if sensor_lane_lookup.csv has correspondence to map edges.
    Returns edge-sensor pairs.
    """
    lookup = pd.read_csv(lookup_path)[["Cod_sens", "id_edge"]].drop_duplicates()
    available_edges = set(edgedata_df["edge_id"].unique())
    available_sensors = set(sensor_df["Cod_sens"].unique())

    pairs = []
    for cod_sens, edge_id in lookup.itertuples(index=False):
        if str(edge_id) in available_edges and cod_sens in available_sensors:
            pairs.append((cod_sens, str(edge_id)))
    return pairs


def plot_geh_bars(geh: pd.Series, sensor_label: str, out_path: Path) -> None:
    colors = ["red" if v > GEH_BAD else "gold" if v > GEH_OK else "green" for v in geh.values]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(geh.index, geh.values, color=colors, edgecolor="black")
    ax.axhline(GEH_OK, color="red", linestyle="--", label=f"Soglia di Validazione (GEH = {GEH_OK:.0f})")
    ax.axhline(GEH_BAD, color="darkred", linestyle=":", label=f"Errore Grave (GEH = {GEH_BAD:.0f})")
    ax.set_xlabel("Time (h)")
    ax.set_ylabel("GEH Index")
    ax.set_title(f"GEH index - Sensor {sensor_label}")
    ax.set_xticks(range(24))
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_flow_comparison(
    sim_samples: pd.DataFrame, real_hourly: pd.Series, sensor_label: str, out_path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(sim_samples["hour"], sim_samples["flow_veh_h"], label="SUMO Flow", alpha=0.6)
    ax.scatter(real_hourly.index, real_hourly.values, label="Real Flow", color="orange")
    ax.set_xlabel("Time (h)")
    ax.set_ylabel("flow")
    ax.set_title(f"Flow on {sensor_label}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# Subplot standard dimension management
WIDTH_PER_SUBPLOT = 5.0
HEIGHT_PER_SUBPLOT = 3.5
DEFAULT_MAX_PER_PAGE = 10


def _grid_pages(
    sens_edge_pairs: List[Tuple[int, str]], max_per_page: int = DEFAULT_MAX_PER_PAGE
) -> List[List[Tuple[int, str]]]:
    if max_per_page < 1:
        raise ValueError("ERROR: max_per_page should be >1.")
    return [
        sens_edge_pairs[i : i + max_per_page]
        for i in range(0, len(sens_edge_pairs), max_per_page)
    ]


def plot_geh_grid(
    sens_edge_pairs: List[Tuple[int, str]],
    edgedata_df: pd.DataFrame,
    sensor_df: pd.DataFrame,
    out_dir: Path,
    filename_prefix: str = "geh_grid",
    max_per_page: int = DEFAULT_MAX_PER_PAGE,
) -> List[Path]:
    
    if not sens_edge_pairs:
        raise ValueError("Nessuna coppia sensore/edge da plottare.")
    out_dir.mkdir(parents=True, exist_ok=True)

    written_paths = []
    for page_idx, page_pairs in enumerate(_grid_pages(sens_edge_pairs, max_per_page), start=1):
        n_cols = math.ceil(len(page_pairs) / 2)
        fig, axs = plt.subplots(
            2, n_cols,
            figsize=(WIDTH_PER_SUBPLOT * n_cols, HEIGHT_PER_SUBPLOT * 2),
            constrained_layout=True, squeeze=False, sharex=True,
        )
        axs_flat = axs.flatten()

        for ax, (cod_sens, edge_id) in zip(axs_flat, page_pairs):
            _, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
            real_hourly = hourly_real_flow(sensor_df, cod_sens)
            geh = geh_by_hour(sim_hourly, real_hourly)

            colors = ["red" if v > GEH_BAD else "gold" if v > GEH_OK else "green" for v in geh.values]
            ax.bar(geh.index, geh.values, color=colors, edgecolor="black")
            ax.axhline(GEH_OK, color="red", linestyle="--", linewidth=1)
            ax.axhline(GEH_BAD, color="darkred", linestyle=":", linewidth=1)
            ax.set_xlabel("Time (h)")
            ax.set_ylabel("GEH Index")
            ax.set_title(f"Sensor {cod_sens} ({edge_id})", fontsize=10)
            ax.set_xticks(range(0, 24, 4))

        for ax in axs_flat[len(page_pairs):]:
            ax.set_visible(False)

        handles = [
            plt.Line2D([0], [0], color="red", linestyle="--", label=f"Soglia validazione (GEH={GEH_OK:.0f})"),
            plt.Line2D([0], [0], color="darkred", linestyle=":", label=f"Errore grave (GEH={GEH_BAD:.0f})"),
        ]
        fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.05))

        out_path = out_dir / f"{filename_prefix}_page{page_idx}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        written_paths.append(out_path)

    return written_paths

def plot_flows_grid(
    sens_edge_pairs: List[Tuple[int, str]],
    edgedata_df: pd.DataFrame,
    sensor_df: pd.DataFrame,
    out_dir: Path,
    filename_prefix: str = "flow_grid",
    max_per_page: int = DEFAULT_MAX_PER_PAGE,
    band: str = "std",  # "std" (media ± dev.std) oppure "minmax" (range osservato)
) -> List[Path]:

    if not sens_edge_pairs:
        raise ValueError("ERROR: no edge-sensor pair to plot.")
    out_dir.mkdir(parents=True, exist_ok=True)

    written_paths = []
    for page_idx, page_pairs in enumerate(_grid_pages(sens_edge_pairs, max_per_page), start=1):
        n_cols = math.ceil(len(page_pairs) / 2)
        fig, axs = plt.subplots(
            2, n_cols,
            figsize=(WIDTH_PER_SUBPLOT * n_cols, HEIGHT_PER_SUBPLOT * 2),
            constrained_layout=True, squeeze=False, sharex=True,
        )
        axs_flat = axs.flatten()

        for ax, (cod_sens, edge_id) in zip(axs_flat, page_pairs):
            sim_samples, _ = hourly_sumo_flow(edgedata_df, edge_id)
            real_samples = real_samples_by_hour(sensor_df, cod_sens)

            sim_agg = sim_samples.groupby("hour")["flow_veh_h"].agg(["mean", "std", "min", "max"])
            real_agg = real_samples.groupby("hour_int")["PASTA_count"].agg(["mean", "std", "min", "max"])

            for agg, color, label in [(sim_agg, "tab:blue", "SUMO"), (real_agg, "orange", "Real")]:
                ax.plot(agg.index, agg["mean"], color=color, marker="o", markersize=3, linewidth=1, label=f"{label} Flow")
                if band == "std":
                    lower = agg["mean"] - agg["std"].fillna(0)
                    upper = agg["mean"] + agg["std"].fillna(0)
                else:
                    lower, upper = agg["min"], agg["max"]
                ax.fill_between(agg.index, lower, upper, color=color, alpha=0.2)

            ax.set_xlabel("Time (h)")
            ax.set_ylabel("flow")
            ax.set_title(f"Sensor {cod_sens} ({edge_id})", fontsize=10)

        for ax in axs_flat[len(page_pairs):]:
            ax.set_visible(False)

        handles = [
            plt.Line2D([0], [0], color="tab:blue", marker="o", label="SUMO Flow (media ± banda)"),
            plt.Line2D([0], [0], color="orange", marker="o", label="Real Flow (media ± banda)"),
        ]
        fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.05))

        out_path = out_dir / f"{filename_prefix}_page{page_idx}.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        written_paths.append(out_path)

    return written_paths

def validate_edge_against_sensor(
    edgedata_df: pd.DataFrame,
    edge_id: str,
    sensor_df: pd.DataFrame,
    cod_sens: int,
    out_dir: Path,
) -> pd.Series:
    """
    Runs the full GEH validation for one edge/sensor pair, saves both reference plots.
    Returns the per-hour GEH series.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sim_samples, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
    real_hourly = hourly_real_flow(sensor_df, cod_sens)
    geh = geh_by_hour(sim_hourly, real_hourly)

    plot_geh_bars(geh, str(cod_sens), out_dir / f"geh_{edge_id}.png")
    plot_flow_comparison(sim_samples, real_hourly, str(cod_sens), out_dir / f"flow_{edge_id}.png")
    return geh

def plot_affinity_distribution(per_sensor_affinity: pd.Series, out_path: Path) -> dict:
    sorted_aff = per_sensor_affinity.sort_values(ascending=False).reset_index(drop=True)
    n = len(sorted_aff)
    if n == 0:
        raise ValueError("No sensor to plot.")

    percentile = [100 * i / n for i in range(n)] + [100]          # chiude fino a 100%
    values = list(sorted_aff.values) + [sorted_aff.values[-1]]     # ripete l'ultimo valore

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.step(percentile, values, where="post", color="tab:blue", linewidth=1.5)
    ax.fill_between(percentile, values, step="post", alpha=0.25, color="tab:blue")
    ax.set_xlabel("Sensors [%]")
    ax.set_ylabel("Affinity [%]")
    ax.set_ylim(0, 100)
    ax.set_xlim(0, 100)
    ax.set_title(f"Quantitative validation analysis on sensors (n={n})")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    return {
        "pct_above_75": float(100 * (sorted_aff > 75).mean()),
        "pct_below_50": float(100 * (sorted_aff < 50).mean()),
    }


def validate_all_sensors(
    edgedata_df: pd.DataFrame, sensor_df: pd.DataFrame, lookup_path: Path, out_dir: Path, cv_threshold: float = None,
) -> pd.DataFrame:
    """
    Validate each edge-sensor pair by plotting GEH, flows per edge, affinity index.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = _sensor_edge_pairs(edgedata_df, sensor_df, lookup_path)
    if not pairs:
        click.echo("[sensor_validation] no validated edges (no edge-sensor matches with available data.)")
        return pd.DataFrame()

    rows = []
    affinity_per_sensor = {}
    for cod_sens, edge_id in pairs:
        geh = validate_edge_against_sensor(edgedata_df, edge_id, sensor_df, cod_sens, out_dir)
        for hour, value in geh.items():
            rows.append({"edge_id": edge_id, "Cod_sens": cod_sens, "hour": hour, "geh": value})

        _, sim_hourly = hourly_sumo_flow(edgedata_df, edge_id)
        real_hourly = hourly_real_flow(sensor_df, cod_sens)
        affinity_h = affinity_by_hour(sim_hourly, real_hourly)
        if len(affinity_h) > 0:
            affinity_per_sensor[cod_sens] = {
                "edge_id": edge_id,
                "affinity_pct": affinity_h.mean(),
                "n_hours": len(affinity_h),
            }

    summary = pd.DataFrame(rows)
    if summary.empty:
        click.echo("[sensor_validation] no edge-sensor data.")
        return summary

    pct_ok = 100 * (summary["geh"] <= GEH_OK).mean()
    pct_caution = 100 * ((summary["geh"] > GEH_OK) & (summary["geh"] <= GEH_BAD)).mean()
    pct_bad = 100 * (summary["geh"] > GEH_BAD).mean()
    click.echo(
        f"[sensor_validation] {len(pairs)} validated edges, {len(summary)} sensors' hour samples."
    )
    click.echo(f"\t{pct_ok:.1f}% having GEH<={GEH_OK:.0f} (good fit)")
    click.echo(f"\t{pct_caution:.1f}% having {GEH_OK:.0f}<GEH<={GEH_BAD:.0f} (wrning)")
    click.echo(f"\t{pct_bad:.1f}% having GEH>{GEH_BAD:.0f} (wrong fit)")

    # worst = summary.groupby("edge_id")["geh"].mean().sort_values(ascending=False).head(10)
    # click.echo("\tpeggiori 10 edge (GEH medio):")
    # for eid, v in worst.items():
    #     click.echo(f"\t  {eid}: {v:.1f}")

    geh_grid_paths = plot_geh_grid(pairs, edgedata_df, sensor_df, out_dir)
    click.echo(f"\nGEH grid saved in: {', '.join(str(p) for p in geh_grid_paths)}")

    flow_grid_paths = plot_flows_grid(pairs, edgedata_df, sensor_df, out_dir)
    click.echo(f"\nFlows grid saved in: {', '.join(str(p) for p in flow_grid_paths)}")

    if affinity_per_sensor:
        affinity_df = (
            pd.DataFrame.from_dict(affinity_per_sensor, orient="index")
            .rename_axis("Cod_sens")
            .reset_index()
            .sort_values("affinity_pct", ascending=False)
        )

        affinity_plot_path = out_dir / "affinity_distribution.png"
        affinity_summary_plot = plot_affinity_distribution(
            affinity_df.set_index("Cod_sens")["affinity_pct"], affinity_plot_path
        )
        click.echo(f"\nAffinity plot saved in: {affinity_plot_path}")
        click.echo(
            f"\t{affinity_summary_plot['pct_above_75']:.0f}% sensor having affinity >75% (paper TuST: 80%)"
        )
        click.echo(
            f"\t{affinity_summary_plot['pct_below_50']:.0f}% sensor having affinity <50% (paper TuST: 5%)"
        )

        affinity_csv_path = out_dir / "affinity_by_sensor.csv"
        affinity_df.to_csv(affinity_csv_path, index=False)
        click.echo(
            f"\naffinity by sensor (n={len(affinity_df)}")
        
    return summary


@click.command(name="sensor-validation")
@click.option("--edgedata", "edgedata_path", type=click.Path(exists=True, path_type=Path), required=True, help="edgeData XML")
@click.option("--edge", "edge_id", default=None, help="To validate a single edge.")
@click.option("--from-sensors", is_flag=True, help="Validate all edges corresponding to a sensor in the lookup file.")
@click.option("--sensor-data", "sensor_data_path", type=click.Path(exists=True, path_type=Path), default=None, help="CSV containing real data, output of prepare_pasta_data().")
@click.option("--pasta-anagraphics", "anagraphics_path", type=click.Path(exists=True, path_type=Path), default=None, help="CSV of PASTA sensors anagraphics. To be used with --pasta-flows.")
@click.option("--pasta-flows", "flows_path", type=click.Path(exists=True, path_type=Path), default=None, help="CSV of real registered flows from PASTA. To be used --pasta-anagraphics.")
@click.option("--out-dir", type=click.Path(path_type=Path), default=None, help="Personalised output path. Default: cfg.IMAGES/sensor_validation.")


def sensor_validation_cli(edgedata_path, edge_id, from_sensors, sensor_data_path, anagraphics_path, flows_path, out_dir):
    """Valida l'output MESO contro i dati reali dei sensori PASTA (statistica GEH)."""
    import src.dtsumo.inputs.config as cfg
    import pandera.errors

    if not edge_id and not from_sensors:
        raise click.UsageError("Use either --edge <id> or --from-sensors.")
    if sensor_data_path and (anagraphics_path or flows_path):
        raise click.UsageError("--sensor-data cannot be used with --pasta-anagraphics/--pasta-flows.")
    if bool(anagraphics_path) != bool(flows_path):
        raise click.UsageError("--pasta-anagraphics and --pasta-flows have to be used together.")
    if not (sensor_data_path or (anagraphics_path and flows_path)):
        raise click.UsageError("Need sensor data: --sensor-data or --pasta-anagraphics/--pasta-flows.")

    out_dir = out_dir or (cfg.IMAGES / "sensor_validation")

    try:
        sensor_df = (
            load_validated_sensor_data(sensor_data_path) if sensor_data_path
            else prepare_pasta_data(pd.read_csv(anagraphics_path), pd.read_csv(flows_path))
        )
    except (pandera.errors.SchemaError, pandera.errors.SchemaErrors) as e:
        raise click.UsageError(f"ERROR: data should be in the correct format:\n{e}")

    edgedata_df = load_edgedata(edgedata_path)

    if from_sensors:
        validate_all_sensors(edgedata_df, sensor_df, cfg.SENS_LANE_LOOKUP, out_dir, #cv_threshold=cv_threshold
                            )
        return

    lookup = pd.read_csv(cfg.SENS_LANE_LOOKUP)
    sensor_ids = lookup.loc[lookup["id_edge"].astype(str) == str(edge_id), "Cod_sens"].unique()
    if len(sensor_ids) == 0:
        raise click.UsageError(f"ERROR: no sensor found for edge {edge_id} in {cfg.SENS_LANE_LOOKUP}.")

    geh = validate_edge_against_sensor(edgedata_df, str(edge_id), sensor_df, sensor_ids[0], out_dir)
    click.echo(f"[sensor_validation] {edge_id} (sensor {sensor_ids[0]}): plot saved in {out_dir}")
    click.echo(geh.to_string())


if __name__ == "__main__":
    sensor_validation_cli()