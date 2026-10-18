"""
The module translates a what-if scenario file into real lanes and edges closures.

Intervention json examples:
1. closures
{"id": "francia_cantiere", "interventions": [
    {"type": "closure", "via": "Corso Francia", "between": ["Via Pozzo Strada", "Corso Monte Cucco"]},
    {"type": "partial_closure", "via": "Corso Peschiera", "severity": 0.5},
    {"type": "closure", "edges": ["125665632#1"]},
    {"type": "partial_closure", "taz": ["412"], "severity": 0.5},
    {"type": "partial_closure", "via": "Corso Francia", "from": "Corso Monte Cucco",
    "to": "Via Pozzo Strada", "fraction": 0.5, "severity": 0.5}]}
2. speed limits and demand
    {"type": "speed", "via": "Corso Peschiera", "between": ["Via Pozzo Strada", "Corso Monte Cucco"], "ms": 8.33},
    {"type": "demand", "taz": ["604"], "direction": "to", "time": ["07:30", "09:00"], "factor": 1.2},
3. traffic lights logics
    {"type": "tls", "via": "Corso Francia", "at": "Corso Monte Cucco", "action": "green", "seconds": 10},
    {"type": "tls", "tls": "joinedS_5743605972_#11more", "action": "cycle", "factor": 1.2},
    {"type": "tls", "tls": "joinedS_5743605972_#11more", "action": "off"}

"interventions": [] is the baseline.
"""

# useful note: https://www.eclipse.org/lists/sumo-user/msg03425.html

import html, json, math, re, sumolib
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import networkx as nx

import dtsumo.inputs.config as cfg
from dtsumo.operations.taz_zones import _taz_edge_ids

CLOSURE_SEVERITY = 0.0  # all lanes closed
INTERVENTION_TYPE = ["closure", "partial_closure", "speed", "demand", "tls"]
TLS_ACTIONS = ("green", "cycle", "off")
MIN_SPEED, MAX_SPEED = 5, 25  # m/s
MIN_PHASE = 5  # minimum seconds of phase duration
WHERE_TO_CLOSE = ("via", "edges", "taz")


class ScenarioError(ValueError):
    pass


### scenario loading
def check_scenario(scenario: dict) -> dict:
    if not re.fullmatch(r"[\w.-]+", str(scenario.get("id", ""))):
        raise ScenarioError("'id' missing or not valid")
    if not isinstance(scenario.get("interventions"), list):
        raise ScenarioError("'interventions' must be a list (empty for the baseline)")
    return scenario


def load_scenario(path_to_json: Path) -> dict:
    scenario = json.loads(Path(path_to_json).read_text(encoding="utf-8"))
    return check_scenario(scenario)


### map elements parsing
def edges_by_street(net: "sumolib.net.Net") -> Dict[str, list]:
    streets = defaultdict(list)
    for edge in net.getEdges():
        if edge.getFunction() in ("internal", "crossing", "walkingarea"):
            continue
        # some OSM names are escaped twice in the net (e.g. "Sant&apos;Ambrogio")
        name = html.unescape(
            (edge.getName() or "").strip()
        )  # html.unescape to avoid html special characters
        if name:
            streets[name].append(edge)
    return streets


# edge to node correspondence
def _nodes(edges) -> dict:
    nodes = {}
    for e in edges:
        for node in (e.getFromNode(), e.getToNode()):
            node_id = node.getID()
            nodes[node_id] = node
    return nodes


def _junction(streets: Dict[str, list], via: str, cross_via: str) -> dict:
    """Nodes shared by two streets (more than one on dual carriageways)."""
    if via not in streets:
        raise ScenarioError(f"street '{via}' not in the network")
    if cross_via not in streets:
        raise ScenarioError(f"cross street '{cross_via}' not in the network")
    via_nodes = _nodes(streets[via])
    cross_nodes = _nodes(streets[cross_via])
    nodes_street_a = set(via_nodes)
    nodes_street_b = set(cross_nodes)
    shared = nodes_street_a.intersection(nodes_street_b)
    if not shared:
        raise ScenarioError(f"'{cross_via}' does not cross '{via}'")
    junctions = {}
    for n in shared:
        junctions[n] = via_nodes[n]
    return junctions


