"""Smoke test for generator.py after the QA-pass refactor.

Not shipped — exists to prove:
  1. All three templates format without KeyError / IndexError.
  2. The emitted Python is syntactically valid (ast.parse round-trip).
  3. The shared preamble appears exactly once in each script.
  4. The batched BP spawn loop replaced the old single-transaction loop.
"""
from __future__ import annotations
import ast
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

import pandas as pd
from app.core.data_model import AppData
from app.core import generator


def _make_data() -> AppData:
    df = pd.DataFrame([
        {"Building": "A", "Entrance": "1", "Floor": 0, "AptID": "101",
         "Direction": "צפון", "Type": "T3"},
        {"Building": "A", "Entrance": "1", "Floor": 0, "AptID": "102",
         "Direction": "דרום", "Type": "T3"},
        {"Building": "A", "Entrance": "1", "Floor": 1, "AptID": "201",
         "Direction": "צפון", "Type": "T3"},
        {"Building": "A", "Entrance": "1", "Floor": 2, "AptID": "301",
         "Direction": "מזרח", "Type": "T4"},
    ])
    data = AppData(
        df=df,
        required_mappings={
            "building": "Building", "entrance": "Entrance", "floor": "Floor",
            "apt_id": "AptID", "direction": "Direction", "type": "Type",
        },
        extra_mappings=[("AptID", "UnitNumber")],
        floor_height_cm=330,
        floor_gaps_cm={1: 900},
        blueprint_path="/Game/BP_Apartment.BP_Apartment_C",
        calibration={
            "scale_px_per_m": 10.0,
            "entrances": [
                {"building_id": "A", "entrance_id": "1",
                 "world_x_m": 0.0, "world_y_m": 0.0,
                 "polygon_img": [(0, 0), (100, 0), (100, 100), (0, 100)]},
            ],
            "apt_type_polygons": [
                {"building_id": "A", "entrance_id": "1", "type_name": "T3",
                 "world_x_m": 5.0, "world_y_m": 5.0,
                 "polygon_world_m": [(0, 0), (8, 0), (8, 6), (0, 6)],
                 "extrusion_m": 3.3, "color_hex": "#6bcb77",
                 "uid": "abc1234567"},
                {"building_id": "A", "entrance_id": "1", "type_name": "T4",
                 "world_x_m": 20.0, "world_y_m": 5.0,
                 "polygon_world_m": [(0, 0), (10, 0), (10, 8), (0, 8)],
                 "extrusion_m": 3.3, "color_hex": "#4d96ff",
                 "uid": "def9876543",
                 "balcony_cams": [
                     {"world_x_m": 22.0, "world_y_m": 7.0, "z_cm": 120.0,
                      "yaw_deg": 45.0},
                     {"world_x_m": 25.0, "world_y_m": 8.0, "z_cm": 140.0,
                      "yaw_deg": 90.0},
                 ],
                 # Per-polygon SA with an explicit pitch override.
                 "spring_arm": {
                     "world_x_m": 28.0, "world_y_m": 9.0,
                     "pitch_deg": 12.5,
                 }},
            ],
            # Global default SA pitch. Used only for the T3 polygon above,
            # which has no per-polygon spring_arm entry — so T3's bake should
            # have value None for its pitch slot while T4 keeps 12.5.
            # We still bake DEFAULT_SA_PITCH_DEG=-5.0 so at spawn time T3
            # would fall back to it (currently T3 has no SA entry so it's
            # simply absent from SPRING_ARM_INFO — the default still ships).
            "default_spring_arm_pitch_deg": -5.0,
        },
    )
    data.default_spring_arm_pitch_deg = -5.0
    return data


