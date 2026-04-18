"""One-shot patcher: stamp world_x_m / world_y_m / polygon_world_m onto every
apt-type polygon in a calibration JSON, using its scale_px_per_m.

Mirrors exactly the formulas used inside the app:
  - apt-type polygons:  cx/s, cy/s              (no rotation, no offset)
  - entrance polygons:  rotated by north_angle_deg, then /s
  - balcony cams:       cam.img_x/s, cam.img_y/s, z_cm = extrusion_m/2 * 100

Usage:
    python backfill_world_coords.py <path_to_calibration.json>
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


def patch(path: Path) -> None:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    s = data.get("scale_px_per_m")
    if not s:
        raise SystemExit(
            f"ERROR: {path} has scale_px_per_m={s}. "
            "Set the scale in the app and Save before running this patcher."
        )
    theta = math.radians(data.get("north_angle_deg", 0.0))
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    n_apts = n_apts_patched = 0
    n_ents = n_ents_patched = 0
    n_cams = n_cams_patched = 0

    for p in data.get("apt_type_polygons", []):
        n_apts += 1
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
        z_cm = round(p.get("extrusion_m", 3.0) / 2.0 * 100, 1)
        for cam in _migrate_balcony_cams(p):
            n_cams += 1
            if "img_x" not in cam or "img_y" not in cam:
                continue
            cam["world_x_m"] = round(cam["img_x"] / s, 3)
            cam["world_y_m"] = round(cam["img_y"] / s, 3)
            cam["z_cm"] = z_cm
            n_cams_patched += 1
        p["committed"] = True
        n_apts_patched += 1

    for ent in data.get("entrances", []):
        n_ents += 1
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
        n_ents_patched += 1

    out = path.with_name(path.stem + ".patched.json")
    with out.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"scale_px_per_m  = {s}")
    print(f"north_angle_deg = {data.get('north_angle_deg', 0.0)}")
    print(f"apt polygons    : {n_apts_patched}/{n_apts} patched")
    print(f"entrances       : {n_ents_patched}/{n_ents} patched")
    print(f"balcony cams    : {n_cams_patched}/{n_cams} patched")
    print(f"\nWrote: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python backfill_world_coords.py <calibration.json>")
    patch(Path(sys.argv[1]))
