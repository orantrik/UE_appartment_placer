"""Inspect PH1-PH4 polygon data and spawn extents."""
import os, re

mesh_root = r"C:\Users\oranbenshaprut\Desktop\PP\meshes"
script = r"C:\Users\oranbenshaprut\Desktop\PP\spawn_volumes.py"

# Spawn positions
spawn_pos = {}
for m in re.finditer(r"\('13', '1', '(PH\d)'\): \(([-\d.]+), ([-\d.]+),", open(script, encoding="utf-8").read()):
    spawn_pos[m.group(1)] = (float(m.group(2)), float(m.group(3)))

# OBJ extents
mesh_extents = {}
for fn in os.listdir(mesh_root):
    m = re.match(r"13_1_(PH\d)_.*\.obj$", fn)
    if not m:
        continue
    name = m.group(1)
    verts = []
    for ln in open(os.path.join(mesh_root, fn)).read().splitlines():
        if ln.startswith("v "):
            p = ln.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
    xs = [v[0] for v in verts]; ys = [v[1] for v in verts]
    mesh_extents[name] = (min(xs), max(xs), min(ys), max(ys))

print("=" * 80)
print(f"{'Type':<6} {'Spawn X':>10} {'Spawn Y':>10} {'World Xmin':>12} {'World Xmax':>12} {'World Ymin':>12} {'World Ymax':>12}")
print("=" * 80)
for ph in ("PH1", "PH2", "PH3", "PH4"):
    if ph not in spawn_pos or ph not in mesh_extents:
        print(f"{ph}: MISSING data"); continue
    sx, sy = spawn_pos[ph]
    xmin, xmax, ymin, ymax = mesh_extents[ph]
    wxmin = sx + xmin; wxmax = sx + xmax
    wymin = sy + ymin; wymax = sy + ymax
    print(f"{ph:<6} {sx:>10.1f} {sy:>10.1f} {wxmin:>12.1f} {wxmax:>12.1f} {wymin:>12.1f} {wymax:>12.1f}")

print()
print("=" * 80)
print("OVERLAP CHECK (world-space bounding boxes)")
print("=" * 80)
from itertools import combinations
pairs = list(combinations(["PH1", "PH2", "PH3", "PH4"], 2))
for a, b in pairs:
    if a not in spawn_pos or b not in spawn_pos: continue
    ax, ay = spawn_pos[a]; bx, by = spawn_pos[b]
    axmin, axmax, aymin, aymax = mesh_extents[a]
    bxmin, bxmax, bymin, bymax = mesh_extents[b]
    wa = (ax+axmin, ay+aymin, ax+axmax, ay+aymax)
    wb = (bx+bxmin, by+bymin, bx+bxmax, by+bymax)
    ox_low = max(wa[0], wb[0]); ox_hi = min(wa[2], wb[2])
    oy_low = max(wa[1], wb[1]); oy_hi = min(wa[3], wb[3])
    if ox_hi > ox_low and oy_hi > oy_low:
        area = (ox_hi - ox_low) * (oy_hi - oy_low) / 10000.0
        print(f"{a:<4} vs {b:<4}: BBOX OVERLAP  X=[{ox_low:.0f},{ox_hi:.0f}]  Y=[{oy_low:.0f},{oy_hi:.0f}]  = {area:.1f} m²")
    else:
        print(f"{a:<4} vs {b:<4}: no overlap (gap in {'X' if ox_hi<=ox_low else 'Y'})")
