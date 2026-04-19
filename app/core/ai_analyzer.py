"""AI-assisted floor-plan region detection via Google Gemini Vision.

Renders a single page of a PDF (or loads an image file) via PyMuPDF, sends it
to Google's Gemini API (the "Nano Banana" family — same API key from
https://aistudio.google.com/apikey), asks the model to locate each apartment
in the floor plan, and returns a list of proportional-coordinate polygons the
UI can turn into apt-type polygons.

All coordinates coming out of this module are proportional (0.0–1.0) relative
to the rendered page image. The UI layer scales them to whatever floor-plan
image is loaded in PlanCanvas.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # numpy only referenced inside helpers; keep imports lazy
    import numpy as _np  # noqa: F401


DEFAULT_DPI = 150
DEFAULT_MODEL = "gemini-2.5-flash"


# ── Hough wall-snap tunables ────────────────────────────────────────────────
#
# All thresholds are scaled by the short edge S = min(w, h) of the image the
# snap runs on, so they work at any render DPI / downsample resolution.
# Drop these into an inch of README if users end up wanting to tweak them;
# for now they're module-level so smoke tests can monkey-patch.

_HOUGH_RHO = 1.0                   # cv2.HoughLinesP rho resolution, pixels
_HOUGH_THETA_FRAC = 180            # theta = pi / _HOUGH_THETA_FRAC  (1°)
_HOUGH_THRESHOLD_PCT = 0.02        # votes threshold ≈ 2 % of S
_HOUGH_THRESHOLD_MIN = 30
_HOUGH_MIN_LINE_LEN_PCT = 0.03     # walls span ≥ 3 % of S
_HOUGH_MIN_LINE_LEN_MIN = 20
_HOUGH_MAX_LINE_GAP_PCT = 0.005    # bridge gaps from door arcs / text (0.5 % S)
_HOUGH_MAX_LINE_GAP_MIN = 5

_CLUSTER_ANGLE_DEG = 2.5           # merge lines within this angle (°)
_CLUSTER_DIST_PX_PCT = 0.003       # merge lines within this perp distance

_SNAP_ANGLE_THRESH_DEG = 15.0      # only snap edges within this of a wall line
_SNAP_DIST_THRESH_PCT = 0.015      # only snap if edge midpoint is within 1.5 %
_SNAP_PARALLEL_THRESH_DEG = 5.0    # vertices with two near-parallel snaps fall
                                   # back to projection instead of intersection

_SORT_BIN_PCT = 0.05               # y-row bin height for raster-scan ordering


# ── File logging ─────────────────────────────────────────────────────────────
#
# Persistent diagnostic log for the AI import feature. Lives in the user
# profile so it survives app re-installs / .exe replacements. Rotated at
# ~2 MB, keeping 3 backups.

def _log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") \
        or os.path.expanduser("~\\AppData\\Local")
    d = os.path.join(base, "UE-Apartment-Placer", "logs")
    os.makedirs(d, exist_ok=True)
    return d


LOG_PATH = os.path.join(_log_dir(), "ai_analyzer.log")


def _make_logger() -> logging.Logger:
    lg = logging.getLogger("ue_placer.ai_analyzer")
    if lg.handlers:
        return lg
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        fh = RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        lg.addHandler(fh)
    except Exception:
        # If we can't open the file, fall back to stderr so we at least have
        # something when running from a terminal.
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"))
        lg.addHandler(sh)
    return lg


log = _make_logger()
log.info("=" * 60)
log.info("ai_analyzer module loaded; log path = %s", LOG_PATH)


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class DetectedRegion:
    label: str
    bbox_pct: tuple[float, float, float, float]        # (x1, y1, x2, y2) in 0..1
    polygon_pct: list[tuple[float, float]]             # 4+ corners in 0..1
    raw: dict = field(default_factory=dict)


@dataclass
class AnalyzeResult:
    page_image_png: bytes                              # rendered page PNG bytes
    page_w: int
    page_h: int
    regions: list[DetectedRegion]


# ── PDF / image loading (via PyMuPDF) ───────────────────────────────────────

def render_pdf_page(pdf_path: str, page_index: int = 0,
                    dpi: int = DEFAULT_DPI) -> tuple[bytes, int, int]:
    """Render one PDF page to a PNG byte-string. Returns (png, w, h)."""
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    try:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(
                f"Page {page_index + 1} is out of range "
                f"(document has {doc.page_count} page(s))."
            )
        page = doc.load_page(page_index)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")
        return png_bytes, pix.width, pix.height
    finally:
        doc.close()


def render_pdf_all_pages(pdf_path: str,
                         dpi: int = DEFAULT_DPI,
                         progress_cb=None) -> list[tuple[bytes, int, int]]:
    """Render every page of a PDF to PNG bytes. Returns a list of (png, w, h).

    Opens the document once and reuses it across all pages, which is much
    faster than calling render_pdf_page() in a loop. `progress_cb`, if given,
    is invoked as progress_cb(page_index, page_count) before each page.
    """
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    try:
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        out: list[tuple[bytes, int, int]] = []
        for i in range(doc.page_count):
            if progress_cb is not None:
                progress_cb(i, doc.page_count)
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out.append((pix.tobytes("png"), pix.width, pix.height))
        return out
    finally:
        doc.close()


def load_image_as_png(path: str) -> tuple[bytes, int, int]:
    """Load an image file (or PDF) as PNG bytes. For PDFs, loads page 0."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return render_pdf_page(path, 0)
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img.width, img.height
    except Exception:
        with open(path, "rb") as fh:
            data = fh.read()
        return data, 0, 0


def pdf_page_count(pdf_path: str) -> int:
    import fitz
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


# ── Key validation ───────────────────────────────────────────────────────────

def _validate_google_key(key: str) -> None:
    """Raise RuntimeError if the key looks malformed.

    Google AI Studio keys typically look like `AIzaSy…` (39 characters,
    starting with `AIza`). We accept anything that's reasonably long but
    flag clearly-broken inputs.
    """
    if not key:
        raise RuntimeError(
            "No API key provided. Get one at https://aistudio.google.com/apikey"
        )
    if " " in key or "\n" in key or "\t" in key:
        raise RuntimeError(
            "API key contains whitespace. Re-copy the key from "
            "https://aistudio.google.com/apikey without any surrounding text.")
    if len(key) < 20:
        raise RuntimeError(
            "That doesn't look like a Google AI Studio key (too short). "
            "Keys typically start with 'AIza' and are 39 characters long. "
            "Copy it again from https://aistudio.google.com/apikey.")


# ── Response parsing ────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Any:
    """Pull the first JSON block out of a Gemini text response.

    Gemini usually honours the 'respond with JSON only' instruction, but it
    sometimes wraps its output in ```json fences```, prefixes it with prose,
    or trails a natural-language summary. Handle all three shapes.
    """
    if not text:
        return None

    # 1) Fenced code block
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass

    # 2) Whole response is JSON
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass

    # 3) First top-level array or object substring
    for opener, closer in [("[", "]"), ("{", "}")]:
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start:end + 1])
            except Exception:
                continue
    return None


def _poly_bbox_area(bbox_pct: tuple[float, float, float, float]) -> float:
    """Axis-aligned bbox area in 0..1 image-space (fraction² of image)."""
    x1, y1, x2, y2 = bbox_pct
    return max(0.0, float(x2) - float(x1)) * max(0.0, float(y2) - float(y1))


def _polygon_area_pct(poly: list[tuple[float, float]]) -> float:
    """Shoelace area of a 0..1 polygon (fraction² of image). Sign-agnostic."""
    if not poly or len(poly) < 3:
        return 0.0
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += float(x1) * float(y2) - float(x2) * float(y1)
    return abs(s) * 0.5


