"""
One-shot patcher for UE Apartment Placer calibration JSONs that were saved
with polygons drawn before the scale was set.

Reads `scale_px_per_m` from the JSON and stamps `world_x_m`, `world_y_m`,
`polygon_world_m` onto every apt-type polygon (and `world_x_m`, `world_y_m`,
`z_cm` onto every balcony cam) using the same formulas as the in-app
Commit action.

Usage:
    python patch_calibration.py <input.json> [output.json]

If output.json is omitted, the input file is overwritten in place.
The original is preserved as <input>.bak (you should also keep your own
copy before running this).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def _migrate_balcony_cams(p: dict) -> list[dict]:
    cams = p.get("balcony_cams")
    if isinstance(cams, list):
        return cams
    legacy = p.get("balcony_cam")
    if isinstance(legacy, dict):
        return [legacy]
    return []


def patch(data: dict) -> tuple[int, int, int]:
    """Mutate `data` in place. Returns (apt_polys_fixed, ents_fixed, cams_fixed)."""
    s = data.get("scale_px_per_m")
    if not s:
        raise SystemExit(
            "ERROR: scale_px_per_m is missing or zero in the JSON. "
            "Open the file in the app, set the Scale tool, hit Save, then re-run."
        )
    theta = math.radians(data.get("north_angle_deg", 0.0))
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    apt_fixed = 0
    ent_fixed = 0
    cam_fixed = 0

    for p in data.get("apt_type_polygons", []):
        pts = p.get("polygon_img") or []
        if len(pts) < 3:
            continue
        cx = sum(pt[0] for pt in pts) / len(pts)
        cy = sum(pt[1] for pt in pts) / len(pts)
        p["center_img"] = [cx, cy]
        p["world_x_m"] = round(cx / s, 3)
        p["world_y_m"] = round(cy / s, 3)
        p["polygon_world_m"] = [
            [round(px / s, 4), round(py / s, 4)] for px, py in pts
        ]
        p["committed"] = True
        z_cm = round(p.get("extrusion_m", 3.0) / 2.0 * 100, 1)
        for cam in _migrate_balcony_cams(p):
            if "img_x" in cam and "img_y" in cam:
                cam["world_x_m"] = round(cam["img_x"] / s, 3)
                cam["world_y_m"] = round(cam["img_y"] / s, 3)
                cam["z_cm"] = z_cm
                cam_fixed += 1
        apt_fixed += 1

    for ent in data.get("entrances", []):
        pts = ent.get("polygon_img") or []
        if len(pts) < 3:
            continue
        cx = sum(pt[0] for pt in pts) / len(pts)
        cy = sum(pt[1] for pt in pts) / len(pts)
        ent["center_img"] = [cx, cy]
        ue_x = (cx * cos_t + cy * sin_t) / s
        ue_y = (-cx * sin_t + cy * cos_t) / s
        ent["world_x_m"] = round(ue_x, 3)
        ent["world_y_m"] = round(ue_y, 3)
        ent_fixed += 1

    return apt_fixed, ent_fixed, cam_fixed


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else in_path

    with in_path.open(encoding="utf-8") as f:
        data = json.load(f)

    s = data.get("scale_px_per_m")
    print(f"Loaded: {in_path}")
    print(f"  scale_px_per_m  = {s}")
    print(f"  north_angle_deg = {data.get('north_angle_deg', 0.0)}")
    print(f"  apt polygons    = {len(data.get('apt_type_polygons', []))}")
    print(f"  entrances       = {len(data.get('entrances', []))}")

    apt_n, ent_n, cam_n = patch(data)

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Patched: {out_path}")
    print(f"  apt polygons stamped: {apt_n}")
    print(f"  entrances  stamped:   {ent_n}")
    print(f"  cameras    stamped:   {cam_n}")


if __name__ == "__main__":
    main()
