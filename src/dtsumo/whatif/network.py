"""
Network building functions for what-if scenarios.
Closed lanes keep existing and only get disallow="all": same edges, lanes and detectors, connections and traffic lights untouched,
Only closed junctions and banned turns go through netconvert (see _remove_connections).
"""

# highest indices, lane 0 is the rightmost (source: https://sumo.dlr.de/docs/Networks/SUMO_Road_Networks.html#lanes)

import sumolib, subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set
from dtsumo.helpers import TLS_OFF_SCENARIO, write_scenario_tls_off
from dtsumo.whatif.scenario import (
    ScenarioError,
    _assign_new_duration,
    which_lanes_to_close,
)


def build_scenario_net(
    base_net: Path,
    net: "sumolib.net.Net",
    lanes_closed: Dict[str, int],
    out_dir: Path,
    new_speeds: Optional[Dict[str, float]] = None,
    tls_rules: Optional[List[dict]] = None,
    removed_connections: Optional[List[dict]] = None,
) -> Path:
    """scenario.net.xml = base_net with disallow="all" on the closed lanes (lanes_closed: {edge: n lanes})
    + modified speed and tls + movements removed from closed junctions"""
    closed_lanes = set()
    for eid, k in lanes_closed.items():
        # leftmost lanes first, skipping the last lane of a turn (see scenario.which_lanes_to_close)
        for lane in which_lanes_to_close(net.getEdge(eid), k):
            closed_lanes.add(lane.getID())

    if removed_connections:
        base_net = _remove_connections(base_net, net, removed_connections, out_dir)
    tree = ET.parse(base_net)
    root = tree.getroot()
    # close the internal lanes of the movements from or to a closed lane
    connections = []
    for conn_el in root.iter("connection"):
        if conn_el.get(
            "via"
        ):  # only connections that cross a junction have an internal lane
            connections.append(conn_el)
    # a left turn can go through two internal lanes (internal junction)
    # from lane = lane index -> from + fromLane = edgeid_laneid
    for _ in range(2):
        for conn in connections:
            from_lane = f"{conn.get('from')}_{conn.get('fromLane')}"
            to_lane = f"{conn.get('to')}_{conn.get('toLane')}"
            if from_lane in closed_lanes or to_lane in closed_lanes:
                closed_lanes.add(conn.get("via"))

    for lane in root.iter("lane"):
        if lane.get("id") in closed_lanes:
            lane.attrib.pop("allow", None)
            lane.set("disallow", "all")
    # speed management
    for edge in root.iter("edge"):
        v_in_ms = (new_speeds or {}).get(edge.get("id"))
        if v_in_ms is not None:
            for lane in edge.iter("lane"):
                lane.set("speed", f"{v_in_ms:.2f}")

    # tls management
    ## NB: STATIC LOGIC ONLY - no minDur/maxDur
    applied_rules = {r["tls"]: r for r in tls_rules or [] if r["action"] != "off"}
    # switched off lights: programID="off" in an additional next to the net (read by entities.build),
    # so SUMO uses the junction's own right of way, as for the non-real lights
    off_ids = sorted(r["tls"] for r in tls_rules or [] if r["action"] == "off")
    (out_dir / TLS_OFF_SCENARIO).unlink(
        missing_ok=True
    )  # nothing left from a previous run
    if off_ids or removed_connections:
        # a fully closed junction loses its light: list only the lights still in the net
        present = {tl.get("id") for tl in root.iter("tlLogic")}
        write_scenario_tls_off(out_dir, off_ids, present=present)
    for tl in root.iter("tlLogic"):
        rule = applied_rules.get(tl.get("id"))
        if rule is None:
            continue
        for idx, phase in enumerate(tl.findall("phase")):  # green, cycle
            new_dur = _assign_new_duration(
                duration=float(phase.get("duration")), phase_idx=idx, int_rule=rule
            )
            phase.set("duration", f"{new_dur:.1f}")

    scenario_net = out_dir / "scenario.net.xml"
    tree.write(scenario_net, encoding="UTF-8", xml_declaration=True)
    return scenario_net


def _remove_connections(
    base_net: Path, net: "sumolib.net.Net", connections: List[dict], out_dir: Path
) -> Path:
    """Net without the prohibited movements. 
    marouter ignores disallow on internal lanes -> better using netconvert to 
    delete them and rebuild both, leaving edges, lanes and junction shapes as they are."""
    conn_file = out_dir / "junctions.con.xml"
    lines_conn_file = ["<connections>"]
    for conn in connections:
        lines_conn_file.append(
            f'    <delete from="{conn["from"]}" to="{conn["to"]}" fromLane="{conn["fromLane"]}" toLane="{conn["toLane"]}"/>'
        )
    lines_conn_file.append("</connections>")
    conn_file.write_text("\n".join(lines_conn_file) + "\n")
    net_modified = out_dir / "junctions.net.xml"
    result = subprocess.run(
        [
            "netconvert",
            "-s",
            str(base_net),
            "--connection-files",
            str(conn_file),
            "-o",
            str(net_modified),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"netconvert could not remove the junction movements:\n{result.stderr[-2000:]}"
        )
    # same lanes as before: detectors, closures and the TAZ still refer to them
    lanes_before = {
        l.getID() for e in net.getEdges() if not e.getFunction() for l in e.getLanes()
    }
    lanes_after = {
        l.get("id")
        for e in ET.parse(net_modified).getroot().iter("edge")
        if not e.get("function")
        for l in e.iter("lane")
    }
    if lanes_before != lanes_after:
        raise RuntimeError(
            f"netconvert changed the lanes of the net ({len(lanes_before ^ lanes_after)} differ):"
            f"\tcheck new net here: {net_modified}"
        )
    return net_modified


def scenario_taz(
    taz_file: Path, closed_edges: Set[str], out_path: Optional[Path] = None
) -> Optional[Path]:
    """
    TAZ without the fully closed edges.
    Without out_path it only checks that every zone keeps an origin and a destination.
    """
    tree = ET.parse(taz_file)
    lost_endpoints = []
    for t_zone in tree.getroot().iter("taz"):
        list_edges = t_zone.get("edges", "").split()
        if list_edges:
            t_zone.set(
                "edges", " ".join(e for e in list_edges if e not in closed_edges)
            )
        for tag in ("tazSource", "tazSink"):
            src_snk = t_zone.findall(tag)
            kept = [el for el in src_snk if el.get("id") not in closed_edges]
            if src_snk and not kept:
                label = "origins" if tag == "tazSource" else "destinations"
                lost_endpoints.append(f"{t_zone.get('id')} ({label})")
            for el in src_snk:
                if el not in kept:
                    t_zone.remove(el)
    if lost_endpoints:
        raise ScenarioError(
            "closing these edges leaves zones without access, their demand could not be served: "
            + ", ".join(lost_endpoints)
            + ". Keep at least one access edge open or use 'partial_closure'."
        )
    if out_path:
        tree.write(out_path, encoding="UTF-8", xml_declaration=True)
    return out_path
