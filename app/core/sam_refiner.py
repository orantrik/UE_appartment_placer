"""SAM refinement for Gemini-detected apartment bboxes.

The placer's PyInstaller-frozen exe cannot import PyTorch / segment_anything
from the user's site-packages (frozen Python uses only its own bundled stdlib).
So instead of bundling ~2.5 GB of PyTorch, we REUSE the user's existing
ComfyUI install: we spawn a subprocess with ComfyUI's python.exe, feed it
the floor-plan image + a list of bboxes (in pixel coords) via stdin JSON,
and read back a list of base64-encoded PNG masks on stdout.

This module is optional. If ComfyUI's python.exe or the SAM weights aren't
available / configured, `refine_bboxes()` returns `None` for every bbox and
the caller keeps Gemini's rougher polygon.

The inference script itself is embedded as a string in INFERENCE_SRC so we
never have to ship a separate .py file or worry about pyinstaller resource
paths. It is run via `python -c "<script>"`.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger("ue_placer.sam_refiner")


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class SamConfig:
    """Everything needed to call SAM as a sidecar subprocess."""
    python_path: str = ""        # path to python.exe that has torch + segment_anything
    model_path: str = ""         # path to sam_vit_*.pth
    device: str = "cuda"         # "cuda" or "cpu". Fallback to cpu if cuda unavailable.
    timeout_s: int = 600         # per-plan upper bound (SAM encode + per-bbox predict)

    @property
    def ok(self) -> bool:
        return (
            bool(self.python_path)
            and bool(self.model_path)
            and os.path.isfile(self.python_path)
            and os.path.isfile(self.model_path)
        )


# ── Auto-detection ───────────────────────────────────────────────────────────
#
# Users install ComfyUI in a variety of places. We probe the common ones and
# return the first hit. All of these are best-effort; `find_*` returns "" if
# nothing is found so the caller falls back to its saved QSettings value.

_USER = os.path.expanduser("~")

_COMFY_ROOT_CANDIDATES = [
    # Windows portable distro (most common)
    r"C:\ComfyUI_windows_portable",
    r"D:\ComfyUI_windows_portable",
    r"E:\ComfyUI_windows_portable",
    os.path.join(_USER, "ComfyUI_windows_portable"),
    os.path.join(_USER, "Documents", "ComfyUI_windows_portable"),
    os.path.join(_USER, "Desktop", "ComfyUI_windows_portable"),
    os.path.join(_USER, "Downloads", "ComfyUI_windows_portable"),
    # Pinokio installs
    os.path.join(_USER, "pinokio", "api", "comfyui.git"),
    os.path.join(_USER, "pinokio", "api", "comfy.git"),
    # StabilityMatrix
    os.path.join(_USER, "StabilityMatrix", "Packages", "ComfyUI"),
    os.path.join(_USER, "AppData", "Roaming", "StabilityMatrix",
                 "Packages", "ComfyUI"),
    # Bare ComfyUI clones
    r"C:\ComfyUI",
    r"D:\ComfyUI",
    os.path.join(_USER, "ComfyUI"),
    os.path.join(_USER, "Documents", "ComfyUI"),
]


def _python_candidates(root: str) -> list[str]:
    """Given a ComfyUI root, list all python.exe paths worth trying."""
    return [
        os.path.join(root, "python_embeded", "python.exe"),
        os.path.join(root, "ComfyUI", "python_embeded", "python.exe"),
        os.path.join(root, "venv", "Scripts", "python.exe"),
        os.path.join(root, "ComfyUI", "venv", "Scripts", "python.exe"),
        os.path.join(root, ".venv", "Scripts", "python.exe"),
        os.path.join(root, "env", "Scripts", "python.exe"),
    ]


def _sam_dir_candidates(root: str) -> list[str]:
    """Given a ComfyUI root, list directories that typically hold SAM weights.

    Covers both SAM v1 (sams/, sam/) and SAM 2 (sam2/, sams2/) layouts.
    """
    return [
        os.path.join(root, "ComfyUI", "models", "sams"),
        os.path.join(root, "models", "sams"),
        os.path.join(root, "ComfyUI", "models", "sam"),
        os.path.join(root, "models", "sam"),
        os.path.join(root, "ComfyUI", "models", "sam2"),
        os.path.join(root, "models", "sam2"),
        os.path.join(root, "ComfyUI", "models", "sams2"),
        os.path.join(root, "models", "sams2"),
    ]


def find_comfyui_python() -> str:
    """Best-effort search for a ComfyUI-bundled python.exe.

    Returns "" if nothing convincing is found.
    """
    for root in _COMFY_ROOT_CANDIDATES:
        if not os.path.isdir(root):
            continue
        for cand in _python_candidates(root):
            if os.path.isfile(cand):
                log.info("Auto-detected ComfyUI python: %s", cand)
                return cand
    return ""


def find_sam_model() -> str:
    """Best-effort search for SAM weights in a ComfyUI models folder.

    Prefers SAM 2 (newer, better masks per bit) over SAM v1, and within each
    family prefers the largest backbone: vit_h > vit_l > vit_b for v1, and
    large > base_plus > small > tiny for SAM 2. Returns "" if nothing is
    found.
    """
    # Lower score wins. Tuple encodes (family, size_rank).
    # family: 0 = SAM 2, 1 = SAM v1 (prefer SAM 2)
    # size_rank: lower = larger / higher quality.
    found: list[tuple[int, int, str]] = []
    for root in _COMFY_ROOT_CANDIDATES:
        if not os.path.isdir(root):
            continue
        for sam_dir in _sam_dir_candidates(root):
            if not os.path.isdir(sam_dir):
                continue
            for fn in os.listdir(sam_dir):
                lower = fn.lower()
                if not lower.endswith((".pth", ".pt", ".safetensors")):
                    continue
                if "sam" not in lower:
                    continue
                # SAM 2 detection (filename contains "sam2" or "sam_2")
                if "sam2" in lower or "sam_2" in lower:
                    for i, tag in enumerate(
                            ("large", "base_plus", "small", "tiny")):
                        if tag in lower:
                            found.append((0, i, os.path.join(sam_dir, fn)))
                            break
                    else:
                        found.append((0, 99, os.path.join(sam_dir, fn)))
                else:
                    for i, tag in enumerate(("vit_h", "vit_l", "vit_b")):
                        if tag in lower:
                            found.append((1, i, os.path.join(sam_dir, fn)))
                            break
                    else:
                        found.append((1, 99, os.path.join(sam_dir, fn)))
    if not found:
        return ""
    found.sort(key=lambda t: (t[0], t[1]))
    best = found[0][2]
    log.info("Auto-detected SAM weights: %s", best)
    return best


# ── Model-type inference (from filename) ────────────────────────────────────

def infer_model_type(model_path: str) -> str:
    """Derive SAM's model_type arg from the weight filename.

    For SAM v1 (`segment_anything` package): returns 'vit_h', 'vit_l', or
    'vit_b' matching `sam_model_registry` keys. For SAM 2 (`sam2` package):
    returns 'sam2_hiera_<tag>' where tag is t/s/b+/l, which the inference
    script maps to the bundled Hydra config. Defaults to 'vit_h' when
    ambiguous.
    """
    name = os.path.basename(model_path).lower()
    # SAM 2 first (more specific pattern).
    is_sam2 = ("sam2" in name) or ("sam_2" in name)
    if is_sam2:
        # Size tag — SAM 2's config filenames use t, s, b+, l as suffixes.
        if "tiny" in name:
            size = "t"
        elif "small" in name:
            size = "s"
        elif "base_plus" in name or "baseplus" in name or "b+" in name \
                or "_b_plus" in name:
            size = "b+"
        elif "large" in name:
            size = "l"
        else:
            size = "b+"
        # Detect v2.1 (backward-compatible with v2 API, just different configs).
        family = "sam2.1" if "sam2.1" in name or "sam2_1" in name else "sam2"
        return f"{family}_hiera_{size}"
    # SAM v1
    if "vit_h" in name or "vit-h" in name or "_h_" in name:
        return "vit_h"
    if "vit_l" in name or "vit-l" in name or "_l_" in name:
        return "vit_l"
    if "vit_b" in name or "vit-b" in name or "_b_" in name:
        return "vit_b"
    return "vit_h"


# ── The embedded inference script ───────────────────────────────────────────
#
# Runs inside the user's ComfyUI python.exe. Reads a single JSON object from
# stdin: {"image": path, "model": path, "model_type": str, "device": str,
#         "boxes": [[x1,y1,x2,y2], ...]  # pixel coords, int or float
#        }
# Writes one JSON object on stdout: {"ok": true, "masks": [b64_png, ...]}
# or {"ok": false, "error": "..."} on any failure.
#
# Kept short / dependency-minimal on purpose: segment_anything + torch + PIL
# + numpy are the only imports. No cv2, no accelerate, no transformers.

INFERENCE_SRC = r"""
import sys, os, json, io, base64, traceback

