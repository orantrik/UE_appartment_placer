from __future__ import annotations
import re
import textwrap
import uuid
import pandas as pd
from .data_model import AppData


def _make_mesh_root(project_name: str) -> str:
    """Build a unique /Game/ApartmentMeshes/<safe_name>_<hash>/ root path.

    The project name is sanitized to ASCII alphanumerics + underscore so
    Unreal accepts it as a folder name. Empty / fully-stripped names fall
    back to ``Project``. An 8-char random suffix guarantees that re-importing
    the same script (or generating a new one) never collides with previous
    batches in the Content Browser.
    """
    raw = (project_name or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    if not safe:
        safe = "Project"
    suffix = uuid.uuid4().hex[:8]
    return f"/Game/ApartmentMeshes/{safe}_{suffix}"

# ── Direction sort helper (used only inside generator, NOT in generated script) ──
_DIR_VEC = {
    "צפון": (0.0, 1.0), "דרום": (0.0, -1.0),
    "מזרח": (1.0, 0.0), "מערב": (-1.0, 0.0),
    "North": (0.0, 1.0), "South": (0.0, -1.0),
    "East": (1.0, 0.0),  "West": (-1.0, 0.0),
}

def _dir_sort_key(s: str):
    # lstrip per-word: strips Hebrew conjunction ו from word START only,
    # so "צפון" stays "צפון" while "ומזרח" becomes "מזרח"
    dx = dy = 0.0; n = 0
    for w in str(s).split():
        w = w.lstrip("ו")
        if w in _DIR_VEC:
            vx, vy = _DIR_VEC[w]; dx += vx; dy += vy; n += 1
    return (-dy / n if n else 0, -dx / n if n else 0)


def _build_apartments(data: AppData) -> list[dict]:
    rm = data.required_mappings
    df = data.df.copy()

    for key in ("building", "entrance", "floor"):
        col = rm.get(key)
        if col and col in df.columns:
            df[col] = df[col].ffill()

    apt_col = rm.get("apt_id")
    if apt_col:
        df = df.dropna(subset=[apt_col])

    rows = []
    for _, row in df.iterrows():
        def get(key, default=""):
            col = rm.get(key)
            if not col or col not in df.columns:
                return default
            v = row[col]
            return default if pd.isna(v) else v

        raw_id = get("apt_id", "")
        try:
            fv = float(str(raw_id))
            apt_id = str(int(fv)) if fv == int(fv) else str(raw_id)
        except (ValueError, TypeError, OverflowError):
            apt_id = str(raw_id)

        apt = {
            "building":  str(get("building", "A")),
            "entrance":  str(get("entrance", "1")),
            "floor":     float(get("floor", 0)),
            "apt_id":    apt_id,
            "direction": str(get("direction", "")),
            "type":      str(get("type", "")),
        }
        for excel_col, ue_var in data.extra_mappings:
            if excel_col in df.columns:
                v = row[excel_col]
                apt[ue_var] = "" if pd.isna(v) else v
        rows.append(apt)
    return rows


def _fmt_apartments(apts: list[dict]) -> str:
    lines = ["["]
    for a in apts:
        lines.append(f"    {repr(a)},")
    lines.append("]")
    return "\n".join(lines)


def _build_z_by_floor_cm(apts: list[dict], default_cm: int,
                         overrides: dict) -> dict[int, int]:
    """Compute a cumulative floor -> Z(cm) table.

    Z(0) is always 0. For each floor n, Z(n+1) = Z(n) + gap(n), where gap(n)
    comes from `overrides` (keyed by int(n)) if present, else `default_cm`.
    Negative floors walk downward symmetrically: Z(n) = Z(n+1) - gap(n).

    `overrides` keys may be str or int (JSON deserialisation sometimes
    stringifies int keys); both are accepted.

    The returned dict covers every integer floor between the min and max
    floors appearing in `apts` (inclusive), plus 0 in case the data skips it.
    """
    def _gap(n: int) -> int:
        if n in overrides:
            return int(overrides[n])
        s = str(n)
        if s in overrides:
            return int(overrides[s])
        return int(default_cm)

    floors = {0}
    for a in apts:
        try:
            floors.add(int(float(a.get("floor", 0))))
        except (TypeError, ValueError):
            continue
    for k in overrides.keys():
        try:
            ki = int(k)
        except (TypeError, ValueError):
            continue
        floors.add(ki)
        floors.add(ki + 1)

    f_min = min(floors)
    f_max = max(floors)

    z_by_floor: dict[int, int] = {0: 0}
    z = 0
    for n in range(0, f_max):
        z += _gap(n)
        z_by_floor[n + 1] = z
    z = 0
    for n in range(0, f_min, -1):
        z -= _gap(n - 1)
        z_by_floor[n - 1] = z
    return z_by_floor


def _fmt_z_by_floor(z_by_floor: dict[int, int]) -> str:
    """Format the Z-by-floor lookup as a deterministic dict literal."""
    if not z_by_floor:
        return "{}"
    items = sorted(z_by_floor.items())
    inside = ", ".join(f"{k}: {v}" for k, v in items)
    return "{" + inside + "}"


def _fmt_origins(calibration: dict) -> str:
    """Build the ENTRANCE_ORIGINS_CM dict literal from calibration data.

    Each value is (x_cm, y_cm, half_w_cm, half_h_cm) where half_w/h are
    derived from the entrance polygon bounding box. This lets compute_location
    clamp direction offsets to the actual footprint, preventing adjacent
    stairwells from overlapping.
    """
    entrances = calibration.get("entrances", [])
    scale = calibration.get("scale_px_per_m") or 1.0
    valid = [e for e in entrances
             if "world_x_m" in e and "world_y_m" in e]
    if not valid:
        return "{}"

    # Shift so the minimum X and Y become 0
    min_x = min(e["world_x_m"] for e in valid)
    min_y = min(e["world_y_m"] for e in valid)

    lines = ["{"]
    for e in valid:
        key = (str(e["building_id"]), str(e["entrance_id"]))
        x_cm = round((e["world_x_m"] - min_x) * 100, 1)
        y_cm = round((e["world_y_m"] - min_y) * 100, 1)

        # Bounding box from polygon pixels → cm
        poly = e.get("polygon_img", [])
        if len(poly) >= 3:
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            half_w_cm = round((max(xs) - min(xs)) / scale * 100 / 2, 1)
            half_h_cm = round((max(ys) - min(ys)) / scale * 100 / 2, 1)
        else:
            half_w_cm = half_h_cm = 400.0   # 4 m fallback

        lines.append(f"    {key!r}: ({x_cm}, {y_cm}, {half_w_cm}, {half_h_cm}),")
    lines.append("}")
    return "\n".join(lines)


def _extra_props_block(extra_mappings: list[tuple]) -> str:
    """Emit the per-apartment extra-property assignment block.

    M1a: previously used a bare `except Exception: pass`, so any failure
    (wrong property type, BP renamed, etc.) was silently dropped and the
    user would see "Spawned N actors" with zero indication that half their
    extra props never made it onto the actors. We now log the exception
    with the actor and property name so failures surface in the Output Log.

    The emitted code lives inside `def _spawn_one_bp(apt):` in the BP
    template, so it carries a 4-space indent (not 8 like the old inline
    version).
    """
    if not extra_mappings:
        return "    # No extra properties configured.\n"
    lines = []
    for _, ue_var in extra_mappings:
        lines.append(textwrap.dedent(f"""\
        try:
            actor.set_editor_property({ue_var!r}, str(apt.get({ue_var!r}, "")))
        except Exception as _exprop_ex:
            print(f"  WARN: could not set {ue_var!r} on apt "
                  f"{{apt.get('apt_id', '?')}}: {{_exprop_ex}}")"""))
    return "\n".join("    " + ln for block in lines for ln in block.splitlines()) + "\n"


# Same palette as plan_canvas._COLORS — used as fallback when color_hex is absent
_COLORS = [
    "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#cc5de8", "#74c0fc", "#f06595",
    "#a9e34b", "#63e6be",
]


def _ear_clip(verts: list) -> list[tuple[int, int, int]]:
    """Ear-clipping triangulation for a simple (non-self-intersecting) polygon.

    Vertices may be in screen-space (Y increases downward).  We negate Y
    internally so the cross-product / area maths use the standard Y-up
    convention, then return 0-based indices into the ORIGINAL verts list.
    """
    n = len(verts)
    if n < 3:
        return []
    if n == 3:
        return [(0, 1, 2)]

    # Convert to math-space (Y-up) for all geometric tests
    mv = [(x, -y) for x, y in verts]

    def _cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def _in_tri(p, a, b, c):
        d1, d2, d3 = _cross(a, b, p), _cross(b, c, p), _cross(c, a, p)
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))

    # Ensure CCW order in math-space
    area = sum(mv[i][0] * mv[(i + 1) % n][1] -
               mv[(i + 1) % n][0] * mv[i][1] for i in range(n))
    idx = list(range(n)) if area > 0 else list(reversed(range(n)))

    tris = []
    guard = n * n * 2
    i = 0
    while len(idx) > 3 and guard > 0:
        guard -= 1
        m = len(idx)
        prev, curr, nxt = idx[(i - 1) % m], idx[i % m], idx[(i + 1) % m]
        a, b, c = mv[prev], mv[curr], mv[nxt]
        if _cross(a, b, c) > 1e-10:          # convex vertex in CCW polygon
            if not any(_in_tri(mv[idx[j]], a, b, c)
                       for j in range(m) if idx[j] not in (prev, curr, nxt)):
                tris.append((prev, curr, nxt))
                idx.pop(i % m)
                i = max(i - 1, 0)
                continue
        i += 1
    if len(idx) == 3:
        tris.append(tuple(idx))
    return tris