def _check_script(name: str, script: str) -> None:
    print(f"\n=== {name} ({len(script)} bytes) ===")
    # Must parse as valid Python.
    ast.parse(script, filename=f"<{name}>")
    print(f"  [ok] ast.parse succeeded")

    # Preamble fingerprints appear exactly once.
    for marker in ("def _spawn_actor(", "def _z_for_floor(",
                   "def _dir_label(",
                   "unreal.EditorLevelLibrary.editor_is_in_play_mode("):
        count = script.count(marker)
        assert count == 1, f"{marker!r} appears {count}x in {name}, expected 1"
        print(f"  [ok] {marker!r} appears exactly once")

    # No raw EditorLevelLibrary.spawn_actor_from_class left (should all go
    # through _spawn_actor now).
    leaks = script.count("EditorLevelLibrary.spawn_actor_from_class")
    # Allowed: the fallback INSIDE _spawn_actor itself (one occurrence).
    assert leaks == 1, f"{name}: {leaks} direct spawn_actor_from_class calls (want 1 inside _spawn_actor)"
    print(f"  [ok] only 1 spawn_actor_from_class call (inside _spawn_actor)")


def _check_bp_template_batching(script: str) -> None:
    # H1: batched BP spawn loop should contain _BATCH_SIZE and _spawn_one_bp.
    assert "_BATCH_SIZE" in script, "BP template missing _BATCH_SIZE"
    assert "def _spawn_one_bp(apt):" in script, "BP template missing _spawn_one_bp"
    assert "for _bidx in range(0, len(APARTMENTS), _BATCH_SIZE):" in script
    print("  [ok] BP spawn loop is batched (H1)")


def _check_m1a_warn(script: str) -> None:
    # M1a: emitted extra-props block should print warnings, not silently pass.
    assert "_exprop_ex" in script, "M1a: extra-props still swallows exceptions"
    assert "except Exception: pass" not in script or script.count("except Exception: pass") <= 0
    print("  [ok] extra-props warns instead of swallowing (M1a)")


def _check_pie_guard(script: str) -> None:
    assert "Play-in-Editor is active" in script, "PIE guard missing"
    print("  [ok] PIE guard present (C2)")


def _check_fs_fallback(script: str) -> None:
    assert "SCRIPT_DIR_OVERRIDE" in script, "__file__ fallback missing"
    assert "except NameError:" in script
    print("  [ok] __file__ fallback present (C1)")


def _check_m4_warn(script: str) -> None:
    assert "AddComponentByClass returned no new" in script
    print("  [ok] AddComponentByClass non-exception failure now warns (M4)")


