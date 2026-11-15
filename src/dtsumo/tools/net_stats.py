"""
DTSumo Network statistics.
"""
import argparse
import math
import xml.etree.ElementTree as ET

import sumolib

from dtsumo.inputs import config as cfg

p = argparse.ArgumentParser()
p.add_argument("--net", default=str(cfg.NET_FILE))
p.add_argument("--taz", default=str(cfg.TAZ))
p.add_argument("--summary", help="Summary xml of the reference run (seed 36)")
p.add_argument("--radius-km", type=float, default=1.0)
arguments = p.parse_args()

net = sumolib.net.readNet(arguments.net, withPrograms=True)
edges = net.getEdges(withInternal=False)
nodes = [n for n in net.getNodes() if n.getType() != "internal"]
xmin, ymin, xmax, ymax = net.getBoundary()

print("network:", arguments.net)
print(f"area (circle, r={arguments.radius_km} km): {math.pi * arguments.radius_km ** 2:.2f} km2")
print(f"area (bounding box): {(xmax - xmin) * (ymax - ymin) / 1e6:.2f} km2")
print("TAZ:", len(list(sumolib.xml.parse(arguments.taz, "taz"))))
print("nodes:", len(nodes))
print("edges:", len(edges))
print("traffic lights:", len(net.getTrafficLights()))
print("roundabouts:", len(net.getRoundabouts()))
print(f"total edge length: {sum(e.getLength() for e in edges) / 1000:.2f} km")
print(f"total lane length: {sum(l.getLength() for e in edges for l in e.getLanes()) / 1000:.2f} km")

if arguments.summary:
    last = None
    for last in ET.parse(arguments.summary).getroot().iter("step"):
        pass
    for k in ("loaded", "inserted", "running", "waiting", "ended", "arrived", "halting", "teleports", "collisions"):
        print(f"{k} (end of day):", last.get(k))