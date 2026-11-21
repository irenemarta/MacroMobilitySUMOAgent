"""
This script is used to implement an ad hoc alorithm to produce realistic ODs matrices for a daily SUMO simulation on the map.
Figure of the hourly profile used for the daily demand (same data as the pipeline):
    uv run python -m dtsumo.inputs.dayODs [--data flows.csv] [--out Figures/flows_perc.png]
"""

import os
from dotenv import load_dotenv
from typing import Dict
from pathlib import Path
from colorama import Fore, init

import click
import matplotlib.pyplot as plt

import numpy as np
import pandas as pd
import dtsumo.inputs.config as cfg
from dtsumo.operations.connections import _get_pasta_data

init(autoreset=True)
AM_PEAK = 8
PM_PEAK = 18  # slightly less than the morning peak
# total_am = df_morning['Flow'].sum() = 27975.968
# total_pm = df_evening['Flow'].sum() = 23196.106


def _load_ods(flows_path: str) -> pd.DataFrame:
    flows = pd.read_csv(
        flows_path, sep=r"\s+", header=None, comment="*", names=["From", "To", "Flow"]
    )
    flows = flows.iloc[3:].reset_index(drop=True)
    flows["Flow"] = pd.to_numeric(
        flows["Flow"], errors="coerce"
    )  # force numeric datatype on flows
    flows[["From", "To"]] = flows[["From", "To"]].astype(int)
    return flows


def __alpha(hour: float) -> float:
    # alpha(8) = 1, alpha(17) = 0
    t = (hour - AM_PEAK) / (PM_PEAK - AM_PEAK)
    if t < 1:
        a = 1 - abs(t)
    else:
        a = t - 1
    return a


def __beta(hour: float) -> float:
    return 1 - __alpha(hour)


def _profile_flows(data: Path = None) -> pd.DataFrame:
    """PASTA counts of the sensors matched to network edges (the profile of the demand)."""
    if data is not None:
        flows = pd.read_csv(data)
    else:
        load_dotenv()
        connection_strings = {
            "ista": os.getenv("ISTA_URL"),
            "istc": os.getenv("ISTC_URL"),
        }
        if not connection_strings["ista"]:
            raise ValueError(Fore.RED + "ISTA_URL not in .env file")
        if not connection_strings["istc"]:
            raise ValueError(Fore.RED + "ISTC_URL not in .env file")

        _, flows = _get_pasta_data(
            connection_strings["ista"], connection_strings["istc"]
        )

    # hourly profile from the case-study sensors only (the ones matched to network edges):
    # the whole-city profile has a much steeper morning ramp and does not fit this area
    case_study = set(pd.read_csv(cfg.SENS_LANE_LOOKUP)["Cod_sens"].astype(str))
    flows = flows[flows["Cod_sens"].astype(str).isin(case_study)]
    if flows.empty:
        raise ValueError(
            Fore.RED + f"no PASTA counts for the sensors in {cfg.SENS_LANE_LOOKUP}"
        )
    return flows


def _scale_factor(data: Path = None) -> tuple[Dict[int, float], float]:
    """
    Scale factor [0,1]: how much traffic at this hour relative to peak.
    """
    flows = _profile_flows(data)
    day_hour_flows = flows.groupby(["hour", "daytime"])["count_all"].sum()
    hourly_mean_counts = day_hour_flows.groupby("hour").mean()
    peak = hourly_mean_counts.max()
    k_perc = round((hourly_mean_counts / peak), 3).to_dict()

    return k_perc, float(peak)


def _write_od_files(hour_matrices, output_dir_data):
    for hour, matrix in hour_matrices.items():
        output_dir_data.mkdir(parents=True, exist_ok=True)
        with open(
            Path(output_dir_data, f"h0{hour}.mtx" if hour < 10 else f"h{hour}.mtx"),
            "w",
            encoding="utf-8",
        ) as file:
            file.write(
                f"$O;D3\n* From-time To-time\n{hour}.00 {hour+1}.00\n* Factor\n1.00\n*\n* 5T srl Gruppo GTT Torino\n* 03/04/26\n"
            )
            for _, row in matrix.iterrows():
                file.write(
                    f"{int(row['From'])}\t{int(row['To'])}\t{round(row['Flow'], 2)}\n"
                )


