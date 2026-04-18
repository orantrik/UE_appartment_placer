"""Verify the Y-flip fix: PH3 and PH4 should tile on Y without overlap.

Simulates what UE does on OBJ import (negates Y) and checks that the
resulting world-space Y extents of PH3 and PH4 do NOT overlap.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.generator import _polygon_to_obj

# PH3 polygon from calibration (scaled slightly, matches exported OBJ at 1.13x)
PH3_POLY = [
    (27.3522, 17.0192), (22.8764, 17.0192), (22.8764,  1.9893),
    (52.9363,  1.9893), (52.9363, 13.3170), (47.0238, 13.3170),
    (47.0238, 11.6592), (44.8135, 11.6592), (44.8135, 10.9409),
    (34.5357, 10.9409), (34.5357, 13.2617), (27.2970, 13.2617),
]
PH3_CENTROID = (38.252, 11.365)

PH4_POLY = [
    (52.646, 19.262), (52.646, 32.005), (23.067, 32.005),
    (23.067, 17.030), (27.452, 17.030), (27.452, 21.351),
    (29.866, 21.351), (29.866, 22.622), (43.529, 22.622),
    (43.529, 20.843), (45.499, 20.843), (45.499, 19.127),
]
PH4_CENTROID = (37.010, 22.174)

def extents(obj_text):
    ys = []
    for ln in obj_text.splitlines():
        if ln.startswith("v "):
            p = ln.split()
            ys.append(float(p[2]))
    return min(ys), max(ys)

ph3_obj = _polygon_to_obj(("13", "1", "PH3"), PH3_POLY, PH3_CENTROID, 3.0)
ph4_obj = _polygon_to_obj(("13", "1", "PH4"), PH4_POLY, PH4_CENTROID, 3.0)

ph3_obj_ymin, ph3_obj_ymax = extents(ph3_obj)
ph4_obj_ymin, ph4_obj_ymax = extents(ph4_obj)

print("OBJ file Y ranges (what we write to disk):")
print(f"  PH3: [{ph3_obj_ymin:+.2f}, {ph3_obj_ymax:+.2f}]")
print(f"  PH4: [{ph4_obj_ymin:+.2f}, {ph4_obj_ymax:+.2f}]")

# Simulate UE's import Y-flip — local mesh Y = -OBJ Y
ph3_local_ymin, ph3_local_ymax = -ph3_obj_ymax, -ph3_obj_ymin
ph4_local_ymin, ph4_local_ymax = -ph4_obj_ymax, -ph4_obj_ymin
print("\nMesh local Y ranges in UE (after UE's auto Y-flip):")
print(f"  PH3: [{ph3_local_ymin:+.2f}, {ph3_local_ymax:+.2f}]")
print(f"  PH4: [{ph4_local_ymin:+.2f}, {ph4_local_ymax:+.2f}]")

# Simulate spawn: actor_Y = (centroid_y - min_y) * 100
min_y = min(PH3_CENTROID[1], PH4_CENTROID[1])
ph3_actor_y = (PH3_CENTROID[1] - min_y) * 100
ph4_actor_y = (PH4_CENTROID[1] - min_y) * 100

ph3_world_ymin = ph3_actor_y + ph3_local_ymin
ph3_world_ymax = ph3_actor_y + ph3_local_ymax
ph4_world_ymin = ph4_actor_y + ph4_local_ymin
ph4_world_ymax = ph4_actor_y + ph4_local_ymax
print(f"\nActor Y (spawn cm): PH3={ph3_actor_y:.1f}  PH4={ph4_actor_y:.1f}")
print("\nWorld-space mesh Y ranges:")
print(f"  PH3: [{ph3_world_ymin:+.1f}, {ph3_world_ymax:+.1f}]")
print(f"  PH4: [{ph4_world_ymin:+.1f}, {ph4_world_ymax:+.1f}]")

overlap_lo = max(ph3_world_ymin, ph4_world_ymin)
overlap_hi = min(ph3_world_ymax, ph4_world_ymax)
overlap = max(0.0, overlap_hi - overlap_lo)

print(f"\nY-overlap: {overlap:.2f} cm  "
      f"({'BUG' if overlap > 1.0 else 'OK - polygons tile correctly'})")

gap = max(ph3_world_ymin, ph4_world_ymin) - min(ph3_world_ymax, ph4_world_ymax)
print(f"Gap between PH3 and PH4: {gap:.2f} cm")