_INSTALL_HINT = (
    "Install SAM into this Python. For SAM 2 (recommended, matches your "
    "sam2_*.safetensors weights): "
    "`python -m pip install git+https://github.com/facebookresearch/"
    "segment-anything-2.git`. For SAM v1 (matches sam_vit_*.pth): "
    "`python -m pip install git+https://github.com/facebookresearch/"
    "segment-anything.git`. Torch must already be installed (it was)."
)

def _err(msg, hint=None, tb=None):
    out = {"ok": False, "error": msg}
    if hint: out["hint"] = hint
    if tb:   out["traceback"] = tb
    print(json.dumps(out)); sys.exit(0)

try:
    import numpy as np
    from PIL import Image
    import torch
except Exception as e:
    _err("numpy / PIL / torch import failed: " + repr(e), _INSTALL_HINT)

# Detect which SAM family the caller asked for based on model_type.
# 'sam2_hiera_*' / 'sam2.1_hiera_*'  → SAM 2
# 'vit_h' / 'vit_l' / 'vit_b'        → SAM v1
def _load_predictor(model_path, model_type, device):
    if model_type.startswith("sam2"):
        # ── SAM 2 path ───────────────────────────────────────────────────
        try:
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
        except Exception as e:
            _err("`sam2` package is not installed in this Python. "
                 + repr(e), _INSTALL_HINT)
        # Map model_type → bundled Hydra config name. SAM 2's __init__ runs
        # `initialize_config_module("sam2")` which registers the `sam2`
        # Python package as hydra's config search root, so the config_name
        # passed to `compose()` MUST be the path RELATIVE to sam2/ —
        # i.e. include the `configs/<family>/` prefix. Just passing the
        # bare yaml basename silently composes it as a nested key rather
        # than inlining the `# @package _global_` payload, and then
        # `instantiate(cfg.model)` blows up with "Missing key to" because
        # cfg.model doesn't exist. Learned the hard way during Route I
        # smoke-testing on the user's SAM 2.0 weights.
        #
        # Examples:
        #   sam2_hiera_b+   → 'configs/sam2/sam2_hiera_b+.yaml'    (SAM 2.0)
        #   sam2.1_hiera_l  → 'configs/sam2.1/sam2.1_hiera_l.yaml' (SAM 2.1)
        if model_type.startswith("sam2.1"):
            cfg_name = "configs/sam2.1/" + model_type + ".yaml"
        else:
            cfg_name = "configs/sam2/" + model_type + ".yaml"
        # Users sometimes keep the original .pt, sometimes the .safetensors
        # conversion ComfyUI ships. We handle both by loading the state
        # dict ourselves and injecting it, bypassing build_sam2's internal
        # torch.load.
        model = build_sam2(cfg_name, ckpt_path=None, device=device)
        ext = os.path.splitext(model_path)[1].lower()
        if ext == ".safetensors":
            from safetensors.torch import load_file as _st_load
            sd = _st_load(model_path, device="cpu")
        else:
            sd = torch.load(model_path, map_location="cpu")
            # Official SAM 2 checkpoints wrap weights under 'model' key.
            if isinstance(sd, dict) and "model" in sd and isinstance(
                    sd["model"], dict):
                sd = sd["model"]
        missing, unexpected = model.load_state_dict(sd, strict=False)
        if unexpected:
            print("WARN: unexpected keys (first 3): "
                  + str(list(unexpected)[:3]), file=sys.stderr)
        if missing:
            print("WARN: missing keys (first 3): "
                  + str(list(missing)[:3]), file=sys.stderr)
        model.to(device=device)
        return SAM2ImagePredictor(model), "sam2"
    else:
        # ── SAM v1 path ──────────────────────────────────────────────────
        try:
            from segment_anything import SamPredictor, sam_model_registry
        except Exception as e:
            _err("`segment_anything` package is not installed in this "
                 "Python. " + repr(e), _INSTALL_HINT)
        sam = sam_model_registry[model_type](checkpoint=model_path)
        sam.to(device=device)
        return SamPredictor(sam), "sam1"