def _hex_to_mtl(hex_color: str, safe_name: str) -> str:
    """Generate a minimal MTL file with the given hex colour as diffuse (Kd)."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return "\n".join([
        f"newmtl {safe_name}_mat",
        f"Kd {r:.4f} {g:.4f} {b:.4f}",
        "Ka 0.0 0.0 0.0",
        "Ks 0.0 0.0 0.0",
        "d 1.0",
    ])


def _polygon_to_obj(key: tuple, polygon_world_m: list, center_world_m: tuple,
                    extrusion_m: float, safe_name: str = "") -> str:
    """Extruded polygon OBJ — footprint in XY plane, Z = up (UE convention).

    Vertices are centred at the polygon centroid (→ actor spawn position).
    Units are UE cm.  Z = 0 is the bottom face; Z = H is the top face.
    The actor is spawned at (ox_cm, oy_cm, floor * FLOOR_HEIGHT_CM) so the
    mesh sits at exactly the drawn XY position and the correct floor height.
    """
    cx, cy = center_world_m
    verts = [(round((x - cx) * 100, 2), round((y - cy) * 100, 2))
             for x, y in polygon_world_m]
    n = len(verts)
    h = round(extrusion_m * 100, 2)

    mtl_ref = f"{safe_name}.mtl" if safe_name else ""
    lines = [f"# type={key[2]}  building={key[0]}  entrance={key[1]}"]
    if mtl_ref:
        lines.append(f"mtllib {mtl_ref}")

    # Ensure CCW winding in XY math space so outward normals are correct.
    # Canvas uses Y-down, so a screen-CCW polygon is CW in math space — reverse it.
    area2 = sum(verts[i][0] * verts[(i+1) % n][1] -
                verts[(i+1) % n][0] * verts[i][1]
                for i in range(n))
    if area2 < 0:
        verts = list(reversed(verts))

    # Vertices: bottom ring (z=0) then top ring (z=h).  Z is up.  Units: cm.
    # IMPORTANT — UE's OBJ importer auto-negates Y to convert from OBJ's
    # right-handed (Y-up math) frame to UE's left-handed world frame.  If we
    # emit the polygon as-is, every imported mesh is mirrored about Y relative
    # to the plan, so asymmetric concave polygons (e.g. PH3/PH4 wrapping a
    # central core) extrude TOWARD their neighbour instead of AWAY, producing
    # the "polygons collapsing into each other on Y" bug.  Pre-negating Y
    # here makes UE's import-flip cancel out, so plan-top ends up at smaller
    # UE-local Y and plan-bottom at larger UE-local Y — matching the spawn
    # positions that use (world_y_m - min_y) directly.
    for x, y in verts:
        lines.append(f"v {x} {-y} 0.0")
    for x, y in verts:
        lines.append(f"v {x} {-y} {h}")

    # One explicit normal per face group:
    #   vn 1 = up (+Z), vn 2 = down (-Z), vn 3… = outward per side edge
    # Normal Y is also pre-negated for the same reason as vertex Y.
    lines.append("vn 0.0 0.0  1.0")   # 1 — top
    lines.append("vn 0.0 0.0 -1.0")   # 2 — bottom
    for i in range(n):
        j  = (i + 1) % n
        ex = verts[j][0] - verts[i][0]
        ey = verts[j][1] - verts[i][1]
        ln = (ex * ex + ey * ey) ** 0.5
        # Outward normal for a CCW polygon: rotate edge +90° → (-ey, ex)
        nx, ny = (-ey / ln, ex / ln) if ln > 0 else (1.0, 0.0)
        lines.append(f"vn {nx:.4f} {-ny:.4f} 0.0")

    lines.append("s off")   # flat shading — no smoothing groups

    if mtl_ref:
        lines.append(f"usemtl {safe_name}_mat")

    # Top & bottom faces — pre-triangulated via ear-clip so concave polygons
    # (e.g. L-/U-shaped penthouses wrapping around a central core) render
    # correctly in UE.  Emitting a single n-gon face would cause UE's OBJ
    # importer to fan-triangulate from vertex 0, filling in every concave
    # notch and turning the footprint into its convex hull.
    _tris = _ear_clip(verts)
    if not _tris:
        # Fallback to fan triangulation (should only hit for degenerate input)
        _tris = [(0, i, i + 1) for i in range(1, n - 1)]

    # Top face — one tri per triangle, CCW from +Z, normal up (vn 1)
    for a, b, c in _tris:
        lines.append(f"f {n+a+1}//1 {n+b+1}//1 {n+c+1}//1")

    # Bottom face — reversed winding per tri, CCW from -Z, normal down (vn 2)
    for a, b, c in _tris:
        lines.append(f"f {a+1}//2 {c+1}//2 {b+1}//2")

    # Side faces — one quad per edge, outward normal (vn 3+i)
    # Winding b0→b1→t1→t0 is CCW when viewed from outside for a CCW polygon
    for i in range(n):
        j   = (i + 1) % n
        vni = 3 + i
        b0, b1 = i + 1, j + 1
        t0, t1 = n + i + 1, n + j + 1
        lines.append(f"f {b0}//{vni} {b1}//{vni} {t1}//{vni} {t0}//{vni}")

    return "\n".join(lines)


def _fmt_apt_type_info(
    calibration: dict,
    mesh_root: str = "/Game/ApartmentMeshes",
) -> tuple[str, dict[str, str], dict, dict]:
    """Return (apt_type_info_src, obj_files, porch_cam_info, spring_arm_info).

    ``spring_arm_info`` maps (building, entrance, type) →
    ``(sx_cm, sy_cm, pitch_deg_or_none)`` where pitch_deg_or_none is the
    user's intuitive (+ = camera UP) per-polygon override, or ``None`` when
    the polygon falls back to the global default baked separately into
    the script as DEFAULT_SA_PITCH_DEG.
    """
    polys = calibration.get("apt_type_polygons", [])
    valid = [p for p in polys
             if "world_x_m" in p
             and "polygon_world_m" in p
             and len(p["polygon_world_m"]) >= 3
             and "building_id" in p
             and "entrance_id" in p
             and "type_name" in p]
    if not valid:
        return "{}", {}, {}, {}

    min_x = min(p["world_x_m"] for p in valid)
    min_y = min(p["world_y_m"] for p in valid)

    # Build stable type → color index for old calibrations that lack color_hex
    _type_order: dict[str, int] = {}
    for p in valid:
        t = str(p["type_name"])
        if t not in _type_order:
            _type_order[t] = len(_type_order)

    info_lines = ["{"]
    obj_files: dict[str, str] = {}
    porch_cam_info: dict = {}
    spring_arm_info: dict = {}

    for p in valid:
        b = str(p["building_id"])
        e = str(p["entrance_id"])
        t = str(p["type_name"])
        key = (b, e, t)
        x_cm = round((p["world_x_m"] - min_x) * 100, 1)
        y_cm = round((p["world_y_m"] - min_y) * 100, 1)
        _uid = str(p.get("uid", ""))[:10]
        safe = f"{b}_{e}_{t}{'_' + _uid if _uid else ''}".replace(" ", "_").replace("/", "_")
        safe_type = t.replace(" ", "_").replace("/", "_")
        asset_path = f"{mesh_root}/{safe_type}/{safe}"
        # Use stored color_hex when available; fall back to palette by type order
        color_hex = p.get("color_hex") or _COLORS[_type_order[t] % len(_COLORS)]
        info_lines.append(
            f"    {key!r}: ({x_cm}, {y_cm}, {asset_path!r}, {color_hex!r}),")
        obj_files[f"meshes/{safe}.obj"] = _polygon_to_obj(
            key, p["polygon_world_m"], (p["world_x_m"], p["world_y_m"]),
            p.get("extrusion_m", 3.0), safe_name=safe
        )
        obj_files[f"meshes/{safe}.mtl"] = _hex_to_mtl(color_hex, safe)
        # Balcony cams: support both legacy single 'balcony_cam' and list
        # 'balcony_cams'. Each polygon may have zero or more cameras; each
        # camera becomes a PorchPawnArrow component on the spawned actor.
        _cam_list = p.get("balcony_cams")
        if not isinstance(_cam_list, list):
            _legacy = p.get("balcony_cam")
            _cam_list = [_legacy] if isinstance(_legacy, dict) else []
        _cam_tuples = []
        for _cam in _cam_list:
            if not (isinstance(_cam, dict)
                    and "world_x_m" in _cam
                    and "world_y_m" in _cam
                    and "z_cm" in _cam):
                continue
            _cam_tuples.append((
                round((_cam["world_x_m"] - min_x) * 100, 1),
                round((_cam["world_y_m"] - min_y) * 100, 1),
                _cam["z_cm"],
                round(-float(_cam.get("yaw_deg", 0.0)), 1),
            ))
        if _cam_tuples:
            porch_cam_info[key] = _cam_tuples

        # Spring-arm cam: one per polygon, optional. Always looks at
        # centroid in the generated script, so we bake only the cam's
        # XY in the same normalized cm frame as the actor origin plus
        # the per-polygon pitch override (or None → use default).
        _sa = p.get("spring_arm")
        if (isinstance(_sa, dict)
                and "world_x_m" in _sa
                and "world_y_m" in _sa):
            _sx_cm = round((_sa["world_x_m"] - min_x) * 100, 1)
            _sy_cm = round((_sa["world_y_m"] - min_y) * 100, 1)
            _pitch = _sa.get("pitch_deg")
            if _pitch is not None:
                try:
                    _pitch = float(_pitch)
                except (TypeError, ValueError):
                    _pitch = None
            spring_arm_info[key] = (_sx_cm, _sy_cm, _pitch)

    info_lines.append("}")
    return "\n".join(info_lines), obj_files, porch_cam_info, spring_arm_info


# ── Shared runtime preamble ──────────────────────────────────────────────────
# Injected into every generated script via {common_preamble}. Contains:
#   • Play-in-Editor refusal guard (C2): spawning during PIE leaks actors
#     into the throwaway PIE world and can corrupt ScopedEditorTransaction.
#   • _spawn_actor() helper (H4): prefers the UE 5.1+ EditorActorSubsystem
#     and falls back to EditorLevelLibrary.spawn_actor_from_class on older
#     engines, avoiding the 5.6+ deprecation removal risk.
#   • _z_for_floor(), _DIR_ABBREV, _dir_label: shared helpers hoisted from
#     three duplicated copies (M5) so future tweaks stay in sync.
# Assumes FLOOR_HEIGHT_CM and Z_BY_FLOOR_CM have already been declared in the
# enclosing template above the injection point.
_COMMON_PREAMBLE = '''\
try:
    if unreal.EditorLevelLibrary.editor_is_in_play_mode():
        raise RuntimeError(
            "Play-in-Editor is active. Stop PIE (Esc) before running this "
            "script so actors spawn into the persistent level, not PIE's "
            "throwaway world."
        )
except AttributeError:
    pass  # pre-UE-5.0 builds without editor_is_in_play_mode

def _spawn_actor(_cls, _loc, _rot):
    """Spawn via EditorActorSubsystem on UE 5.1+, else EditorLevelLibrary.

    Returns the spawned actor or None. Callers still need to None-check.
    """
    _sub = None
    try:
        _sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    except Exception:
        _sub = None
    if _sub is not None:
        try:
            return _sub.spawn_actor_from_class(_cls, _loc, _rot)
        except Exception:
            pass
    return unreal.EditorLevelLibrary.spawn_actor_from_class(_cls, _loc, _rot)

def _z_for_floor(f):
    _fi = int(f)
    if _fi in Z_BY_FLOOR_CM:
        return Z_BY_FLOOR_CM[_fi]
    return _fi * FLOOR_HEIGHT_CM

_DIR_ABBREV = {
    "\u05e6\u05e4\u05d5\u05df": "N", "\u05d3\u05e8\u05d5\u05dd": "S",
    "\u05de\u05d6\u05e8\u05d7": "E", "\u05de\u05e2\u05e8\u05d1": "W",
    "North": "N", "South": "S", "East": "E", "West": "W",
}
def _dir_label(s):
    parts = []
    for w in str(s).split():
        w = w.lstrip("\u05d5")
        if w in _DIR_ABBREV:
            parts.append(_DIR_ABBREV[w])
    return "".join(parts) or "?"
'''


# ── Volumes-only preamble: robust script-directory detection (C1) ───────────
# `exec(open(p).read())` does NOT set __file__ in the exec'd namespace, so
# the prior code that did `os.path.dirname(os.path.abspath(__file__))` could
# crash before importing a single mesh. This preamble tries __file__ first,
# falls back to a user-editable SCRIPT_DIR_OVERRIDE constant, and raises a
# clear error (instead of an opaque NameError) if neither works.
_VOLUMES_FS_PREAMBLE = '''\
SCRIPT_DIR_OVERRIDE = ""  # paste the folder containing this .py + meshes/ if __file__ is unset

try:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    if SCRIPT_DIR_OVERRIDE and os.path.isdir(SCRIPT_DIR_OVERRIDE):
        _script_dir = SCRIPT_DIR_OVERRIDE
    else:
        raise RuntimeError(
            "__file__ is not defined in this Python context. To fix, either:\\n"
            "  (a) Run via 'File > Execute Python Script' in the UE editor, or\\n"
            "  (b) Use exec(open(p).read(), {\\"__file__\\": p}) instead of "
            "exec(open(p).read()), or\\n"
            "  (c) Paste this script's absolute folder into SCRIPT_DIR_OVERRIDE "
            "at the top of this file."
        )
_asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
'''


# ── Shared: script header + OBJ import section ──────────────────────────────
_VOLUMES_HEADER = '''\
"""
UE Apartment Placer — Volume Script (Auto-Generated)
Place this file in a folder alongside the /meshes/ subfolder.
Run in UE: Window > Output Log > Python tab
  exec(open(r"path/to/spawn_volumes.py").read())