# create shortest path graph leveraging networkx library
def _shortest_path(edges, e_sources, e_targets) -> List[str]:
    graph = nx.DiGraph()
    for e in edges:
        graph.add_edge(
            u_of_edge=e.getFromNode().getID(),
            v_of_edge=e.getToNode().getID(),
            weight=e.getLength(),
            id=e.getID(),
        )
    best_length, best_nodes = None, None
    for t in e_targets:  # one or two nodes
        try:
            length, nodes = nx.multi_source_dijkstra(graph, set(e_sources), target=t)
        except nx.NetworkXNoPath:
            continue
        if best_length is None or length < best_length:
            best_length, best_nodes = length, nodes
    if best_nodes is None:
        return []
    return [graph[a][b]["id"] for a, b in zip(best_nodes, best_nodes[1:])]


def dedup_edges(edge_list: list) -> list:
    seen = set()
    unique = []
    for edge in edge_list:
        if edge not in seen:
            seen.add(edge)
            unique.append(edge)
    return unique


def resolve_street(
    streets: Dict[str, list], via: str, between: Optional[List[str]] = None
) -> List[str]:
    """Find a street section between two defined roads defined in the intervention json."""
    if via not in streets:
        raise ScenarioError(f"street '{via}' not in the network")
    if not between:
        return [e.getID() for e in streets[via]]
    if len(between) != 2:
        raise ScenarioError(
            f"'between' of '{via}' must have two streets, got {between}"
        )
    start, end = (set(_junction(streets, via, x)) for x in between)
    # both directions
    direct_direction = _shortest_path(
        edges=streets[via], e_sources=start, e_targets=end
    )
    backwards = _shortest_path(edges=streets[via], e_sources=end, e_targets=start)
    if not direct_direction and not backwards:
        raise ScenarioError(
            f"no path along '{via}' between '{between[0]}' and '{between[1]}'"
        )
    all_edges = direct_direction + backwards
    return dedup_edges(all_edges)


def _centroid(nodes: dict) -> Tuple[float, float]:
    coords = [n.getCoord() for n in nodes.values()]
    x, y = zip(*coords)
    avg_x, avg_y = sum(x) / len(coords), sum(y) / len(coords)
    return avg_x, avg_y


def _street_edges(streets: Dict[str, list], intervention: dict, name: str) -> List[str]:
    """Name to edges correspondence"""
    via = intervention["via"]
    if intervention.get("fraction") is None:
        # whole street, or the stretch between two cross streets
        return resolve_street(streets, via, intervention.get("between"))
    # worksite: a stretch long 'fraction' of the street, starting at 'from'
    if intervention.get("between") or not intervention.get("from"):
        raise ScenarioError(
            f"{name}: 'fraction' needs 'from' (and optionally 'to'), not 'between'"
        )
    return resolve_fraction_closure(
        streets,
        via,
        intervention["from"],
        intervention["fraction"],
        intervention.get("to"),
    )


def _turns(edge, lanes) -> set:
    """Turns still possible using only these lanes of the edge:
    ("out", next_edge) or ("in", previous_edge). U-turns are ignored."""
    turns = set()
    for lane in lanes:
        # where can a car go from this lane?
        for conn in lane.getOutgoing():
            if conn.getDirection() != "t":
                turns.add(("out", conn.getTo().getID()))
    for prev_edge in edge.getIncoming():
        # can a car coming from prev_edge get into one of these lanes?
        for conn in prev_edge.getConnections(edge):
            if conn.getToLane() in lanes and conn.getDirection() != "t":
                turns.add(("in", prev_edge.getID()))
    return turns


# for speed changes
def _speed(
    net: "sumolib.net.Net",
    streets: Dict[str, list],
    zones: Dict[str, List[str]],
    intervention: dict,
    n: int,
):
    """Returns edges on which a speed change has been applied."""
    name = f"intervention {n}"
    new_speed = intervention.get("ms")
    if (
        not isinstance(new_speed, (int, float))
        or not MIN_SPEED <= new_speed <= MAX_SPEED
    ):
        raise ScenarioError(
            f"{name}: new speed {new_speed} out of bounds - [{MIN_SPEED}; {MAX_SPEED}]"
        )
    edges_new_speed = _target_closure_edges(
        net=net, streets=streets, zones=zones, intervention=intervention, name=name
    )
    return edges_new_speed, new_speed


