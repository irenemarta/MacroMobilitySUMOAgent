import re
from typing import List
import xml.etree.ElementTree as ET
from pathlib import Path
from tqdm import tqdm
from colorama import init, Fore

init(autoreset=True)


#### FILTERING
def _get_edges(element: ET.Element) -> list[str] | None:
    route = element.find("route")
    if route is not None:
        return route.attrib.get("edges", "").split()
    rd = element.find("routeDistribution")
    if rd is not None:
        routes = rd.findall("route")
        if routes:
            best = max(routes, key=lambda r: float(r.attrib.get("probability", 0)))
            return best.attrib.get("edges", "").split()
    return None


def _count_edges(element: ET.Element) -> int:
    edges = _get_edges(element)
    if edges:
        return len(edges)
    else:
        return 0


def filter_short_flows(
    input_xml: Path, output_new: Path, edge_taz_map: dict, min_edges: int = 2
) -> Path:
    """
    Overwrites XML to eliminate trips which run over a single edge and create artificial bottlenecks in the system.
    """
    tree = ET.parse(input_xml)
    root = tree.getroot()
    short_ids = []
    total_veh_removed = 0

    for tag in ["vehicle", "flow"]:
        for el in root.findall(tag):
            n_edges = _count_edges(el)
            edges = _get_edges(el)
            if not edges:
                continue
            from_taz = int(edge_taz_map.get(edges[0]))
            to_taz = int(edge_taz_map.get(edges[-1]))
            is_external = (
                from_taz >= 10000 or to_taz >= 10000
            )  # convention for external zones offsets

            if n_edges < min_edges and is_external:
                short_ids.append(el.get("id"))
                if tag == "flow":
                    n_veh_removed = int(el.attrib.get("number", 0))
                    total_veh_removed += n_veh_removed
                else:
                    total_veh_removed += 1

    removed_count = 0
    for tag in ["vehicle", "flow"]:
        elements = root.findall(tag)
        for el in tqdm(elements, desc=f"Procesing {tag}s"):
            if el.get("id") in short_ids:
                root.remove(el)
                removed_count += 1
    if removed_count == 0:
        print(Fore.MAGENTA + f"ROUTE CLEANING: no short flows found or removed.")

    ET.indent(tree, space="    ")
    output_new.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_new, encoding="utf-8", xml_declaration=True)
    print(
        Fore.MAGENTA + f"ROUTE CLEANING: removed {len(short_ids)} flows "
        f"({total_veh_removed} vehicles) from {input_xml.name}"
    )

    return output_new


def filter_zero_flows(
    trips_xml: Path,
) -> Path:  # to be used with _preprocess_multiple_ods
    tree = ET.parse(trips_xml)
    root = tree.getroot()

    removed = 0
    for flow in root.findall(".//flow"):
        number = int(flow.get("number", 0))
        if number == 0:
            root.remove(flow)
            removed += 1
    print(Fore.MAGENTA + f"Removed {removed} zero-flow entries")
    tree.write(trips_xml, encoding="utf-8", xml_declaration=True)

    return trips_xml


def filter_zero_prob(marouter_output: Path) -> Path:
    """Post-processing of routes to prevent SUE crash running marouter"""

    tree = ET.parse(marouter_output)
    root = tree.getroot()
    to_remove = []

    for tag in ["vehicle", "flow"]:
        for vehicle in root.findall(tag):
            rd = vehicle.find("routeDistribution")
            if rd is None:
                continue
            routes = rd.findall("route")
            if not routes:
                to_remove.append(vehicle)
                continue
            # unique route with 0 prob or total route prob is 0
            tot_prob = sum(float(r.get("probability", 0)) for r in routes)
            if tot_prob == 0.0:
                to_remove.append(vehicle)
    print(
        Fore.MAGENTA
        + f"ZERO-PROB: Removing {len(to_remove)} zero-probability vehicles/flows"
    )

    for v in tqdm(to_remove):
        root.remove(v)
    ET.indent(tree, space="    ")
    marouter_output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(marouter_output, encoding="utf-8", xml_declaration=True)

    return marouter_output


### WHAT-IF demand scaling
def _hour_factor(origin: str, destination: str, hour: int, demand_rules: list) -> float:
    """Product of the factors of the demand rules OD pairs affected by the disturbance demand rule of the hour. 
    Ex: +20% demand only from 8am to 9 am from TAZ x to TAZ y."""
    factor = 1.0
    for dr in demand_rules:
        # overlap = earliest end - latest start
        overlap = min(dr["end"], (hour + 1) * 3600) - max(dr["begin"], hour * 3600)
        if overlap <= 0:
            continue
        if dr["taz"]:
            zones = set(dr["taz"])
            if dr["direction"] == "from":
                affected_trip = origin in zones
            elif dr["direction"] == "to":
                affected_trip = destination in zones
            else:
                affected_trip = origin in zones or destination in zones
            if not affected_trip:
                continue
        factor *= 1 + (dr["factor"] - 1) * overlap / 3600
        # TODO: spezzare matrici per evitare dispersione unifore del +10% su tutta l'ora per od2trips?
        """
        Ex: +20% from 7:30 to 9:00 am
        7-7:30 does not change, +20% 7:30-8 --> 7:00-8:00 = +10%
        f = 1 + (1.2 - 1) * 0.5 = 1.1
        """
    return factor


def scale_od_matrices(od_files: list, demand_rules: list, output_dir: Path) -> List[Path]:
    """
    What-if demand change on OD matrices.
    rules: [{"taz": [...] (empty = every zone), "direction": "from" | "to" | "both", "begin": s, "end": s, "factor": f}]
    (see scripts/src/whatif/scenario.resolve_demand).
    Scaled copies are then saved in output_dir and original ODs remain untouched.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    scaled_od_files = []
    before = after = 0.0
    for od in od_files:
        # name format for the file: hHH.mtx
        hour = int(re.fullmatch(r"h(\d+)\.mtx", Path(od).name).group(1))
        scaled_od = output_dir / Path(od).name
        with open(od, "r", encoding="utf-8") as f_in, open(scaled_od, "w", encoding="utf-8") as f_out:
            for data_od_pair in f_in:
                info = data_od_pair.split()
                if len(info) == 3 and info[0].isdigit() and info[1].isdigit():
                    flow = float(info[2])
                    new_flow = flow * _hour_factor(origin=info[0], destination=info[1], hour=hour, demand_rules=demand_rules)
                    before, after = before+flow, after+new_flow
                    data_od_pair = f"{info[0]}\t{info[1]}\t{round(new_flow, 2)}\n"   
                f_out.write(data_od_pair)
        scaled_od_files.append(scaled_od)
    print(Fore.MAGENTA + f"Demand change on the O/D matrices: {before:.0f} -> {after:.0f} vehicles")
    return scaled_od_files