def _run():
    req = json.loads(sys.stdin.read())
    img_path   = req["image"]
    model_path = req["model"]
    model_type = req.get("model_type", "vit_h")
    device     = req.get("device", "cuda")
    boxes      = req.get("boxes") or []

    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"

    predictor, family = _load_predictor(model_path, model_type, device)

    img = np.array(Image.open(img_path).convert("RGB"))
    predictor.set_image(img)

    H, W = img.shape[:2]
    masks_out = []
    picks = []  # diagnostic: which of the 3 candidate masks we picked per bbox
    for box in boxes:
        x1, y1, x2, y2 = [float(v) for v in box[:4]]
        x1 = max(0.0, min(W - 1.0, x1))
        x2 = max(0.0, min(W - 1.0, x2))
        y1 = max(0.0, min(H - 1.0, y1))
        y2 = max(0.0, min(H - 1.0, y2))
        if x2 <= x1 or y2 <= y1:
            masks_out.append(None); picks.append(None); continue

        bbox_area = max(1.0, (x2 - x1) * (y2 - y1))

        # Ask SAM for all 3 candidate masks. On floor-plan line art, the
        # single "best" mask often degenerates to the bbox itself (a blob)
        # because SAM was trained on natural photos and treats uniform
        # white background as "the object". Giving it an extra positive
        # point at the bbox CENTER anchors the segmentation to the
        # apartment interior, and picking the best of 3 masks by score
        # within a plausible area range tends to rescue the concave cases.
        cx = 0.5 * (x1 + x2)
        cy = 0.5 * (y1 + y2)
        box_arr   = np.array([[x1, y1, x2, y2]], dtype=np.float32)
        pt_coords = np.array([[cx, cy]], dtype=np.float32)
        pt_labels = np.array([1], dtype=np.int32)   # 1 = positive click

        with torch.inference_mode():
            masks, scores, _ = predictor.predict(
                box=box_arr,
                point_coords=pt_coords,
                point_labels=pt_labels,
                multimask_output=True,
            )
        # masks shape: (K, H, W), scores shape: (K,). K is typically 3.
        scored = []
        for i in range(masks.shape[0]):
            mi = masks[i]
            if mi.dtype != np.uint8:
                mi_bool = mi > 0.5
            else:
                mi_bool = mi > 127
            area = int(mi_bool.sum())
            frac = area / bbox_area
            scored.append((float(scores[i]), area, frac, i, mi_bool))

        # Reject "blob" masks (covers entire bbox + spillover ≥ ~98%) and
        # "sliver" masks (< ~8% — usually just a wall line or door arc).
        # Among the survivors pick the highest SAM score. If nothing
        # survives, fall back to the highest-scored mask anyway so we at
        # least return something; the Python-side quality gate (post
        # polygon trace) will reject it if the polygon is garbage too.
        survivors = [s for s in scored if 0.08 <= s[2] <= 0.98]
        if survivors:
            best = max(survivors, key=lambda s: s[0])
        else:
            best = max(scored, key=lambda s: s[0])
        sam_score, sam_area, sam_frac, pick_idx, m_bool = best
        picks.append({
            "pick": pick_idx,
            "score": round(sam_score, 4),
            "area_frac": round(sam_frac, 4),
            "n_candidates": int(masks.shape[0]),
        })

        m = (m_bool.astype(np.uint8)) * 255

        # Crop to the bbox so the returned PNG is small and can be
        # re-mapped by the caller via `_mask_to_polygon(box_in_img, ...)`.
        cx1, cy1 = int(max(0, x1)), int(max(0, y1))
        cx2, cy2 = int(min(W, x2 + 1)), int(min(H, y2 + 1))
        if cx2 <= cx1 or cy2 <= cy1:
            masks_out.append(None); continue
        crop = m[cy1:cy2, cx1:cx2]
        buf = io.BytesIO()
        Image.fromarray(crop, mode="L").save(buf, format="PNG", optimize=True)
        masks_out.append(base64.b64encode(buf.getvalue()).decode("ascii"))

    print(json.dumps({"ok": True, "masks": masks_out,
                      "picks": picks,
                      "device": device, "family": family,
                      "img_w": W, "img_h": H}))