def generate_hour_matrices(
    od_morning: str,
    od_evening: str,
    input_data: Path,
    output_dir_data: Path,
    demand_scale: float = 1.0,
) -> Dict[int, pd.DataFrame]:

    df_morning = _load_ods(od_morning)
    df_evening = _load_ods(od_evening)
    df_combined = pd.merge(
        df_morning, df_evening, on=["From", "To"], how="outer", suffixes=("_AM", "_PM")
    )

    df_combined["Flow_AM"] = df_combined["Flow_AM"].fillna(0.0)
    df_combined["Flow_PM"] = df_combined["Flow_PM"].fillna(0.0)

    k_perc, _ = _scale_factor(input_data)
    # print(f"Peaks: AM = {df_combined["Flow_AM"].sum()}, PM = {df_combined["Flow_PM"].sum()}")

    hour_matrices = {}

    for hour, perc in k_perc.items():
        a = __alpha(hour)
        b = __beta(hour)
        shape_factor = perc / (a * k_perc[AM_PEAK] + b * k_perc[PM_PEAK])
        interpol_mat = df_combined[["From", "To"]].copy()
        interpol_mat["Flow"] = shape_factor * (
            a * df_combined["Flow_AM"] + b * df_combined["Flow_PM"]
        )
        interpol_mat["Flow"] *= demand_scale
        hour_matrices[hour] = interpol_mat

    accumulator = []
    for hour, matrix in hour_matrices.items():
        sum_hour = matrix["Flow"].sum()
        print(f"hour {hour}: flow {sum_hour:.0f}")
        accumulator.append(sum_hour)

    print(f"Total vehicles: {sum(accumulator)}")

    _write_od_files(hour_matrices, output_dir_data)
    return hour_matrices


@click.command()
@click.option(
    "--data",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="PASTA counts. Default: the file used by the pipeline (cfg.SENS_DATA_FOLDER/flows.csv).",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Default: cfg.IMAGES/flows_perc.png.",
)
def profile_cli(data, out):
    data = data or cfg.SENS_DATA_FOLDER / "flows.csv"
    out = out or cfg.IMAGES / "flows_perc.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    flows = _profile_flows(data)
    k_perc, peak = _scale_factor(data)
    click.echo(
        f"{flows['Cod_sens'].nunique()} sensors, {flows['daytime'].nunique()} days, "
        f"peak hour {max(k_perc, key=k_perc.get)}:00 with {peak:.0f} veh/h (sum of the sensors)"
    )

    hours = sorted(k_perc)
    values = [k_perc[h] * 100 for h in hours]
    colors = ["tab:red" if h in (AM_PEAK, PM_PEAK) else "tab:blue" for h in hours]
    fig, ax = plt.subplots(figsize=(10, 4.5), constrained_layout=True)
    ax.bar(hours, values, color=colors, width=0.8)
    for h, v in zip(hours, values):
        ax.text(h, v + 1, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(hours, [f"{h:02d}" for h in hours])
    ax.set_xlim(-0.6, 23.6)
    ax.set_ylim(0, 112)
    ax.set_xlabel("hour of the day")
    ax.set_ylabel("traffic with respect to the peak hour (%)")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    fig.suptitle("Percentual flow distribution of the traffic in the case-study area", fontsize=12)
    # peaks of the VISUM matrices (08:00-09:00 and 18:00-19:00) in red
    fig.savefig(out, dpi=300)
    fig.savefig(out.with_suffix(".pdf"))
    plt.close(fig)
    click.echo(f"Profile saved in {out} and {out.with_suffix('.pdf')}")
    for h in hours:
        click.echo(f"  {h:02d}:00  {k_perc[h] * 100:5.1f}%")


if __name__ == "__main__":
    profile_cli()