All apartment meshes are imported into a unique Content Browser folder
prefixed with the project name so re-imports never overwrite previous batches:
  {mesh_root}/<TypeName>/<MeshName>
"""
import unreal, os

FLOOR_HEIGHT_CM = {floor_cm}
# Per-floor Z offsets in cm. Overrides the default floor*FLOOR_HEIGHT_CM
# stacking. Generated from the user-configured floor-gap table; any floor
# not listed here falls back to int(floor) * FLOOR_HEIGHT_CM.
Z_BY_FLOOR_CM = {z_by_floor}

{common_preamble}
MESH_ROOT = "{mesh_root}"

APT_TYPE_MESH_INFO = {apt_type_mesh_info}

APARTMENTS = {apartments}

{fs_preamble}
# ── Step 1: Import OBJ meshes + patch colours ─────────────────────────────
print(f"  Mesh root folder: {{MESH_ROOT}}")
for _key, (_ox, _oy, _ap, _col) in APT_TYPE_MESH_INFO.items():
    _b, _e, _t = _key
    _safe      = _ap.split("/")[-1]   # includes uid suffix from asset path
    _safe_type = _t.replace(" ", "_").replace("/", "_")
    _dest_dir  = f"{{MESH_ROOT}}/{{_safe_type}}"
    _obj  = os.path.join(_script_dir, "meshes", f"{{_safe}}.obj")
    if os.path.exists(_obj):
        if not unreal.EditorAssetLibrary.does_directory_exist(_dest_dir):
            unreal.EditorAssetLibrary.make_directory(_dest_dir)
            print(f"  Created folder: {{_dest_dir}}")
        _task = unreal.AssetImportTask()
        _task.set_editor_property("filename",         _obj)
        _task.set_editor_property("destination_path", _dest_dir)
        _task.set_editor_property("destination_name", _safe)
        _task.set_editor_property("replace_existing", True)
        _task.set_editor_property("automated",        True)
        _task.set_editor_property("save",             True)
        _asset_tools.import_asset_tasks([_task])
        print(f"  Imported: {{_safe}} → {{_dest_dir}}")
        # Disable Nanite — Nanite does not support translucent materials and
        # will silently hide the mesh at runtime if a translucent mat is assigned
        _sm = unreal.load_asset(f"{{_dest_dir}}/{{_safe}}")
        if isinstance(_sm, unreal.StaticMesh):
            _ns = _sm.get_editor_property("nanite_settings")
            _ns.enabled = False
            _sm.set_editor_property("nanite_settings", _ns)
            _sm.modify()
            unreal.EditorAssetLibrary.save_asset(f"{{_dest_dir}}/{{_safe}}")
            print(f"  Nanite disabled: {{_safe}}")
        _mat_path = f"{{_dest_dir}}/{{_safe}}_mat"
        _mat_inst = unreal.load_asset(_mat_path)
        if _mat_inst and isinstance(_mat_inst, unreal.MaterialInstanceConstant):
            _h = _col.lstrip("#")
            _lr = int(_h[0:2], 16) / 255.0
            _lg = int(_h[2:4], 16) / 255.0
            _lb = int(_h[4:6], 16) / 255.0
            unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
                _mat_inst, "BaseColor", unreal.LinearColor(_lr, _lg, _lb, 1.0))
            unreal.EditorAssetLibrary.save_asset(_mat_path)
            print(f"  Coloured: {{_mat_path}}")
        else:
            print(f"  NOTE: material not patched ({{_mat_path}} → {{type(_mat_inst).__name__}})")
    else:
        print(f"  WARNING OBJ not found: {{_obj}}")

print("\\nAvailable mesh keys:")
for _k in sorted(APT_TYPE_MESH_INFO.keys()):
    print(f"  {{_k}}")

'''

# ── POI header: same as above but skips colour patching ───────────────────
_VOLUMES_HEADER_POI = '''\
"""
UE Apartment Placer — Volume Script / POI Mode (Auto-Generated)
Place this file in a folder alongside the /meshes/ subfolder.
Run in UE: Window > Output Log > Python tab
  exec(open(r"path/to/spawn_volumes.py").read())

All apartment meshes are imported into a unique Content Browser folder
prefixed with the project name so re-imports never overwrite previous batches:
  {mesh_root}/<TypeName>/<MeshName>
