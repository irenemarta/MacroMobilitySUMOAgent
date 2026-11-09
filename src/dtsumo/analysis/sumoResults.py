"""
Dashboards for the SUMO summary output (summary.xml).
    uv run python -m dtsumo.analysis.sumoResults SUMMARY.xml 
    Options: [SUMMARY.xml ...] --title "..." [--label ...] [--period DAY]

Every file gets its own dashboard.
With two or more files, a comparison plot is added too (e.g. SUMO 1.26 vs 1.28, TLS on vs off, meso vs LTM).
"""

from pathlib import Path
from typing import Dict, List, Tuple
import xml.etree.ElementTree as ET

import click
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

import dtsumo.inputs.config as cfg

# OD load hours shaded in the plots (seconds)
LOAD_WINDOWS = {
    "AM": [(8 * 3600, 9 * 3600)],
    "PM": [(18 * 3600, 19 * 3600)],
    "DAY": [(8 * 3600, 9 * 3600), (18 * 3600, 19 * 3600)],
}
LOAD_COLORS = [(0.3, 0.5, 1.0, 0.10), (1.0, 0.70, 0.1, 0.12)]  # morning blue, evening gold

COLORS = {
    "running": "#1565C0",
    "halting": "#C62828",
    "inserted": "#2E7D32",
    "arrived": "#6A1515",
    "loaded": "#E65100",
    "waiting": "#B71C1C",
    "meanSpeed": "#0277BD",
    "meanSpeedRelative": "#78909C",
    "meanWaitingTime": "#F57F17",
    "meanTravelTime": "#388E3C",
    "collisions": "#BF360C",
    "discarded": "#D32F2F",
    "teleports": "#1565C0",
}


def parse_sumo_summary(file_path: Path) -> pd.DataFrame:
    try:
        root = ET.parse(file_path).getroot()
    except ET.ParseError as e:
        raise ValueError(f"ERROR:\t Could not parse XML file {file_path}: {e}")
    return pd.DataFrame([step.attrib for step in root.findall("step")]).apply(pd.to_numeric)


def datetime_format(df_summary: pd.DataFrame) -> pd.DataFrame:
    df_summary["time"] = pd.to_datetime(df_summary["time"], unit="s")
    df_summary["meanWaitingTime_dt"] = pd.to_datetime(df_summary["meanWaitingTime"], unit="s")
    df_summary["meanTravelTime_dt"] = pd.to_datetime(df_summary["meanTravelTime"], unit="s")
    return df_summary


def load_summary(file_path: Path) -> pd.DataFrame:
    return datetime_format(parse_sumo_summary(file_path))


def _style(ax, title: str, ylabel: str):
    ax.set_facecolor("#FAFAFA")
    ax.set_title(title, fontsize=12, fontweight="bold", pad=5)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_xlabel("time", fontsize=10)
    ax.tick_params(labelsize=10)
    ax.grid(True, color="#DDDDDD", linewidth=0.6, linestyle="--")
    for spine in ax.spines.values():
        spine.set_edgecolor("#CCCCCC")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")


def _shade_load_windows(ax, windows: List[Tuple[int, int]]):
    for i, (start, end) in enumerate(windows):
        ax.axvspan(
            pd.to_datetime(start, unit="s"),
            pd.to_datetime(end, unit="s"),
            color=LOAD_COLORS[i % len(LOAD_COLORS)],
            zorder=0,
            label=f"Load {i + 1}",
        )


def _legend(ax, loc="upper left"):
    ax.legend(loc=loc, fontsize=9, framealpha=0.6, facecolor="white", edgecolor="#BBBBBB")


def _compute_hourly(df: pd.DataFrame) -> pd.DataFrame:
    tmp = df.copy()
    tmp["hour"] = tmp["time"].dt.floor("h")
    inserted = (
        tmp.groupby("hour")["inserted"]
        .agg(lambda s: s.iloc[-1] - s.iloc[0])
        .rename("inserted_per_hour")
    )
    waiting_running = (
        tmp.groupby("hour")[["waiting", "running"]]
        .mean()
        .rename(columns={"waiting": "waiting_per_hour", "running": "running_per_hour"})
    )
    return pd.concat([inserted, waiting_running], axis=1).reset_index()