# for tls manipulation
def _variation_tls(
    net: "sumolib.net.Net", streets: Dict[str, list], via: str, at: str
) -> Tuple[str, List[int], List[int]]:
    nodes = set(_junction(streets=streets, via=via, cross_via=at))
    found_tls_ids = defaultdict(
        lambda: ([], [])
    )  # tls id -> (links from via, links from at)
    via_edges = {e.getID() for e in streets[via]}
    at_edges = {e.getID() for e in streets[at]}
    for tls in net.getTrafficLights():
            for inLane, outLane, linkNum in tls.getConnections():
                edge = inLane.getEdge()
                if edge.getToNode().getID() not in nodes:  # only the junction of 'via' and 'at'
                    continue
                if edge.getID() in via_edges:
                    found_tls_ids[tls.getID()][0].append(linkNum)
                elif edge.getID() in at_edges:
                    found_tls_ids[tls.getID()][1].append(linkNum)
    if not found_tls_ids:
        raise ScenarioError(f"no traffic light where '{at}' meets '{via}'")
    tls_id, (via_links, at_links) = next(iter(found_tls_ids.items())) # in the case study at most one tll per junction
    return tls_id, via_links, at_links


def _green_share(state: str, links: List[int]) -> float:
    """Share of the links with green in a phase state."""
    if links:
        share = sum(state[l] in "Gg" for l in links) / len(links)
    else:
        share = 0.0
    return share


def _assign_new_duration(duration: float, phase_idx: int, int_rule: dict) -> float:
    if phase_idx not in int_rule.get("phases", ()):
        return duration
    if int_rule["action"] == "green":
        return max(MIN_PHASE, duration + int_rule["seconds"])
    return max(MIN_PHASE, duration * int_rule["factor"])


### closures
def which_lanes_to_close(edge, to_close: int) -> list:
    """Up to to_close lanes, leftmost first, never the last lane of a turn in or out:
    a worksite narrows the road but every turn stays possible."""
    lanes = edge.getLanes()
    if to_close >= len(lanes):
        return list(lanes)  # full closure
    needed = _turns(edge, lanes)  # turns the edge has now, they must survive
    open_lanes, closed = list(lanes), []
    # highest index = leftmost, lane 0 is the rightmost
    # (https://sumo.dlr.de/docs/Networks/SUMO_Road_Networks.html#lanes)
    for lane in reversed(lanes):
        if len(closed) == to_close:
            break
        rest = [l for l in open_lanes if l != lane]
        if _turns(edge, rest) == needed:
            open_lanes = rest
            closed.append(lane)
    return closed


def taz_edges(taz_file: Path) -> Dict[str, List[str]]:
    taz_edges_dict = {}
    for taz in ET.parse(taz_file).getroot().iter("taz"):
        taz_id = taz.get("id")
        associated_edges = _taz_edge_ids(taz)
        taz_edges_dict[taz_id] = associated_edges
    return taz_edges_dict


def lanes_to_close(n_lanes: int, severity: float) -> int:
    """severity = share of capacity left; at least one lane stays open unless severity is 0."""
    if severity == CLOSURE_SEVERITY:
        return n_lanes  # full closure
    # lanes that should close, rounded half up (e.g. 1.5 -> 2)
    to_close = math.floor((1 - severity) * n_lanes + 0.5)
    # a partial closure always leaves at least one lane open
    return min(to_close, n_lanes - 1)


def _listed_edges(net: "sumolib.net.Net", edge_ids: List[str], name: str) -> List[str]:
    missing = [e for e in edge_ids if not net.hasEdge(e)]
    if missing:
        raise ScenarioError(f"{name}: edges not in the network: {missing}")
    return list(edge_ids)


def _zone_edges(
    net: "sumolib.net.Net", zones: Dict[str, List[str]], taz_ids: list, name: str
) -> List[str]:
    """TAZs to edges correspondence"""
    taz_ids = [
        str(z) for z in taz_ids
    ]  # ids may be written as numbers in the json => normalise to str
    missing = [z for z in taz_ids if z not in zones]
    if missing:
        raise ScenarioError(f"{name}: TAZ not in the TAZ file: {missing}")
    edges = []
    for z in taz_ids:
        for e in zones[z]:
            if net.hasEdge(e):
                edges.append(e)
    return dedup_edges(edges)  # an edge can belong to more than one zone


### demand changes
def _seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def _target_closure_edges(
    net: "sumolib.net.Net",
    streets: Dict[str, list],
    zones: Dict[str, List[str]],
    intervention: dict,
    name: str,
):
    # where?
    given = [key for key in WHERE_TO_CLOSE if intervention.get(key)]
    if len(given) != 1:
        raise ScenarioError(
            f"{name}: give exactly one of {WHERE_TO_CLOSE}, got {given or 'none'}"
        )
    target = given[0]
    # involved edges
    if target == "via":
        edges = _street_edges(streets, intervention, name)
    elif target == "edges":
        edges = _listed_edges(net, intervention["edges"], name)
    else:
        edges = _zone_edges(net, zones, intervention["taz"], name)
    if not edges:
        raise ScenarioError(f"{name}: no edges found")

    return edges