def _normalize_bbox(bbox: Any, img_w: int, img_h: int
                    ) -> tuple[float, float, float, float] | None:
    """Coerce a model-emitted bbox into (x1,y1,x2,y2) in 0..1 image coords.

    Gemini returns bounding boxes in **normalized 0–1000** coords (this is
    documented in the Gemini Vision grounding docs). We accept a few other
    shapes too, just in case the model returns pixels or a {x,y,w,h} dict.
    """
    if bbox is None:
        return None

    # Dict shapes: {ymin, xmin, ymax, xmax} (Gemini's canonical key order),
    # {x, y, w, h}, {x1, y1, x2, y2}
    if isinstance(bbox, dict):
        if all(k in bbox for k in ("xmin", "ymin", "xmax", "ymax")):
            b = [bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"]]
        elif all(k in bbox for k in ("x1", "y1", "x2", "y2")):
            b = [bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]]
        elif all(k in bbox for k in ("x", "y", "w", "h")):
            b = [bbox["x"], bbox["y"],
                 bbox["x"] + bbox["w"], bbox["y"] + bbox["h"]]
        else:
            return None
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        # Gemini's standard order is [ymin, xmin, ymax, xmax] in 0..1000
        y1, x1, y2, x2 = bbox[0], bbox[1], bbox[2], bbox[3]
        b = [x1, y1, x2, y2]
    else:
        return None

    try:
        x1, y1, x2, y2 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    except Exception:
        return None

    # Detect coord system: 0..1 floats, 0..1000 ints (Gemini's grounding),
    # or raw pixels.
    max_val = max(x1, y1, x2, y2)
    if max_val <= 1.5:
        # Already normalized 0..1
        pass
    elif max_val <= 1001:
        # Gemini's 0..1000 convention
        x1 /= 1000.0
        y1 /= 1000.0
        x2 /= 1000.0
        y2 /= 1000.0
    elif img_w > 0 and img_h > 0:
        # Pixel coords
        x1 /= img_w
        y1 /= img_h
        x2 /= img_w
        y2 /= img_h
    else:
        return None

    # Clip to [0,1] and normalize order
    x1, x2 = max(0.0, min(x1, x2)), min(1.0, max(x1, x2))
    y1, y2 = max(0.0, min(y1, y2)), min(1.0, max(y1, y2))
    if x2 - x1 < 0.002 or y2 - y1 < 0.002:
        return None  # degenerate
    return (x1, y1, x2, y2)


def _normalize_polygon(poly: Any, img_w: int, img_h: int
                       ) -> list[tuple[float, float]] | None:
    """Coerce a polygon-as-list-of-points into 0..1 image coords."""
    if not isinstance(poly, (list, tuple)) or len(poly) < 3:
        return None
    pts: list[tuple[float, float]] = []
    max_val = 0.0
    for pt in poly:
        if isinstance(pt, dict) and "x" in pt and "y" in pt:
            x, y = float(pt["x"]), float(pt["y"])
        elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
            # Gemini polygon convention is typically (y, x) in 0..1000 too,
            # BUT we can't easily distinguish (x,y) from (y,x) heuristically.
            # Assume (y, x) to match the bbox convention.
            y, x = float(pt[0]), float(pt[1])
        else:
            return None
        pts.append((x, y))
        max_val = max(max_val, x, y)

    if max_val <= 1.5:
        scale_x = scale_y = 1.0
    elif max_val <= 1001:
        scale_x = scale_y = 1.0 / 1000.0
    elif img_w > 0 and img_h > 0:
        scale_x, scale_y = 1.0 / img_w, 1.0 / img_h
    else:
        return None

    out = [(max(0.0, min(1.0, x * scale_x)),
            max(0.0, min(1.0, y * scale_y))) for x, y in pts]
    return out


# ── Main Gemini call ─────────────────────────────────────────────────────────

# ── Mask decoding + contour tracing ──────────────────────────────────────────

_DATA_URI_RE = re.compile(
    r"^data:image/[^;]+;base64,", re.IGNORECASE)


def _decode_mask_png(mask_field: Any) -> "_np.ndarray | None":
    """Decode Gemini's base64-encoded PNG mask to a 2D uint8 numpy array."""
    if not isinstance(mask_field, str) or not mask_field.strip():
        return None
    s = mask_field.strip()
    s = _DATA_URI_RE.sub("", s)
    # Strip whitespace sometimes embedded by the model
    s = re.sub(r"\s+", "", s)
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        return None
    try:
        import numpy as _np
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("L")
        return _np.asarray(img, dtype=_np.uint8)
    except Exception:
        return None


def _trace_contour(mask: "_np.ndarray", threshold: int = 127
                   ) -> list[tuple[int, int]]:
    """Trace the outer boundary of the largest foreground blob in `mask`.

    Uses Moore-neighbor boundary tracing (8-connectivity). Returns a list of
    (x, y) integer pixel coordinates. Returns [] on degenerate inputs.
    """
    import numpy as _np
    if mask is None or mask.size == 0:
        return []
    bw = mask > threshold
    if not bw.any():
        return []

    # Keep the largest connected component — Gemini sometimes paints tiny
    # speckles that would otherwise derail the trace.
    try:
        from scipy import ndimage as _ndi
        labels, n = _ndi.label(bw)
        if n > 1:
            sizes = _ndi.sum(bw, labels, range(1, n + 1))
            keep = int(sizes.argmax()) + 1
            bw = labels == keep
    except Exception:
        pass  # scipy not available → trace whole mask

    h, w = bw.shape
    # Padded copy so we can safely index neighbours at the border.
    padded = _np.zeros((h + 2, w + 2), dtype=bool)
    padded[1:-1, 1:-1] = bw

    # Find a start pixel (top-left-most True)
    ys, xs = _np.where(padded)
    if ys.size == 0:
        return []
    start = (int(xs[0]), int(ys[0]))

    # Clockwise neighbour order starting from "west"
    neighbours = [(-1, 0), (-1, -1), (0, -1), (1, -1),
                  (1, 0), (1, 1), (0, 1), (-1, 1)]

    def _nb_index(prev_dir: int) -> int:
        # Backtrack: step rotates to the direction we came from + 1
        return (prev_dir + 6) % 8

    contour: list[tuple[int, int]] = [start]
    prev = (start[0] - 1, start[1])  # west of start
    # Initial backtrack direction: from start, prev was west → dir=0
    current = start
    back_dir = 0
    max_steps = 4 * (w + h) * 8 + 100

    for _step in range(max_steps):
        # Scan neighbours clockwise starting just past the backtrack pixel
        found = False
        for k in range(8):
            d = (back_dir + k) % 8
            dx, dy = neighbours[d]
            nx, ny = current[0] + dx, current[1] + dy
            if padded[ny, nx]:
                # Backtrack = previous pixel direction (opposite of this step)
                back_dir = (d + 4) % 8
                # Actually want to start scanning from one past the pixel
                # we came FROM, which is the last `prev`; in Moore tracing
                # the standard is: next back_dir = d + 6 mod 8.
                back_dir = (d + 6) % 8
                prev = current
                current = (nx, ny)
                contour.append(current)
                found = True
                break
        if not found:
            break
        if current == start and len(contour) > 2:
            contour.pop()  # dedupe closing pixel
            break

    # Un-pad
    return [(x - 1, y - 1) for (x, y) in contour]


