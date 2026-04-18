"""Inspect the actual exported spawn_volumes.py + OBJ files for PH3/PH4."""
import re, os

script = r"C:\Users\oranbenshaprut\Desktop\PP\spawn_volumes.py"
mesh_root = r"C:\Users\oranbenshaprut\Desktop\PP\meshes"

src = open(script, encoding="utf-8").read()

# Find APT_TYPE_MESH_INFO entries
m = re.search(r'APT_TYPE_MESH_INFO\s*=\s*\{(.*?)^\}', src, re.DOTALL | re.MULTILINE)
if not m:
    print("Could not find APT_TYPE_MESH_INFO"); raise SystemExit
body = m.group(1)

# Per-type dict blocks: "PH3": { ... }
for name in ("PH3", "PH4"):
    mm = re.search(rf'"{name}"\s*:\s*\{{([^{{}}]*)\}}', body)
    if not mm:
        print(f"{name}: not found in APT_TYPE_MESH_INFO"); continue
    blk = mm.group(1)
    print(f"=== {name} (in spawn_volumes.py) ===")
    for line in blk.strip().splitlines():
        print("  ", line.strip())
    print()

# Find APARTMENTS list entries for PH3/PH4
# Loose regex: captures dict-ish lines containing '"type_name": "PH3"' etc.
for name in ("PH3", "PH4"):
    for mm in re.finditer(rf'"type_name"\s*:\s*"{name}"[^\n]*\n(?:[^\n]*\n){{0,4}}', src):
        seg = mm.group(0)
        print(f"--- APARTMENTS entry for {name} ---")
        print(seg.strip()[:400])
        print()

# Inspect OBJ files
print("=" * 60)
print("OBJ FILES")
print("=" * 60)
for name in ("PH3", "PH4"):
    matches = [f for f in os.listdir(mesh_root) if name in f and f.endswith(".obj")]
    for fn in matches:
        path = os.path.join(mesh_root, fn)
        txt = open(path).read()
        verts = []
        for ln in txt.splitlines():
            if ln.startswith("v "):
                parts = ln.split()
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        if not verts:
            continue
        xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
        print(f"{fn}")
        print(f"  vertex count: {len(verts)}")
        print(f"  X range: {min(xs):8.1f} .. {max(xs):8.1f}  (span {max(xs)-min(xs):7.1f})")
        print(f"  Y range: {min(ys):8.1f} .. {max(ys):8.1f}  (span {max(ys)-min(ys):7.1f})")
        print(f"  Z range: {min(zs):8.1f} .. {max(zs):8.1f}  (span {max(zs)-min(zs):7.1f})")
        print(f"  (all units are cm in OBJ)")
        print()