def disturbances(net: "sumolib.net.Net", severities: Dict[str, float]) -> List[dict]:
    """Every connection into and out of the changed edges."""
    found_pairs = {}  # (from, to) -> severity
    for edge_id, severity in severities.items():
        edge = net.getEdge(edge_id)
        from_to = []
        for incoming in edge.getIncoming():
            from_to.append((incoming.getID(), edge_id))
        for outgoing in edge.getOutgoing():
            from_to.append((edge_id, outgoing.getID()))
        for pair in from_to:
            if pair not in found_pairs or severity < found_pairs[pair]:
                found_pairs[pair] = severity

    endpoints_severity = []
    for (from_edge, to_edge), severity in sorted(found_pairs.items()):
        endpoints_severity.append(
            {"from": from_edge, "to": to_edge, "severity": severity}
        )
    return endpoints_severity


### scenario resolution
def resolve_fraction_closure(
    streets: Dict[str, list],
    via: str,
    from_via: str,
    fraction: float,
    to_via: Optional[str] = None,
) -> List[str]:
    """'fraction' of the street's total length to close according to the intervation,
    to model worksites and carriageway narrowing."""
    if not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        raise ScenarioError(f"'fraction' of '{via}' must be in (0, 1], got {fraction}")
    x0, y0 = _centroid(_junction(streets, via, from_via))
    axis = None
    if to_via:
        x1, y1 = _centroid(_junction(streets, via, to_via))
        norm = math.hypot(x1 - x0, y1 - y0)
        if norm == 0:
            raise ScenarioError(
                f"'{from_via}' and '{to_via}' meet '{via}' at the same point"
            )
        axis = ((x1 - x0) / norm, (y1 - y0) / norm)

    # distance of the edge midpoint from the junction
    def distance_jun2midpoint(e):
        a, b = e.getFromNode().getCoord(), e.getToNode().getCoord()
        mx, my = (a[0] + b[0]) / 2 - x0, (a[1] + b[1]) / 2 - y0
        return mx * axis[0] + my * axis[1] if axis else math.hypot(mx, my)

    edges = streets[via]
    target = fraction * sum(e.getLength() for e in edges)
    chosen = []
    closed = 0.0
    for e in sorted(
        (e for e in edges if distance_jun2midpoint(e) >= 0),
        key=lambda e: (distance_jun2midpoint(e), e.getID()),
    ):
        # stop when the next edge would take further from the target
        if abs(closed + e.getLength() - target) >= abs(closed - target):
            break
        chosen.append(e.getID())
        closed += e.getLength()
    if not chosen:
        raise ScenarioError(
            f"'fraction' {fraction} of '{via}' from '{from_via}' selects no edge"
        )
    return chosen


def resolve_demand(
    streets: Dict[str, list], zones: Dict[str, List[str]], intervention: dict, n: int
) -> dict:
    """A demand change as a rule for filtering.scale_od_matrices. The zones are given as 'taz',
    or as the zones crossed by a 'via' (optionally 'between' two cross streets), or none (whole net).
    """
    name = f"intervention {n}"
    factor = intervention.get("factor")
    if not isinstance(factor, (int, float)) or factor < 0 or factor == 1:
        raise ScenarioError(
            f"{name}: 'factor' must be >= 0 and not 1 (0.8 = -20%, 1.2 = +20%)"
        )
    if intervention.get("edges") or (
        intervention.get("via") and intervention.get("taz")
    ):
        raise ScenarioError(
            f"{name}: a demand change takes 'taz' or 'via', or nothing for the whole network"
        )
    if intervention.get("via"):
        street_edges = set(
            resolve_street(streets, intervention["via"], intervention.get("between"))
        )
        taz_ids = [z for z, edges in zones.items() if street_edges & set(edges)]
        if not taz_ids:
            raise ScenarioError(
                f"{name}: no TAZ contains edges of '{intervention['via']}'"
            )
    else:
        taz_ids = [str(z) for z in intervention.get("taz") or []]
        missing = [z for z in taz_ids if z not in zones]
        if missing:
            raise ScenarioError(f"{name}: TAZ not in the TAZ file: {missing}")
    direction = intervention.get("direction", "both")
    if direction not in ("both", "from", "to"):
        raise ScenarioError(
            f"{name}: 'direction' must be 'from', 'to' or 'both', got '{direction}'"
        )
    begin, end = 0, 24 * 3600  # whole day
    time = intervention.get("time")
    if time is not None:
        if (
            not isinstance(time, list)
            or len(time) != 2
            or not all(
                isinstance(t, str) and re.fullmatch(r"\d{1,2}:\d{2}", t) for t in time
            )
        ):
            raise ScenarioError(
                f"{name}: 'time' must be [start, end] in the format HH:MM, got {time}"
            )
        begin, end = _seconds(time[0]), _seconds(time[1])
        if not 0 <= begin < end <= 24 * 3600:
            raise ScenarioError(f"{name}: 'time' {time} must go forward within the day")
    return {
        "taz": taz_ids,
        "direction": direction,
        "begin": begin,
        "end": end,
        "factor": factor,
    }


