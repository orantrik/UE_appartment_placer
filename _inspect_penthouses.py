"""Directly inspect PH3/PH4 polygons in the most recent calibration."""
import json, os

cal = r"C:\Users\oranbenshaprut\Documents\Claude\ue-apartment-placer\dist\plan_calibration_soho_lastonewithsameplace22222.json"
d = json.load(open(cal, encoding="utf-8"))

print(f"File: {os.path.basename(cal)}")
print(f"scale_px_per_m: {d.get('scale_px_per_m')}")
print(f"north_angle_deg: {d.get('north_angle_deg')}\n")

polys = d.get("apt_type_polygons", [])
for p in polys:
    if p.get("type_name", "").upper() in ("PH3", "PH4"):
        t = p["type_name"]
        pw = p.get("polygon_world_m", [])
        pi = p.get("polygon_img", [])
        cw = p.get("center_img")
        wx = p.get("world_x_m")
        wy = p.get("world_y_m")
        print(f"=== {t} ===")
        print(f"  world centroid: ({wx}, {wy})")
        print(f"  center_img: {cw}")
        print(f"  polygon_world_m ({len(pw)} pts):")
        xs = [pt[0] for pt in pw]
        ys = [pt[1] for pt in pw]
        if xs:
            print(f"    X range: {min(xs):.2f} .. {max(xs):.2f}  (span {max(xs)-min(xs):.2f} m)")
            print(f"    Y range: {min(ys):.2f} .. {max(ys):.2f}  (span {max(ys)-min(ys):.2f} m)")
        for i, pt in enumerate(pw):
            print(f"    {i:2d}: ({pt[0]:8.3f}, {pt[1]:8.3f})")
        print(f"  polygon_img ({len(pi)} pts):")
        for i, pt in enumerate(pi):
            print(f"    {i:2d}: ({pt[0]:8.1f}, {pt[1]:8.1f})")
        print()

# Compare PH3 vs PH4 overlap
ph3 = next((p for p in polys if p.get("type_name", "").upper() == "PH3"), None)
ph4 = next((p for p in polys if p.get("type_name", "").upper() == "PH4"), None)
if ph3 and ph4:
    print("=" * 60)
    print("OVERLAP ANALYSIS")
    print("=" * 60)
    ph3_pw = ph3.get("polygon_world_m", [])
    ph4_pw = ph4.get("polygon_world_m", [])
    x3 = [pt[0] for pt in ph3_pw]; y3 = [pt[1] for pt in ph3_pw]
    x4 = [pt[0] for pt in ph4_pw]; y4 = [pt[1] for pt in ph4_pw]
    print(f"PH3 bounding box: X=[{min(x3):.2f}, {max(x3):.2f}]  Y=[{min(y3):.2f}, {max(y3):.2f}]")
    print(f"PH4 bounding box: X=[{min(x4):.2f}, {max(x4):.2f}]  Y=[{min(y4):.2f}, {max(y4):.2f}]")
    ox_low = max(min(x3), min(x4)); ox_hi = min(max(x3), max(x4))
    oy_low = max(min(y3), min(y4)); oy_hi = min(max(y3), max(y4))
    if ox_hi > ox_low and oy_hi > oy_low:
        print(f"BBox OVERLAP: X=[{ox_low:.2f}, {ox_hi:.2f}]  Y=[{oy_low:.2f}, {oy_hi:.2f}]  "
              f"area={((ox_hi-ox_low)*(oy_hi-oy_low)):.1f} m²")
    else:
        print("Bounding boxes do NOT overlap")

    # Actual polygon overlap via shapely
    try:
        from shapely.geometry import Polygon as SPoly
        p3 = SPoly(ph3_pw)
        p4 = SPoly(ph4_pw)
        print(f"\nPH3 area: {p3.area:.1f} m²   is_valid: {p3.is_valid}")
        print(f"PH4 area: {p4.area:.1f} m²   is_valid: {p4.is_valid}")
        inter = p3.intersection(p4)
        print(f"Actual polygon intersection area: {inter.area:.2f} m²")
        if inter.area > 0.1:
            print(f"  -> THE POLYGONS OVERLAP BY {inter.area:.2f} m²")
            print(f"  -> This is a SOURCE DATA issue (polygons overlap on the plan)")
        else:
            print(f"  -> polygons are properly separated on the plan")
    except ImportError:
        print("(install shapely for exact area intersection)")