"""
import unreal, os

FLOOR_HEIGHT_CM = {floor_cm}
# Per-floor Z offsets in cm. Overrides the default floor*FLOOR_HEIGHT_CM
# stacking. Generated from the user-configured floor-gap table; any floor
# not listed here falls back to int(floor) * FLOOR_HEIGHT_CM.
Z_BY_FLOOR_CM = {z_by_floor}

{common_preamble}
MESH_ROOT = "{mesh_root}"

APT_TYPE_MESH_INFO = {apt_type_mesh_info}

APARTMENTS = {apartments}

{fs_preamble}
# ── Step 1: Import OBJ meshes (no colour patching in POI mode) ────────────
print(f"  Mesh root folder: {{MESH_ROOT}}")
for _key, (_ox, _oy, _ap, _col) in APT_TYPE_MESH_INFO.items():
    _b, _e, _t = _key
    _safe      = _ap.split("/")[-1]   # includes uid suffix from asset path
    _safe_type = _t.replace(" ", "_").replace("/", "_")
    _dest_dir  = f"{{MESH_ROOT}}/{{_safe_type}}"
    _obj  = os.path.join(_script_dir, "meshes", f"{{_safe}}.obj")
    if os.path.exists(_obj):
        if not unreal.EditorAssetLibrary.does_directory_exist(_dest_dir):
            unreal.EditorAssetLibrary.make_directory(_dest_dir)
            print(f"  Created folder: {{_dest_dir}}")
        _task = unreal.AssetImportTask()
        _task.set_editor_property("filename",         _obj)
        _task.set_editor_property("destination_path", _dest_dir)
        _task.set_editor_property("destination_name", _safe)
        _task.set_editor_property("replace_existing", True)
        _task.set_editor_property("automated",        True)
        _task.set_editor_property("save",             True)
        _asset_tools.import_asset_tasks([_task])
        print(f"  Imported: {{_safe}} → {{_dest_dir}}")
        # Disable Nanite — translucent materials cause silent invisible mesh at runtime
        _sm = unreal.load_asset(f"{{_dest_dir}}/{{_safe}}")
        if isinstance(_sm, unreal.StaticMesh):
            _ns = _sm.get_editor_property("nanite_settings")
            _ns.enabled = False
            _sm.set_editor_property("nanite_settings", _ns)
            _sm.modify()
            unreal.EditorAssetLibrary.save_asset(f"{{_dest_dir}}/{{_safe}}")
            print(f"  Nanite disabled: {{_safe}}")
    else:
        print(f"  WARNING OBJ not found: {{_obj}}")

print("\\nAvailable mesh keys:")
for _k in sorted(APT_TYPE_MESH_INFO.keys()):
    print(f"  {{_k}}")

'''

# ── Shared: summary footer ────────────────────────────────────────────────
_VOLUMES_FOOTER = '''\
print(f"\\n✓ Spawned {{_spawned}} volumes.")
if _skipped_no_key:
    print(f"\\n⚠ {{len(_skipped_no_key)}} apartments skipped — no polygon drawn for their (building, entrance, type) key:")
    for _aid, _k in sorted(_skipped_no_key):
        print(f"   apt {{_aid:>10}}  →  key {{_k}}")
if _skipped_no_mesh:
    print(f"\\n⚠ {{len(_skipped_no_mesh)}} skipped — mesh asset failed to load: {{_skipped_no_mesh}}")
if _skipped_no_actor:
    print(f"\\n⚠ {{len(_skipped_no_actor)}} skipped — spawn returned None: {{_skipped_no_actor}}")
'''

# ── Spawn loop A: StaticMeshActor ─────────────────────────────────────────
_STATIC_MESH_SPAWN_LOOP = '''\
# ── Step 2: Spawn StaticMeshActors ────────────────────────────────────────
# Spawn in batches so the UE editor (MassLODSubsystem in particular) does
# not exceed its ClientIndex pool and the undo buffer stays manageable.
# Each batch lives in its own ScopedEditorTransaction so progress is
# preserved if a later batch errors out.
_BATCH_SIZE = 100

_spawned = 0
_skipped_no_key   = []   # no polygon drawn for this (building, entrance, type)
_skipped_no_mesh  = []   # polygon drawn but asset failed to load
_skipped_no_actor = []   # spawn call returned None

def _spawn_one_static(apt):
    global _spawned
    _key = (apt["building"], apt["entrance"], apt.get("type", ""))
    if _key not in APT_TYPE_MESH_INFO:
        _skipped_no_key.append((apt["apt_id"], _key))
        return
    _ox, _oy, _ap, _col = APT_TYPE_MESH_INFO[_key]
    _z    = _z_for_floor(apt["floor"])
    _mesh = unreal.load_asset(_ap)
    if _mesh is None:
        print(f"  WARNING: could not load asset {{_ap}}")
        _skipped_no_mesh.append(apt["apt_id"])
        return
    _actor = _spawn_actor(
        unreal.StaticMeshActor.static_class(),
        unreal.Vector(_ox, _oy, _z), unreal.Rotator(0, 0, 0))
    if _actor is None:
        print(f"  WARNING: spawn failed for apt {{apt['apt_id']}}")
        _skipped_no_actor.append(apt["apt_id"])
        return
    _comp = _actor.get_component_by_class(unreal.StaticMeshComponent)
    if _comp is not None:
        _comp.set_static_mesh(_mesh)
    _actor.set_actor_label(f"{{apt['apt_id']}}_{{int(apt['floor'])}}")
{folder_code}    _spawned += 1

for _bidx in range(0, len(APARTMENTS), _BATCH_SIZE):
    _batch = APARTMENTS[_bidx:_bidx + _BATCH_SIZE]
    _label = f"Spawn Apartment Volumes [{{_bidx + 1}}-{{_bidx + len(_batch)}}]"
    with unreal.ScopedEditorTransaction(_label) as _trans:
        for apt in _batch:
            _spawn_one_static(apt)
    print(f"  [batch {{_bidx // _BATCH_SIZE + 1}}: {{_spawned}}/{{len(APARTMENTS)}} spawned]")

'''

# ── Spawn loop B: BP_POI instances ────────────────────────────────────────
_POI_SPAWN_LOOP = '''\
# ── Step 2: Spawn BP_POI instances ───────────────────────────────────────
POI_BLUEPRINT_PATH = {poi_bp_path!r}
# unreal.load_class requires the path to end with _C
if not POI_BLUEPRINT_PATH.endswith("_C"):
    POI_BLUEPRINT_PATH += "_C"

PORCH_CAM_INFO = {porch_cam_info}

# Spring-arm targets: (cam_x_cm, cam_y_cm, pitch_deg_or_None) per apt key.
# Pitch is in the user's intuitive convention (+ = camera UP). The
# spawner INVERTS it before feeding UE's SpringArm, which uses the
# opposite sign (+ SA pitch rotates the arm up, dropping the camera).
# When pitch is None for a given key the global DEFAULT_SA_PITCH_DEG is
# applied instead — same intuitive sign, same inversion at the setter.
SPRING_ARM_INFO = {spring_arm_info}

DEFAULT_SA_PITCH_DEG = {default_sa_pitch_deg}

# Spawn in batches so the UE editor's MassLODSubsystem does not exceed its
# fixed-size ClientIndex pool (assertion in MassLODSubsystem.cpp) as many
# BalconyViewArrowComponent viewers register simultaneously. 100 is safe
# across UE 5.x. Each batch gets its own ScopedEditorTransaction so a
# mid-run error still leaves earlier batches on disk.
_BATCH_SIZE = 100

# ──────────────────────────────────────────────────────────────────────────
# Mass-LOD ClientIndex crash mitigations — read this if UE crashed with
#   Assertion failed: ClientIndex >= ... && ClientIndex <= ...
#   [File: .../MassLOD/Private/MassLODSubsystem.cpp] [Line: ~537]
#
# Each PorchPawnArrow / BalconyViewArrowComponent registers itself as a
# Mass-LOD viewer on OnRegister(). The subsystem's ClientIndex type is a
# small integer (int8 or similar). If you have more apartments x cams
# than its range, OR if UE re-registers components on Select-All / level
# reload / Ctrl-Z, the counter overflows and the engine asserts.
#
# These two constants let you trade runtime balcony-cam functionality
# against editor stability. Flip them at the top of this file before
# running if the baseline taming isn't enough.
# ──────────────────────────────────────────────────────────────────────────

# If True, destroy the stock PorchPawnArrow on any apt that has NO
# configured balcony cams. Zero functional loss (there was nothing
# configured for it anyway) — just fewer Mass-LOD viewers registered.
DESTROY_STOCK_ARROW_IF_NO_CAMS = True

# If True, after all apts are spawned, destroy EVERY arrow / balcony-view
# component on every spawned actor. This removes them from Mass-LOD
# tracking entirely, at the cost of runtime balcony-cam functionality
# (the pawn can no longer "peek" from the porch). Only enable this if
# DESTROY_STOCK_ARROW_IF_NO_CAMS=True alone isn't enough.
DESTROY_PORCH_CAMS_POST_SPAWN = False

_poi_class = unreal.load_class(None, POI_BLUEPRINT_PATH)
if _poi_class is None:
    raise RuntimeError(f"POI Blueprint not found: {{POI_BLUEPRINT_PATH}}")

# ── SpringArm math helper ─────────────────────────────────────────────────
# Computes (target_arm_length_cm, sa_pitch_ue, sa_yaw_ue) from the apt
# origin + cam target + intuitive pitch. Pitch INVERSION happens here,
# once — callers pass the user-facing value and receive UE-ready numbers.
# Geometry (UE frame, +X forward, +Y right, +Z up, Rotator=(Pitch,Yaw,Roll)):
#   Camera sits at SpringArm origin - L * Forward, where
#     Forward = (cos(Y)cos(P), sin(Y)cos(P), sin(P))
#   We only constrain the XY placement (dx, dy); Z of cam comes from pitch.
#   L_xy = hypot(dx, dy); SpringArm yaw Y satisfies
#     (dx, dy) = -L_xy * (cos(Y), sin(Y))  -> Y = atan2(-dy, -dx)
#   Intuitive pitch P_u (+ = camera UP) maps to UE SpringArm pitch = -P_u.
import math as _math_sa
def _sa_compute(_ox, _oy, _sx, _sy, _pitch_user):
    _dx = _sx - _ox
    _dy = _sy - _oy
    _L = _math_sa.hypot(_dx, _dy)
    _yaw = _math_sa.degrees(_math_sa.atan2(-_dy, -_dx))
    _pitch_ue = -float(_pitch_user)
    return _L, _pitch_ue, _yaw

def _apply_spring_arm(_actor, _target_len_cm, _pitch_ue, _yaw_ue):
    """Locate the SpringArm component on ``_actor`` and stamp its
    TargetArmLength + rotation. Silent no-op if the BP has no SpringArm
    (keeps the generator compatible with non-SpringArm POI Blueprints).
    Returns True on apply, False otherwise.
    """
    _sa_cls = getattr(unreal, 'SpringArmComponent', None)
    _sa_comp = None
    if _sa_cls is not None:
        try:
            for _c in _actor.get_components_by_class(_sa_cls):
                _sa_comp = _c
                break
        except Exception:
            _sa_comp = None
    if _sa_comp is None:
        # Fallback: search by component name. Matches the name used by
        # BP__Persistant_POI ("SpringArm") but also any BP that happens
        # to name a SceneComponent "SpringArm".
        try:
            for _c in _actor.get_components_by_class(unreal.SceneComponent):
                if _c.get_name() == "SpringArm":
                    _sa_comp = _c
                    break
        except Exception:
            pass
    if _sa_comp is None:
        return False
    try:
        _sa_comp.set_editor_property("target_arm_length", float(_target_len_cm))
    except Exception:
        try:
            _sa_comp.set_editor_property("TargetArmLength", float(_target_len_cm))
        except Exception as _ex:
            print(f"  WARN: SpringArm.TargetArmLength set failed: {{_ex}}")
            return False
    try:
        # CRITICAL: unreal.Rotator positional args are ALPHABETICAL by
        # field name: (pitch, roll, yaw) — NOT the C++ (pitch, yaw, roll)
        # order. Using keyword args here so a future refactor doesn't
        # accidentally stuff the yaw into the roll slot (which is what
        # happened before and silently zeroed out the actual yaw).
        _sa_comp.set_relative_rotation(
            unreal.Rotator(pitch=float(_pitch_ue),
                           yaw=float(_yaw_ue),
                           roll=0.0),
            False, False)
    except Exception as _ex:
        print(f"  WARN: SpringArm.set_relative_rotation failed: {{_ex}}")
        return False
    return True

def _tame_viewer(_c):
    """Minimise an arrow/viewer component's Mass-LOD footprint.

    IMPORTANT: This deliberately does NOT call unregister_component() +
    register_component(). Empirically, UE's MassLODSubsystem appears to
    allocate a FRESH ClientIndex on each OnRegister — so re-registering
    to pick up cleared flags actually accumulates indexes and hastens
    the ClientIndex overflow crash. We only set properties and
    deactivate; no re-registration cycles happen here.

    Every step is best-effort — exact flag names vary across UE versions
    and SimplexUtils builds, so unknown properties simply raise and are
    swallowed.
    """
    if _c is None:
        return
    try: _c.set_visibility(False)
    except Exception: pass
    try: _c.set_hidden_in_game(True)
    except Exception: pass
    try: _c.set_auto_activate(False)
    except Exception: pass
    try: _c.deactivate()
    except Exception: pass
    try: _c.set_component_tick_enabled(False)
    except Exception: pass
    for _prop in (
        "bAutoRegisterWithLODSubsystem", "AutoRegisterWithLODSubsystem",
        "bRegisterAsViewer", "RegisterAsViewer",
        "bIsActiveLODViewer", "bEnableViewerLOD",
        "bIsViewer", "bIsLODViewer",
    ):
        try: _c.set_editor_property(_prop, False)
        except Exception: pass

_spawned = 0
_spawned_actors = []     # kept for the optional post-spawn cleanup pass
_skipped_no_key   = []   # no polygon drawn for this (building, entrance, type)
_skipped_no_mesh  = []   # polygon drawn but asset failed to load
_skipped_no_actor = []   # spawn call returned None

def _spawn_one_poi(apt):
    global _spawned
    _key = (apt["building"], apt["entrance"], apt.get("type", ""))
    if _key not in APT_TYPE_MESH_INFO:
        _skipped_no_key.append((apt["apt_id"], _key))
        return
    _ox, _oy, _ap, _col = APT_TYPE_MESH_INFO[_key]
    _z    = _z_for_floor(apt["floor"])
    _mesh = unreal.load_asset(_ap)
    if _mesh is None:
        print(f"  WARNING: could not load asset {{_ap}}")
        _skipped_no_mesh.append(apt["apt_id"])
        return
    _actor = _spawn_actor(
        _poi_class, unreal.Vector(_ox, _oy, _z), unreal.Rotator(0, 0, 0))
    if _actor is None:
        print(f"  WARNING: spawn failed for apt {{apt['apt_id']}}")
        _skipped_no_actor.append(apt["apt_id"])
        return
    # ── Set POI_Geometry mesh (instance edit only — blueprint unchanged) ──
    for _c in _actor.get_components_by_class(unreal.StaticMeshComponent):
        if _c.get_name() == "POI_Geometry":
            _c.set_static_mesh(_mesh)
            break
    # ── Set POI_Type = Apartment (instance edit only) ─────────────────────
    try:
        _ecls = type(_actor.get_editor_property("POI_Type"))
        _apt_val = getattr(_ecls, "APARTMENT", None) or getattr(_ecls, "Apartment", None)
        if _apt_val is not None:
            _actor.set_editor_property("POI_Type", _apt_val)
    except Exception as _ex:
        print(f"  NOTE: POI_Type not set for {{apt['apt_id']}}: {{_ex}}")
    # ── Set RowName = unit Number (instance edit only) ────────────────────
    try:
        _actor.set_editor_property("RowName", str(apt["apt_id"]))
    except Exception as _ex:
        print(f"  NOTE: RowName not set for {{apt['apt_id']}}: {{_ex}}")
    _actor.set_actor_label(f"{{apt['apt_id']}}_{{int(apt['floor'])}}")
    _spawned_actors.append(_actor)

    # ── Decide what to do with the BP's stock PorchPawnArrow ─────────────
    # If this apt has NO configured balcony cams, we destroy the stock
    # arrow entirely — nothing uses it and one less Mass-LOD viewer
    # dramatically reduces ClientIndex pressure on large scenes.
    # Otherwise, tame it (hide/deactivate/clear viewer flags).
    _existing = None
    for _c in _actor.get_components_by_class(unreal.ArrowComponent):
        if _c.get_name() == "PorchPawnArrow":
            _existing = _c
            break

    _pcam_raw = PORCH_CAM_INFO.get(_key)
    _pcams = []
    if _pcam_raw:
        if isinstance(_pcam_raw, tuple):
            _pcams = [_pcam_raw]
        elif isinstance(_pcam_raw, list):
            _pcams = list(_pcam_raw)

    if not _pcams and _existing is not None and DESTROY_STOCK_ARROW_IF_NO_CAMS:
        try:
            _existing.destroy_component()
            _existing = None
        except Exception as _dex:
            print(f"  NOTE: could not destroy stock PorchPawnArrow on "
                  f"{{apt['apt_id']}}: {{_dex}}")
            _tame_viewer(_existing)
    elif _existing is not None:
        _tame_viewer(_existing)

    # ── Set PorchPawnArrow location/rotation for each placed balcony cam ─
    # Cam #1 reuses the BP's existing PorchPawnArrow (SCS-persistent, stays
    # attached to root through editor moves).
    # Cam #2+ are created via Actor.AddComponentByClass with an explicit
    # manual_attachment + register_component + attach_to_component sequence
    # so they survive editor moves that re-run the Construction Script.
    # The component is attached to the actor's root via KEEP_RELATIVE rules
    # and positioned with set_relative_location/rotation so its transform
    # follows the actor when the user drags the BP in the viewport.
    #
    # PLACEMENT MODEL (important — see generator history):
    #   - Actor spawns with Rotator(0,0,0), so relative_yaw == world_yaw.
    #   - Cam world target: (_pcx, _pcy, _z + _pcz)
    #   - Actor world origin: (_ox, _oy, _z)
    #   - Cam relative to root: (_pcx-_ox, _pcy-_oy, _pcz)
    # We never layer transforms: create with IDENTITY, attach with
    # KEEP_RELATIVE, then set relative ONCE. Re-fetching the component via
    # the name diff is preserved because the C++ AddComponentByClass return
    # value does not always round-trip cleanly through reflection.
    if _pcams and _actor:
        _cam_class = None
        try:
            _cam_class = unreal.load_class(
                None, "/Script/SimplexUtils.BalconyViewArrowComponent")
        except Exception:
            pass
        if _cam_class is None:
            _cam_class = getattr(unreal, 'BalconyViewArrowComponent', None)
        if _cam_class is None:
            _cam_class = unreal.ArrowComponent
        _root = None
        try:
            _root = _actor.root_component
        except Exception:
            pass
        if _root is None:
            try:
                _root = _actor.get_editor_property("RootComponent")
            except Exception:
                _root = None
        for _ci, _t in enumerate(_pcams):
            _pcx, _pcy, _pcz = _t[0], _t[1], _t[2]
            _pyaw = _t[3] if len(_t) >= 4 else 0.0
            if _ci == 0:
                # Cam #1: the BP's existing PorchPawnArrow. Already SCS-
                # attached to root — use world setters (these recompute its
                # RelativeLocation against the current actor transform, so
                # the cam still follows the actor on later editor moves).
                _comp = _existing
                if _comp is None:
                    print(f"  WARN: no component for cam #{{_ci + 1}}")
                    continue
                _comp.set_world_location(
                    unreal.Vector(_pcx, _pcy, _z + _pcz), False, False)
                _comp.set_world_rotation(
                    unreal.Rotator(0.0, 0.0, _pyaw), False, False)
                _tame_viewer(_comp)
                print(f"  Cam #1 "
                      f"({{_pcx:.1f}}, {{_pcy:.1f}}, {{_z + _pcz:.1f}}) "
                      f"yaw={{_pyaw}} [SCS:world]")
                continue

            # Cam #2+: create, register, attach, then set relative.
            _comp = None
            try:
                _actor.modify()
                _names_before = set(
                    c.get_name() for c in
                    _actor.get_components_by_class(unreal.ArrowComponent))
                # manual_attachment=True so UE doesn't auto-attach to an
                # unspecified parent; identity transform so the later
                # set_relative_* is the single source of truth.
                _actor.call_method(
                    'AddComponentByClass',
                    args=(_cam_class, True, unreal.Transform(), False))
                for _a in _actor.get_components_by_class(
                        unreal.ArrowComponent):
                    if _a.get_name() not in _names_before:
                        _comp = _a
                        break
                if _comp is not None:
                    print(f"  +{{_comp.get_name()}} "
                          f"class={{_comp.get_class().get_name()}}")
                else:
                    # M4: previously swallowed silently. The Python wrapper
                    # does not surface AddComponentByClass failures as
                    # exceptions, so we detect the no-new-component case by
                    # diffing ArrowComponent names and warn explicitly.
                    print(f"  WARN: AddComponentByClass returned no new "
                          f"component for apt {{apt['apt_id']}} cam "
                          f"#{{_ci + 1}} — is {{POI_BLUEPRINT_PATH}} a "
                          f"compatible BP with reflected AddComponentByClass?")
            except Exception as _aex:
                print(f"  WARN: create component for apt "
                      f"{{apt['apt_id']}} cam #{{_ci + 1}}: {{_aex}}")
                _comp = None
            if _comp is None:
                print(f"  WARN: no component for cam #{{_ci + 1}}")
                continue

            # Register + attach — order matters: register FIRST so the
            # component has a registered SceneComponent identity, then
            # attach to the actor root with KEEP_RELATIVE so our upcoming
            # set_relative_* call is what defines the final position.
            try:
                _comp.register_component()
            except Exception as _rex:
                # Already registered paths raise "already registered" in
                # some UE versions — that's the state we want, so swallow
                # only that case by warning and continuing.
                print(f"  NOTE: register_component on cam #{{_ci + 1}} "
                      f"({{_rex}}) — continuing")
            if _root is not None:
                try:
                    _comp.attach_to_component(
                        _root,
                        "",  # socket name
                        unreal.AttachmentRule.KEEP_RELATIVE,
                        unreal.AttachmentRule.KEEP_RELATIVE,
                        unreal.AttachmentRule.KEEP_RELATIVE,
                        False)
                except Exception as _atex:
                    # Fallback: older unreal Python builds use a 5-arg
                    # signature without the socket name.
                    try:
                        _comp.attach_to_component(
                            _root,
                            unreal.AttachmentRule.KEEP_RELATIVE,
                            unreal.AttachmentRule.KEEP_RELATIVE,
                            unreal.AttachmentRule.KEEP_RELATIVE,
                            False)
                    except Exception as _atex2:
                        print(f"  WARN: attach_to_component failed on "
                              f"cam #{{_ci + 1}}: {{_atex}} / {{_atex2}}")
            else:
                print(f"  WARN: no root component for apt "
                      f"{{apt['apt_id']}} — cam #{{_ci + 1}} will not "
                      f"follow actor on move")

            _rel_loc = unreal.Vector(_pcx - _ox, _pcy - _oy, _pcz)
            _rel_rot = unreal.Rotator(0.0, 0.0, _pyaw)
            _comp.set_relative_location(_rel_loc, False, False)
            _comp.set_relative_rotation(_rel_rot, False, False)
            _tame_viewer(_comp)
            print(f"  Cam #{{_ci + 1}} "
                  f"rel({{_rel_loc.x:.1f}}, {{_rel_loc.y:.1f}}, "
                  f"{{_rel_loc.z:.1f}}) yaw={{_pyaw}} [attached:relative]")

    # ── Position the SpringArm (BP-provided, SCS-persistent) ─────────────
    # The SpringArm already exists on the BP; we only stamp its
    # TargetArmLength + rotation. When this apt has no user-placed SA
    # cam, we leave the BP defaults alone rather than forcing zero.
    _sa_info = SPRING_ARM_INFO.get(_key)
    if _sa_info is not None:
        _sx_cm, _sy_cm, _sa_pitch_override = _sa_info
        _sa_pitch_user = (DEFAULT_SA_PITCH_DEG
                          if _sa_pitch_override is None
                          else float(_sa_pitch_override))
        _sa_len, _sa_pitch_ue, _sa_yaw_ue = _sa_compute(
            _ox, _oy, _sx_cm, _sy_cm, _sa_pitch_user)
        _ok = _apply_spring_arm(_actor, _sa_len, _sa_pitch_ue, _sa_yaw_ue)
        if _ok:
            print(f"  SpringArm len={{_sa_len:.1f}}cm "
                  f"yaw={{_sa_yaw_ue:.1f}} "
                  f"pitch_ue={{_sa_pitch_ue:+.1f}} "
                  f"(user={{_sa_pitch_user:+.1f}})")
        else:
            print(f"  NOTE: SpringArm not found on {{apt['apt_id']}} "
                  f"({{POI_BLUEPRINT_PATH}}) — skipping SA positioning")
{folder_code}    _spawned += 1

for _bidx in range(0, len(APARTMENTS), _BATCH_SIZE):
    _batch = APARTMENTS[_bidx:_bidx + _BATCH_SIZE]
    _label = f"Spawn Apartment POIs [{{_bidx + 1}}-{{_bidx + len(_batch)}}]"
    with unreal.ScopedEditorTransaction(_label) as _trans:
        for apt in _batch:
            _spawn_one_poi(apt)
    print(f"  [batch {{_bidx // _BATCH_SIZE + 1}}: {{_spawned}}/{{len(APARTMENTS)}} spawned]")

# ── Optional post-spawn cleanup ──────────────────────────────────────────
# Destroys every arrow component on every spawned actor. Use this if
# selecting actors still crashes UE with the MassLODSubsystem ClientIndex
# assertion. This REMOVES runtime balcony-cam functionality.
if DESTROY_PORCH_CAMS_POST_SPAWN:
    print("  DESTROY_PORCH_CAMS_POST_SPAWN=True — removing all arrow components")
    with unreal.ScopedEditorTransaction("Destroy porch arrows post-spawn") as _trans:
        _destroyed = 0
        for _actor in _spawned_actors:
            if _actor is None:
                continue
            try:
                _arrows = list(_actor.get_components_by_class(unreal.ArrowComponent))
            except Exception:
                _arrows = []
            for _c in _arrows:
                try:
                    _c.destroy_component()
                    _destroyed += 1
                except Exception as _dex:
                    print(f"  NOTE: destroy failed on {{_c.get_name()}}: {{_dex}}")
        print(f"  Destroyed {{_destroyed}} arrow components")

'''

# ── Assemble full templates ───────────────────────────────────────────────
_VOLUMES_TEMPLATE     = _VOLUMES_HEADER     + _STATIC_MESH_SPAWN_LOOP + _VOLUMES_FOOTER
_POI_VOLUMES_TEMPLATE = _VOLUMES_HEADER_POI + _POI_SPAWN_LOOP         + _VOLUMES_FOOTER


# ──────────────────────────────────────────────────────────────────────────
# Refresh balcony-cams utility — ships next to spawn_volumes.py so users
# can restore cam #2+ on BP_POI actors after an editor move wipes them
# (Construction Script rerun destroys instance-added components).
#
# Safe to run multiple times: actors that already have the right number
# of extra arrows are skipped. Cam #1 (SCS PorchPawnArrow) is never
# touched. Matches the robust attach pipeline from the main spawn loop:
#   AddComponentByClass(manual_attachment=True, identity)
#   → register_component()
#   → attach_to_component(root, KEEP_RELATIVE × 3)
#   → set_relative_location / set_relative_rotation
# ──────────────────────────────────────────────────────────────────────────
_REFRESH_CAMS_TEMPLATE = '''\
"""refresh_balcony_cams.py — UE editor utility