def main() -> int:
    data = _make_data()

    bp_script = generator.generate(data)
    _check_script("BP Script", bp_script)
    _check_bp_template_batching(bp_script)
    _check_m1a_warn(bp_script)
    _check_pie_guard(bp_script)

    vol_script, obj_files = generator.generate_volumes(
        data, use_folders=True, use_poi=False, project_name="SmokeTest")
    _check_script("Volumes", vol_script)
    _check_pie_guard(vol_script)
    _check_fs_fallback(vol_script)
    print(f"  [ok] {len(obj_files)} OBJ/MTL files emitted")

    poi_script, poi_obj_files = generator.generate_volumes(
        data, use_folders=True, use_poi=True, project_name="SmokeTest")
    _check_script("POI Volumes", poi_script)
    _check_pie_guard(poi_script)
    _check_fs_fallback(poi_script)
    _check_m4_warn(poi_script)
    print(f"  [ok] {len(poi_obj_files)} asset file(s) emitted")

    # Part 1 guarantees on the main POI spawn script:
    assert "args=(_cam_class, True, unreal.Transform(), False)" in poi_script, \
        "POI spawn: cam #2+ should use manual_attachment=True"
    assert "_comp.register_component()" in poi_script, \
        "POI spawn: cam #2+ should call register_component"
    assert "attach_to_component" in poi_script and \
        "KEEP_RELATIVE" in poi_script, \
        "POI spawn: cam #2+ should attach_to_component with KEEP_RELATIVE"
    assert "set_relative_location" in poi_script and \
        "set_relative_rotation" in poi_script, \
        "POI spawn: cam #2+ should set relative location/rotation"
    # Cam #1 must still use world setters (SCS-persistent, follows actor).
    assert "set_world_location" in poi_script and \
        "set_world_rotation" in poi_script, \
        "POI spawn: cam #1 should still use world setters"
    print("  [ok] POI spawn: cam #1=world / cam #2+=attached-relative (Part 1)")

    # Part 2: refresh utility shipped alongside spawn_volumes.py.
    assert "refresh_balcony_cams.py" in poi_obj_files, \
        "refresh_balcony_cams.py not emitted with POI scripts"
    refresh = poi_obj_files["refresh_balcony_cams.py"]
    ast.parse(refresh, filename="<refresh_balcony_cams.py>")
    print(f"  [ok] refresh_balcony_cams.py emitted and parseable "
          f"({len(refresh)} bytes)")
    for m in ("PORCH_CAM_INFO", "APT_TYPE_MESH_INFO", "APARTMENTS",
              "_APT_LOOKUP", "unreal.AttachmentRule.KEEP_RELATIVE",
              "register_component", "set_relative_location",
              "ScopedEditorTransaction"):
        assert m in refresh, f"refresh script missing {m!r}"
    # Cam #1 must NOT be recreated in refresh (SCS-persistent).
    assert "_pcams[1:]" in refresh, \
        "refresh script should iterate only cam #2..N"
    print("  [ok] refresh script uses Part 1 attach pipeline + skips cam #1")

    # SpringArm bake checks (POI spawn + refresh).
    for name, script in (("POI Volumes", poi_script),
                         ("refresh_balcony_cams.py", refresh)):
        for marker in ("SPRING_ARM_INFO", "DEFAULT_SA_PITCH_DEG",
                       "def _sa_compute(", "def _apply_spring_arm("):
            assert marker in script, f"{name} missing {marker!r}"
        # Default must be baked as -5.0 (not 0.0) — proves AppData wiring.
        assert "DEFAULT_SA_PITCH_DEG = -5.0" in script, \
            f"{name}: DEFAULT_SA_PITCH_DEG not baked from AppData"
        # T4 polygon's per-polygon pitch override (12.5) must survive.
        assert "12.5" in script, \
            f"{name}: per-polygon SA pitch override not baked"
        # SpringArmComponent must be looked up robustly.
        assert "SpringArmComponent" in script, \
            f"{name}: missing SpringArmComponent lookup"
        assert "target_arm_length" in script, \
            f"{name}: missing target_arm_length setter"
        print(f"  [ok] {name}: SA baked correctly "
              f"(DEFAULT=-5.0, override=12.5, helpers present)")

    # Refresh script must actively call _apply_spring_arm (not just define it).
    assert refresh.count("_apply_spring_arm(") >= 2, \
        "refresh script should both define and call _apply_spring_arm"
    print("  [ok] refresh script invokes _apply_spring_arm")

    # Regression guard for the UE Python Rotator alphabetical positional
    # footgun: `unreal.Rotator(pitch, yaw, roll)` MUST use keyword args in
    # the SA apply path, because positional order in UE Python is actually
    # (pitch, roll, yaw) — passing yaw as the 2nd positional would silently
    # land in the roll slot, zeroing the real yaw. See commit touching
    # _apply_spring_arm for the full postmortem.
    for name, script in (("POI Volumes", poi_script),
                         ("refresh_balcony_cams.py", refresh)):
        assert "pitch=float(_pitch_ue)" in script, \
            f"{name}: _apply_spring_arm must use Rotator(pitch=..., yaw=...)"
        assert "yaw=float(_yaw_ue)" in script, \
            f"{name}: _apply_spring_arm must use Rotator(pitch=..., yaw=...)"
        # And make sure the old buggy positional form isn't lurking.
        assert "Rotator(float(_pitch_ue), float(_yaw_ue)" not in script, \
            f"{name}: positional Rotator(pitch, yaw, roll) is wrong — " \
            f"UE Python uses (pitch, ROLL, yaw) positionally"
    print("  [ok] SA apply uses keyword-arg Rotator (pitch=..., yaw=...)")

    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