def _rdp(points: list[tuple[float, float]], epsilon: float
         ) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker polyline simplification."""
    if len(points) < 3:
        return list(points)

    def _perp_sq(p, a, b):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return (p[0] - ax) ** 2 + (p[1] - ay) ** 2
        t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        return (p[0] - px) ** 2 + (p[1] - py) ** 2

    def _recur(pts, eps2):
        if len(pts) < 3:
            return pts
        a, b = pts[0], pts[-1]
        max_d = 0.0
        max_i = 0
        for i in range(1, len(pts) - 1):
            d = _perp_sq(pts[i], a, b)
            if d > max_d:
                max_d = d
                max_i = i
        if max_d > eps2:
            left = _recur(pts[:max_i + 1], eps2)
            right = _recur(pts[max_i:], eps2)
            return left[:-1] + right
        return [a, b]

    return _recur(points, epsilon * epsilon)


def _smooth_mask_architectural(mask: "_np.ndarray") -> "_np.ndarray":
    """Morphological close + fill-holes for architectural masks.

    Floor-plan apartments are ~rectilinear rooms surrounded by walls that
    SAM often splits at door swings / wall thickness changes, leaving a
    clean-ish boundary pitted with 3–15-pixel notches. A morphological
    close (dilate-then-erode) with a kernel sized as a fraction of the
    mask fills those notches without noticeably shrinking or growing
    the outer boundary, and `binary_fill_holes` wipes interior specks
    (e.g. a door-swing that got carved all the way through).

    Returns a uint8 mask (0 / 255). If scipy isn't available returns the
    input unchanged.
    """
    import numpy as _np
    try:
        from scipy import ndimage as _ndi
    except Exception:
        return mask
    if mask is None or mask.size == 0:
        return mask
    bw = mask > 127
    mh, mw = bw.shape
    # Kernel radius ≈ 1.5 % of the short mask edge. Empirically this is
    # the smallest that reliably fills door swings (~0.8 m at typical
    # extraction DPI) without merging apartments in multi-unit crops.
    short = max(1, min(mh, mw))
    r = max(2, int(round(short * 0.015)))
    # Box kernel via `iterations` is equivalent to a flat square SE of
    # side 2r+1 (scipy uses 3×3 cross by default). Good enough.
    bw = _ndi.binary_closing(bw, iterations=r)
    try:
        bw = _ndi.binary_fill_holes(bw)
    except Exception:
        pass
    # Also open by 1–2 px to knock off any remaining spurs.
    bw = _ndi.binary_opening(bw, iterations=max(1, r // 3))
    return (bw.astype(_np.uint8)) * 255


def _orthogonalize_polygon(poly_pct: list[tuple[float, float]],
                           angle_thresh_deg: float = 12.0,
                           ) -> list[tuple[float, float]]:
    """Snap edges within ``angle_thresh_deg`` of H/V to exactly H/V.

    .. deprecated::
        Superseded by the Hough wall-snap pass wired into
        ``analyze_image`` (see ``_detect_wall_lines`` and
        ``_snap_polygon_to_walls``). That pass handles non-axis-aligned
        walls correctly and produces watertight corners via line-line
        intersection. This function is kept for backward compatibility
        with any external callers of ``_mask_to_polygon`` and for
        dependency-free emergencies where cv2 isn't importable.

    After RDP simplification an architectural polygon still has a few
    3–8° edges that should obviously be horizontal/vertical. This
    function walks each edge, classifies it (H / V / diagonal), then
    drags its two endpoints so that H edges share a common Y and V
    edges share a common X. Vertices at the junction of an H edge and
    a V edge get both updates naturally (x from the V neighbour, y
    from the H neighbour) which produces a clean orthogonal corner.

    A single pass is usually enough for architectural polygons; if
    adjacent H or V edges disagree on the shared coordinate we simply
    average, which is slightly lossy but doesn't compound across the
    polygon.
    """
    import math
    n = len(poly_pct)
    if n < 4:
        return poly_pct
    pts = [list(p) for p in poly_pct]

    # Classify each edge i..i+1
    kinds: list[str] = []
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        if dx == 0.0 and dy == 0.0:
            kinds.append("D")
            continue
        # angle in [0, 90] from nearest axis
        ang = math.degrees(math.atan2(abs(dy), abs(dx)))
        if ang <= angle_thresh_deg:
            kinds.append("H")
        elif ang >= 90.0 - angle_thresh_deg:
            kinds.append("V")
        else:
            kinds.append("D")

    # For each vertex collect "target Y" from adjacent H edges and
    # "target X" from adjacent V edges, then average.
    x_targets: list[list[float]] = [[] for _ in range(n)]
    y_targets: list[list[float]] = [[] for _ in range(n)]
    for i, kind in enumerate(kinds):
        j = (i + 1) % n
        if kind == "H":
            y_mid = 0.5 * (pts[i][1] + pts[j][1])
            y_targets[i].append(y_mid)
            y_targets[j].append(y_mid)
        elif kind == "V":
            x_mid = 0.5 * (pts[i][0] + pts[j][0])
            x_targets[i].append(x_mid)
            x_targets[j].append(x_mid)

    for i in range(n):
        if x_targets[i]:
            pts[i][0] = sum(x_targets[i]) / len(x_targets[i])
        if y_targets[i]:
            pts[i][1] = sum(y_targets[i]) / len(y_targets[i])

    return [(float(p[0]), float(p[1])) for p in pts]


def _mask_to_polygon(mask_field: Any,
                     box_in_img: tuple[float, float, float, float],
                     img_w: int, img_h: int,
                     *,
                     architectural: bool = False,
                     ) -> list[tuple[float, float]] | None:
    """Full mask → 0..1 image-space polygon pipeline.

    `box_in_img` is (x1, y1, x2, y2) in 0..1 image coords (the bounding box
    associated with this mask). The mask's own pixel grid corresponds to
    this box in full-image space.

    Pass ``architectural=True`` for SAM-refined masks of floor plans:
    this enables morphological smoothing of the mask, a more aggressive
    RDP epsilon (1.5 % of diagonal vs. 0.5 %), and H/V edge snapping,
    which together turn the raw pixel-accurate trace into clean
    architectural lines. Gemini-only masks are coarse to begin with so
    they go through the legacy path unchanged.
    """
    mask = _decode_mask_png(mask_field)
    if mask is None:
        return None

    if architectural:
        mask = _smooth_mask_architectural(mask)

    contour = _trace_contour(mask)
    if len(contour) < 3:
        return None

    mh, mw = mask.shape
    if mw <= 1 or mh <= 1:
        return None

    # Simplify in mask-pixel space first. Architectural masks have been
    # morphologically cleaned so we can afford a tighter RDP budget
    # (fewer verts = cleaner walls); Gemini's coarse masks keep the
    # gentler 0.5 % epsilon.
    diag = (mw * mw + mh * mh) ** 0.5
    eps_frac = 0.015 if architectural else 0.005
    epsilon = max(1.0, diag * eps_frac)
    simplified = _rdp(
        [(float(x), float(y)) for (x, y) in contour], epsilon)
    if len(simplified) < 3:
        simplified = [(float(x), float(y)) for (x, y) in contour]

    bx1, by1, bx2, by2 = box_in_img
    bw_img = max(1e-9, bx2 - bx1)
    bh_img = max(1e-9, by2 - by1)

    poly_pct: list[tuple[float, float]] = []
    for (mx, my) in simplified:
        fx = bx1 + (mx / (mw - 1)) * bw_img
        fy = by1 + (my / (mh - 1)) * bh_img
        poly_pct.append((max(0.0, min(1.0, fx)),
                         max(0.0, min(1.0, fy))))

    # Approximate close-dedupe (RDP can leave the last vertex within a
    # sub-pixel of the first) and drop near-collinear runs.
    poly_pct = _dedupe_and_straighten(poly_pct, tol=5e-4)

    # NOTE: architectural H/V orthogonalization used to happen here, but has
    # been replaced with a global Hough wall-snap pass at the end of
    # ``analyze_image`` — see ``_detect_wall_lines`` and
    # ``_snap_polygon_to_walls``. The new pass handles non-axis-aligned
    # walls correctly and recovers watertight corners via line-line
    # intersection, which the per-polygon orthogonalize could not. A
    # slightly wider dedupe tolerance is still useful for architectural
    # masks because the mask itself has been morph-cleaned upstream.
    if architectural and len(poly_pct) >= 4:
        poly_pct = _dedupe_and_straighten(poly_pct, tol=3e-3)

    if len(poly_pct) < 3:
        return None
    return poly_pct


def _dedupe_and_straighten(pts: list[tuple[float, float]],
                           tol: float = 1e-3
                           ) -> list[tuple[float, float]]:
    """Remove near-duplicate vertices and collapse near-collinear triples."""
    if len(pts) < 3:
        return pts

    # 1) Approximate dedupe — drop neighbours closer than `tol`
    cleaned: list[tuple[float, float]] = []
    for p in pts:
        if not cleaned:
            cleaned.append(p)
            continue
        qx, qy = cleaned[-1]
        if ((p[0] - qx) ** 2 + (p[1] - qy) ** 2) ** 0.5 > tol:
            cleaned.append(p)
    # Close-loop dedupe
    if len(cleaned) > 3:
        qx, qy = cleaned[0]
        px, py = cleaned[-1]
        if ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5 < tol:
            cleaned.pop()

    if len(cleaned) < 3:
        return cleaned

    # 2) Collapse three nearly-collinear consecutive points
    out: list[tuple[float, float]] = []
    n = len(cleaned)
    for i in range(n):
        a = cleaned[(i - 1) % n]
        b = cleaned[i]
        c = cleaned[(i + 1) % n]
        # Signed twice-area of triangle abc; small → collinear
        cross = abs((b[0] - a[0]) * (c[1] - a[1])
                    - (b[1] - a[1]) * (c[0] - a[0]))
        if cross > tol * tol * 4:  # keep the vertex
            out.append(b)
    return out if len(out) >= 3 else cleaned


# ── Hough wall detection + polygon snap ─────────────────────────────────────
#
# Replaces the old per-polygon `_orthogonalize_polygon` pass with a global
# wall-line detector that runs ONCE on the downsampled image we send to
# Gemini / SAM and a per-polygon snap step that moves each edge onto the
# nearest matching wall line. Corner positions are then recovered as the
# intersection of adjacent snapped lines (Feltes-style), producing
# watertight, pixel-exact architectural corners for any wall orientation
# (not just horizontal / vertical).
#
# References: Pizarro "Wall polygon retrieval…" (CC7910), §3; Feltes et al.
# 2014 "Improved Contour-Based Corner Detection"; Macé et al. 2010.


def _decode_png_gray(png_bytes: bytes) -> "_np.ndarray | None":
    """Decode a PNG byte-string to a uint8 grayscale numpy array."""
    try:
        import numpy as _np
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        return _np.asarray(img, dtype=_np.uint8)
    except Exception:
        return None


def _line_normal_form(x1: float, y1: float, x2: float, y2: float
                      ) -> tuple[float, float, float] | None:
    """Return (nx, ny, c) with nx² + ny² = 1 and nx·x + ny·y = c.

    `c` is the signed perpendicular distance from the origin; `(nx, ny)` is
    the unit normal. Degenerate (zero-length) segments return None.
    """
    import math
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    # Normal is perpendicular to direction; sign choice is arbitrary but we
    # normalise so the normal always points into the upper-right half-plane
    # to keep clustering stable regardless of endpoint order.
    nx, ny = -dy / length, dx / length
    if (nx < 0) or (nx == 0 and ny < 0):
        nx, ny = -nx, -ny
    c = nx * x1 + ny * y1
    return nx, ny, c


def _line_angle_deg(x1: float, y1: float, x2: float, y2: float) -> float:
    """Line angle in [0, 180). 0 = horizontal, 90 = vertical."""
    import math
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return 0.0
    a = math.degrees(math.atan2(dy, dx))
    # Collapse to [0, 180) — lines don't care about direction.
    while a < 0.0:
        a += 180.0
    while a >= 180.0:
        a -= 180.0
    return a


def _angle_diff_deg(a: float, b: float) -> float:
    """Smallest angle between two [0, 180) orientations."""
    d = abs(a - b) % 180.0
    if d > 90.0:
        d = 180.0 - d
    return d


def _detect_wall_lines(png_bytes: bytes, w: int, h: int
                       ) -> list[tuple[float, float, float, float]]:
    """Detect dominant wall lines in a floor-plan PNG.

    Returns a list of (x1, y1, x2, y2) line endpoints in **normalized 0..1
    image coordinates**. The line set has been clustered so that near-parallel
    and near-collinear Hough segments are merged into single representative
    "wall axes".

    Safe on any failure mode (missing cv2, small image, Hough fails): returns
    `[]` and the caller treats the snap step as a no-op.
    """
    if w <= 0 or h <= 0:
        return []
    try:
        import cv2 as _cv2
        import numpy as _np
    except Exception as exc:
        log.warning("Hough wall detection skipped — cv2/numpy not available: %r",
                    exc)
        return []

    gray = _decode_png_gray(png_bytes)
    if gray is None or gray.size == 0:
        log.warning("Hough wall detection skipped — failed to decode PNG")
        return []

    img_h, img_w = gray.shape[:2]
    S = max(1, min(img_w, img_h))

    # Otsu binarize + invert so walls (dark) become foreground (255).
    try:
        _thr, bw = _cv2.threshold(
            gray, 0, 255,
            _cv2.THRESH_BINARY_INV | _cv2.THRESH_OTSU)
    except Exception as exc:
        log.warning("Otsu threshold failed: %r", exc)
        return []

    # Bridge 1-2 px gaps (door arcs crossing wall lines, dimension text, etc.)
    try:
        kern = _np.ones((2, 2), dtype=_np.uint8)
        bw = _cv2.morphologyEx(bw, _cv2.MORPH_CLOSE, kern)
    except Exception:
        pass

    thresh = max(_HOUGH_THRESHOLD_MIN, int(round(_HOUGH_THRESHOLD_PCT * S)))
    min_len = max(_HOUGH_MIN_LINE_LEN_MIN,
                  int(round(_HOUGH_MIN_LINE_LEN_PCT * S)))
    max_gap = max(_HOUGH_MAX_LINE_GAP_MIN,
                  int(round(_HOUGH_MAX_LINE_GAP_PCT * S)))
    theta = 3.141592653589793 / _HOUGH_THETA_FRAC

    try:
        raw = _cv2.HoughLinesP(bw, _HOUGH_RHO, theta, thresh,
                               minLineLength=min_len, maxLineGap=max_gap)
    except Exception as exc:
        log.warning("HoughLinesP failed: %r", exc)
        return []

    if raw is None or len(raw) == 0:
        log.info("Hough: no lines detected")
        return []

    # `raw` shape: (N, 1, 4) with int endpoints in pixel space.
    segs: list[tuple[float, float, float, float]] = []
    for row in raw:
        x1, y1, x2, y2 = row[0]
        segs.append((float(x1), float(y1), float(x2), float(y2)))

    # ── Cluster near-parallel, near-collinear segments ──────────────────
    # Bucket key = (angle_bin, distance_bin). Angle rounded to _CLUSTER_ANGLE_DEG;
    # perpendicular-distance-from-origin rounded to _CLUSTER_DIST_PX_PCT * S.
    dist_bin_px = max(1.0, _CLUSTER_DIST_PX_PCT * S)
    buckets: dict[tuple[int, int], list[tuple[float, float, float, float, float]]] = {}
    for (x1, y1, x2, y2) in segs:
        ang = _line_angle_deg(x1, y1, x2, y2)
        nf = _line_normal_form(x1, y1, x2, y2)
        if nf is None:
            continue
        _nx, _ny, c = nf
        ang_key = int(round(ang / _CLUSTER_ANGLE_DEG))
        # Angles near 180° should bucket with angles near 0° (same orientation).
        if ang_key * _CLUSTER_ANGLE_DEG >= 180.0 - _CLUSTER_ANGLE_DEG * 0.5:
            ang_key = 0
        dist_key = int(round(c / dist_bin_px))
        key = (ang_key, dist_key)
        length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        buckets.setdefault(key, []).append((x1, y1, x2, y2, length))

    clustered: list[tuple[float, float, float, float]] = []
    for key, members in buckets.items():
        # Representative line: pick the longest segment's angle, then extend
        # to the span of all member endpoints projected onto that direction.
        members.sort(key=lambda m: m[4], reverse=True)
        x1, y1, x2, y2, _ = members[0]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        if norm < 1e-9:
            continue
        ux, uy = dx / norm, dy / norm  # unit direction
        # Project every endpoint in the bucket onto this direction.
        ts: list[float] = []
        for (mx1, my1, mx2, my2, _L) in members:
            ts.append((mx1 - x1) * ux + (my1 - y1) * uy)
            ts.append((mx2 - x1) * ux + (my2 - y1) * uy)
        t_min, t_max = min(ts), max(ts)
        ex1 = x1 + ux * t_min
        ey1 = y1 + uy * t_min
        ex2 = x1 + ux * t_max
        ey2 = y1 + uy * t_max
        clustered.append((ex1, ey1, ex2, ey2))

    # Normalize to 0..1 image coords.
    out = [(x1 / img_w, y1 / img_h, x2 / img_w, y2 / img_h)
           for (x1, y1, x2, y2) in clustered]

    log.info("Hough wall detection: %d raw segments -> %d clustered "
             "(thresh=%d min_len=%d gap=%d on %dx%d)",
             len(segs), len(out), thresh, min_len, max_gap, img_w, img_h)

    # Graceful fallback: need enough walls for snap to be meaningful.
    if len(out) < 4:
        log.info("Hough: only %d clustered lines — skipping snap", len(out))
        return []
    return out


def _point_line_perp_distance(px: float, py: float,
                              lx1: float, ly1: float,
                              lx2: float, ly2: float) -> float:
    """Perpendicular distance from (px, py) to the INFINITE line through L."""
    import math
    dx, dy = lx2 - lx1, ly2 - ly1
    n = math.hypot(dx, dy)
    if n < 1e-12:
        return math.hypot(px - lx1, py - ly1)
    # 2D cross product magnitude / direction length = perp distance
    return abs((py - ly1) * dx - (px - lx1) * dy) / n


def _project_point_on_line(px: float, py: float,
                           lx1: float, ly1: float,
                           lx2: float, ly2: float
                           ) -> tuple[float, float]:
    """Orthogonal projection of (px, py) onto the infinite line through L."""
    dx, dy = lx2 - lx1, ly2 - ly1
    denom = dx * dx + dy * dy
    if denom < 1e-18:
        return (lx1, ly1)
    t = ((px - lx1) * dx + (py - ly1) * dy) / denom
    return (lx1 + t * dx, ly1 + t * dy)


def _line_line_intersection(a1: tuple[float, float], a2: tuple[float, float],
                            b1: tuple[float, float], b2: tuple[float, float]
                            ) -> tuple[float, float] | None:
    """Intersection of two INFINITE lines through (a1,a2) and (b1,b2).

    Returns None if the lines are parallel (within numerical tolerance).
    """
    x1, y1 = a1
    x2, y2 = a2
    x3, y3 = b1
    x4, y4 = b2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-15:
        return None
    t_num = (x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)
    t = t_num / denom
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _snap_polygon_to_walls(poly_pct: list[tuple[float, float]],
                           wall_lines: list[tuple[float, float, float, float]],
                           S_px: int,
                           ) -> list[tuple[float, float]]:
    """Snap each edge of a 0..1 polygon onto the closest matching wall line.

    Two-pass algorithm:

    1. **Edge → line assignment.** For each edge, find the wall line whose
       direction matches (within ``_SNAP_ANGLE_THRESH_DEG``) and whose
       perpendicular distance to the edge midpoint is below
       ``_SNAP_DIST_THRESH_PCT`` of the short image edge. Pick the closest.
    2. **Vertex resolution (Feltes refs [19]).** A vertex sitting between
       two snapped edges is placed at their line-line intersection; if
       only one neighbouring edge snapped, the vertex is projected onto
       that line; if neither snapped, the vertex stays put.

    Result: a watertight polygon whose edges lie exactly on detected walls
    wherever possible, with pixel-exact corners at the geometric
    intersections of those walls.
    """
    import math

    n = len(poly_pct)
    if n < 3 or not wall_lines:
        return list(poly_pct)

    # Short-edge scale in 0..1 is 1.0 for a square, but edges can be
    # asymmetric. We use min(w,h) = S_px in pixels (the caller passes it)
    # converted to 0..1 by dividing by S_px itself — which gives 1.0. In
    # practice the snap threshold is already in normalised units, so:
    #   dist_thresh_norm = _SNAP_DIST_THRESH_PCT   (S-units already)
    # We keep S_px around for potential diagnostic logging.
    _ = S_px
    dist_thresh = _SNAP_DIST_THRESH_PCT

    # Precompute each wall line's angle in [0, 180).
    line_angles: list[float] = []
    for (x1, y1, x2, y2) in wall_lines:
        line_angles.append(_line_angle_deg(x1, y1, x2, y2))

    # ── Pass 1: assign each edge to a wall line (or None) ───────────────
    snap: list[int | None] = [None] * n
    for i in range(n):
        x1, y1 = poly_pct[i]
        x2, y2 = poly_pct[(i + 1) % n]
        edge_len = math.hypot(x2 - x1, y2 - y1)
        if edge_len < 1e-6:
            continue
        edge_ang = _line_angle_deg(x1, y1, x2, y2)
        mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)

        best_idx = -1
        best_dist = dist_thresh + 1.0  # must be strictly below threshold
        for li, (lx1, ly1, lx2, ly2) in enumerate(wall_lines):
            ang_diff = _angle_diff_deg(edge_ang, line_angles[li])
            if ang_diff > _SNAP_ANGLE_THRESH_DEG:
                continue
            d = _point_line_perp_distance(mx, my, lx1, ly1, lx2, ly2)
            if d < best_dist:
                best_dist = d
                best_idx = li
        if best_idx >= 0:
            snap[i] = best_idx

    # ── Pass 2: resolve each vertex from its two adjacent snaps ─────────
    new_pts: list[tuple[float, float]] = []
    for i in range(n):
        p = poly_pct[i]
        s_prev = snap[(i - 1) % n]   # edge i-1 ends at vertex i
        s_curr = snap[i]             # edge i   starts at vertex i

        if s_prev is not None and s_curr is not None:
            a_prev = line_angles[s_prev]
            a_curr = line_angles[s_curr]
            if _angle_diff_deg(a_prev, a_curr) > _SNAP_PARALLEL_THRESH_DEG:
                lp = wall_lines[s_prev]
                lc = wall_lines[s_curr]
                inter = _line_line_intersection(
                    (lp[0], lp[1]), (lp[2], lp[3]),
                    (lc[0], lc[1]), (lc[2], lc[3]))
                if inter is not None:
                    new_pts.append((float(inter[0]), float(inter[1])))
                    continue
            # Near-parallel (same wall on either side of vertex): just
            # project onto one of them to keep the edge on the wall.
            lp = wall_lines[s_prev]
            new_pts.append(_project_point_on_line(
                p[0], p[1], lp[0], lp[1], lp[2], lp[3]))
            continue

        if s_prev is not None:
            lp = wall_lines[s_prev]
            new_pts.append(_project_point_on_line(
                p[0], p[1], lp[0], lp[1], lp[2], lp[3]))
            continue

        if s_curr is not None:
            lc = wall_lines[s_curr]
            new_pts.append(_project_point_on_line(
                p[0], p[1], lc[0], lc[1], lc[2], lc[3]))
            continue

        new_pts.append((float(p[0]), float(p[1])))

    # Clip to [0, 1] in case an intersection fell slightly outside the frame.
    clipped = [(max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))
               for (x, y) in new_pts]

    # Collapse any vertices that coincided at the same intersection point.
    clipped = _dedupe_and_straighten(clipped, tol=3e-3)
    if len(clipped) < 3:
        # Snap collapsed the polygon — revert to the original rather than
        # return garbage.
        return list(poly_pct)
    return clipped


# ── Raster-scan ordering + stable APT_N renaming ────────────────────────────
#
# Gemini returns apartments in whatever order its attention happens to pick;
# SAM refinement preserves that order. For reproducible runs and stable UI
# labels we re-sort top-to-bottom, left-to-right, with a small y-bin so
# apartments on the "same row" don't swap just because their top edges
# differ by a pixel (Raster2Seq §3.3 ordering trick).

_GENERIC_LABEL_RE = re.compile(r"^\s*APT[_\s\-]?\d+\s*$", re.IGNORECASE)


def _sort_regions_raster_scan(regions: list["DetectedRegion"],
                              bin_pct: float = _SORT_BIN_PCT,
                              ) -> list["DetectedRegion"]:
    """Return regions sorted top-to-bottom, left-to-right with stable APT_N.

    Sort key:
      1. y-bucket of ``bbox_pct.y1`` (bucket size = ``bin_pct`` of image).
      2. ``bbox_pct.x1`` within the bucket.

    Any region whose label matches the generic ``APT_<digits>`` pattern is
    renamed to ``APT_<N>`` where ``N`` is its 1-based position in the sorted
    order. Descriptive labels (e.g. ``"Type A"``, ``"Unit 3B"``) are kept.
    The pre-sort label is stashed in ``region.raw["_original_label"]``.
    """
    if not regions:
        return regions
    bin_size = max(1e-6, float(bin_pct))

    def _key(r: "DetectedRegion") -> tuple[int, float, float]:
        y1 = float(r.bbox_pct[1]) if r.bbox_pct else 0.0
        x1 = float(r.bbox_pct[0]) if r.bbox_pct else 0.0
        return (int(y1 / bin_size), x1, y1)

    ordered = sorted(regions, key=_key)
    for i, r in enumerate(ordered):
        if _GENERIC_LABEL_RE.match(r.label or ""):
            r.raw["_original_label"] = r.label
            r.label = f"APT_{i + 1}"
    return ordered


_MAX_EDGE_PX = 1600  # downsample longer edge to this before API call


def _downsample_png(png_bytes: bytes, max_edge: int = _MAX_EDGE_PX
                    ) -> tuple[bytes, int, int]:
    """Shrink a PNG so its longer edge is <= max_edge.

    Returns the re-encoded PNG bytes plus (w, h). If the image is already
    small enough, returns the original bytes unchanged.
    """
    try:
        from PIL import Image
    except ImportError:
        return png_bytes, 0, 0
    try:
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
    except Exception:
        return png_bytes, 0, 0
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= max_edge:
        return png_bytes, w, h
    scale = max_edge / float(long_edge)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    img = img.convert("RGB")
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), new_w, new_h


def _looks_repetition_looped(text: str) -> bool:
    """Cheap heuristic: did Gemini get stuck in a base64 repetition loop?

    Example failure mode: the model emits "2g/2g/2g/..." forever inside a
    mask string, eating the entire output-token budget.
    """
    if len(text) < 2000:
        return False
    # Grab a ~200-char window from the middle where masks usually start,
    # then check if a short (2-4 char) repeating unit explains >90% of it.
    mid = text[len(text) // 2: len(text) // 2 + 400]
    for unit_len in (2, 3, 4):
        if len(mid) < unit_len * 10:
            continue
        unit = mid[:unit_len]
        if not unit.strip():
            continue
        reps = mid.count(unit)
        if reps * unit_len > len(mid) * 0.9:
            return True
    return False


def _response_diagnostics(response: Any) -> str:
    """Best-effort, human-readable summary of a Gemini response."""
    parts: list[str] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        for i, cand in enumerate(candidates):
            fr = getattr(cand, "finish_reason", None)
            fm = getattr(cand, "finish_message", None)
            if fr is not None:
                parts.append(f"candidate[{i}].finish_reason={fr}")
            if fm:
                parts.append(f"candidate[{i}].finish_message={fm}")
    except Exception:
        pass
    try:
        pf = getattr(response, "prompt_feedback", None)
        if pf:
            br = getattr(pf, "block_reason", None)
            if br is not None:
                parts.append(f"prompt_feedback.block_reason={br}")
    except Exception:
        pass
    try:
        um = getattr(response, "usage_metadata", None)
        if um:
            for attr in ("prompt_token_count",
                         "candidates_token_count",
                         "thoughts_token_count",
                         "total_token_count"):
                v = getattr(um, attr, None)
                if v is not None:
                    parts.append(f"usage.{attr}={v}")
    except Exception:
        pass
    return "\n".join(parts) if parts else "(no diagnostic fields on response)"


_PROMPT = """You are an expert architectural plan analyzer.