def plot_summary(df: pd.DataFrame, title: str, windows: List[Tuple[int, int]], out_path: Path):
    """Full dashboard of one run: 3x2 panels + hourly metrics on the last row."""
    fig = plt.figure(figsize=(18, 17), constrained_layout=True)
    fig.suptitle(f"SUMO Summary - {title}", fontsize=16, fontweight="bold")
    gs = fig.add_gridspec(nrows=4, ncols=2)
    axs = [[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(3)]
    ax_hourly = fig.add_subplot(gs[3, :])

    def lines(ax, columns, title_sub, ylabel, lw=1.5):
        for col, label in columns:
            ax.plot(df["time"], df[col], color=COLORS[col], lw=lw, label=label)
        _shade_load_windows(ax, windows)
        _style(ax, title_sub, ylabel)
        _legend(ax)

    lines(axs[0][0], [("running", "Running"), ("halting", "Halting")], "Net Congestion", "Number of vehicles")
    lines(axs[0][1], [("inserted", "Inserted"), ("arrived", "Arrived")], "Inserted vs Arrived", "Cumulative total")

    # speed on two axes: meanSpeedRelative is a ratio, not m/s
    ax = axs[1][0]
    ax2 = ax.twinx()
    ax.plot(df["time"], df["meanSpeed"], color=COLORS["meanSpeed"], lw=1.4, label="AVG speed (m/s)")
    ax2.plot(df["time"], df["meanSpeedRelative"], color=COLORS["meanSpeedRelative"], lw=1.1, alpha=0.6, label="AVG Rel Speed")
    _shade_load_windows(ax, windows)
    _style(ax, "Average Speed & Relative Speed", "Speed (m/s)")
    ax2.set_ylabel("Ratio w.r.t. speed limit", fontsize=8)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax2.legend(h1 + h2, l1 + l2, loc="lower right", fontsize=7, framealpha=0.6)

    ax = axs[1][1]
    ax.plot(df["time"], df["meanWaitingTime_dt"], color=COLORS["meanWaitingTime"], lw=1.5, label="AVG waiting time")
    ax.plot(df["time"], df["meanTravelTime_dt"], color=COLORS["meanTravelTime"], lw=1.5, label="AVG travel time")
    _shade_load_windows(ax, windows)
    _style(ax, "Running & Waiting Times", "Time")
    ax.yaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    _legend(ax)

    # loaded = vehicles read from the route files, also with future departures
    # waiting = vehicles loaded but not inserted yet (insertion backlog)
    ax = axs[2][0]
    ax.set_yscale("log")
    lines(ax, [("inserted", "Inserted"), ("loaded", "Loaded"), ("waiting", "Waiting")], "Infrastructure Performance", "Number of Vehicles")
    lines(axs[2][1], [("collisions", "Collisions"), ("discarded", "Discarded"), ("teleports", "Teleports")], "Simulation Failures", "Number of events")

    hourly = _compute_hourly(df)
    ax_hourly.plot(hourly["hour"], hourly["inserted_per_hour"], color=COLORS["inserted"], lw=2, marker="o", ms=4, label="Inserted / hour")
    ax_hourly.plot(hourly["hour"], hourly["waiting_per_hour"], color=COLORS["meanWaitingTime"], lw=2, marker="s", ms=4, label="Avg Waiting / hour")
    ax_hourly.plot(hourly["hour"], hourly["running_per_hour"], color=COLORS["running"], lw=2, marker="^", ms=4, label="Avg Running / hour")
    _shade_load_windows(ax_hourly, windows)
    _style(ax_hourly, "Hourly metrics: Inserted / Avg Waiting / Avg Running", "Vehicles")
    _legend(ax_hourly)

    fig.savefig(out_path, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_compare(dfs: Dict[str, pd.DataFrame], title: str, windows: List[Tuple[int, int]], out_path: Path):
    """Same metrics for several runs, one line per run."""
    panels = [
        ("running", "Running", "Vehicles"),
        ("halting", "Halting", "Vehicles"),
        ("arrived", "Arrived (cumulative)", "Vehicles"),
        ("meanSpeed", "Mean speed", "m/s"),
        ("meanWaitingTime", "Mean waiting time", "s"),
        ("teleports", "Teleports (cumulative)", "Events"),
    ]
    fig, axs = plt.subplots(3, 2, figsize=(18, 14), constrained_layout=True)
    fig.suptitle(f"SUMO Comparison - {title}", fontsize=16, fontweight="bold")

    for ax, (col, title_sub, ylabel) in zip(axs.flatten(), panels):
        for label, df in dfs.items():
            ax.plot(df["time"], df[col], lw=1.5, label=label)
        _shade_load_windows(ax, windows)
        _style(ax, title_sub, ylabel)
        _legend(ax)

    fig.savefig(out_path, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)


@click.command(name="sumo-summary")
@click.argument("summaries", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--label", "labels", multiple=True, help="One per summary file, same order (default: file name).")
@click.option("--title", required=True)
@click.option("--period", type=click.Choice(list(LOAD_WINDOWS)), default="DAY", show_default=True, help="Load hours to shade.")
@click.option("--out-dir", type=click.Path(path_type=Path), default=None, help="Default: cfg.OUTPUT_DASHBOARDS.")
def main(summaries, labels, title, period, out_dir):
    if labels and len(labels) != len(summaries):
        raise click.UsageError("Give one --label per summary file, or none.")
    labels = labels or [p.stem for p in summaries]
    out_dir = out_dir or cfg.OUTPUT_DASHBOARDS
    out_dir.mkdir(parents=True, exist_ok=True)
    windows = LOAD_WINDOWS[period]
    slug = title.replace(" ", "_")

    dfs = {label: load_summary(path) for label, path in zip(labels, summaries)}
    for label, df in dfs.items():
        out_path = out_dir / f"dashboard_{slug}_{label}.png"
        plot_summary(df, f"{title} - {label}", windows, out_path)
        click.echo(f"Dashboard saved in {out_path}")

    if len(dfs) > 1:
        out_path = out_dir / f"compare_{slug}.png"
        plot_compare(dfs, title, windows, out_path)
        click.echo(f"Comparison saved in {out_path}")


if __name__ == "__main__":
    main()