def resolve_tls(
    net: "sumolib.net.Net", streets: Dict[str, list], intervention: dict, n: int
) -> dict:
    """Changes in traffic light programs.
    tls = id, via+at = streets involved in the junction. action=color state, cycle=time factor for every phase except yellow, or off state.
    """
    name = f"intervention {n}"
    action = intervention.get("action")
    if action not in TLS_ACTIONS:
        raise ScenarioError(
            f"{name}: 'action' must be one of {TLS_ACTIONS}, got '{action}'"
        )
    if intervention.get("via") and intervention.get("at"):
        tls_id, via_links, at_links = _variation_tls(
            net, streets, intervention["via"], intervention["at"]
        )
    elif intervention.get("tls") and action != "green":
        tls_id = intervention["tls"]
    else:
        raise ScenarioError(
            f"{name}: give 'via' and 'at', or 'tls' for 'cycle' and 'off'"
        )
    if tls_id not in cfg.CONTROLLED_TLS:
        raise ScenarioError(
            f"{name}: traffic light '{tls_id}' has no real program set. \nOptions: {cfg.CONTROLLED_TLS}"
        )

    program = next(
        iter(net.getTLS(tls_id).getPrograms().values())
    )  # one program per light
    if program.getType() != "static":
        raise ScenarioError(
            f"{name}: traffic light '{tls_id}' is not static, only static programs can be changed"
        )
    phases = [(float(p.duration), p.state) for p in program.getPhases()]
    not_yellow = [i for i, (_, state) in enumerate(phases) if "y" not in state]
    rule = {"tls": tls_id, "action": action}
    if action == "green":
        seconds = intervention.get("seconds")
        if not isinstance(seconds, (int, float)) or seconds == 0:
            raise ScenarioError(
                f"{name}: 'seconds' must be a number other than 0 (+10 = 10 s more green)"
            )
        phases_via = [
            i
            for i in not_yellow
            if _green_share(phases[i][1], via_links)
            > _green_share(phases[i][1], at_links)
        ]
        if not phases_via:
            raise ScenarioError(
                f"{name}: no phase of '{tls_id}' gives green to '{intervention['via']}'"
            )
        rule.update(seconds=seconds, phases=phases_via)
    elif action == "cycle":
        factor = intervention.get("factor")
        if not isinstance(factor, (int, float)) or factor <= 0 or factor == 1:
            raise ScenarioError(
                f"{name}: 'factor' must be > 0 and not 1 (1.2 = cycle 20% longer)"
            )
        rule.update(factor=factor, phases=not_yellow)
    rule["cycle_before_s"] = round(sum(d for d, _ in phases), 1)
    rule["cycle_after_s"] = (
        None
        if action == "off"
        else round(
            sum(_assign_new_duration(dur, idx, rule) for idx, (dur, _) in enumerate(phases)), 1
        )
    )
    return rule


def resolve_intervention(
    net: "sumolib.net.Net",
    streets: Dict[str, list],
    zones: Dict[str, List[str]],
    intervention: dict,
    n: int,
) -> Tuple[List[str], float]:
    """Edges touched by one intervention, and its severity."""
    name = f"intervention {n}"

    # what kind of intervention?
    kind = intervention.get("type")
    if kind not in INTERVENTION_TYPE:
        raise ScenarioError(
            f"{name}: 'type' must be one of {INTERVENTION_TYPE}, got '{kind}'"
        )
    # how severe?
    if kind == "closure":
        severity = CLOSURE_SEVERITY
    elif kind == "partial_closure":
        severity = intervention.get("severity")
        if not isinstance(severity, (int, float)) or not 0 < severity < 1:
            raise ScenarioError(
                f"{name}: 'severity' must be between 0 and 1 (excluded)"
            )
    else:
        raise ScenarioError(f"{name}: {kind} not a closure")
    edges_to_close = _target_closure_edges(
        net=net, streets=streets, zones=zones, intervention=intervention, name=name
    )

    return edges_to_close, severity