The image is a floor plan of a residential building. Identify each
separately-outlined apartment (dwelling unit) visible in the plan. Ignore
hallways, elevators, stair cores, lobbies, and mechanical rooms.

For EACH apartment, trace its outer wall outline as a polygon. Apartments
are often L-shaped, T-shaped, or have notches around shared cores — do
NOT return a plain rectangle unless the apartment is truly rectangular.

Respond with ONLY a JSON array (no prose, no markdown fences). Each item
must have this exact shape:

  {
    "label": "<short human-readable name, e.g. 'APT-1', 'Type A', 'Unit 3B'>",
    "box_2d": [ymin, xmin, ymax, xmax],
    "polygon_2d": [[y1, x1], [y2, x2], [y3, x3], ...]
  }

Rules:
- box_2d values are integers in 0..1000 (image-normalized; 0 = top/left,
  1000 = bottom/right).
- polygon_2d is an ordered list of 4 to 24 points tracing the apartment's
  perimeter clockwise. Each point is [y, x] in the same 0..1000 space.
- Put a vertex at every real corner of the apartment (including concave /
  notched corners); do NOT oversample straight edges.
- DO NOT include segmentation masks, base64 PNGs, or any binary data.

If there are no apartments visible, return an empty array: [].
"""

_PROMPT_BBOX_ONLY = """You are an expert architectural plan analyzer.