try:
    _run()
except Exception as _exc:
    _err(repr(_exc), tb=traceback.format_exc())
"""


# ── Public API ───────────────────────────────────────────────────────────────

def refine_bboxes(png_bytes: bytes,
                  img_w: int, img_h: int,
                  bboxes_pct: Iterable[tuple[float, float, float, float]],
                  config: SamConfig,
                  progress_cb=None,
                  ) -> tuple[list[str | None], dict]:
    """Run SAM over the image for each bbox and return per-bbox mask PNGs.

    Parameters
    ----------
    png_bytes : bytes
        The floor-plan image as PNG bytes (same bytes sent to Gemini).
    img_w, img_h : int
        Full-image pixel dimensions of `png_bytes`.
    bboxes_pct : iterable of (x1, y1, x2, y2) in 0..1 image coords
        One box per apartment; ordering is preserved in the output.
    config : SamConfig
    progress_cb : callable(str) or None
        Optional status callback. Called once at the start and once at the end.

    Returns
    -------
    (masks, diag)
        masks : list of (base64-PNG-string | None). None means SAM either
                wasn't run for this bbox, produced an empty/degenerate mask,
                or failed. The PNGs are CROPPED to their source bbox, so the
                caller must pass the same bbox into
                `_mask_to_polygon(mask, bbox, img_w, img_h)`.
                Returning base64 strings (rather than raw bytes) matches the
                existing Gemini-mask pipeline so callers can feed the result
                straight into `_mask_to_polygon` with zero glue.
        diag : dict with keys {"ok": bool, "error": str,
                               "elapsed_ms": int, "device": str}.
    """
    diag: dict = {"ok": False, "error": "", "elapsed_ms": 0, "device": ""}
    boxes_list = list(bboxes_pct)
    if not boxes_list:
        diag["ok"] = True
        return [], diag
    if not config.ok:
        diag["error"] = ("SAM not configured (need python_path + model_path "
                         "both pointing to real files).")
        return [None] * len(boxes_list), diag

    boxes_px = []
    for (x1, y1, x2, y2) in boxes_list:
        boxes_px.append([
            float(x1) * img_w,
            float(y1) * img_h,
            float(x2) * img_w,
            float(y2) * img_h,
        ])

    # Write the image to a temp PNG so the subprocess doesn't have to
    # re-decode a huge base64 blob from stdin.
    tmp = tempfile.NamedTemporaryFile(
        prefix="ueplacer_sam_", suffix=".png", delete=False)
    tmp.write(png_bytes)
    tmp.flush()
    tmp.close()
    image_path = tmp.name

    req = {
        "image": image_path,
        "model": config.model_path,
        "model_type": infer_model_type(config.model_path),
        "device": config.device,
        "boxes": boxes_px,
    }

    if progress_cb:
        progress_cb(
            f"SAM: encoding image + {len(boxes_px)} bbox(es) via "
            f"{os.path.basename(config.python_path)}…")
    log.info("SAM subprocess: python=%s model=%s n_boxes=%d",
             config.python_path, config.model_path, len(boxes_px))

    t0 = time.monotonic()
    creation_flags = 0
    if sys.platform == "win32":
        # Don't pop a console window for the child process.
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [config.python_path, "-c", INFERENCE_SRC],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            timeout=max(30, int(config.timeout_s)),
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as exc:
        diag["error"] = f"SAM subprocess timed out after {config.timeout_s}s"
        log.error(diag["error"])
        _try_unlink(image_path)
        return [None] * len(boxes_list), diag
    except Exception as exc:
        diag["error"] = f"Failed to spawn SAM subprocess: {exc!r}"
        log.exception("spawn SAM subprocess")
        _try_unlink(image_path)
        return [None] * len(boxes_list), diag
    finally:
        pass  # image_path unlinked below

    elapsed = int((time.monotonic() - t0) * 1000)
    diag["elapsed_ms"] = elapsed

    # The child prints exactly one JSON object on stdout. Anything on stderr
    # is usually a torch warning we don't care about, but log it at DEBUG.
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stderr:
        log.debug("SAM subprocess stderr (first 2000 chars):\n%s",
                  stderr[:2000])

    # Keep the scratch image around for diagnostics if the child blew up;
    # otherwise delete it now.
    if proc.returncode == 0 and stdout.startswith("{"):
        _try_unlink(image_path)
    else:
        log.warning("SAM subprocess returncode=%d; keeping %s for inspection",
                    proc.returncode, image_path)

    if not stdout:
        diag["error"] = (
            f"SAM subprocess returned no stdout (returncode={proc.returncode}). "
            f"stderr: {stderr[:400]}"
        )
        log.error(diag["error"])
        return [None] * len(boxes_list), diag

    try:
        parsed = json.loads(stdout)
    except Exception as exc:
        diag["error"] = f"Could not parse SAM subprocess JSON: {exc!r}"
        log.error("%s\nfirst 1000 chars of stdout:\n%s",
                  diag["error"], stdout[:1000])
        return [None] * len(boxes_list), diag

    if not parsed.get("ok"):
        diag["error"] = str(parsed.get("error") or "unknown subprocess error")
        hint = parsed.get("hint")
        if hint:
            diag["error"] += "\n\nHint: " + str(hint)
        tb = parsed.get("traceback")
        if tb:
            log.error("SAM subprocess traceback:\n%s", tb)
        return [None] * len(boxes_list), diag

    diag["device"] = str(parsed.get("device") or "")
    diag["picks"] = parsed.get("picks") or []
    masks_b64 = parsed.get("masks") or []
    out: list[str | None] = []
    for item in masks_b64:
        if not item or not isinstance(item, str):
            out.append(None)
            continue
        # Light sanity check — must round-trip through base64 decode.
        try:
            base64.b64decode(item, validate=False)
        except Exception:
            out.append(None)
            continue
        out.append(item)
    # Pad / truncate to match input length
    while len(out) < len(boxes_list):
        out.append(None)
    out = out[:len(boxes_list)]

    diag["ok"] = True
    if progress_cb:
        n_ok = sum(1 for m in out if m)
        progress_cb(
            f"SAM refined {n_ok}/{len(out)} region(s) "
            f"in {elapsed} ms on {diag['device'] or '?'}.")
    log.info("SAM subprocess OK: %d/%d masks in %d ms on %s",
             sum(1 for m in out if m), len(out), elapsed, diag["device"])
    return out, diag


def _try_unlink(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.unlink(path)
    except Exception:
        pass