Run this in Unreal Engine AFTER you've moved/fine-tuned BP_POI actors in
the editor and some balcony cameras (#2+) have disappeared, OR the
SpringArm has drifted from the configured length/rotation.

Cam #1 (the Blueprint's SCS PorchPawnArrow) is SCS-persistent and never
lost. The SpringArm is also SCS-persistent, but editor moves that
re-run the Construction Script can reset its TargetArmLength and
Rotation back to BP defaults — this script restores them.

Usage:
  1. Drop this file next to spawn_volumes.py (same folder).
  2. In UE: Tools → Python → Execute Python Script… → pick this file.
     (Or run `py refresh_balcony_cams.py` from the UE Python console.)

What it does:
  • Scans the current level for BP_POI actors.
  • Identifies each actor by its label (format '<apt_id>_<floor>').
  • Looks up the baked PORCH_CAM_INFO and SPRING_ARM_INFO for that key.
  • If the actor is missing one or more of cam #2..N, wipes any partial
    survivors and recreates the full cam #2..N set with the same robust
    manual_attachment + register + attach_to_component pipeline the
    main spawn script uses, so future editor moves won't wipe them.
  • Re-applies SpringArm TargetArmLength + rotation on every processed
    actor, so tilted/elongated cams stay that way after editor moves.
  • Batched (100 actors per ScopedEditorTransaction) to avoid Mass-LOD
    ClientIndex overflow.

Idempotent: cam #2..N recreation is skipped when counts already match;
SpringArm is always re-stamped (the setters are cheap and overwriting
with identical values is a no-op from the editor's perspective).
"""

import math, os, unreal

FLOOR_HEIGHT_CM = {floor_cm}
Z_BY_FLOOR_CM = {z_by_floor}

{common_preamble}

POI_BLUEPRINT_PATH = {poi_bp_path!r}
if not POI_BLUEPRINT_PATH.endswith("_C"):
    POI_BLUEPRINT_PATH += "_C"

APT_TYPE_MESH_INFO = {apt_type_mesh_info}

APARTMENTS = {apartments}

PORCH_CAM_INFO = {porch_cam_info}

SPRING_ARM_INFO = {spring_arm_info}

DEFAULT_SA_PITCH_DEG = {default_sa_pitch_deg}


def _sa_compute(_ox, _oy, _sx, _sy, _pitch_user):
    """See main spawn script. Returns UE-ready (len, pitch, yaw)."""
    _dx = _sx - _ox
    _dy = _sy - _oy
    _L = math.hypot(_dx, _dy)
    _yaw = math.degrees(math.atan2(-_dy, -_dx))
    return _L, -float(_pitch_user), _yaw


def _apply_spring_arm(_actor, _len_cm, _pitch_ue, _yaw_ue):
    _sa_cls = getattr(unreal, 'SpringArmComponent', None)
    _sa_comp = None
    if _sa_cls is not None:
        try:
            for _c in _actor.get_components_by_class(_sa_cls):
                _sa_comp = _c
                break
        except Exception:
            pass
    if _sa_comp is None:
        try:
            for _c in _actor.get_components_by_class(unreal.SceneComponent):
                if _c.get_name() == "SpringArm":
                    _sa_comp = _c
                    break
        except Exception:
            pass
    if _sa_comp is None:
        return False
    try:
        _sa_comp.set_editor_property("target_arm_length", float(_len_cm))
    except Exception:
        try:
            _sa_comp.set_editor_property("TargetArmLength", float(_len_cm))
        except Exception:
            return False
    try:
        # unreal.Rotator positional order is ALPHABETICAL (pitch, roll, yaw);
        # use keyword args to make intent explicit and survive refactors.
        _sa_comp.set_relative_rotation(
            unreal.Rotator(pitch=float(_pitch_ue),
                           yaw=float(_yaw_ue),
                           roll=0.0),
            False, False)
    except Exception:
        return False
    return True

# (apt_id str, floor int) → (building, entrance, type)
_APT_LOOKUP = {{
    (str(_a["apt_id"]), int(_a["floor"])): (
        _a["building"], _a["entrance"], _a.get("type", ""))
    for _a in APARTMENTS
}}

def _get_cam_class():
    try:
        _cc = unreal.load_class(
            None, "/Script/SimplexUtils.BalconyViewArrowComponent")
        if _cc is not None:
            return _cc
    except Exception:
        pass
    _cc = getattr(unreal, 'BalconyViewArrowComponent', None)
    if _cc is None:
        _cc = unreal.ArrowComponent
    return _cc

_cam_class = _get_cam_class()
_poi_class = unreal.load_class(None, POI_BLUEPRINT_PATH)
if _poi_class is None:
    raise RuntimeError(f"POI Blueprint not found: {{POI_BLUEPRINT_PATH}}")

try:
    _actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    _all_actors = _actor_sub.get_all_level_actors()
except Exception:
    _all_actors = unreal.EditorLevelLibrary.get_all_level_actors()

# only keep actors that are the POI BP or a child class of it
_target_actors = []
for _a in _all_actors:
    if _a is None:
        continue
    try:
        _cls = _a.get_class()
    except Exception:
        continue
    try:
        if _cls is _poi_class or _cls.is_child_of(_poi_class):
            _target_actors.append(_a)
    except Exception:
        continue

print(f"[refresh_balcony_cams] scanning {{len(_target_actors)}} BP_POI actor(s)...")

_processed = 0
_restored = 0
_already_ok = 0
_unknown_label = 0
_no_extras_configured = 0
_sa_applied = 0
_sa_skipped = 0

def _parse_label(_label):
    """'<apt_id>_<floor>' -> (apt_id_str, floor_int) or None."""
    if not _label or "_" not in _label:
        return None
    _apt_id, _, _floor_str = _label.rpartition("_")
    try:
        _f = int(_floor_str)
    except ValueError:
        return None
    return _apt_id, _f

def _process_actor(_actor):
    global _processed, _restored, _already_ok, _unknown_label
    global _no_extras_configured, _sa_applied, _sa_skipped
    try:
        _label = _actor.get_actor_label()
    except Exception:
        _unknown_label += 1
        return
    _parsed = _parse_label(_label)
    if _parsed is None:
        _unknown_label += 1
        return
    _apt_id, _floor = _parsed
    _key = _APT_LOOKUP.get((_apt_id, _floor))
    if _key is None:
        _unknown_label += 1
        return

    # SpringArm is refreshed on every processed actor (it's SCS-persistent
    # but Construction Script reruns can reset its properties). We do this
    # BEFORE the cam-extras early-out so SA gets refreshed even on actors
    # that have no configured balcony cams beyond cam #1.
    _sa_info = SPRING_ARM_INFO.get(_key)
    if _sa_info is not None:
        _mesh_info_sa = APT_TYPE_MESH_INFO.get(_key)
        if _mesh_info_sa is not None:
            _ox_sa, _oy_sa, _ap_sa, _col_sa = _mesh_info_sa
            _sx_cm, _sy_cm, _sa_pitch_override = _sa_info
            _sa_pitch_user = (DEFAULT_SA_PITCH_DEG
                              if _sa_pitch_override is None
                              else float(_sa_pitch_override))
            _L, _p_ue, _y_ue = _sa_compute(
                _ox_sa, _oy_sa, _sx_cm, _sy_cm, _sa_pitch_user)
            if _apply_spring_arm(_actor, _L, _p_ue, _y_ue):
                _sa_applied += 1
            else:
                _sa_skipped += 1

    _pcam_raw = PORCH_CAM_INFO.get(_key)
    if not _pcam_raw:
        _no_extras_configured += 1
        return
    if isinstance(_pcam_raw, tuple):
        _pcams = [_pcam_raw]
    elif isinstance(_pcam_raw, list):
        _pcams = list(_pcam_raw)
    else:
        _no_extras_configured += 1
        return
    if len(_pcams) <= 1:
        _no_extras_configured += 1
        return  # only cam #1 configured; SCS arrow handles it

    _processed += 1
    try:
        _existing_arrows = list(
            _actor.get_components_by_class(unreal.ArrowComponent))
    except Exception:
        _existing_arrows = []
    _extras = [c for c in _existing_arrows
               if c.get_name() != "PorchPawnArrow"]
    _expected_extra = len(_pcams) - 1
    if len(_extras) >= _expected_extra:
        _already_ok += 1
        return

    # Wipe any partial survivors — matching a stale extra arrow to a
    # specific cam config entry is guesswork, so we recreate cam #2..N
    # from a clean slate. Cam #1 (PorchPawnArrow) is left alone.
    for _c in _extras:
        try:
            _c.destroy_component()
        except Exception as _dex:
            print(f"  NOTE: {{_label}}: could not destroy stale "
                  f"{{_c.get_name()}}: {{_dex}}")

    _mesh_info = APT_TYPE_MESH_INFO.get(_key)
    if _mesh_info is None:
        print(f"  WARN: {{_label}}: no mesh info for key {{_key}}; "
              f"can't compute relative origin, skipping")
        return
    _ox, _oy, _ap, _col = _mesh_info

    _root = None
    try:
        _root = _actor.root_component
    except Exception:
        try:
            _root = _actor.get_editor_property("RootComponent")
        except Exception:
            _root = None

    _actor.modify()
    for _ci, _t in enumerate(_pcams[1:], start=2):
        _pcx, _pcy, _pcz = _t[0], _t[1], _t[2]
        _pyaw = _t[3] if len(_t) >= 4 else 0.0
        _names_before = set(
            c.get_name() for c in
            _actor.get_components_by_class(unreal.ArrowComponent))
        try:
            _actor.call_method(
                'AddComponentByClass',
                args=(_cam_class, True, unreal.Transform(), False))
        except Exception as _aex:
            print(f"  WARN: {{_label}}: AddComponentByClass cam "
                  f"#{{_ci}}: {{_aex}}")
            continue
        _comp = None
        for _a in _actor.get_components_by_class(unreal.ArrowComponent):
            if _a.get_name() not in _names_before:
                _comp = _a
                break
        if _comp is None:
            print(f"  WARN: {{_label}}: no new arrow for cam #{{_ci}}")
            continue
        try:
            _comp.register_component()
        except Exception as _rex:
            print(f"  NOTE: {{_label}}: register cam #{{_ci}}: {{_rex}}")
        if _root is not None:
            try:
                _comp.attach_to_component(
                    _root, "",
                    unreal.AttachmentRule.KEEP_RELATIVE,
                    unreal.AttachmentRule.KEEP_RELATIVE,
                    unreal.AttachmentRule.KEEP_RELATIVE,
                    False)
            except Exception:
                try:
                    _comp.attach_to_component(
                        _root,
                        unreal.AttachmentRule.KEEP_RELATIVE,
                        unreal.AttachmentRule.KEEP_RELATIVE,
                        unreal.AttachmentRule.KEEP_RELATIVE,
                        False)
                except Exception as _atex:
                    print(f"  WARN: {{_label}}: attach_to_component cam "
                          f"#{{_ci}} failed: {{_atex}}")
        _rel = unreal.Vector(_pcx - _ox, _pcy - _oy, _pcz)
        _comp.set_relative_location(_rel, False, False)
        _comp.set_relative_rotation(
            unreal.Rotator(0.0, 0.0, _pyaw), False, False)
        _restored += 1
        print(f"  {{_label}}: +{{_comp.get_name()}} cam #{{_ci}} "
              f"rel=({{_rel.x:.1f}}, {{_rel.y:.1f}}, {{_rel.z:.1f}}) "
              f"yaw={{_pyaw}}")

_BATCH = 100
for _bi in range(0, len(_target_actors), _BATCH):
    _batch = _target_actors[_bi:_bi + _BATCH]
    _tlabel = f"Refresh porch cams [{{_bi + 1}}-{{_bi + len(_batch)}}]"
    with unreal.ScopedEditorTransaction(_tlabel) as _trans:
        for _actor in _batch:
            _process_actor(_actor)
    print(f"  [batch {{_bi // _BATCH + 1}}: processed={{_processed}} "
          f"restored={{_restored}}]")

print(
    f"\\n[OK] Refresh complete. "
    f"Scanned={{len(_target_actors)}} "
    f"NeededRefresh={{_processed}} "
    f"Restored={{_restored}} "
    f"AlreadyOK={{_already_ok}} "
    f"NoExtrasConfigured={{_no_extras_configured}} "
    f"UnknownLabel={{_unknown_label}} "
    f"SpringArmApplied={{_sa_applied}} "
    f"SpringArmSkipped={{_sa_skipped}}")
'''


_SCRIPT_TEMPLATE = '''\
"""
UE Apartment Placer — Auto-Generated Script

Run inside Unreal Engine (Window > Output Log > Python tab). Pick ONE:
  • File > Execute Python Script (recommended — always sets __file__)
  • exec(open(r"path/to/this_script.py").read(), {{"__file__": r"path/to/this_script.py"}})

Plain `exec(open(p).read())` does NOT set __file__; this script is tolerant
of that case (the preamble falls back gracefully), but the volume script is
not, so prefer the patterns above.

Note on undo (H1): spawns are committed in batches of _BATCH_SIZE (default
100). One Ctrl+Z undoes one batch, not the whole run. If a batch errors out
mid-way, earlier completed batches stay on disk.
"""
import unreal

# ── Config ────────────────────────────────────────────
BLUEPRINT_PATH       = {bp_path!r}
FLOOR_HEIGHT_CM      = {floor_cm}
# Per-floor Z offsets in cm. Any floor not listed falls back to
# int(floor) * FLOOR_HEIGHT_CM. Built from the user's floor-gap overrides.
Z_BY_FLOOR_CM        = {z_by_floor}

{common_preamble}
BUILDING_SPACING_CM  = {building_cm}
DIRECTION_SPACING_CM = {direction_cm}
ENTRANCE_OFFSET_CM   = {entrance_cm}
UNIT_STACK_OFFSET_CM = {stack_cm}

# ── Apartment Data ────────────────────────────────────
APARTMENTS = {apartments}

# ── Direction Vectors (unique to the BP spawner) ──────
DIRECTION_VECTORS = {{
    "צפון": (0.0,  1.0), "דרום": (0.0, -1.0),
    "מזרח": (1.0,  0.0), "מערב": (-1.0, 0.0),
    "North": (0.0, 1.0), "South": (0.0, -1.0),
    "East":  (1.0, 0.0), "West":  (-1.0, 0.0),
}}

def _dir_vec(s):
    # lstrip per-word: keeps "צפון" intact while stripping conjunction "ו" prefix
    dx = dy = 0.0; n = 0
    for w in str(s).split():
        w = w.lstrip("ו")
        if w in DIRECTION_VECTORS:
            vx, vy = DIRECTION_VECTORS[w]; dx += vx; dy += vy; n += 1
    return (dx / n if n else 0, dy / n if n else 0)

# ── Building & Entrance Order ─────────────────────────
BUILDING_ORDER = {{}}
ENTRANCE_ORDER = {{}}
for _apt in APARTMENTS:
    _b, _e = _apt["building"], _apt["entrance"]
    if _b not in BUILDING_ORDER:
        BUILDING_ORDER[_b] = len(BUILDING_ORDER)
    if (_b, _e) not in ENTRANCE_ORDER:
        ENTRANCE_ORDER[(_b, _e)] = len([k for k in ENTRANCE_ORDER if k[0] == _b])

# ── Direction-group index (consistent across ALL floors) ──
# Groups apartments by (building, entrance, direction).
# The index within each direction group is stable per apt_id so that
# the same apartment always occupies the same XY slot on every floor.
_dir_groups = {{}}
for _apt in APARTMENTS:
    _dk = (_apt["building"], _apt["entrance"], _apt["direction"])
    _dir_groups.setdefault(_dk, [])
    if _apt["apt_id"] not in _dir_groups[_dk]:
        _dir_groups[_dk].append(_apt["apt_id"])

APT_DIR_IDX = {{}}
for _dk, _ids in _dir_groups.items():
    for _i, _aid in enumerate(sorted(_ids)):
        APT_DIR_IDX[(_dk[0], _dk[1], _dk[2], _aid)] = _i

# ── Calibrated Entrance Origins (from floor plan) ──────
# Keys: (building_id, entrance_id) → (X_cm, Y_cm)
# When present these override the auto-calculated offsets.
ENTRANCE_ORIGINS_CM = {calibrated_origins}

def compute_location(apt):
    b_idx = BUILDING_ORDER[apt["building"]]
    e_idx = ENTRANCE_ORDER[(apt["building"], apt["entrance"])]
    key   = (apt["building"], apt["entrance"])

    # ── Entrance centre + footprint from calibration (or fallback) ──────
    if key in ENTRANCE_ORIGINS_CM:
        ox, oy, half_w, half_h = ENTRANCE_ORIGINS_CM[key]
    else:
        ox     = b_idx * BUILDING_SPACING_CM + e_idx * ENTRANCE_OFFSET_CM
        oy     = 0
        half_w = DIRECTION_SPACING_CM          # use global value as fallback
        half_h = DIRECTION_SPACING_CM

    # ── Direction: offset clamped to polygon footprint ───────────────────
    dx, dy = _dir_vec(apt["direction"])        # normalised –1…+1
    dir_x  = dx * half_w                      # E/W — stays inside footprint
    dir_y  = dy * half_h                      # N/S — stays inside footprint

    # ── Stack index: same-direction apts spread perpendicular ────────────
    sidx   = APT_DIR_IDX.get(
        (apt["building"], apt["entrance"], apt["direction"], apt["apt_id"]), 0)
    n_same = len(_dir_groups.get(
        (apt["building"], apt["entrance"], apt["direction"]), [0]))
    centre_offset = (sidx - (n_same - 1) / 2) * UNIT_STACK_OFFSET_CM

    if abs(dy) >= abs(dx):   # primarily N/S → stack along X
        stack_x, stack_y = centre_offset, 0
    else:                    # primarily E/W → stack along Y
        stack_x, stack_y = 0, centre_offset

    X = ox + dir_x + stack_x
    Y = oy + dir_y + stack_y
    Z = _z_for_floor(apt["floor"])
    return X, Y, Z

# ── Spawn ─────────────────────────────────────────────
bp_class = unreal.load_class(None, BLUEPRINT_PATH)
if bp_class is None:
    raise RuntimeError(f"Blueprint not found: {{BLUEPRINT_PATH}}")

# H1: Batch the spawn loop the same way the volumes templates do. A single
# giant ScopedEditorTransaction for hundreds of apartments (a) rolls back the
# entire run on any one failure, (b) inflates the undo buffer in RAM, and (c)
# keeps the Output Log silent until the whole loop finishes. Batching gives
# progress visibility and partial-success resilience; the trade-off is that
# one Ctrl+Z now undoes one batch, not the whole run.
_BATCH_SIZE = 100

spawned = 0
skipped = []

def _spawn_one_bp(apt):
    global spawned
    x, y, z = compute_location(apt)
    actor = _spawn_actor(
        bp_class, unreal.Vector(x, y, z), unreal.Rotator(0, 0, 0))
    if actor is None:
        skipped.append(apt["apt_id"])
        return
{extra_props}
    actor.set_actor_label(
        f"Apt_{{apt['building']}}{{apt['entrance']}}_Fl{{int(apt['floor'])}}_E{{int(z // 100)}}m_{{_dir_label(apt['direction'])}}_ID{{apt['apt_id']}}"
    )
    spawned += 1

for _bidx in range(0, len(APARTMENTS), _BATCH_SIZE):
    _batch = APARTMENTS[_bidx:_bidx + _BATCH_SIZE]
    _label = f"Spawn Apartment Blueprints [{{_bidx + 1}}-{{_bidx + len(_batch)}}]"
    with unreal.ScopedEditorTransaction(_label) as _trans:
        for apt in _batch:
            _spawn_one_bp(apt)
    print(f"  [batch {{_bidx // _BATCH_SIZE + 1}}: {{spawned}}/{{len(APARTMENTS)}} spawned]")

print(f"\\n✓ Spawned {{spawned}} actors. Skipped: {{len(skipped)}}")
'''


def generate(data: AppData) -> str:
    if data.df is None:
        raise ValueError("No file loaded.")
    rm = data.required_mappings
    for key in ("floor", "apt_id", "direction"):
        if not rm.get(key):
            raise ValueError(f"Required field '{key}' is not mapped.")

    apts = _build_apartments(data)
    if not apts:
        raise ValueError("No valid rows found after processing.")

    z_by_floor = _build_z_by_floor_cm(
        apts, data.floor_height_cm, data.floor_gaps_cm or {})
    return _SCRIPT_TEMPLATE.format(
        bp_path=data.blueprint_path,
        floor_cm=data.floor_height_cm,
        z_by_floor=_fmt_z_by_floor(z_by_floor),
        common_preamble=_COMMON_PREAMBLE,
        building_cm=data.building_spacing_cm,
        direction_cm=data.direction_spacing_cm,
        entrance_cm=data.entrance_offset_cm,
        stack_cm=data.stack_offset_cm,
        apartments=_fmt_apartments(apts),
        extra_props=_extra_props_block(data.extra_mappings),
        calibrated_origins=_fmt_origins(data.calibration),
    )


_FOLDER_CODE = (
    "    # World Outliner folder: Apartments → Building → Entrance → Type\n"
    "    _folder = f\"Apartments/{apt['building']}/E{apt['entrance']}/{apt.get('type', 'Unknown')}\"\n"
    "    _actor.set_folder_path(unreal.Name(_folder))\n"
)

_FOLDER_CODE_FLAT = (
    "    # World Outliner folder: all actors in a single Apartments folder\n"
    "    _actor.set_folder_path(unreal.Name(\"Apartments\"))\n"
)


def generate_volumes(
    data: AppData,
    use_folders: bool = True,
    use_poi: bool = False,
    poi_bp_path: str = "/Game/ArchVizExplorer/Blueprints/BP__Persistant_POI.BP__Persistant_POI_C",
    project_name: str = "",
) -> tuple[str, dict[str, str]]:
    """Returns (ue_python_script, {relative_path: file_content}).

    use_poi=False    → spawns StaticMeshActors (original behaviour)
    use_poi=True     → spawns BP_POI instances; sets POI_Geometry mesh and
                       row_name = apt_id (instance edits only, blueprint unchanged)
    project_name     → free-text label used as the prefix of the unique
                       /Game/ApartmentMeshes/<ProjectName>_<hash>/ folder.
                       Falls back to ``Project`` when blank. A fresh 8-char
                       hex suffix is generated each call so successive
                       script generations never collide in UE.
    """
    if data.df is None:
        raise ValueError("No file loaded.")
    rm = data.required_mappings
    for key in ("floor", "apt_id", "direction"):
        if not rm.get(key):
            raise ValueError(f"Required field '{key}' is not mapped.")

    polys = data.calibration.get("apt_type_polygons", [])
    if not polys:
        raise ValueError(
            "No apartment type polygons found.\n"
            "Draw polygons on the Floor Plan tab (🏠 Apt Type mode) first."
        )

    apts = _build_apartments(data)
    if not apts:
        raise ValueError("No valid rows found after processing.")

    mesh_root = _make_mesh_root(project_name or data.project_name)

    apt_type_info_str, obj_files, porch_cam_info, spring_arm_info = (
        _fmt_apt_type_info(data.calibration, mesh_root=mesh_root))
    porch_cam_info_str   = repr(porch_cam_info)
    spring_arm_info_str  = repr(spring_arm_info)
    # AppData is authoritative for the default SA pitch, but the loaded
    # calibration dict keeps a mirror; prefer AppData so in-app tweaks
    # that haven't been saved still bake correctly.
    _default_sa_pitch = float(
        getattr(data, "default_spring_arm_pitch_deg", 0.0) or 0.0)
    default_sa_pitch_str = repr(_default_sa_pitch)
    folder_code = _FOLDER_CODE if use_folders else _FOLDER_CODE_FLAT

    z_by_floor = _build_z_by_floor_cm(
        apts, data.floor_height_cm, data.floor_gaps_cm or {})
    z_by_floor_str = _fmt_z_by_floor(z_by_floor)
    if use_poi:
        script = _POI_VOLUMES_TEMPLATE.format(
            floor_cm=data.floor_height_cm,
            z_by_floor=z_by_floor_str,
            common_preamble=_COMMON_PREAMBLE,
            fs_preamble=_VOLUMES_FS_PREAMBLE,
            mesh_root=mesh_root,
            apt_type_mesh_info=apt_type_info_str,
            apartments=_fmt_apartments(apts),
            folder_code=folder_code,
            poi_bp_path=poi_bp_path,
            porch_cam_info=porch_cam_info_str,
            spring_arm_info=spring_arm_info_str,
            default_sa_pitch_deg=default_sa_pitch_str,
        )
        # Ship the refresh utility alongside spawn_volumes.py so the user
        # can restore cam #2+ if a BP_POI move in the editor ever wipes
        # them (Construction Script rerun destroys instance-added comps).
        obj_files["refresh_balcony_cams.py"] = _REFRESH_CAMS_TEMPLATE.format(
            floor_cm=data.floor_height_cm,
            z_by_floor=z_by_floor_str,
            common_preamble=_COMMON_PREAMBLE,
            apt_type_mesh_info=apt_type_info_str,
            apartments=_fmt_apartments(apts),
            poi_bp_path=poi_bp_path,
            porch_cam_info=porch_cam_info_str,
            spring_arm_info=spring_arm_info_str,
            default_sa_pitch_deg=default_sa_pitch_str,
        )
    else:
        script = _VOLUMES_TEMPLATE.format(
            floor_cm=data.floor_height_cm,
            z_by_floor=z_by_floor_str,
            common_preamble=_COMMON_PREAMBLE,
            fs_preamble=_VOLUMES_FS_PREAMBLE,
            mesh_root=mesh_root,
            apt_type_mesh_info=apt_type_info_str,
            apartments=_fmt_apartments(apts),
            folder_code=folder_code,
        )
    return script, obj_files