Identify each separately-outlined apartment (dwelling unit) in this
residential floor plan. Ignore hallways, elevators, stair cores, lobbies,
and mechanical rooms.

Respond with ONLY a JSON array. Each item:
  { "label": "<short name>", "box_2d": [ymin, xmin, ymax, xmax] }
where coordinates are integers in 0..1000 (image-normalized).

Return [] if no apartments are visible.
"""


def _log_footer() -> str:
    return f"\n\nFull diagnostic log: {LOG_PATH}"


def analyze_image(png_bytes: bytes, img_w: int, img_h: int,
                  api_key: str,
                  *,
                  model: str = DEFAULT_MODEL,
                  progress_cb=None,
                  sam_config: "Any | None" = None,
                  ) -> list[DetectedRegion]:
    """Send a floor-plan PNG to Google Gemini and return detected regions.

    If `sam_config` is a `sam_refiner.SamConfig` with `.ok == True`, each
    Gemini bbox is further refined into a pixel-accurate polygon via Meta's
    Segment Anything Model running in the user's ComfyUI Python. When SAM
    succeeds, the refined polygon REPLACES Gemini's rougher mask/bbox result;
    when it fails or is disabled, the original Gemini polygon is kept.
    """
    t_start = time.monotonic()
    log.info("-" * 60)
    log.info("analyze_image called: img=%dx%d, png=%d bytes, model=%s",
             img_w, img_h, len(png_bytes or b""), model)
    key = (api_key or "").strip()
    log.info("api_key present=%s len=%d starts_with_AIza=%s",
             bool(key), len(key), key.startswith("AIza"))
    try:
        _validate_google_key(key)
    except Exception as exc:
        log.error("API key validation failed: %s", exc)
        raise

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        log.exception("google-genai import failed")
        raise RuntimeError(
            "google-genai is not installed. Run:  pip install google-genai"
            + _log_footer()
        ) from e

    # Downsample large scans before sending. Floor-plan renders at 150 DPI
    # easily hit 3000+px on a side, which (a) bloats prompt tokens and
    # (b) makes Gemini 2.5 Flash more likely to degenerate into repetition
    # loops on high-frequency line art.
    sent_png = png_bytes
    sent_w, sent_h = img_w, img_h
    if max(img_w, img_h) > _MAX_EDGE_PX:
        ds_png, ds_w, ds_h = _downsample_png(png_bytes, _MAX_EDGE_PX)
        if ds_w and ds_h:
            log.info("Downsampled %dx%d -> %dx%d (%d bytes -> %d bytes)",
                     img_w, img_h, ds_w, ds_h, len(png_bytes), len(ds_png))
            sent_png, sent_w, sent_h = ds_png, ds_w, ds_h
        else:
            log.warning("Downsample failed; sending original image")

    if progress_cb:
        progress_cb(f"Calling Google Gemini ({model})…")
    log.info("Creating genai.Client")

    try:
        client = genai.Client(api_key=key)
    except Exception as exc:
        log.exception("Client init failed")
        raise RuntimeError(
            f"Failed to initialize Gemini client: {exc}" + _log_footer()
        ) from exc

    def _build_config() -> "types.GenerateContentConfig":
        kwargs: dict[str, Any] = dict(
            response_mime_type="application/json",
            temperature=0.1,
            max_output_tokens=32768,
        )
        try:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
        return types.GenerateContentConfig(**kwargs)

    def _call_api(prompt: str, attempt: str) -> Any:
        log.info("API attempt '%s' — prompt length=%d chars", attempt,
                 len(prompt))
        t_api = time.monotonic()
        try:
            r = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(
                        data=sent_png, mime_type="image/png"),
                    prompt,
                ],
                config=_build_config(),
            )
        except Exception as exc:
            api_ms = int((time.monotonic() - t_api) * 1000)
            log.error("API call '%s' raised after %d ms: %s",
                      attempt, api_ms, exc)
            log.error("Traceback:\n%s", traceback.format_exc())
            msg = str(exc)
            if "401" in msg or "403" in msg or "API_KEY" in msg.upper():
                raise RuntimeError(
                    "Google Gemini rejected the API key.\n\n"
                    "Double-check:\n"
                    "  - You copied the full key from "
                    "https://aistudio.google.com/apikey\n"
                    "  - The key is enabled (not revoked)\n"
                    "  - The Generative Language API is enabled on that "
                    "project\n\n"
                    f"Raw error: {msg}" + _log_footer()
                ) from exc
            if "429" in msg or "quota" in msg.lower():
                raise RuntimeError(
                    "Google Gemini quota exceeded (429). Wait a minute "
                    "and try again, or check your usage at "
                    "https://aistudio.google.com/" + _log_footer()
                ) from exc
            raise RuntimeError(
                f"Gemini API call failed: {msg}" + _log_footer()) from exc

        api_ms = int((time.monotonic() - t_api) * 1000)
        log.info("API call '%s' returned in %d ms", attempt, api_ms)
        log.info("Response diagnostics:\n%s", _response_diagnostics(r))
        return r

    def _response_text(r: Any) -> str:
        t = getattr(r, "text", "") or ""
        log.info("response.text length = %d", len(t))
        if t:
            log.debug("response.text first 2000 chars:\n%s", t[:2000])
            if len(t) > 2000:
                log.debug("response.text last 1000 chars:\n%s", t[-1000:])
        return t

    # ── First pass: full polygons ────────────────────────────────────────
    response = _call_api(_PROMPT, "polygon")
    text = _response_text(response)

    def _finish_reason(r: Any) -> str:
        try:
            cands = getattr(r, "candidates", None) or []
            if cands:
                return str(getattr(cands[0], "finish_reason", "")).upper()
        except Exception:
            pass
        return ""

    need_retry = False
    retry_reason = ""
    if not text.strip():
        need_retry = True
        retry_reason = "empty response"
    elif "MAX_TOKENS" in _finish_reason(response):
        need_retry = True
        retry_reason = "MAX_TOKENS"
    elif _looks_repetition_looped(text):
        need_retry = True
        retry_reason = "repetition loop detected"
        log.warning("Repetition-loop detected in first-pass response")

    data = None if need_retry else _extract_json(text)
    if data is None and not need_retry:
        need_retry = True
        retry_reason = "JSON parse failed"

    # ── Second pass: bbox-only ───────────────────────────────────────────
    if need_retry:
        log.warning("Retrying with bbox-only prompt (reason: %s)",
                    retry_reason)
        if progress_cb:
            progress_cb(
                f"First pass had problems ({retry_reason}); "
                "retrying with simpler prompt…")
        response = _call_api(_PROMPT_BBOX_ONLY, "bbox-only")
        text = _response_text(response)

        if not text.strip():
            diag = _response_diagnostics(response)
            log.error("Empty response on bbox-only retry. Diagnostics:\n%s",
                      diag)
            raise RuntimeError(
                "Gemini returned no text even on the simplified bbox-only "
                "retry.\n\n"
                f"{diag}\n\n"
                "Try a lower DPI, crop to a single floor, or switch model "
                "to gemini-2.5-pro." + _log_footer())

        data = _extract_json(text)
        if data is None:
            diag = _response_diagnostics(response)
            preview = text[:400].replace("\n", " ")
            log.error("JSON parse failed on bbox-only retry.\n%s", diag)
            log.error("First 2000 chars of retry response:\n%s", text[:2000])
            raise RuntimeError(
                "Gemini response was not parseable JSON on either pass.\n\n"
                f"{diag}\n\n"
                f"First 400 chars of response:\n{preview}" + _log_footer())

    log.info("Parsed JSON: top-level type=%s", type(data).__name__)

    # Accept either a bare array or {"apartments":[...]} / {"regions":[...]}
    if isinstance(data, dict):
        for key_name in ("apartments", "regions", "items", "objects", "results"):
            if key_name in data and isinstance(data[key_name], list):
                data = data[key_name]
                break

    if not isinstance(data, list):
        return []

    regions: list[DetectedRegion] = []
    n_mask_polys = 0
    n_bbox_fallbacks = 0
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name")
                    or f"APT_{i + 1}").strip() or f"APT_{i + 1}"

        bbox_pct = _normalize_bbox(
            item.get("box_2d") or item.get("bbox_2d")
            or item.get("bbox") or item.get("box"),
            img_w, img_h)

        # Preferred path: mask PNG → traced contour → polygon
        poly_pct = None
        mask_field = item.get("mask") or item.get("segmentation")
        if mask_field and bbox_pct is not None:
            poly_pct = _mask_to_polygon(mask_field, bbox_pct, img_w, img_h)
            if poly_pct is not None:
                n_mask_polys += 1

        # Fallback 1: explicit polygon field (if the model returned one)
        if poly_pct is None:
            poly_pct = _normalize_polygon(
                item.get("polygon") or item.get("polygon_2d"), img_w, img_h)

        # Fallback 2: derive a rectangle from the bounding box
        if poly_pct is None:
            if bbox_pct is None:
                continue
            x1, y1, x2, y2 = bbox_pct
            poly_pct = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            n_bbox_fallbacks += 1

        if bbox_pct is None:
            xs = [p[0] for p in poly_pct]
            ys = [p[1] for p in poly_pct]
            bbox_pct = (min(xs), min(ys), max(xs), max(ys))

        regions.append(DetectedRegion(
            label=label,
            bbox_pct=bbox_pct,
            polygon_pct=poly_pct,
            raw=item,
        ))

    # ── Optional: refine Gemini bboxes with SAM ─────────────────────────
    # Gemini is great at KNOWING which region is an apartment, but its mask
    # output is coarse and often stair-steps. SAM, given a tight bbox as a
    # prompt, produces a pixel-exact segmentation. We run SAM ONCE over the
    # same (possibly downsampled) image we sent to Gemini, batching all
    # bboxes in a single subprocess call so SAM's expensive ViT image
    # encoding amortises across regions.
    #
    # IMPORTANT: we always preserve BOTH the original Gemini polygon and
    # the SAM polygon on the region (when SAM ran), and only PROMOTE the
    # SAM polygon to `polygon_pct` if it passes a quality gate. This way
    # the UI can offer a per-region "Use Gemini vs SAM" toggle and the
    # user never loses the Gemini result if SAM produced a blob.
    n_sam_refined = 0      # SAM polygon accepted (promoted to polygon_pct)
    n_sam_rejected = 0     # SAM ran but quality gate rejected its polygon
    n_sam_failed = 0       # SAM returned no mask / polygon trace failed
    if sam_config is not None and regions:
        try:
            ok = bool(getattr(sam_config, "ok", False))
        except Exception:
            ok = False
        if ok:
            try:
                from . import sam_refiner  # local import to keep core import-light
            except Exception as _imp_exc:
                log.warning("sam_refiner import failed: %r", _imp_exc)
                sam_refiner = None  # type: ignore[assignment]
            if sam_refiner is not None:
                bboxes = [r.bbox_pct for r in regions]
                try:
                    masks_b64, diag = sam_refiner.refine_bboxes(
                        sent_png, sent_w, sent_h, bboxes, sam_config,
                        progress_cb=progress_cb)
                except Exception as exc:
                    log.exception("SAM refinement raised")
                    masks_b64, diag = (
                        [None] * len(regions),
                        {"ok": False, "error": repr(exc)})

                if not diag.get("ok"):
                    log.warning("SAM refinement disabled for this run: %s",
                                diag.get("error"))
                    if progress_cb:
                        progress_cb(
                            "SAM refinement skipped — see AI log for details.")

                picks = diag.get("picks") or [None] * len(regions)
                for region, mb64, pick in zip(regions, masks_b64, picks):
                    # Record that SAM at least attempted this region.
                    region.raw["_sam_attempted"] = bool(mb64)
                    region.raw["_polygon_pct_gemini"] = list(region.polygon_pct)
                    if pick:
                        region.raw["_sam_pick"] = pick

                    if not mb64:
                        n_sam_failed += 1
                        region.raw["_source"] = "gemini"
                        continue
                    sam_poly = _mask_to_polygon(
                        mb64, region.bbox_pct, sent_w, sent_h,
                        architectural=True)
                    if not sam_poly or len(sam_poly) < 3:
                        n_sam_failed += 1
                        region.raw["_source"] = "gemini"
                        continue

                    # Quality gate: reject SAM polygons whose area is far
                    # outside the Gemini bbox area. A "blob" mask that
                    # covers the whole bbox comes out ~1.0; a healthy
                    # apartment ranges ~0.5–0.95 of bbox; a sliver/noise
                    # mask comes out <0.2. The SAM-side pick already filters
                    # out the worst of these but the polygon trace +
                    # simplification can also degrade mediocre masks further.
                    bbox_area = _poly_bbox_area(region.bbox_pct)
                    sam_area  = _polygon_area_pct(sam_poly)
                    area_frac = sam_area / bbox_area if bbox_area > 0 else 0.0
                    region.raw["_sam_area_frac"] = round(area_frac, 3)
                    region.raw["_polygon_pct_sam"] = sam_poly

                    # Accept if 25 % ≤ area ≤ 105 % of bbox (5 % slack
                    # above 100 % because the mask can spill over bbox
                    # edges by a pixel or two on downsampled images).
                    if 0.25 <= area_frac <= 1.05:
                        region.polygon_pct = sam_poly
                        region.raw["_source"] = "sam"
                        n_sam_refined += 1
                    else:
                        region.raw["_source"] = "gemini"
                        n_sam_rejected += 1

                log.info("SAM refinement: %d accepted, %d rejected by gate, "
                         "%d no-mask (of %d regions) — elapsed %d ms on %s; "
                         "first-5 picks=%s",
                         n_sam_refined, n_sam_rejected, n_sam_failed,
                         len(regions),
                         int(diag.get("elapsed_ms") or 0),
                         diag.get("device") or "?",
                         picks[:5])

    # ── Hough wall-snap ─────────────────────────────────────────────────
    # Runs ONCE on the downsampled image (the same `sent_png` Gemini / SAM
    # saw) and snaps every region's current polygon onto the detected wall
    # lines. This subsumes the old per-polygon H/V orthogonalize pass and
    # correctly handles non-axis-aligned walls. See `_detect_wall_lines`
    # and `_snap_polygon_to_walls` for details.
    n_snapped = 0
    n_wall_lines = 0
    if regions:
        if progress_cb:
            progress_cb("Detecting wall lines for polygon snap…")
        try:
            wall_lines = _detect_wall_lines(sent_png, sent_w, sent_h)
        except Exception:
            log.exception("Hough wall detection raised; skipping snap")
            wall_lines = []
        n_wall_lines = len(wall_lines)
        if wall_lines:
            if progress_cb:
                progress_cb(
                    f"Snapping {len(regions)} polygon(s) to "
                    f"{n_wall_lines} wall line(s)…")
            S_px = max(1, min(sent_w, sent_h))
            for region in regions:
                try:
                    region.raw["_polygon_pct_pre_snap"] = list(
                        region.polygon_pct)
                    snapped = _snap_polygon_to_walls(
                        region.polygon_pct, wall_lines, S_px)
                    if snapped and len(snapped) >= 3 \
                            and snapped != region.polygon_pct:
                        region.polygon_pct = snapped
                        region.raw["_wall_snapped"] = True
                        n_snapped += 1
                    else:
                        region.raw["_wall_snapped"] = False
                except Exception:
                    log.exception("wall-snap failed for region %r",
                                  getattr(region, "label", "?"))
                    region.raw["_wall_snapped"] = False
            log.info("Hough wall-snap: %d lines clustered, %d/%d regions "
                     "had edges moved", n_wall_lines, n_snapped, len(regions))

    # ── Deterministic ordering + stable APT_N labels ────────────────────
    # Raster2Seq §3.3: rooms sorted by top-left y then x gives a stable,
    # reproducible enumeration. Generic ``APT_<digits>`` labels get
    # renumbered to match the sorted order; human-readable labels survive.
    regions = _sort_regions_raster_scan(regions)

    total_ms = int((time.monotonic() - t_start) * 1000)
    log.info(
        "analyze_image done in %d ms: %d region(s), %d mask-traced, "
        "%d bbox-fallback, %d sam-accepted, %d sam-rejected, "
        "%d wall-lines, %d wall-snapped",
        total_ms, len(regions), n_mask_polys, n_bbox_fallbacks,
        n_sam_refined, n_sam_rejected, n_wall_lines, n_snapped)

    if progress_cb and regions:
        sam_bits: list[str] = []
        if n_sam_refined:
            sam_bits.append(f"{n_sam_refined} SAM-refined")
        if n_sam_rejected:
            sam_bits.append(f"{n_sam_rejected} SAM rejected")
        if n_snapped:
            sam_bits.append(f"{n_snapped} wall-snapped")
        extra = (", " + ", ".join(sam_bits)) if sam_bits else ""
        progress_cb(
            f"Got {len(regions)} region(s): "
            f"{n_mask_polys} traced from masks, "
            f"{n_bbox_fallbacks} as bbox rectangles"
            f"{extra}."
        )
    return regions


def analyze_floor_plan(source_path: str, page_index: int, api_key: str,
                       *, dpi: int = DEFAULT_DPI, model: str = DEFAULT_MODEL,
                       progress_cb=None,
                       sam_config: "Any | None" = None,
                       ) -> AnalyzeResult:
    """Top-level helper: PDF/image path → rendered PNG + detected regions."""
    log.info("=" * 60)
    log.info(
        "analyze_floor_plan called: source=%s page=%d dpi=%d model=%s sam=%s",
        source_path, page_index, dpi, model,
        bool(sam_config and getattr(sam_config, "ok", False)))
    try:
        ext = os.path.splitext(source_path)[1].lower()
        if ext == ".pdf":
            png, w, h = render_pdf_page(source_path, page_index, dpi=dpi)
            log.info("PDF rendered: %dx%d, %d bytes PNG", w, h, len(png))
        else:
            png, w, h = load_image_as_png(source_path)
            log.info("Image loaded: %dx%d, %d bytes PNG", w, h, len(png))
        regions = analyze_image(
            png, w, h, api_key, model=model, progress_cb=progress_cb,
            sam_config=sam_config)
        log.info("analyze_floor_plan OK: %d region(s)", len(regions))
        return AnalyzeResult(
            page_image_png=png, page_w=w, page_h=h, regions=regions)
    except Exception:
        log.exception("analyze_floor_plan raised")
        raise