def resolve_scenario(net: "sumolib.net.Net", scenario: dict, taz_file: Path) -> dict:
    streets = edges_by_street(net)
    zones = taz_edges(taz_file)

    severities = {}  # edge -> severity
    speeds = {}  # edge -> new speed
    tls_rules = []
    edges_per_intervention = []
    report = []
    demand = []  # rules for filtering.scale_od_matrices
    for n, intervention in enumerate(scenario["interventions"], start=1):
        kind = intervention.get("type")
        if kind == "demand":
            rule = resolve_demand(streets, zones, intervention, n)
            demand.append(rule)
            edges_per_intervention.append([])
            report.append(
                {
                    "n_edges": 0,
                    "length_m": 0.0,
                    "severity": None,
                    "edge_ids": [],
                    **rule,
                }
            )
            continue

        elif kind == "tls":
            rule = resolve_tls(net=net, streets=streets, intervention=intervention, n=n)
            tls_rules.append(rule)
            edges_per_intervention.append([])
            incoming_edges = sorted(
                e.getID() for e in net.getTLS(rule["tls"]).getEdges()
            )
            rule_data = {k: v for k, v in rule.items() if k != "phases"}
            report.append(
                {
                    "n_edges": 0,
                    "length_m": 0.0,
                    "severity": None,
                    "edge_ids": incoming_edges,
                    **rule_data,
                }
            )
            continue

        elif kind == "speed":
            edges, speed = _speed(net, streets, zones, intervention, n)
            edges_per_intervention.append([])
            for e in edges:  # RULE: an edge hit twice keeps the lowest limit
                speeds[e] = min(speed, speeds.get(e, speed))
            report.append(
                {
                    "n_edges": len(edges),
                    "length_m": round(
                        sum(net.getEdge(e).getLength() for e in edges), 1
                    ),
                    "severity": None,
                    "edge_ids": edges,
                    "ms": intervention["ms"],
                    "ms_before": sorted(
                        {round(net.getEdge(e).getSpeed(), 1) for e in edges}
                    ),
                }
            )
            continue

        edges, severity = resolve_intervention(net, streets, zones, intervention, n)
        edges_per_intervention.append(edges)

        # an edge hit twice keeps the strongest closure (lowest severity)
        for e in edges:
            if e not in severities or severity < severities[e]:
                severities[e] = severity

        length = sum(net.getEdge(e).getLength() for e in edges)
        rep = {
            "n_edges": len(edges),
            "length_m": round(length, 1),
            "severity": severity,
            "edge_ids": edges,  # for the chatbot map
        }
        if intervention.get("fraction") is not None:
            street_length = sum(e.getLength() for e in streets[intervention["via"]])
            rep["fraction_effective"] = round(length / street_length, 3)
        report.append(rep)

    # severity -> number of lanes to close
    lanes_closed = {}
    lanes_kept = {}  # lanes left open because a turn starts or ends only there
    for e, severity in severities.items():
        edge = net.getEdge(e)
        wanted = lanes_to_close(edge.getLaneNumber(), severity)
        k = len(which_lanes_to_close(edge, wanted))
        if k > 0:
            lanes_closed[e] = k
        if k < wanted:
            lanes_kept[e] = wanted - k

    # now that the lanes are known, complete the report
    for rep, edges in zip(report, edges_per_intervention):
        rep["lanes_closed"] = sum(lanes_closed.get(e, 0) for e in edges)
        rep["lanes_kept_for_turns"] = sum(lanes_kept.get(e, 0) for e in edges)
        rep["edges_unchanged"] = len([e for e in edges if e not in lanes_closed])

    # edges with every lane closed
    closed_edges = sorted(
        e for e, k in lanes_closed.items() if k == net.getEdge(e).getLaneNumber()
    )
    changed = {e: severities[e] for e in lanes_closed}
    return {
        "edges": changed,
        "lanes_closed": lanes_closed,
        "closed_edges": closed_edges,
        "speeds": speeds,
        "tls": tls_rules,
        "demand": demand,
        "interventions": report,
    }
