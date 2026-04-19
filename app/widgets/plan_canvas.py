"""
Plan Canvas — interactive floor plan calibration widget.

Toolbar modes
─────────────
  ↖ Select      – click a polygon to select it (Delete/Backspace to remove)
  📏 Scale      – drag an arrow → enter its real-world length in metres
  🧭 North      – drag an arrow to indicate true north
  🏢 Entrance   – click polygon vertices → close near first point → dialog
                  (building ID + entrance ID) → click centre point
  🏠 Apt Type   – draw apartment type footprint polygon
  ✏  Edit Verts – click a polygon to select it, then drag vertices to edit
"""
from __future__ import annotations

import json
import math
import os
import uuid as _uuid_mod

from PyQt6.QtCore import Qt, QPointF, QRectF, QLineF, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPen, QPixmap, QPolygonF,
    QPainterPath, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGraphicsEllipseItem, QGraphicsItem, QGraphicsScene, QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout, QInputDialog, QLabel,
    QMainWindow, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)
from PyQt6.QtGui import QAction

from app.widgets.georef_dialog import (
    Correspondence as _GeorefPair,
    GeorefPanel as _GeorefPanel,
    UELocationDialog as _UELocationDialog,
)

# ── Snap / alignment threshold (screen pixels) ─────────────────────────────
_SNAP_SCREEN_PX = 7.0

# ── Palette ────────────────────────────────────────────────────────────────
_COLORS = [
    "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#cc5de8", "#74c0fc", "#f06595",
    "#a9e34b", "#63e6be",
]


def _migrate_balcony_cams(p: dict) -> list[dict]:
    """Return p['balcony_cams'] as a list, migrating legacy single 'balcony_cam'."""
    cams = p.get("balcony_cams")
    if isinstance(cams, list):
        return cams
    legacy = p.get("balcony_cam")
    if isinstance(legacy, dict):
        return [legacy]
    return []


class _OutlinedTextItem(QGraphicsTextItem):
    """QGraphicsTextItem that renders white text with a black outline stroke."""

    def paint(self, painter, option, widget=None):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = self.font()
        fm = QFontMetrics(font)
        margin = self.document().documentMargin()
        path = QPainterPath()
        for i, line in enumerate(self.toPlainText().split('\n')):
            y = margin + fm.ascent() + i * fm.lineSpacing()
            if line:
                path.addText(margin, y, font, line)
        pen = QPen(QColor(0, 0, 0), 2.5,
                   Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(path, pen)
        painter.fillPath(path, QBrush(QColor(255, 255, 255)))
        painter.restore()


# ── Layers Panel ─────────────────────────────────────────────────────────────
class _LayersPanel(QWidget):
    """Right-side panel showing toggleable visibility for entrances and apt types."""

    visibility_changed = pyqtSignal(str, int, bool)  # kind, idx, visible

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        title = QLabel("Layers")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        # Select All / Deselect All buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._btn_all  = QPushButton("All")
        self._btn_none = QPushButton("None")
        for btn in (self._btn_all, self._btn_none):
            btn.setFixedHeight(20)
            btn.setStyleSheet("font-size:11px; padding:0 4px;")
        self._btn_all .clicked.connect(lambda: self._set_all(True))
        self._btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(self._btn_all)
        btn_row.addWidget(self._btn_none)
        layout.addLayout(btn_row)

        self._all_checkboxes: list[QCheckBox] = []

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self._scroll)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(2)
        self._content_layout.addStretch()
        self._scroll.setWidget(self._content)

    def _set_all(self, checked: bool):
        """Check or uncheck every checkbox without firing individual signals."""
        for cb in self._all_checkboxes:
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
        # Emit a single bulk visibility signal for each checkbox
        for cb in self._all_checkboxes:
            cb.stateChanged.emit(Qt.CheckState.Checked.value if checked
                                 else Qt.CheckState.Unchecked.value)

    def rebuild(self, entrances, apt_type_polygons, type_color_fn, visibility_fn=None):
        """Rebuild all checkbox rows from current data.

        visibility_fn(kind, idx) → bool  — if supplied, pre-sets each checkbox
        to match the saved visibility state so toggled-off layers stay off.
        """
        self._all_checkboxes.clear()
        # Remove all widgets from content layout except the trailing stretch
        while self._content_layout.count() > 1:
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Entrances section
        if entrances:
            ent_lbl = QLabel("Entrances")
            ent_lbl.setStyleSheet("color:#aaa; font-size:10px;")
            self._content_layout.insertWidget(self._content_layout.count() - 1, ent_lbl)
            for i, ent in enumerate(entrances):
                cb = QCheckBox(f"{ent['building_id']}/{ent['entrance_id']}")
                is_vis = visibility_fn("entrance", i) if visibility_fn else True
                cb.blockSignals(True)
                cb.setChecked(is_vis)
                cb.blockSignals(False)
                cb.stateChanged.connect(
                    lambda state, kind="entrance", idx=i:
                        self.visibility_changed.emit(kind, idx, state == Qt.CheckState.Checked.value)
                )
                self._all_checkboxes.append(cb)
                self._content_layout.insertWidget(self._content_layout.count() - 1, cb)

        # Apt Types section
        if apt_type_polygons:
            apt_lbl = QLabel("Apt Types")
            apt_lbl.setStyleSheet("color:#aaa; font-size:10px;")
            self._content_layout.insertWidget(self._content_layout.count() - 1, apt_lbl)
            for i, p in enumerate(apt_type_polygons):
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(4)

                color = type_color_fn(p['type_name'])
                color_swatch = QLabel()
                color_swatch.setFixedSize(12, 12)
                color_swatch.setStyleSheet(
                    f"background-color: {color.name()}; border: 1px solid #555;")
                row_layout.addWidget(color_swatch)

                label_text = f"{p['type_name']} {p['building_id']}/E{p['entrance_id']}"
                cb = QCheckBox(label_text)
                is_vis = visibility_fn("apt_type", i) if visibility_fn else True
                cb.blockSignals(True)
                cb.setChecked(is_vis)
                cb.blockSignals(False)
                cb.stateChanged.connect(
                    lambda state, kind="apt_type", idx=i:
                        self.visibility_changed.emit(kind, idx, state == Qt.CheckState.Checked.value)
                )
                self._all_checkboxes.append(cb)
                row_layout.addWidget(cb)
                row_layout.addStretch()

                self._content_layout.insertWidget(self._content_layout.count() - 1, row_widget)


class PlanCanvas(QWidget):
    """Interactive floor plan calibration canvas."""

    calibration_changed  = pyqtSignal(dict)
    auto_place_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # ── State ──────────────────────────────────────────────────────────
        self.image_path: str | None = None
        self.scale_px_per_m: float | None = None
        self.north_angle_deg: float = 0.0
        self.entrances: list[dict] = []
        self.building_ids: list[str] = []
        self.apt_type_polygons: list[dict] = []
        self.apt_types_list: list[str] = []
        self.entrance_ids_all: list[str] = []
        self._pending_apt_type: dict | None = None

        self._cal_path: str | None = None
        self._mode = "select"
        self._arrow_start: QPointF | None = None
        self._arrow_preview = None
        self._poly_points: list[QPointF] = []
        self._poly_preview_items: list = []
        self._selected_idx: int | None = None
        self._selected_type: str | None = None
        # Multi-selection: list of (kind, idx) — Ctrl+click to toggle members.
        # When non-empty, the LAST entry mirrors _selected_idx/_selected_type
        # so single-target operations (commit, delete, dialog) still target
        # the most-recently-clicked polygon.
        self._multi_selection: list[tuple[str, int]] = []
        self._selection_items: list = []   # cyan dashed outlines per selected
        # Background pixmap items.
        # _bg_items holds one item per PDF page (or a single item for an
        # image file). _bg_item aliases the first item and exists so that
        # older code paths still work; _bg_size_px stores the total stacked
        # (width, height) so AI-Import coord scaling stays correct.
        self._bg_item = None
        self._bg_items: list = []
        self._bg_size_px: tuple[int, int] = (0, 0)

        # ── Per-type color map (Feature 1) ─────────────────────────────────
        self._type_color_map: dict[str, int] = {}

        # ── Per-polygon scene item tracking (Feature 2) ────────────────────
        self._entrance_items: list[list] = []
        self._apt_type_items: list[list] = []

        # ── Vertex edit state (Feature 3) ──────────────────────────────────
        self._edit_target: tuple | None = None
        self._edit_handles: list = []
        self._drag_handle_idx: int | None = None
        self._drag_handle_origin: QPointF | None = None  # anchor for Shift axis-lock
        self._edit_live_poly = None   # live-updating polygon outline during drag

        # ── Move polygon state (supports multi-poly group drag) ─────────────
        # _move_targets is a list of (kind, idx); length 1 = classic single
        # polygon drag, length >1 = group drag of all multi-selected polygons.
        self._move_targets: list[tuple[str, int]] = []
        self._move_drag_start: QPointF | None = None
        self._move_orig_pts_list: list[list] = []   # original polygon_img per target
        self._move_orig_centers: list[tuple] = []   # original center_img per target
        self._move_orig_cams_list: list[list] = []  # [[(img_x,img_y), ...], ...]

        # ── Balcony cam two-click state ─────────────────────────────────────
        self._cam_pending_idx: int | None = None      # apt_type idx awaiting yaw click
        self._cam_pending_pos: QPointF | None = None  # position of first click (scene coords)
        self._cam_yaw_preview = None                  # live arrow preview item

        # ── Transform mode state ────────────────────────────────────────────
        self._xform_target: tuple | None = None
        self._xform_handles: list = []
        self._xform_drag: str | None = None      # 'tl','tr','bl','br','rot'
        self._xform_drag_start: QPointF | None = None
        self._xform_orig_pts: list | None = None
        self._xform_orig_center: tuple | None = None
        self._xform_bbox: tuple | None = None    # (minx, miny, maxx, maxy)

        # ── Alignment guide lines ───────────────────────────────────────────
        self._align_guides: list = []

        # ── Layer visibility state (persists across redraws) ────────────────
        # Keys: ("E", building_id, entrance_id) or ("A", building_id, entrance_id, type_name)
        self._visibility_state: dict[tuple, bool] = {}

        # ── Georef state ─────────────────────────────────────────────────────
        # While _mode == "georef", left-clicks on the canvas pop a UE-location
        # dialog and add a correspondence (plan pixel ↔ UE cm). The non-modal
        # _georef_panel floats beside the main window with the table + Compute
        # + Apply controls. Pins live on the scene so they visually track the
        # image under pan/zoom.
        self._georef_pairs: list[_GeorefPair] = []
        self._georef_panel: _GeorefPanel | None = None
        self._georef_pin_items: list = []

        self._build_ui()

    # ── Feature 1: Per-type color ───────────────────────────────────────────
    def _get_type_color(self, type_name: str) -> QColor:
        if type_name not in self._type_color_map:
            self._type_color_map[type_name] = len(self._type_color_map)
        idx = self._type_color_map[type_name]
        return QColor(_COLORS[idx % len(_COLORS)])

    # ── UI ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        tb = QToolBar()
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { spacing: 4px; }")

        def _act(icon, slot, tip="", checkable=False):
            a = QAction(icon, self)
            a.setToolTip(tip)
            a.setCheckable(checkable)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        _act("📂  Load Image",      self._load_image,      "Load a floor plan image")
        _act("💾  Save",            self._save_calibration, "Save calibration as JSON")
        _act("📁  Load",            self._load_calibration, "Load a saved calibration")
        tb.addSeparator()

        self._mode_actions: dict[str, QAction] = {}
        for mode, icon, tip in [
            ("select",       "↖  Select",      "Click a polygon to select it. Ctrl+Click to multi-select. Ctrl+A selects every apt polygon. Delete to remove."),
            ("move",         "✥  Move",        "Click a polygon then drag it. Ctrl+Click adds polygons; drag any selected polygon to move the whole group."),
            ("transform",    "⟳  Transform",   "Click a polygon to show rotate/scale handles"),
            ("scale",        "📏  Scale",       "Drag to draw a reference line, then enter its real length"),
            ("apt_type",     "🏠  Apt Type",    "Click vertices around one apartment's footprint. Close near first vertex or double-click."),
            ("vertex_edit",  "✏  Edit Verts",  "Click polygon → drag yellow handles  |  Ctrl+Click edge → add vertex  |  Alt+Click vertex → delete"),
            ("balcony_cam",  "📷  Balcony Cam", "Two-click: position then aim. Click on existing cam to remove. Multiple cams per polygon supported."),
        ]:
            a = _act(icon, lambda _, m=mode: self._set_mode(m),
                     tip, checkable=True)
            self._mode_actions[mode] = a

        self._mode_actions["select"].setChecked(True)
        tb.addSeparator()
        _act("↩  Undo",    self._undo_last, "Undo last polygon  (Ctrl+Z)")
        _act("🗑  Clear All", self._clear_all, "Remove all calibration data")
        tb.addSeparator()
        _act("↕  Set Height", self._bulk_set_height,
             "Bulk-set extrusion height for the selected apt-type polygons. "
             "Optional: apply to every polygon of the same type. "
             "Tip: Ctrl+A selects every apt polygon first.  (H)")
        _act("✓  Commit", self._commit_selected,
             "Recalculate camera parameters from polygon's current state  (Ctrl+S)")
        _act("✓✓  Commit All", self._commit_all,
             "Recalculate world coords for every apt-type polygon (uses current scale)")
        tb.addSeparator()
        a_georef = _act(
            "📍  Georef",
            lambda: self._set_mode("georef"),
            "Georef calibration: click landmarks on the plan and paste the "
            "UE Location for each one. Tool back-solves the correct "
            "scale + rotation from the correspondences, then Apply re-stamps "
            "all committed polygons with the corrected calibration.",
            checkable=True,
        )
        self._mode_actions["georef"] = a_georef
        tb.addSeparator()
        _act("⚡  Auto-Place", lambda: self.auto_place_requested.emit(),
             "Auto-generate bounding-box polygons from your data (no floor plan needed)")

        # Status label (right-aligned)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self._status_lbl = QLabel("Load a floor plan image to begin.")
        self._status_lbl.setStyleSheet("color:#aaa; padding:0 8px;")
        tb.addWidget(self._status_lbl)

        layout.addWidget(tb)

        # Splitter: canvas left, layers panel right
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._scene = QGraphicsScene()
        self._view = _PlanView(self._scene, self)
        self._view.sig_press.connect(self._on_press)
        self._view.sig_ctrl_press.connect(self._on_ctrl_press)
        self._view.sig_alt_press.connect(self._on_alt_press)
        self._view.sig_move.connect(self._on_move)
        self._view.sig_release.connect(self._on_release)
        self._view.sig_double.connect(self._on_double)
        self._view.sig_delete.connect(self._delete_selected)
        self._view.sig_undo.connect(self._undo_last)
        self._view.sig_commit.connect(self._commit_selected)
        self._view.sig_escape.connect(self._on_escape)
        self._view.sig_set_height.connect(self._bulk_set_height)
        self._view.sig_select_all.connect(self._select_all_apt)
        splitter.addWidget(self._view)

        self._layers_panel = _LayersPanel()
        self._layers_panel.visibility_changed.connect(self._on_visibility_changed)
        splitter.addWidget(self._layers_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        layout.addWidget(splitter)

    # ── Mode ───────────────────────────────────────────────────────────────
    def _set_mode(self, mode: str):
        # Leaving vertex_edit: clear handles
        if self._mode == "vertex_edit" and mode != "vertex_edit":
            self._clear_edit_handles()
        # Leaving transform: clear handles
        if self._mode == "transform" and mode != "transform":
            self._clear_transform_handles()
        # Leaving move: reset state
        if self._mode == "move" and mode != "move":
            self._move_targets = []
            self._move_drag_start = None
            self._move_orig_pts_list = []
            self._move_orig_centers = []
            self._move_orig_cams_list = []
        # Leaving balcony_cam: cancel pending two-click placement
        if self._mode == "balcony_cam" and mode != "balcony_cam":
            self._cancel_cam_pending()
        # Entering / leaving georef: open or close the floating panel + pins
        if mode == "georef" and self._mode != "georef":
            self._open_georef_panel()
        if self._mode == "georef" and mode != "georef":
            self._close_georef_panel()
        self._mode = mode
        for m, a in self._mode_actions.items():
            a.setChecked(m == mode)
        self._cancel_drawing()
        cursors = {
            "select":      Qt.CursorShape.ArrowCursor,
            "scale":       Qt.CursorShape.CrossCursor,
            "apt_type":    Qt.CursorShape.CrossCursor,
            "vertex_edit": Qt.CursorShape.PointingHandCursor,
            "georef":      Qt.CursorShape.CrossCursor,
        }
        self._view.setCursor(cursors.get(mode, Qt.CursorShape.ArrowCursor))
        hints = {
            "select":      "Click a polygon to select it.  Ctrl+Click to multi-select.  Delete removes all selected.",
            "move":        "Click + drag a polygon to move it.  With multiple polygons selected (Ctrl+Click), drag any one to move the whole group.",
            "scale":       "Click + drag a reference line, then type its real length (m).",
            "apt_type":    "Click vertices around one apartment's footprint. Close near first vertex or double-click.",
            "vertex_edit": "Click polygon → drag yellow handles to reshape  |  Ctrl+Click edge → add vertex  |  Alt+Click vertex → delete",
            "georef":      "Click a real-world landmark on the plan, then paste its UE Location in the popup.  Add 2+ landmarks and press Compute → Apply in the panel.",
        }
        self._set_status(hints.get(mode, ""))

    def _cancel_drawing(self):
        if self._arrow_preview:
            self._scene.removeItem(self._arrow_preview)
            self._arrow_preview = None
        self._arrow_start = None
        for item in self._poly_preview_items:
            self._scene.removeItem(item)
        self._poly_preview_items.clear()
        self._poly_points.clear()
        self._pending_apt_type = None
        self._clear_align_guides()
        self._clear_edit_handles()

    # ── Image ──────────────────────────────────────────────────────────────
    def _load_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Floor Plan",
            filter=("Floor plans (*.pdf *.png *.jpg *.jpeg *.bmp *.tif "
                    "*.tiff);;All (*)"))
        if path:
            self.image_path = path
            self._render_image()
            self._set_status(f"Loaded: {os.path.basename(path)}")

    def _render_image(self):
        if not self.image_path:
            return
        self._scene.clear()
        self._bg_item = None
        self._bg_items = []
        self._bg_size_px = (0, 0)

        ext = os.path.splitext(self.image_path)[1].lower()
        try:
            if ext == ".pdf":
                ok = self._render_pdf_pages(self.image_path)
            else:
                ok = self._render_single_image(self.image_path)
        except Exception as exc:
            QMessageBox.warning(self, "Error",
                                f"Cannot load plan:\n{self.image_path}\n\n{exc}")
            return
        if not ok:
            return

        w, h = self._bg_size_px
        # Pad by 3x plan size on every side so zoom-in never traps the view.
        self._scene.setSceneRect(QRectF(-3 * w, -3 * h, 7 * w, 7 * h))
        # Fit to the plan bounds, not the full padded scene rect,
        # so the user's initial view still frames the floor plan.
        self._view.fitInView(QRectF(0, 0, w, h),
                             Qt.AspectRatioMode.KeepAspectRatio)
        self._redraw_overlay()

    def _render_single_image(self, path: str) -> bool:
        pix = QPixmap(path)
        if pix.isNull():
            QMessageBox.warning(self, "Error",
                                f"Cannot load image:\n{path}")
            return False
        item = self._scene.addPixmap(pix)
        item.setZValue(-1)
        self._bg_item = item
        self._bg_items = [item]
        self._bg_size_px = (pix.width(), pix.height())
        return True

    def _render_pdf_pages(self, path: str, dpi: int = 150) -> bool:
        """Render every page of a PDF and stack them vertically as separate
        background pixmap items. Returns False if the user cancels or the
        render fails.
        """
        from app.core import ai_analyzer

        try:
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            n_pages = doc.page_count
            doc.close()
        except Exception as exc:
            QMessageBox.warning(self, "PDF error",
                                f"Cannot open PDF:\n{path}\n\n{exc}")
            return False
        if n_pages <= 0:
            QMessageBox.warning(self, "PDF error", "PDF has no pages.")
            return False

        dlg = QProgressDialog(
            f"Rendering {n_pages} page(s) at {dpi} DPI…",
            "Cancel", 0, n_pages, self)
        dlg.setWindowTitle("Loading PDF")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        QApplication.processEvents()

        y_cursor = 0
        max_w = 0
        cancelled = False

        def _on_page(i: int, total: int) -> None:
            dlg.setValue(i)
            dlg.setLabelText(f"Rendering page {i + 1} of {total}…")
            QApplication.processEvents()

        try:
            pages = ai_analyzer.render_pdf_all_pages(
                path, dpi=dpi, progress_cb=_on_page)
        except Exception as exc:
            dlg.close()
            QMessageBox.warning(self, "PDF error",
                                f"Failed rendering PDF:\n{path}\n\n{exc}")
            return False

        for i, (png, w, h) in enumerate(pages):
            if dlg.wasCanceled():
                cancelled = True
                break
            pm = QPixmap()
            pm.loadFromData(png)
            item = self._scene.addPixmap(pm)
            item.setZValue(-1)
            item.setOffset(0, y_cursor)
            self._bg_items.append(item)
            if self._bg_item is None:
                self._bg_item = item
            y_cursor += h
            max_w = max(max_w, w)
            dlg.setValue(i + 1)
            QApplication.processEvents()

        dlg.close()

        if cancelled or not self._bg_items:
            for item in self._bg_items:
                self._scene.removeItem(item)
            self._bg_items = []
            self._bg_item = None
            if cancelled:
                self._set_status("PDF load cancelled.")
            return False

        self._bg_size_px = (max_w, y_cursor)
        return True

    # ── Mouse events ───────────────────────────────────────────────────────
    def _axis_lock(self, pos: QPointF, anchor: QPointF) -> QPointF:
        """If Shift is held, snap `pos` to share X or Y with `anchor` (bigger delta wins)."""
        from PyQt6.QtWidgets import QApplication
        if not (QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier):
            return pos
        dx = pos.x() - anchor.x()
        dy = pos.y() - anchor.y()
        if abs(dx) >= abs(dy):
            return QPointF(pos.x(), anchor.y())
        return QPointF(anchor.x(), pos.y())

    def _on_press(self, pos: QPointF):
        mode = self._mode

        if mode == "georef":
            self._georef_handle_click(pos)
            return

        if mode == "scale":
            self._arrow_start = pos

        elif mode == "apt_type":
            if not self._poly_points:
                self._poly_points.append(pos)
                self._update_poly_preview()
            else:
                pos = self._axis_lock(pos, self._poly_points[-1])
                first = self._poly_points[0]
                dist = math.hypot(pos.x() - first.x(), pos.y() - first.y())
                if dist < 15 and len(self._poly_points) >= 3:
                    self._close_apt_polygon()
                else:
                    self._poly_points.append(pos)
                    self._update_poly_preview()

        elif mode == "select":
            self._try_select(pos)

        elif mode == "move":
            kind, idx = self._hit_test_polygon(pos)
            if kind is None:
                # Click on empty canvas: clear group + status
                self._move_targets = []
                self._move_drag_start = None
                self._move_orig_pts_list = []
                self._move_orig_centers = []
                self._move_orig_cams_list = []
                self._set_multi_selection([])
                return
            hit_pair = (kind, idx)
            # If user clicked a polygon that is already part of the multi-
            # selection, drag the whole group together. Otherwise replace
            # the selection with just this polygon and drag it solo.
            if hit_pair in self._multi_selection:
                self._move_targets = list(self._multi_selection)
            else:
                self._move_targets = [hit_pair]
                self._set_multi_selection([hit_pair])
            self._move_drag_start = pos
            self._move_orig_pts_list = []
            self._move_orig_centers = []
            self._move_orig_cams_list = []
            for _k, _i in self._move_targets:
                _p = (self.apt_type_polygons[_i] if _k == "apt_type"
                      else self.entrances[_i])
                self._move_orig_pts_list.append(list(_p["polygon_img"]))
                self._move_orig_centers.append(_p["center_img"])
                self._move_orig_cams_list.append([
                    (c["img_x"], c["img_y"])
                    for c in _migrate_balcony_cams(_p)])
            if len(self._move_targets) > 1:
                self._set_status(
                    f"✥  Dragging group of {len(self._move_targets)} polygons "
                    f"— release to confirm.")

        elif mode == "transform":
            # Check if near an existing handle
            if self._xform_handles and self._xform_target:
                _hs = 10
                for _item in self._xform_handles:
                    _tag = getattr(_item, '_xform_tag', None)
                    if _tag is None:
                        continue
                    _r = _item.rect() if hasattr(_item, 'rect') else None
                    if _r is None:
                        continue
                    _hx = _item.pos().x() + _r.x() + _r.width() / 2
                    _hy = _item.pos().y() + _r.y() + _r.height() / 2
                    if math.hypot(pos.x() - _hx, pos.y() - _hy) < _hs:
                        self._xform_drag = _tag
                        self._xform_drag_start = pos
                        _kind, _idx = self._xform_target
                        _p = (self.apt_type_polygons[_idx]
                              if _kind == "apt_type" else self.entrances[_idx])
                        self._xform_orig_pts = list(_p["polygon_img"])
                        self._xform_orig_center = _p["center_img"]
                        _ocams = _migrate_balcony_cams(_p)
                        self._xform_orig_cams = [
                            (c["img_x"], c["img_y"], c.get("yaw_deg", 0.0))
                            for c in _ocams]
                        return
            found_kind, found_idx = self._hit_test_polygon(pos)
            if found_kind is not None:
                self._show_transform_handles(found_kind, found_idx)
            else:
                self._clear_transform_handles()

        elif mode == "balcony_cam":
            # ── Second click: set yaw orientation and append cam ────────────
            if self._cam_pending_idx is not None and self._cam_pending_pos is not None:
                _idx = self._cam_pending_idx
                _start = self._cam_pending_pos
                _p = self.apt_type_polygons[_idx]
                _dx = pos.x() - _start.x()
                _dy = pos.y() - _start.y()
                if math.hypot(_dx, _dy) < 2.0:
                    self._set_status("📷 Click further from the camera point to set orientation")
                    return
                # Image Y grows downward; world Y grows upward → negate dy for yaw
                _yaw_deg = round(math.degrees(math.atan2(-_dy, _dx)), 1)
                _z_m = _p.get("extrusion_m", 3.0) / 2.0
                _cam = {
                    "img_x":   _start.x(),
                    "img_y":   _start.y(),
                    "z_m":     _z_m,
                    "yaw_deg": _yaw_deg,
                }
                if self.scale_px_per_m:
                    _s = self.scale_px_per_m
                    _cam["world_x_m"] = round(_start.x() / _s, 3)
                    _cam["world_y_m"] = round(_start.y() / _s, 3)
                    _cam["z_cm"]      = round(_z_m * 100, 1)
                _cams = _migrate_balcony_cams(_p)
                _cams.append(_cam)
                _p["balcony_cams"] = _cams
                self._cancel_cam_pending()
                self._redraw_overlay()
                self._emit()
                self._set_status(
                    f"📷 Camera #{len(_cams)} added to {_p['type_name']} "
                    f"({_p['building_id']}/E{_p['entrance_id']})  "
                    f"Z={_z_m:.1f}m  Yaw={_yaw_deg}°")
                return

            # ── First click: maybe remove an existing cam, else start new ──
            _hit_idx, _hit_cam_i = self._hit_test_cam(pos)
            if _hit_idx is not None:
                _hp = self.apt_type_polygons[_hit_idx]
                _cams = _migrate_balcony_cams(_hp)
                _removed = _cams.pop(_hit_cam_i)
                _hp["balcony_cams"] = _cams
                self._redraw_overlay()
                self._emit()
                self._set_status(
                    f"📷 Removed cam #{_hit_cam_i + 1} from {_hp['type_name']} "
                    f"({_hp['building_id']}/E{_hp['entrance_id']})  "
                    f"— {len(_cams)} remaining")
                return

            _kind, _idx = self._hit_test_polygon(pos)
            if _kind == "apt_type" and _idx is not None:
                _target = _idx
            else:
                _target = self._nearest_apt_type_idx(pos)
                if _target is None:
                    self._set_status("📷 No apartment polygons — create one first")
                    return
            self._cam_pending_idx = _target
            self._cam_pending_pos = QPointF(pos)
            _tp = self.apt_type_polygons[_target]
            self._set_status(
                f"📷 Position set on {_tp['type_name']} "
                f"({_tp['building_id']}/E{_tp['entrance_id']})  "
                f"— now click to set camera orientation (ESC to cancel)")

        elif mode == "vertex_edit":
            # Check if clicking near an existing handle
            if self._edit_handles:
                for hi, handle in enumerate(self._edit_handles):
                    r = handle.rect()
                    cx = handle.pos().x() + r.x() + r.width() / 2
                    cy = handle.pos().y() + r.y() + r.height() / 2
                    dist = math.hypot(pos.x() - cx, pos.y() - cy)
                    if dist < 12:
                        self._drag_handle_idx = hi
                        self._drag_handle_origin = QPointF(
                            handle.pos().x(), handle.pos().y())
                        return
            # Try to select a polygon
            found_kind, found_idx = self._hit_test_polygon(pos)
            if found_kind is not None:
                self._enter_vertex_edit(found_kind, found_idx)

    def _on_move(self, pos: QPointF):
        mode = self._mode

        if mode == "scale" and self._arrow_start:
            if self._arrow_preview:
                self._scene.removeItem(self._arrow_preview)
            self._arrow_preview = self._scene.addLine(
                QLineF(self._arrow_start, pos),
                QPen(QColor("#00aaff"), 2, Qt.PenStyle.SolidLine))

        elif mode == "balcony_cam" and self._cam_pending_pos is not None:
            # Live yaw preview arrow
            if self._cam_yaw_preview is not None:
                self._scene.removeItem(self._cam_yaw_preview)
                self._cam_yaw_preview = None
            _sp = self._cam_pending_pos
            if math.hypot(pos.x() - _sp.x(), pos.y() - _sp.y()) >= 2.0:
                self._cam_yaw_preview = self._scene.addLine(
                    QLineF(_sp, pos),
                    QPen(QColor("#ff6b6b"), 2, Qt.PenStyle.SolidLine))
                self._cam_yaw_preview.setZValue(17)

        elif mode == "apt_type" and self._poly_points and self._pending_apt_type is None:
            # Floating edge from last vertex to cursor
            pos = self._axis_lock(pos, self._poly_points[-1])
            for item in self._poly_preview_items[:]:
                if getattr(item, "_float_edge", False):
                    self._scene.removeItem(item)
                    self._poly_preview_items.remove(item)
            line = self._scene.addLine(
                QLineF(self._poly_points[-1], pos),
                QPen(QColor("#ffff00"), 1.5, Qt.PenStyle.DashLine))
            line._float_edge = True
            self._poly_preview_items.append(line)
            self._update_draw_alignment(pos)

        elif mode == "move" and self._move_targets and self._move_drag_start:
            dx = pos.x() - self._move_drag_start.x()
            dy = pos.y() - self._move_drag_start.y()
            for _ti, (kind, idx) in enumerate(self._move_targets):
                p = (self.apt_type_polygons[idx] if kind == "apt_type"
                     else self.entrances[idx])
                orig_pts = self._move_orig_pts_list[_ti]
                p["polygon_img"] = [(ox + dx, oy + dy) for ox, oy in orig_pts]
                ocx, ocy = self._move_orig_centers[_ti]
                p["center_img"] = (ocx + dx, ocy + dy)
                _move_cams = _migrate_balcony_cams(p)
                _orig_cams = self._move_orig_cams_list[_ti]
                for _ci, _cam in enumerate(_move_cams):
                    if _ci < len(_orig_cams):
                        _ocx2, _ocy2 = _orig_cams[_ci]
                        _cam["img_x"] = _ocx2 + dx
                        _cam["img_y"] = _ocy2 + dy
                self._refresh_polygon_visual(kind, idx)
            self._redraw_selection_outlines()

        elif mode == "transform" and self._xform_drag and self._xform_target:
            _kind, _idx = self._xform_target
            _p = (self.apt_type_polygons[_idx]
                  if _kind == "apt_type" else self.entrances[_idx])
            _orig = self._xform_orig_pts
            _cx, _cy = self._xform_orig_center
            _cams = _migrate_balcony_cams(_p)
            _orig_cams = getattr(self, '_xform_orig_cams', [])
            if self._xform_drag == 'rot':
                _sa = math.atan2(self._xform_drag_start.y() - _cy,
                                 self._xform_drag_start.x() - _cx)
                _ca = math.atan2(pos.y() - _cy, pos.x() - _cx)
                _d = _ca - _sa
                _cos, _sin = math.cos(_d), math.sin(_d)
                _p["polygon_img"] = [
                    (_cx + (ox - _cx) * _cos - (oy - _cy) * _sin,
                     _cy + (ox - _cx) * _sin + (oy - _cy) * _cos)
                    for ox, oy in _orig]
                _d_deg = math.degrees(_d)
                for _ci, _cam in enumerate(_cams):
                    if _ci < len(_orig_cams):
                        _ocx2, _ocy2, _oyaw = _orig_cams[_ci]
                        _cam["img_x"] = _cx + (_ocx2 - _cx) * _cos - (_ocy2 - _cy) * _sin
                        _cam["img_y"] = _cy + (_ocx2 - _cx) * _sin + (_ocy2 - _cy) * _cos
                        _cam["yaw_deg"] = round(_oyaw + _d_deg, 1)
            else:
                _minx, _miny, _maxx, _maxy = self._xform_bbox
                _anchors = {
                    'tl': (_maxx, _maxy), 'tr': (_minx, _maxy),
                    'bl': (_maxx, _miny), 'br': (_minx, _miny),
                }
                _ax, _ay = _anchors[self._xform_drag]
                _sd = math.hypot(self._xform_drag_start.x() - _ax,
                                 self._xform_drag_start.y() - _ay)
                _cd = math.hypot(pos.x() - _ax, pos.y() - _ay)
                if _sd < 1e-3:
                    return
                _sf = _cd / _sd
                _p["polygon_img"] = [
                    (_ax + (ox - _ax) * _sf, _ay + (oy - _ay) * _sf)
                    for ox, oy in _orig]
                for _ci, _cam in enumerate(_cams):
                    if _ci < len(_orig_cams):
                        _ocx2, _ocy2, _oyaw = _orig_cams[_ci]
                        _cam["img_x"] = _ax + (_ocx2 - _ax) * _sf
                        _cam["img_y"] = _ay + (_ocy2 - _ay) * _sf
            _pts = _p["polygon_img"]
            _p["center_img"] = (
                sum(pt[0] for pt in _pts) / len(_pts),
                sum(pt[1] for pt in _pts) / len(_pts))
            self._refresh_polygon_visual(_kind, _idx)
            # _show_transform_handles calls _clear_transform_handles which would
            # wipe _xform_drag and _xform_orig_pts — save and restore them
            _saved_drag       = self._xform_drag
            _saved_start      = self._xform_drag_start
            _saved_orig_pts   = self._xform_orig_pts
            _saved_orig_ctr   = self._xform_orig_center
            _saved_orig_cams  = getattr(self, '_xform_orig_cams', [])
            self._show_transform_handles(_kind, _idx)
            self._xform_drag        = _saved_drag
            self._xform_drag_start  = _saved_start
            self._xform_orig_pts    = _saved_orig_pts
            self._xform_orig_center = _saved_orig_ctr
            self._xform_orig_cams   = _saved_orig_cams

        elif mode == "vertex_edit" and self._drag_handle_idx is not None:
            hi = self._drag_handle_idx
            handle = self._edit_handles[hi]
            if self._drag_handle_origin is not None:
                pos = self._axis_lock(pos, self._drag_handle_origin)
            # Move handle to cursor (pos is scene coords; rect stays at (-5,-5,10,10))
            handle.setPos(pos.x(), pos.y())
            # Update stored polygon point in-place
            kind, idx = self._edit_target
            if kind == "apt_type":
                pts = self.apt_type_polygons[idx]["polygon_img"]
            else:
                pts = self.entrances[idx]["polygon_img"]
            pts[hi] = (pos.x(), pos.y())
            # Update live polygon outline so user sees shape changing in real-time
            if self._edit_live_poly:
                self._edit_live_poly.setPolygon(
                    QPolygonF([QPointF(x, y) for x, y in pts]))
            self._update_alignment_highlights(pos)

    def _on_release(self, pos: QPointF):
        mode = self._mode
        if mode == "scale" and self._arrow_start:
            start = self._arrow_start
            self._arrow_start = None
            if self._arrow_preview:
                self._scene.removeItem(self._arrow_preview)
                self._arrow_preview = None
            length = math.hypot(pos.x() - start.x(), pos.y() - start.y())
            if length < 5:
                return
            self._finish_scale(start, pos, length)

        elif mode == "move" and self._move_targets and self._move_drag_start:
            moved_targets = list(self._move_targets)
            self._move_targets = []
            self._move_drag_start = None
            self._move_orig_pts_list = []
            self._move_orig_centers = []
            self._move_orig_cams_list = []
            s = self.scale_px_per_m
            theta = math.radians(self.north_angle_deg)
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            for kind, idx in moved_targets:
                p = (self.apt_type_polygons[idx] if kind == "apt_type"
                     else self.entrances[idx])
                pts = p["polygon_img"]
                cx = sum(pt[0] for pt in pts) / len(pts)
                cy = sum(pt[1] for pt in pts) / len(pts)
                p["center_img"] = (cx, cy)
                if not s:
                    continue
                if kind == "apt_type":
                    p["world_x_m"] = round(cx / s, 3)
                    p["world_y_m"] = round(cy / s, 3)
                    p["polygon_world_m"] = [
                        (round(px / s, 4), round(py / s, 4))
                        for px, py in pts]
                    p["committed"] = False
                else:
                    p["world_x_m"] = round((cx * cos_t + cy * sin_t) / s, 3)
                    p["world_y_m"] = round((-cx * sin_t + cy * cos_t) / s, 3)
                for _cam in _migrate_balcony_cams(p):
                    _cam["world_x_m"] = round(_cam["img_x"] / s, 3)
                    _cam["world_y_m"] = round(_cam["img_y"] / s, 3)
            self._redraw_overlay()
            self._emit()

        elif mode == "transform" and self._xform_drag:
            self._xform_drag = None
            self._xform_orig_cams = []
            if self._xform_target:
                _kind, _idx = self._xform_target
                _p = (self.apt_type_polygons[_idx]
                      if _kind == "apt_type" else self.entrances[_idx])
                _pts = _p["polygon_img"]
                _cx = sum(pt[0] for pt in _pts) / len(_pts)
                _cy = sum(pt[1] for pt in _pts) / len(_pts)
                _p["center_img"] = (_cx, _cy)
                if self.scale_px_per_m:
                    _s = self.scale_px_per_m
                    if _kind == "apt_type":
                        _p["world_x_m"] = round(_cx / _s, 3)
                        _p["world_y_m"] = round(_cy / _s, 3)
                        _p["polygon_world_m"] = [
                            (round(px / _s, 4), round(py / _s, 4))
                            for px, py in _pts]
                    else:
                        _theta = math.radians(self.north_angle_deg)
                        _p["world_x_m"] = round(
                            (_cx * math.cos(_theta) + _cy * math.sin(_theta)) / _s, 3)
                        _p["world_y_m"] = round(
                            (-_cx * math.sin(_theta) + _cy * math.cos(_theta)) / _s, 3)
                    for _cam in _migrate_balcony_cams(_p):
                        _cam["world_x_m"] = round(_cam["img_x"] / _s, 3)
                        _cam["world_y_m"] = round(_cam["img_y"] / _s, 3)
                if _kind == "apt_type":
                    _p["committed"] = False
                self._redraw_overlay()
                self._show_transform_handles(_kind, _idx)
                self._emit()

        elif mode == "vertex_edit" and self._drag_handle_idx is not None:
            hi = self._drag_handle_idx
            if self._edit_target is None:
                self._drag_handle_idx = None
                self._drag_handle_origin = None
                return
            kind, idx = self._edit_target
            self._drag_handle_idx = None
            self._drag_handle_origin = None
            self._clear_align_guides()

            # Recompute world coords and centroid
            if kind == "apt_type":
                p = self.apt_type_polygons[idx]
                pts = p["polygon_img"]
                cx = sum(pt[0] for pt in pts) / len(pts)
                cy = sum(pt[1] for pt in pts) / len(pts)
                p["center_img"] = (cx, cy)
                if self.scale_px_per_m:
                    s = self.scale_px_per_m
                    p["world_x_m"] = round(cx / s, 3)
                    p["world_y_m"] = round(cy / s, 3)
                    p["polygon_world_m"] = [
                        (round(px / s, 4), round(py / s, 4))
                        for px, py in pts
                    ]
                    for _cam in _migrate_balcony_cams(p):
                        _cam["world_x_m"] = round(_cam["img_x"] / s, 3)
                        _cam["world_y_m"] = round(_cam["img_y"] / s, 3)
                p["committed"] = False
            else:
                ent = self.entrances[idx]
                pts = ent["polygon_img"]
                cx = sum(pt[0] for pt in pts) / len(pts)
                cy = sum(pt[1] for pt in pts) / len(pts)
                ent["center_img"] = (cx, cy)
                if self.scale_px_per_m:
                    theta = math.radians(self.north_angle_deg)
                    ue_x = (cx * math.cos(theta) + cy * math.sin(theta)) / self.scale_px_per_m
                    ue_y = (-cx * math.sin(theta) + cy * math.cos(theta)) / self.scale_px_per_m
                    ent["world_x_m"] = round(ue_x, 3)
                    ent["world_y_m"] = round(ue_y, 3)

            self._redraw_overlay()
            self._enter_vertex_edit(kind, idx)
            self._emit()

    def _on_double(self, pos: QPointF):
        if self._mode == "apt_type" and len(self._poly_points) >= 3:
            self._close_apt_polygon()
        elif self._mode == "select":
            # Double-click on a polygon in Select mode → re-open properties dialog
            kind, idx = self._hit_test_polygon(pos)
            if kind == "apt_type":
                p = self.apt_type_polygons[idx]
                dlg = AptTypeDialog(
                    self, self.building_ids, self.entrance_ids_all, self.apt_types_list,
                    prefill=p)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    bld, ent, typ, h = dlg.result_values()
                    p["building_id"]  = bld
                    p["entrance_id"]  = ent
                    p["type_name"]    = typ
                    p["extrusion_m"]  = h
                    p["color_hex"]    = self._get_type_color(typ).name()
                    p["committed"]    = False
                    self._redraw_overlay()
                    self._emit()
                    self._set_status(f"Updated polygon → {bld}/E{ent} type {typ}")

    # ── Scale ──────────────────────────────────────────────────────────────
    def _finish_scale(self, start: QPointF, end: QPointF, length_px: float):
        val, ok = QInputDialog.getDouble(
            self, "Set Scale",
            "Real-world length of this arrow (metres):",
            value=10.0, min=0.01, max=99999.0, decimals=2)
        if not ok:
            return
        self.scale_px_per_m = length_px / val

        # Draw permanent arrow
        self._scene.addLine(QLineF(start, end),
                             QPen(QColor("#00aaff"), 2.5)).setZValue(10)
        mid = QPointF((start.x() + end.x()) / 2,
                      (start.y() + end.y()) / 2 - 16)
        lbl = self._scene.addText(f"📏  {val} m")
        lbl.setDefaultTextColor(QColor("#00aaff"))
        lbl.setPos(mid)
        lbl.setZValue(10)

        _n_backfilled = self._backfill_world_coords_all()
        if _n_backfilled:
            self._redraw_overlay()
            self._layers_panel.rebuild(
                self.entrances, self.apt_type_polygons, self._get_type_color)

        _msg = (f"Scale set: {self.scale_px_per_m:.2f} px/m  "
                f"({length_px:.0f} px = {val} m)")
        if _n_backfilled:
            _msg += f"  — back-filled {_n_backfilled} polygon(s)"
        self._set_status(_msg)
        self._emit()

    # ── Polygon ────────────────────────────────────────────────────────────
    def _update_poly_preview(self):
        # Clear non-floating preview items
        for item in self._poly_preview_items[:]:
            if not getattr(item, "_float_edge", False):
                self._scene.removeItem(item)
                self._poly_preview_items.remove(item)

        pts = self._poly_points
        pen = QPen(QColor("#ffff00"), 1.5)
        fill = QBrush(QColor(255, 255, 0, 30))

        if len(pts) >= 3:
            poly = self._scene.addPolygon(QPolygonF(pts), pen, fill)
            poly._float_edge = False
            self._poly_preview_items.append(poly)

        # Feature 4: fixed-size dots using ItemIgnoresTransformations
        for p in pts:
            dot = self._scene.addEllipse(
                -2.5, -2.5, 5, 5,
                QPen(QColor("#ffaa00")), QBrush(QColor("#ffff00")))
            dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            dot.setPos(p.x(), p.y())
            dot._float_edge = False
            self._poly_preview_items.append(dot)

        # Close-zone ring around first point — fixed size
        if len(pts) >= 3:
            fp = pts[0]
            ring = self._scene.addEllipse(
                -12, -12, 24, 24,
                QPen(QColor("#ff4444"), 1.5, Qt.PenStyle.DashLine))
            ring.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            ring.setPos(fp.x(), fp.y())
            ring._float_edge = False
            self._poly_preview_items.append(ring)

    def _draw_entrance(self, idx: int):
        ent = self.entrances[idx]
        color = QColor(_COLORS[idx % len(_COLORS)])
        fill = QColor(color)
        fill.setAlpha(50)

        pts = [QPointF(*p) for p in ent["polygon_img"]]
        cx, cy = ent["center_img"]

        items = []

        poly_item = self._scene.addPolygon(
            QPolygonF(pts), QPen(color, 2), QBrush(fill))
        poly_item.setZValue(5)
        poly_item.setData(0, idx)  # entrance index for selection
        items.append(poly_item)

        # Centre cross
        r = 6
        h1 = self._scene.addLine(
            QLineF(cx - r, cy, cx + r, cy),
            QPen(Qt.GlobalColor.white, 1.5))
        h1.setZValue(7)
        items.append(h1)

        h2 = self._scene.addLine(
            QLineF(cx, cy - r, cx, cy + r),
            QPen(Qt.GlobalColor.white, 1.5))
        h2.setZValue(7)
        items.append(h2)

        dot = self._scene.addEllipse(
            cx - r, cy - r, r * 2, r * 2,
            QPen(Qt.GlobalColor.white, 1.5), QBrush(color))
        dot.setZValue(6)
        items.append(dot)

        # Label — show coords relative to first entrance so numbers are readable
        avg_x = sum(p.x() for p in pts) / len(pts)
        avg_y = sum(p.y() for p in pts) / len(pts)
        label = f"{ent['building_id']}\nE{ent['entrance_id']}"
        if "world_x_m" in ent and self.entrances:
            ref_x = self.entrances[0].get("world_x_m", 0)
            ref_y = self.entrances[0].get("world_y_m", 0)
            rel_x = round(ent["world_x_m"] - ref_x, 1)
            rel_y = round(ent["world_y_m"] - ref_y, 1)
            label += f"\n({rel_x:+.1f}, {rel_y:+.1f}) m"
        txt = _OutlinedTextItem(label)
        txt.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        br = txt.boundingRect()
        txt.setPos(avg_x - br.width() / 2, avg_y - br.height() / 2)
        txt.setZValue(8)
        self._scene.addItem(txt)
        items.append(txt)

        # Store items
        while len(self._entrance_items) <= idx:
            self._entrance_items.append([])
        self._entrance_items[idx] = items

    # ── Apt Type Polygon ───────────────────────────────────────────────────
    def _close_apt_polygon(self):
        pts = [(p.x(), p.y()) for p in self._poly_points]
        self._poly_points.clear()
        for item in self._poly_preview_items:
            self._scene.removeItem(item)
        self._poly_preview_items.clear()

        dlg = AptTypeDialog(self, self.building_ids, self.entrance_ids_all, self.apt_types_list)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        bld, ent, type_name, extrusion_m = dlg.result_values()

        # ── Auto-compute geometric centroid (no manual click needed) ──────────
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)

        entry = {
            "building_id": bld,
            "entrance_id": ent,
            "type_name":   type_name,
            "uid":         _uuid_mod.uuid4().hex[:10],
            "extrusion_m": extrusion_m,
            "polygon_img": pts,
            "center_img":  (cx, cy),
            "color_hex":   self._get_type_color(type_name).name(),
        }

        if self.scale_px_per_m:
            # Direct pixel → metre scaling only — no north rotation.
            # UE top-down view has X→right and Y→down, identical to image
            # space, so the volumes land exactly where they were drawn.
            s = self.scale_px_per_m
            entry["world_x_m"] = round(cx / s, 3)
            entry["world_y_m"] = round(cy / s, 3)
            entry["polygon_world_m"] = [
                (round(px / s, 4), round(py / s, 4))
                for px, py in pts
            ]

        self.apt_type_polygons.append(entry)
        idx = len(self.apt_type_polygons) - 1
        while len(self._apt_type_items) <= idx:
            self._apt_type_items.append([])
        self._draw_apt_type(idx)
        self._layers_panel.rebuild(
            self.entrances, self.apt_type_polygons,
            self._get_type_color, visibility_fn=self._make_vis_fn())
        self._set_status(
            f"✓  {type_name} ({bld}/{ent}) placed. Draw next or switch mode.")
        self._emit()

    def _draw_apt_type(self, idx: int):
        p = self.apt_type_polygons[idx]
        # Feature 1: per-type color
        color = self._get_type_color(p['type_name'])
        fill = QColor(color)
        fill.setAlpha(40)

        pts_q = [QPointF(*pt) for pt in p["polygon_img"]]
        cx, cy = p["center_img"]

        items = []

        poly_item = self._scene.addPolygon(
            QPolygonF(pts_q), QPen(color, 2), QBrush(fill))
        poly_item.setZValue(5)
        poly_item.setData(1, idx)
        items.append(poly_item)

        r = 6
        h1 = self._scene.addLine(QLineF(cx-r, cy, cx+r, cy), QPen(Qt.GlobalColor.white, 1.5))
        h1.setZValue(7)
        items.append(h1)

        h2 = self._scene.addLine(QLineF(cx, cy-r, cx, cy+r), QPen(Qt.GlobalColor.white, 1.5))
        h2.setZValue(7)
        items.append(h2)

        dot = self._scene.addEllipse(cx-r, cy-r, r*2, r*2, QPen(Qt.GlobalColor.white, 1.5), QBrush(color))
        dot.setZValue(6)
        items.append(dot)

        avg_x = sum(pt[0] for pt in p["polygon_img"]) / len(p["polygon_img"])
        avg_y = sum(pt[1] for pt in p["polygon_img"]) / len(p["polygon_img"])
        direction_str = p.get("direction", "")
        dir_line = f"\n{direction_str}" if direction_str else ""
        _committed = p.get("committed", False)
        _badge = " ✓" if _committed else " *"
        label = f"{p['type_name']}\n{p['building_id']}/E{p['entrance_id']}{dir_line}\n↕{p['extrusion_m']}m{_badge}"
        txt = _OutlinedTextItem(label)
        txt.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        if not _committed:
            txt.setDefaultTextColor(QColor("#ffaa00"))
        br = txt.boundingRect()
        txt.setPos(avg_x - br.width()/2, avg_y - br.height()/2)
        txt.setZValue(8)
        self._scene.addItem(txt)
        items.append(txt)

        # Camera markers (PorchPawnArrow positions — multiple allowed per polygon)
        _cams = _migrate_balcony_cams(p)
        for _ci, _cam in enumerate(_cams):
            _cx, _cy = _cam["img_x"], _cam["img_y"]
            _r = 10
            # Dashed tether from polygon centroid → camera (shows ownership,
            # important when the cam is placed outside the polygon)
            _tether = self._scene.addLine(
                QLineF(cx, cy, _cx, _cy),
                QPen(QColor("#ff6b6b"), 1.2, Qt.PenStyle.DashLine))
            _tether.setZValue(14); items.append(_tether)
            _circle = self._scene.addEllipse(
                _cx - _r, _cy - _r, _r * 2, _r * 2,
                QPen(QColor("#ff6b6b"), 2),
                QBrush(QColor(255, 107, 107, 80)))
            _circle.setZValue(15); items.append(_circle)
            for _lf in [QLineF(_cx - _r, _cy, _cx + _r, _cy),
                        QLineF(_cx, _cy - _r, _cx, _cy + _r)]:
                _ln = self._scene.addLine(_lf, QPen(QColor("#ff6b6b"), 1.5))
                _ln.setZValue(15); items.append(_ln)
            # Orientation arrow from cam in yaw direction
            _yaw = _cam.get("yaw_deg")
            if _yaw is not None:
                _al = 28  # arrow length in scene pixels
                _yr = math.radians(_yaw)
                # Image Y grows downward → invert yaw's Y component
                _ex = _cx + _al * math.cos(_yr)
                _ey = _cy - _al * math.sin(_yr)
                _arrow = self._scene.addLine(
                    QLineF(_cx, _cy, _ex, _ey),
                    QPen(QColor("#ff6b6b"), 2.5))
                _arrow.setZValue(16); items.append(_arrow)
                # Arrowhead
                _hl = 7
                _ha = math.radians(25)
                for _sign in (-1, 1):
                    _hx = _ex - _hl * math.cos(_yr + _sign * _ha)
                    _hy = _ey + _hl * math.sin(_yr + _sign * _ha)
                    _hline = self._scene.addLine(
                        QLineF(_ex, _ey, _hx, _hy),
                        QPen(QColor("#ff6b6b"), 2.5))
                    _hline.setZValue(16); items.append(_hline)
            _num = f"#{_ci + 1}"
            _label = f"CAM {_num}" if _yaw is None else f"CAM {_num} {_yaw:.0f}°"
            _ctxt = _OutlinedTextItem(_label)
            _ctxt.setFont(QFont("Arial", 7, QFont.Weight.Bold))
            _cbr = _ctxt.boundingRect()
            _ctxt.setPos(_cx - _cbr.width() / 2, _cy - _r - _cbr.height() - 1)
            _ctxt.setZValue(16)
            self._scene.addItem(_ctxt); items.append(_ctxt)

        # Store items
        while len(self._apt_type_items) <= idx:
            self._apt_type_items.append([])
        self._apt_type_items[idx] = items

    # ── Layers visibility ──────────────────────────────────────────────────
    def _make_vis_fn(self):
        """Return a closure (kind, idx) → bool for the current visibility state."""
        vs = self._visibility_state
        ents = self.entrances
        apts = self.apt_type_polygons

        def _vis(kind, idx):
            if kind == "entrance" and idx < len(ents):
                e = ents[idx]
                return vs.get(("E", e["building_id"], e["entrance_id"]), True)
            if kind == "apt_type" and idx < len(apts):
                p = apts[idx]
                return vs.get(("A", p["building_id"], p["entrance_id"], p["type_name"]), True)
            return True

        return _vis

    def _on_visibility_changed(self, kind: str, idx: int, visible: bool):
        if kind == "entrance" and idx < len(self._entrance_items):
            # Persist by polygon identity so state survives redraws
            if idx < len(self.entrances):
                ent = self.entrances[idx]
                self._visibility_state[
                    ("E", ent["building_id"], ent["entrance_id"])] = visible
            for item in self._entrance_items[idx]:
                item.setVisible(visible)
        elif kind == "apt_type" and idx < len(self._apt_type_items):
            if idx < len(self.apt_type_polygons):
                p = self.apt_type_polygons[idx]
                self._visibility_state[
                    ("A", p["building_id"], p["entrance_id"], p["type_name"])] = visible
            for item in self._apt_type_items[idx]:
                item.setVisible(visible)

    # ── Select / Delete ────────────────────────────────────────────────────
    def _hit_test_cam(self, pos: QPointF, radius: float = 12.0) -> tuple[int | None, int | None]:
        """Return (apt_idx, cam_idx) of the nearest cam within `radius`, or (None, None)."""
        best = (None, None, float("inf"))
        for ai, p in enumerate(self.apt_type_polygons):
            for ci, cam in enumerate(_migrate_balcony_cams(p)):
                d = math.hypot(pos.x() - cam["img_x"], pos.y() - cam["img_y"])
                if d < radius and d < best[2]:
                    best = (ai, ci, d)
        return best[0], best[1]

    def _nearest_apt_type_idx(self, pos: QPointF) -> int | None:
        """Return idx of nearest apt_type polygon (by centroid distance), or None."""
        if not self.apt_type_polygons:
            return None
        best_i, best_d = None, float("inf")
        for i, p in enumerate(self.apt_type_polygons):
            cx, cy = p.get("center_img", (0, 0))
            d = math.hypot(pos.x() - cx, pos.y() - cy)
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    def _on_escape(self):
        if self._mode == "balcony_cam" and self._cam_pending_idx is not None:
            self._cancel_cam_pending()
            self._set_status("📷 Camera placement cancelled")

    def _cancel_cam_pending(self):
        """Reset pending balcony-cam two-click state and remove any preview."""
        self._cam_pending_idx = None
        self._cam_pending_pos = None
        if self._cam_yaw_preview is not None:
            try:
                self._scene.removeItem(self._cam_yaw_preview)
            except Exception:
                pass
            self._cam_yaw_preview = None

    def _hit_test_polygon(self, pos: QPointF):
        """Return (kind, idx) of first polygon hit, or (None, None)."""
        hit = self._scene.items(QRectF(pos.x()-6, pos.y()-6, 12, 12))
        for item in hit:
            idx0 = item.data(0)
            idx1 = item.data(1)
            if idx0 is not None:
                return "entrance", idx0
            if idx1 is not None:
                return "apt_type", idx1
        return None, None

    # ── Multi-selection helpers ────────────────────────────────────────────
    def _set_multi_selection(self, items: list[tuple[str, int]]):
        """Replace the multi-selection. Mirrors the last entry into the
        single-selection fields so existing commit/delete/dialog flows keep
        targeting the most-recently-clicked polygon."""
        self._multi_selection = list(items)
        if self._multi_selection:
            kind, idx = self._multi_selection[-1]
            self._selected_type = kind
            self._selected_idx = idx
        else:
            self._selected_type = None
            self._selected_idx = None
        self._redraw_selection_outlines()

    def _toggle_multi_selection(self, kind: str, idx: int):
        """Toggle one polygon in/out of the multi-selection (Ctrl+click)."""
        pair = (kind, idx)
        if pair in self._multi_selection:
            self._multi_selection.remove(pair)
        else:
            self._multi_selection.append(pair)
        if self._multi_selection:
            k, i = self._multi_selection[-1]
            self._selected_type = k
            self._selected_idx = i
        else:
            self._selected_type = None
            self._selected_idx = None
        self._redraw_selection_outlines()

    def _clear_selection_outlines(self):
        for it in self._selection_items:
            if it.scene():
                self._scene.removeItem(it)
        self._selection_items.clear()

    def _redraw_selection_outlines(self):
        """Draw a thick dashed cyan outline around each multi-selected polygon."""
        self._clear_selection_outlines()
        # Filter out stale indices (e.g. after delete) silently
        valid = []
        for kind, idx in self._multi_selection:
            if kind == "apt_type" and 0 <= idx < len(self.apt_type_polygons):
                pts = self.apt_type_polygons[idx]["polygon_img"]
            elif kind == "entrance" and 0 <= idx < len(self.entrances):
                pts = self.entrances[idx]["polygon_img"]
            else:
                continue
            if len(pts) < 2:
                continue
            valid.append((kind, idx))
            poly = QPolygonF([QPointF(x, y) for x, y in pts])
            outline = self._scene.addPolygon(
                poly,
                QPen(QColor("#00ffff"), 3, Qt.PenStyle.DashLine),
                QBrush(Qt.BrushStyle.NoBrush))
            outline.setZValue(25)
            self._selection_items.append(outline)
        self._multi_selection = valid

    def _try_select(self, pos: QPointF):
        kind, idx = self._hit_test_polygon(pos)
        if kind == "entrance":
            self._set_multi_selection([("entrance", idx)])
            ent = self.entrances[idx]
            self._set_status(
                f"Selected entrance: {ent['building_id']} / {ent['entrance_id']}  "
                f"— Ctrl+Click to add more, Delete to remove")
        elif kind == "apt_type":
            self._set_multi_selection([("apt_type", idx)])
            p = self.apt_type_polygons[idx]
            self._set_status(
                f"Selected apt type: {p['type_name']} "
                f"({p['building_id']}/{p['entrance_id']})  "
                f"— Ctrl+Click to add more, Delete to remove")
        else:
            self._set_multi_selection([])
            self._set_status("Click a polygon to select it — Ctrl+Click to multi-select.")

    def _delete_selected(self):
        # Build the set of items to remove from either the multi-selection
        # (preferred) or, as a fallback, the legacy single selection.
        targets = list(self._multi_selection)
        if not targets and self._selected_idx is not None and self._selected_type:
            targets = [(self._selected_type, self._selected_idx)]
        if not targets:
            return

        ent_idxs = sorted(
            {i for k, i in targets if k == "entrance"
             and 0 <= i < len(self.entrances)},
            reverse=True)
        apt_idxs = sorted(
            {i for k, i in targets if k == "apt_type"
             and 0 <= i < len(self.apt_type_polygons)},
            reverse=True)

        for i in ent_idxs:
            self.entrances.pop(i)
        for i in apt_idxs:
            self.apt_type_polygons.pop(i)

        self._multi_selection = []
        self._selected_idx = None
        self._selected_type = None
        self._clear_selection_outlines()
        self._redraw_overlay()
        self._emit()

        n = len(ent_idxs) + len(apt_idxs)
        if n == 1:
            self._set_status("Removed.")
        else:
            self._set_status(f"Removed {n} polygons.")

    def _undo_last(self):
        """Remove the most recently added polygon (entrance or apt_type)."""
        # If currently mid-draw, cancel that first
        if self._poly_points:
            self._cancel_drawing()
            self._set_status("Drawing cancelled.")
            return
        # Otherwise pop the last added item (apt_type takes priority as it's newer)
        if self.apt_type_polygons:
            removed = self.apt_type_polygons.pop()
            self._redraw_overlay()
            self._emit()
            self._set_status(
                f"↩  Removed: {removed['type_name']} ({removed['building_id']}/{removed['entrance_id']})")
        elif self.entrances:
            removed = self.entrances.pop()
            self._redraw_overlay()
            self._emit()
            self._set_status(
                f"↩  Removed entrance: {removed['building_id']} / {removed['entrance_id']}")
        else:
            self._set_status("Nothing to undo.")

    # ── Vertex editing ─────────────────────────────────────────────────────
    def _enter_vertex_edit(self, kind: str, idx: int):
        self._clear_edit_handles()
        if kind == "apt_type":
            pts = self.apt_type_polygons[idx]["polygon_img"]
        else:
            pts = self.entrances[idx]["polygon_img"]

        for x, y in pts:
            handle = QGraphicsEllipseItem(x - 5, y - 5, 10, 10)
            handle.setBrush(QBrush(QColor("#ffff00")))
            handle.setPen(QPen(QColor("#ff8800"), 1.5))
            handle.setZValue(20)
            handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            # With ItemIgnoresTransformations the item pos is scene coords;
            # we set rect to (-5,-5,10,10) and pos to (x,y) so it centers correctly
            handle.setRect(-5, -5, 10, 10)
            handle.setPos(x, y)
            self._scene.addItem(handle)
            self._edit_handles.append(handle)

        # Live polygon outline (white dashed) that updates while dragging
        q_pts = [QPointF(x, y) for x, y in pts]
        self._edit_live_poly = self._scene.addPolygon(
            QPolygonF(q_pts),
            QPen(QColor("#ffffff"), 1.5, Qt.PenStyle.DashLine),
            QBrush(Qt.BrushStyle.NoBrush))
        self._edit_live_poly.setZValue(19)

        self._edit_target = (kind, idx)
        name = (self.apt_type_polygons[idx]["type_name"] if kind == "apt_type"
                else f"{self.entrances[idx]['building_id']}/{self.entrances[idx]['entrance_id']}")
        self._set_status(f"✏  {name} — click a yellow handle and drag to move it")

    def _clear_align_guides(self):
        for g in self._align_guides:
            if g.scene():
                self._scene.removeItem(g)
        self._align_guides.clear()

    def _clear_edit_handles(self):
        for handle in self._edit_handles:
            if handle.scene():
                self._scene.removeItem(handle)
        self._edit_handles.clear()
        if self._edit_live_poly and self._edit_live_poly.scene():
            self._scene.removeItem(self._edit_live_poly)
        self._edit_live_poly = None
        self._edit_target = None
        self._drag_handle_idx = None
        self._drag_handle_origin = None
        self._clear_align_guides()

    # ── Alignment highlighting ──────────────────────────────────────────────
    def _snap_threshold(self) -> float:
        """Alignment threshold in scene pixels (based on current zoom level)."""
        scale = self._view.transform().m11() or 1.0
        return _SNAP_SCREEN_PX / scale

    def _update_alignment_highlights(self, pos: QPointF):
        """While dragging a vertex handle: colour aligned handles cyan, draw guide lines."""
        self._clear_align_guides()
        if not self._edit_handles or self._drag_handle_idx is None:
            return

        snap = self._snap_threshold()
        sr = self._scene.sceneRect()

        # Reset all non-dragged handles to default yellow
        for hi, h in enumerate(self._edit_handles):
            if hi != self._drag_handle_idx:
                h.setBrush(QBrush(QColor("#ffff00")))

        for hi, h in enumerate(self._edit_handles):
            if hi == self._drag_handle_idx:
                continue
            hx, hy = h.pos().x(), h.pos().y()
            x_aligned = abs(pos.x() - hx) < snap
            y_aligned = abs(pos.y() - hy) < snap
            if x_aligned or y_aligned:
                h.setBrush(QBrush(QColor("#00ffff")))
            if x_aligned:
                g = self._scene.addLine(
                    hx, sr.top(), hx, sr.bottom(),
                    QPen(QColor("#00ffff"), 0.5, Qt.PenStyle.DashLine))
                g.setZValue(25)
                self._align_guides.append(g)
            if y_aligned:
                g = self._scene.addLine(
                    sr.left(), hy, sr.right(), hy,
                    QPen(QColor("#00ffff"), 0.5, Qt.PenStyle.DashLine))
                g.setZValue(25)
                self._align_guides.append(g)

    def _update_draw_alignment(self, pos: QPointF):
        """While drawing: show cyan guide lines when cursor aligns with existing vertices."""
        self._clear_align_guides()
        if len(self._poly_points) < 1:
            return

        snap = self._snap_threshold()
        sr = self._scene.sceneRect()

        for p in self._poly_points:
            if abs(pos.x() - p.x()) < snap:
                g = self._scene.addLine(
                    p.x(), sr.top(), p.x(), sr.bottom(),
                    QPen(QColor("#00ffff"), 0.5, Qt.PenStyle.DashLine))
                g.setZValue(25)
                self._align_guides.append(g)
            if abs(pos.y() - p.y()) < snap:
                g = self._scene.addLine(
                    sr.left(), p.y(), sr.right(), p.y(),
                    QPen(QColor("#00ffff"), 0.5, Qt.PenStyle.DashLine))
                g.setZValue(25)
                self._align_guides.append(g)

    # ── Add / Delete vertex (Ctrl+Click / Alt+Click in vertex_edit mode) ───
    def _update_polygon_world_coords(self, kind: str, idx: int):
        """Recompute centroid and world-space coords after vertex list changes."""
        if kind == "apt_type":
            p = self.apt_type_polygons[idx]
            pts = p["polygon_img"]
            cx = sum(pt[0] for pt in pts) / len(pts)
            cy = sum(pt[1] for pt in pts) / len(pts)
            p["center_img"] = (cx, cy)
            if self.scale_px_per_m:
                s = self.scale_px_per_m
                p["world_x_m"] = round(cx / s, 3)
                p["world_y_m"] = round(cy / s, 3)
                p["polygon_world_m"] = [
                    (round(px / s, 4), round(py / s, 4)) for px, py in pts]
                for _cam in _migrate_balcony_cams(p):
                    _cam["world_x_m"] = round(_cam["img_x"] / s, 3)
                    _cam["world_y_m"] = round(_cam["img_y"] / s, 3)
        else:
            ent = self.entrances[idx]
            pts = ent["polygon_img"]
            cx = sum(pt[0] for pt in pts) / len(pts)
            cy = sum(pt[1] for pt in pts) / len(pts)
            ent["center_img"] = (cx, cy)
            if self.scale_px_per_m:
                theta = math.radians(self.north_angle_deg)
                ue_x = (cx * math.cos(theta) + cy * math.sin(theta)) / self.scale_px_per_m
                ue_y = (-cx * math.sin(theta) + cy * math.cos(theta)) / self.scale_px_per_m
                ent["world_x_m"] = round(ue_x, 3)
                ent["world_y_m"] = round(ue_y, 3)

    def _on_ctrl_press(self, pos: QPointF):
        """Ctrl+Click:
          - select / move modes → toggle that polygon in/out of multi-selection
          - vertex_edit mode    → insert a new vertex on the nearest edge
        """
        if self._mode in ("select", "move"):
            kind, idx = self._hit_test_polygon(pos)
            if kind is None:
                return
            self._toggle_multi_selection(kind, idx)
            n = len(self._multi_selection)
            if n == 0:
                self._set_status("Multi-selection cleared.")
            else:
                _label = "polygon" if n == 1 else "polygons"
                self._set_status(
                    f"{n} {_label} selected  — switch to ✥ Move and drag any one of them"
                    " to move the whole group together. Ctrl+Click again to remove from selection.")
            return
        if self._mode != "vertex_edit" or self._edit_target is None:
            return
        kind, idx = self._edit_target
        pts = (self.apt_type_polygons[idx]["polygon_img"] if kind == "apt_type"
               else self.entrances[idx]["polygon_img"])
        if len(pts) < 2:
            return

        # Find the closest edge (segment between consecutive vertices)
        best_dist = float("inf")
        best_i = 0
        n = len(pts)
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            dx, dy = bx - ax, by - ay
            seg_sq = dx * dx + dy * dy
            if seg_sq < 1e-10:
                dist = math.hypot(pos.x() - ax, pos.y() - ay)
            else:
                t = max(0.0, min(1.0,
                    ((pos.x() - ax) * dx + (pos.y() - ay) * dy) / seg_sq))
                proj_x, proj_y = ax + t * dx, ay + t * dy
                dist = math.hypot(pos.x() - proj_x, pos.y() - proj_y)
            if dist < best_dist:
                best_dist = dist
                best_i = i

        # Insert at click position after vertex best_i
        pts.insert(best_i + 1, (pos.x(), pos.y()))
        self._update_polygon_world_coords(kind, idx)
        if kind == "apt_type":
            self.apt_type_polygons[idx]["committed"] = False
        self._redraw_overlay()
        self._enter_vertex_edit(kind, idx)
        self._emit()
        self._set_status(f"✚  Vertex added — {len(pts)} vertices total")

    def _on_alt_press(self, pos: QPointF):
        """Alt+Click in vertex_edit mode: delete the nearest vertex (min 3 kept)."""
        if self._mode != "vertex_edit" or self._edit_target is None:
            return
        kind, idx = self._edit_target
        pts = (self.apt_type_polygons[idx]["polygon_img"] if kind == "apt_type"
               else self.entrances[idx]["polygon_img"])
        if len(pts) <= 3:
            self._set_status("⚠  Cannot delete — minimum 3 vertices required.")
            return

        # Find closest vertex
        best_dist = float("inf")
        best_hi = 0
        for hi, (x, y) in enumerate(pts):
            d = math.hypot(pos.x() - x, pos.y() - y)
            if d < best_dist:
                best_dist = d
                best_hi = hi

        # Only delete if close enough (20 scene px)
        if best_dist > 20:
            self._set_status("⚠  Click closer to a vertex to delete it.")
            return

        pts.pop(best_hi)
        self._update_polygon_world_coords(kind, idx)
        if kind == "apt_type":
            self.apt_type_polygons[idx]["committed"] = False
        self._redraw_overlay()
        self._enter_vertex_edit(kind, idx)
        self._emit()
        self._set_status(f"✖  Vertex removed — {len(pts)} vertices remaining")

    # ── Lightweight in-place polygon visual update (no scene rebuild) ─────
    def _refresh_polygon_visual(self, kind: str, idx: int):
        """Update one polygon's scene items in-place — used during drag to avoid
        the expensive full _redraw_overlay on every mouse-move event."""
        items_list = (self._apt_type_items if kind == "apt_type"
                      else self._entrance_items)
        if idx >= len(items_list) or not items_list[idx]:
            return
        items = items_list[idx]
        p = (self.apt_type_polygons[idx] if kind == "apt_type"
             else self.entrances[idx])
        pts = p["polygon_img"]
        cx, cy = p["center_img"]
        r = 6
        # items[0] = polygon, [1] = h1 line, [2] = h2 line, [3] = dot, [4] = label
        if len(items) >= 1:
            items[0].setPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
        if len(items) >= 3:
            items[1].setLine(cx - r, cy, cx + r, cy)
            items[2].setLine(cx, cy - r, cx, cy + r)
        if len(items) >= 4:
            items[3].setRect(cx - r, cy - r, r * 2, r * 2)
        if len(items) >= 5:
            avg_x = sum(pt[0] for pt in pts) / len(pts)
            avg_y = sum(pt[1] for pt in pts) / len(pts)
            br = items[4].boundingRect()
            items[4].setPos(avg_x - br.width() / 2, avg_y - br.height() / 2)

    # ── Redraw ─────────────────────────────────────────────────────────────
    def _redraw_overlay(self):
        """Remove all overlay items (keep background) and redraw."""
        # Clear edit handles first (they'll be re-added if needed)
        self._clear_edit_handles()
        # Selection outlines are about to be wiped by the loop below; clear our
        # tracking list so we don't keep stale references to removed items.
        self._selection_items = []

        bg_set = set(self._bg_items) if self._bg_items else (
            {self._bg_item} if self._bg_item is not None else set())
        for item in list(self._scene.items()):
            if item not in bg_set:
                self._scene.removeItem(item)

        # Reset item tracking lists
        self._entrance_items = []
        self._apt_type_items = []

        for i in range(len(self.entrances)):
            self._entrance_items.append([])
            self._draw_entrance(i)
        for i in range(len(self.apt_type_polygons)):
            self._apt_type_items.append([])
            self._draw_apt_type(i)

        # Apply saved visibility to newly drawn items (before rebuilding checkboxes)
        _vis = self._make_vis_fn()
        for i, items in enumerate(self._entrance_items):
            if not _vis("entrance", i):
                for item in items:
                    item.setVisible(False)
        for i, items in enumerate(self._apt_type_items):
            if not _vis("apt_type", i):
                for item in items:
                    item.setVisible(False)

        self._layers_panel.rebuild(
            self.entrances, self.apt_type_polygons,
            self._get_type_color, visibility_fn=_vis)

        # Re-apply multi-selection outlines (also drops indices that no
        # longer exist after a delete/clear).
        self._redraw_selection_outlines()

    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear All",
            "Remove all calibration data (scale, north, all entrances)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.scale_px_per_m = None
            self.north_angle_deg = 0.0
            self.entrances.clear()
            self.apt_type_polygons = []
            self._type_color_map = {}
            self._visibility_state = {}
            self._cancel_drawing()
            self._selected_idx = None
            self._selected_type = None
            self._multi_selection = []
            self._clear_selection_outlines()
            if self.image_path:
                self._render_image()
            self._set_status("Cleared.")
            self._emit()

    # ── Save / Load ────────────────────────────────────────────────────────
    def _save_calibration(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Calibration",
            self._cal_path or "plan_calibration.json",
            "Calibration JSON (*.json)")
        if not path:
            return
        data = {
            "image_path":        self.image_path,
            "scale_px_per_m":    self.scale_px_per_m,
            "north_angle_deg":   self.north_angle_deg,
            "entrances":         self.entrances,
            "apt_type_polygons": self.apt_type_polygons,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._cal_path = path
        self._set_status(f"Saved: {os.path.basename(path)}")

    def _load_calibration(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Calibration",
            filter="Calibration JSON (*.json);;All Files (*)")
        if not path:
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._cal_path        = path
        self.scale_px_per_m   = data.get("scale_px_per_m")
        self.north_angle_deg  = data.get("north_angle_deg", 0.0)
        self.entrances        = data.get("entrances", [])
        self.apt_type_polygons = data.get("apt_type_polygons", [])

        # Rebuild color map from loaded polygons (Feature 1 fix)
        self._type_color_map = {}
        for p in self.apt_type_polygons:
            self._get_type_color(p["type_name"])

        img = data.get("image_path")
        try:
            if img and os.path.exists(img):
                self.image_path = img
                self._render_image()
            else:
                self._redraw_overlay()
            self._layers_panel.rebuild(self.entrances, self.apt_type_polygons, self._get_type_color)
        except Exception as exc:
            QMessageBox.warning(self, "Load Error",
                                f"Calibration loaded but failed to render:\n{exc}")

        _n_backfilled = 0
        if self.scale_px_per_m:
            _n_backfilled = self._backfill_world_coords_all()
            if _n_backfilled:
                self._redraw_overlay()
                self._layers_panel.rebuild(
                    self.entrances, self.apt_type_polygons,
                    self._get_type_color)

        _msg = f"Loaded: {os.path.basename(path)}"
        if _n_backfilled:
            _msg += f"  — back-filled {_n_backfilled} polygon(s)"
        self._set_status(_msg)
        self._emit()

    # ── Public API ─────────────────────────────────────────────────────────
    def set_building_ids(self, ids: list[str]):
        self.building_ids = sorted(set(str(i) for i in ids))

    def set_apt_types(self, types: list[str]):
        self.apt_types_list = sorted(set(str(t) for t in types))

    def set_entrance_ids(self, ids: list[str]):
        self.entrance_ids_all = sorted(set(str(i) for i in ids))

    def get_calibration(self) -> dict:
        return {
            "scale_px_per_m":    self.scale_px_per_m,
            "north_angle_deg":   self.north_angle_deg,
            "entrances":         self.entrances,
            "apt_type_polygons": self.apt_type_polygons,
        }

    # ── Auto-Place ─────────────────────────────────────────────────────────
    # ── Direction parsing ───────────────────────────────────────────────────
    @staticmethod
    def _parse_dir_vec(s: str):
        """Return a (dx, dy) unit vector for a direction string.

        Supports Hebrew (צפון/דרום/מזרח/מערב) and English (North/South/East/West),
        compound words (northeast / צפון-מזרח), ו-conjunction prefix stripping,
        and slash/hyphen separators.  Canvas Y-axis points down, so North → (0,-1).

        Returns (0.0, 0.0) when the string is empty or unrecognisable.
        """
        import math as _math

        _DV = {
            # Hebrew
            "צפון": (0.0, -1.0), "דרום": (0.0, 1.0),
            "מזרח": (1.0,  0.0), "מערב": (-1.0, 0.0),
            # English – full
            "north": (0.0, -1.0), "south": (0.0, 1.0),
            "east":  (1.0,  0.0), "west":  (-1.0, 0.0),
            # English – abbreviated
            "n": (0.0, -1.0), "s": (0.0, 1.0),
            "e": (1.0,  0.0), "w": (-1.0, 0.0),
            "ne": (1.0, -1.0), "nw": (-1.0, -1.0),
            "se": (1.0,  1.0), "sw": (-1.0,  1.0),
        }

        # Tokenise: split on spaces, hyphens, slashes, commas
        import re as _re
        tokens = _re.split(r"[\s\-/,]+", str(s).strip())
        dx = dy = 0.0
        hits = 0
        for tok in tokens:
            # Strip Hebrew ו-conjunction prefix (e.g. "וצפון" → "צפון")
            tok_h = tok.lstrip("ו")
            # Try lowercase English
            tok_l = tok.lower()
            for candidate in (tok_h, tok_l):
                if candidate in _DV:
                    vx, vy = _DV[candidate]
                    dx += vx
                    dy += vy
                    hits += 1
                    break

        if hits == 0:
            return (0.0, 0.0)
        ln = _math.sqrt(dx * dx + dy * dy)
        return (dx / ln, dy / ln) if ln > 1e-9 else (0.0, 0.0)

    def do_auto_place(self, combos: list[tuple], params: dict):
        """Generate a square bounding-box polygon for every (building, entrance, type) combo.

        combos  : list of (building_id, entrance_id, type_name, direction) 4-tuples
        params  : {area_m2, type_gap_m, entrance_gap_m, building_gap_m,
                   dir_spread_m, extrusion_m}

        Layout hierarchy (outer → inner):
          Building  — spread along X, separated by building_gap_m
          Entrance  — stacked along Y within each building, separated by entrance_gap_m
          Direction — each direction gets a sub-cluster offset from the entrance centre
                      by dir_spread_m in the compass direction
          Type      — spread along the axis perpendicular to the direction vector,
                      centred on the direction sub-cluster
        """
        import math as _math
        from collections import defaultdict as _dd

        side       = _math.sqrt(max(params["area_m2"], 1.0))
        type_gap   = params["type_gap_m"]
        ent_gap    = params["entrance_gap_m"]
        bld_gap    = params["building_gap_m"]
        dir_spread = params.get("dir_spread_m", 50.0)
        extrusion  = params["extrusion_m"]
        scale      = self.scale_px_per_m or 10.0   # px / m (fallback when no image)

        # Group: building → entrance → direction → [types]
        bld_ent_dir: dict = _dd(lambda: _dd(lambda: _dd(list)))
        for combo in combos:
            b, e, t = combo[0], combo[1], combo[2]
            d = combo[3] if len(combo) == 4 else ""
            if t not in bld_ent_dir[b][e][d]:
                bld_ent_dir[b][e][d].append(t)

        buildings = sorted(bld_ent_dir)

        # ── Estimate per-building width (X-axis) ──────────────────────────
        # Each entrance contributes a width = sum of all its type counts * (side+type_gap)
        # plus room on both sides for the direction spread.
        def _ent_width(ent_dirs):
            total = sum(len(ts) for ts in ent_dirs.values())
            return max(total * (side + type_gap), side) + 2 * dir_spread

        bld_cx: dict[str, float] = {}
        x_cursor = 0.0
        for b in buildings:
            max_w = max(_ent_width(ent_dirs) for ent_dirs in bld_ent_dir[b].values())
            bld_cx[b] = x_cursor + max_w / 2
            x_cursor += max_w + bld_gap

        # ── Entrance Y spacing ─────────────────────────────────────────────
        # Enough room for a full direction spread in ±Y.
        ent_row_h = 2 * (dir_spread + side) + ent_gap

        new_polys = []

        for b in buildings:
            cx_bld = bld_cx[b]
            entrances = sorted(bld_ent_dir[b])
            # Centre entrances vertically around 0 within the building
            n_ent = len(entrances)
            ent_y_start = -((n_ent - 1) * ent_row_h) / 2.0

            for e_idx, e in enumerate(entrances):
                cy_ent = ent_y_start + e_idx * ent_row_h   # entrance anchor Y

                dirs = bld_ent_dir[b][e]
                dir_keys = sorted(dirs)

                # Distribute direction sub-clusters along X if they share the same
                # direction vector (so identical-direction types don't overlap).
                # For each unique vector, group all directions that map to it.
                vec_groups: dict = _dd(list)  # (dvx_r, dvy_r) → [dir_keys]
                for dk in dir_keys:
                    dvx, dvy = self._parse_dir_vec(dk)
                    # Round to 2 dp so near-identical vectors merge
                    vec_groups[(round(dvx, 2), round(dvy, 2))].append(dk)

                for (dvx, dvy), dk_list in vec_groups.items():
                    has_dir = (dvx != 0.0 or dvy != 0.0)
                    # Perpendicular axis for type spread
                    perp_x, perp_y = (-dvy, dvx) if has_dir else (1.0, 0.0)

                    # Collect all types for this direction vector
                    all_types: list[tuple] = []  # (dir_key, type_name)
                    for dk in sorted(dk_list):
                        for t in sorted(dirs[dk]):
                            all_types.append((dk, t))

                    n_t = len(all_types)
                    # Direction sub-cluster centre
                    sc_x = cx_bld + dvx * dir_spread
                    sc_y = cy_ent + dvy * dir_spread

                    for i, (dk, t) in enumerate(all_types):
                        # Spread types along the perpendicular axis, centred
                        perp_offset = (i - (n_t - 1) / 2.0) * (side + type_gap)
                        cx_m = sc_x + perp_x * perp_offset
                        cy_m = sc_y + perp_y * perp_offset

                        hw = side / 2
                        poly_world = [
                            (cx_m - hw, cy_m - hw),
                            (cx_m + hw, cy_m - hw),
                            (cx_m + hw, cy_m + hw),
                            (cx_m - hw, cy_m + hw),
                        ]
                        poly_img = [(px * scale, py * scale) for px, py in poly_world]

                        new_polys.append({
                            "building_id":     b,
                            "entrance_id":     e,
                            "type_name":       t,
                            "direction":       dk,
                            "uid":             _uuid_mod.uuid4().hex[:10],
                            "extrusion_m":     extrusion,
                            "polygon_img":     poly_img,
                            "center_img":      (cx_m * scale, cy_m * scale),
                            "world_x_m":       round(cx_m, 3),
                            "world_y_m":       round(cy_m, 3),
                            "polygon_world_m": [(round(px, 4), round(py, 4))
                                                for px, py in poly_world],
                            "color_hex":       self._get_type_color(t).name(),
                        })

        # ── Deduplication guardrail ────────────────────────────────────────
        # Key: (building_id, entrance_id, type_name) must be unique.
        # Existing manually-positioned polygons are kept; auto-placed ones are
        # only added if no polygon with the same key already exists.
        existing_keys = {
            (p["building_id"], p["entrance_id"], p["type_name"])
            for p in self.apt_type_polygons
        }
        added = 0
        for poly in new_polys:
            key = (poly["building_id"], poly["entrance_id"], poly["type_name"])
            if key not in existing_keys:
                self.apt_type_polygons.append(poly)
                existing_keys.add(key)
                added += 1

        if self.scale_px_per_m is None:
            self.scale_px_per_m = scale

        self._redraw_overlay()
        self._layers_panel.rebuild(
            self.entrances, self.apt_type_polygons, self._get_type_color)
        self._emit()
        skipped = len(new_polys) - added
        msg = f"Auto-placed {added} polygon(s)."
        if skipped:
            msg += f"  ({skipped} already existed — skipped)"
        self._set_status(msg)

    # ── Transform handles ──────────────────────────────────────────────────
    def _clear_transform_handles(self):
        for _item in self._xform_handles:
            if _item.scene():
                self._scene.removeItem(_item)
        self._xform_handles.clear()
        self._xform_target = None
        self._xform_drag = None
        self._xform_orig_pts = None
        self._xform_orig_center = None
        self._xform_bbox = None

    def _show_transform_handles(self, kind: str, idx: int):
        self._clear_transform_handles()
        _p = self.apt_type_polygons[idx] if kind == "apt_type" else self.entrances[idx]
        _pts = _p["polygon_img"]
        _xs = [pt[0] for pt in _pts]; _ys = [pt[1] for pt in _pts]
        _minx, _miny = min(_xs), min(_ys)
        _maxx, _maxy = max(_xs), max(_ys)
        _cx = (_minx + _maxx) / 2; _cy = (_miny + _maxy) / 2
        self._xform_target = (kind, idx)
        self._xform_bbox = (_minx, _miny, _maxx, _maxy)
        self._xform_orig_pts = list(_pts)
        self._xform_orig_center = _p["center_img"]

        _items = []
        _hs = 8  # handle half-size

        # Dashed bounding box
        _bb = self._scene.addRect(
            _minx, _miny, _maxx - _minx, _maxy - _miny,
            QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine))
        _bb.setZValue(20); _items.append(_bb)

        # Corner scale handles
        for _tag, (_hx, _hy) in [
            ('tl', (_minx, _miny)), ('tr', (_maxx, _miny)),
            ('bl', (_minx, _maxy)), ('br', (_maxx, _maxy)),
        ]:
            _h = self._scene.addRect(
                _hx - _hs, _hy - _hs, _hs * 2, _hs * 2,
                QPen(QColor("#222222"), 1.5), QBrush(QColor("#ffffff")))
            _h.setZValue(21); _h._xform_tag = _tag; _items.append(_h)

        # Rotation handle (yellow circle above top-center)
        _rot_y = _miny - 22
        _rl = self._scene.addLine(
            _cx, _miny, _cx, _rot_y,
            QPen(QColor("#ffffff"), 1))
        _rl.setZValue(20); _items.append(_rl)
        _rh = self._scene.addRect(
            _cx - _hs, _rot_y - _hs, _hs * 2, _hs * 2,
            QPen(QColor("#222222"), 1.5), QBrush(QColor("#ffd93d")))
        _rh.setZValue(21); _rh._xform_tag = 'rot'; _items.append(_rh)

        self._xform_handles = _items

    # ── Backfill world coords for polygons saved without scale ─────────────
    # ── Georef mode ──────────────────────────────────────────────────────────
    def _open_georef_panel(self) -> None:
        """Create (lazily) and show the floating georef panel. Wires all its
        signals back to the canvas."""
        if self._georef_panel is None:
            panel = _GeorefPanel(self)
            panel.request_undo_last.connect(self._georef_undo_last)
            panel.request_clear_all.connect(self._georef_clear_all)
            panel.request_remove_row.connect(self._georef_remove_row)
            panel.apply_requested.connect(self._georef_apply)
            panel.closed.connect(self._on_georef_panel_closed)
            self._georef_panel = panel
        self._georef_panel.set_old_calibration(
            self.scale_px_per_m, self.north_angle_deg)
        self._georef_panel.refresh(self._georef_pairs)
        self._redraw_georef_pins()
        self._georef_panel.show()
        self._georef_panel.raise_()
        self._georef_panel.activateWindow()

    def _close_georef_panel(self) -> None:
        """Hide (don't destroy — pairs survive in self._georef_pairs) and clear pins."""
        if self._georef_panel is not None:
            # Block the 'closed' signal so we don't re-enter _set_mode.
            self._georef_panel.blockSignals(True)
            self._georef_panel.close()
            self._georef_panel.blockSignals(False)
        self._georef_clear_pins()

    def _on_georef_panel_closed(self) -> None:
        """Panel was closed by the user (X button). Kick out of georef mode."""
        if self._mode == "georef":
            self._set_mode("select")

    def _georef_handle_click(self, pos: QPointF) -> None:
        """Left-click in georef mode → UE-location popup → new correspondence."""
        px, py = pos.x(), pos.y()
        dlg = _UELocationDialog(self, px, py, existing_count=len(self._georef_pairs))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status(f"Georef pick cancelled at ({px:.1f}, {py:.1f})")
            return
        parsed = dlg.result_value()
        if parsed is None:
            return
        ux, uy, uz = parsed
        label = f"#{len(self._georef_pairs) + 1}"
        self._georef_pairs.append(_GeorefPair(
            px=px, py=py, ux_cm=ux, uy_cm=uy, uz_cm=uz, label=label))
        self._redraw_georef_pins()
        if self._georef_panel is not None:
            self._georef_panel.refresh(self._georef_pairs)
        self._set_status(
            f"Georef {label}: pixel ({px:.1f}, {py:.1f}) ↔ "
            f"UE ({ux:.1f}, {uy:.1f}, {uz:.1f}) cm — "
            f"{len(self._georef_pairs)} pair(s) total")

    def _georef_undo_last(self) -> None:
        if not self._georef_pairs:
            return
        self._georef_pairs.pop()
        self._redraw_georef_pins()
        if self._georef_panel is not None:
            self._georef_panel.refresh(self._georef_pairs)
        self._set_status("Georef: removed last correspondence")

    def _georef_remove_row(self, row: int) -> None:
        if 0 <= row < len(self._georef_pairs):
            del self._georef_pairs[row]
            for i, p in enumerate(self._georef_pairs):
                p.label = f"#{i + 1}"
            self._redraw_georef_pins()
            if self._georef_panel is not None:
                self._georef_panel.refresh(self._georef_pairs)
            self._set_status(f"Georef: removed row {row + 1}")

    def _georef_clear_all(self) -> None:
        self._georef_pairs.clear()
        self._redraw_georef_pins()
        if self._georef_panel is not None:
            self._georef_panel.refresh(self._georef_pairs)
        self._set_status("Georef: cleared all correspondences")

    def _redraw_georef_pins(self) -> None:
        """Re-render the pin/label items on the scene from self._georef_pairs."""
        self._georef_clear_pins()
        if not self._bg_item:
            return  # no image loaded → nothing to anchor to
        pen = QPen(QColor("white"), 2)
        brush = QBrush(QColor("#ff5555"))
        r = 8.0
        for p in self._georef_pairs:
            pin = self._scene.addEllipse(
                p.px - r, p.py - r, 2 * r, 2 * r, pen, brush)
            pin.setZValue(200)
            self._georef_pin_items.append(pin)
            txt = self._scene.addSimpleText(p.label or "")
            txt.setBrush(QBrush(QColor("white")))
            f = QFont()
            f.setPointSize(12)
            f.setBold(True)
            txt.setFont(f)
            txt.setPos(p.px + r + 2, p.py - r - 6)
            txt.setZValue(201)
            self._georef_pin_items.append(txt)

    def _georef_clear_pins(self) -> None:
        for it in self._georef_pin_items:
            try:
                self._scene.removeItem(it)
            except Exception:
                pass
        self._georef_pin_items.clear()

    def _georef_apply(self, fit) -> None:
        """Panel clicked 'Apply' — write the corrected scale + rotation into
        the canvas state and force-re-stamp every committed polygon's world
        coords from the new calibration."""
        old_scale = self.scale_px_per_m
        old_rot   = self.north_angle_deg
        self.scale_px_per_m = float(fit.scale_px_per_m)
        self.north_angle_deg = float(fit.rotation_deg)

        # Force-restamp: old world coords were based on the wrong scale.
        n = self._backfill_world_coords_all(force=True)

        # Keep the panel open but refresh the "old" values so the user
        # sees that the calibration now matches their fit.
        if self._georef_panel is not None:
            self._georef_panel.set_old_calibration(
                self.scale_px_per_m, self.north_angle_deg)

        self._redraw_overlay()
        self._layers_panel.rebuild(
            self.entrances, self.apt_type_polygons, self._get_type_color)

        msg = (
            f"Georef applied: scale {old_scale!s} → "
            f"{self.scale_px_per_m:.4f} px/m, "
            f"rot {old_rot:.2f}° → {self.north_angle_deg:.2f}°, "
            f"re-stamped {n} polygon(s). RMS = {fit.rms_cm:.2f} cm.")
        self._set_status(msg)
        QMessageBox.information(self, "Georef applied", msg +
            "\n\nNow go to the Output tab and regenerate the volume script "
            "to export with the corrected calibration.")

    def _backfill_world_coords_all(self, force: bool = False) -> int:
        """Stamp world_x_m/world_y_m/polygon_world_m onto every polygon that
        is missing them, using the current self.scale_px_per_m. Idempotent —
        polygons already carrying valid polygon_world_m (>=3 pts) are skipped
        *unless* ``force=True``, in which case all polygons are re-stamped
        from their pixel coords (used after a georef Apply that changes the
        scale retroactively).

        Returns the number of polygons that were updated.
        """
        s = self.scale_px_per_m
        if not s:
            return 0
        n = 0
        for p in self.apt_type_polygons:
            if (not force
                    and isinstance(p.get("polygon_world_m"), list)
                    and len(p["polygon_world_m"]) >= 3
                    and "world_x_m" in p):
                continue
            pts = p.get("polygon_img") or []
            if len(pts) < 3:
                continue
            cx = sum(pt[0] for pt in pts) / len(pts)
            cy = sum(pt[1] for pt in pts) / len(pts)
            p["center_img"] = (cx, cy)
            p["world_x_m"] = round(cx / s, 3)
            p["world_y_m"] = round(cy / s, 3)
            p["polygon_world_m"] = [
                (round(px / s, 4), round(py / s, 4)) for px, py in pts
            ]
            z_cm = round(p.get("extrusion_m", 3.0) / 2.0 * 100, 1)
            for cam in _migrate_balcony_cams(p):
                if "img_x" in cam and "img_y" in cam:
                    cam["world_x_m"] = round(cam["img_x"] / s, 3)
                    cam["world_y_m"] = round(cam["img_y"] / s, 3)
                    cam["z_cm"] = z_cm
            p["committed"] = True
            n += 1
        theta = math.radians(self.north_angle_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        for ent in self.entrances:
            if not force and "world_x_m" in ent:
                continue
            pts = ent.get("polygon_img") or []
            if len(pts) < 3:
                continue
            cx = sum(pt[0] for pt in pts) / len(pts)
            cy = sum(pt[1] for pt in pts) / len(pts)
            ent["center_img"] = (cx, cy)
            ent["world_x_m"] = round((cx * cos_t + cy * sin_t) / s, 3)
            ent["world_y_m"] = round((-cx * sin_t + cy * cos_t) / s, 3)
            n += 1
        return n

    # ── Commit polygon → recalculate camera attributes ───────────────────
    def _commit_polygon(self, kind: str, idx: int):
        """Recalculate all derived world coords for polygon *and* its cameras."""
        if kind != "apt_type":
            return
        p = self.apt_type_polygons[idx]
        pts = p["polygon_img"]
        cx = sum(pt[0] for pt in pts) / len(pts)
        cy = sum(pt[1] for pt in pts) / len(pts)
        p["center_img"] = (cx, cy)
        if self.scale_px_per_m:
            s = self.scale_px_per_m
            p["world_x_m"] = round(cx / s, 3)
            p["world_y_m"] = round(cy / s, 3)
            p["polygon_world_m"] = [
                (round(px / s, 4), round(py / s, 4))
                for px, py in pts]
            z_cm = round(p.get("extrusion_m", 3.0) / 2.0 * 100, 1)
            for cam in _migrate_balcony_cams(p):
                cam["world_x_m"] = round(cam["img_x"] / s, 3)
                cam["world_y_m"] = round(cam["img_y"] / s, 3)
                cam["z_cm"] = z_cm
        p["committed"] = True
        self._redraw_overlay()
        self._emit()

    def _commit_selected(self):
        """Commit the currently selected (or last interacted) apt polygon."""
        if (self._selected_idx is not None
                and self._selected_type == "apt_type"
                and 0 <= self._selected_idx < len(self.apt_type_polygons)):
            self._commit_polygon("apt_type", self._selected_idx)
            p = self.apt_type_polygons[self._selected_idx]
            self._set_status(
                f"✓  Committed {p['type_name']} "
                f"({p['building_id']}/E{p['entrance_id']}) — "
                f"cameras synced")
            return
        self._set_status("⚠  Select an apartment polygon first, then commit.")

    def _commit_all(self):
        """Recalculate world coords + camera params for every apt polygon."""
        if not self.scale_px_per_m:
            self._set_status("⚠  Set the scale (📏) first.")
            return
        n = 0
        for i in range(len(self.apt_type_polygons)):
            self._commit_polygon("apt_type", i)
            n += 1
        self._set_status(f"✓  Committed {n} polygon(s) — cameras synced")

    # ── Select all apt polygons (Ctrl+A) ──────────────────────────────────
    def _select_all_apt(self):
        """Multi-select every apt_type polygon. Pair with H to bulk-set height.

        Entrances and balcony cameras are intentionally not included — the
        main Ctrl+A use case is bulk height, which only applies to apt
        polygons. If the canvas is empty, status-bar the user instead of
        silently doing nothing.
        """
        if not self.apt_type_polygons:
            self._set_status("⚠  No apartment polygons to select.")
            return
        items = [("apt_type", i) for i in range(len(self.apt_type_polygons))]
        self._set_multi_selection(items)
        self._set_status(
            f"↖  Selected all {len(items)} apt polygon(s). "
            "Press H or click '↕ Set Height' to bulk-edit height.")

    # ── Bulk set extrusion height ─────────────────────────────────────────
    def _bulk_set_height(self):
        """Open a dialog to set extrusion_m on every selected apt polygon.

        Target set:
          - base targets = all currently selected apt_type polygons
          - if the user ticks "Apply to every polygon of this type", the
            targets expand to every polygon matching type_name of the first
            selected polygon (order preserved, no duplicates).

        Polygons are marked uncommitted so the '*' badge reappears.
        """
        sel_idxs = [idx for (k, idx) in self._multi_selection if k == "apt_type"]
        if (not sel_idxs and self._selected_type == "apt_type"
                and self._selected_idx is not None):
            sel_idxs = [self._selected_idx]
        sel_idxs = [i for i in sel_idxs if 0 <= i < len(self.apt_type_polygons)]
        sel_idxs = list(dict.fromkeys(sel_idxs))
        if not sel_idxs:
            self._set_status(
                "⚠  Select at least one apartment polygon before bulk-setting height.")
            return

        sel_polys = [self.apt_type_polygons[i] for i in sel_idxs]
        first_type = sel_polys[0].get("type_name", "")
        n_of_type = sum(1 for p in self.apt_type_polygons
                        if p.get("type_name", "") == first_type)
        heights = {float(p.get("extrusion_m", 3.0)) for p in sel_polys}
        prefill = heights.pop() if len(heights) == 1 else 3.0

        dlg = _BulkHeightDialog(
            self,
            prefill=prefill,
            n_selected=len(sel_idxs),
            type_name=first_type,
            n_of_type=n_of_type,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_h, apply_to_type = dlg.result_values()

        if apply_to_type and first_type:
            target_idxs = [i for i, p in enumerate(self.apt_type_polygons)
                           if p.get("type_name", "") == first_type]
        else:
            target_idxs = sel_idxs

        n = 0
        for i in target_idxs:
            p = self.apt_type_polygons[i]
            if float(p.get("extrusion_m", 3.0)) == new_h:
                continue
            p["extrusion_m"] = new_h
            p["committed"] = False
            n += 1
        if n == 0:
            self._set_status(
                f"Height already {new_h:g} m on all targeted polygons — no change.")
            return
        self._redraw_overlay()
        self._emit()
        scope = (f"all {len(target_idxs)} '{first_type}' polygon(s)"
                 if apply_to_type and first_type
                 else f"{n} selected polygon(s)")
        self._set_status(f"↕  Set height to {new_h:g} m on {scope}.")

    # ── Helpers ────────────────────────────────────────────────────────────
    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)

    def _emit(self):
        self.calibration_changed.emit(self.get_calibration())

    # ── External AI-import entry points ────────────────────────────────
    @property
    def canvas_image_size(self) -> tuple[int, int]:
        """Width/height of the currently loaded floor-plan image in pixels.

        Returns (0, 0) if no image has been loaded yet. Used by the AI Import
        tab to scale proportional (0..1) coords to pixel-space polygon_img.
        """
        if self._bg_item is None:
            return (0, 0)
        # For multi-page PDFs, return the stacked (max_width, total_height).
        # For single images, this equals the pixmap size.
        if self._bg_size_px != (0, 0):
            return self._bg_size_px
        pix = self._bg_item.pixmap()
        return (pix.width(), pix.height())

    def add_pending_polygons(self, polygons: list[dict]) -> int:
        """Append AI-detected (or otherwise externally-supplied) polygons to
        the apt-type list.

        Each dict must provide:
          - building_id, entrance_id, type_name, polygon_img (list[(x,y)])
        Optional: uid, extrusion_m, center_img, committed, source, ai_label.

        World coords are back-filled automatically if a scale is set. Polygons
        start uncommitted so the orange '*' badge appears until the user
        positions them and clicks Commit.
        """
        import uuid as _uuid_mod
        if not polygons:
            return 0
        added = 0
        for p in polygons:
            pts = p.get("polygon_img") or []
            if len(pts) < 3:
                continue
            entry = dict(p)
            entry.setdefault("uid", _uuid_mod.uuid4().hex[:10])
            entry.setdefault("extrusion_m", 3.0)
            entry.setdefault("building_id", "1")
            entry.setdefault("entrance_id", "1")
            entry.setdefault("type_name", "APT")
            entry.setdefault("committed", False)
            cx = sum(pt[0] for pt in pts) / len(pts)
            cy = sum(pt[1] for pt in pts) / len(pts)
            entry["center_img"] = (cx, cy)
            entry.setdefault(
                "color_hex", self._get_type_color(entry["type_name"]).name())
            if self.scale_px_per_m:
                s = self.scale_px_per_m
                entry["world_x_m"] = round(cx / s, 3)
                entry["world_y_m"] = round(cy / s, 3)
                entry["polygon_world_m"] = [
                    (round(px / s, 4), round(py / s, 4)) for px, py in pts
                ]
            self.apt_type_polygons.append(entry)
            added += 1

        if added:
            self._redraw_overlay()
            self._emit()
            self._set_status(
                f"Added {added} AI-detected polygon(s). "
                "Use the Move tool to drag into position, then Commit All.")
        return added


# ── Custom Graphics View ────────────────────────────────────────────────────
class _PlanView(QGraphicsView):
    """Zoom (wheel) + pan (middle-click) view with forwarded mouse signals."""

    sig_press      = pyqtSignal(QPointF)
    sig_ctrl_press = pyqtSignal(QPointF)   # Ctrl + left-click
    sig_alt_press  = pyqtSignal(QPointF)   # Alt  + left-click
    sig_move    = pyqtSignal(QPointF)
    sig_release = pyqtSignal(QPointF)
    sig_double  = pyqtSignal(QPointF)
    sig_delete     = pyqtSignal()
    sig_undo       = pyqtSignal()
    sig_commit     = pyqtSignal()
    sig_escape     = pyqtSignal()
    sig_set_height = pyqtSignal()
    sig_select_all = pyqtSignal()

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._pan_last = None          # QPoint when middle-button panning
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#2b2b2b")))

    def wheelEvent(self, e):
        factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.sig_delete.emit()
        elif (e.key() == Qt.Key.Key_Z and
              e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.sig_undo.emit()
        elif (e.key() == Qt.Key.Key_S and
              e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.sig_commit.emit()
        elif (e.key() == Qt.Key.Key_A and
              e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.sig_select_all.emit()
        elif (e.key() == Qt.Key.Key_H and
              not (e.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                    Qt.KeyboardModifier.AltModifier))):
            self.sig_set_height.emit()
        elif e.key() == Qt.Key.Key_Escape:
            self.sig_escape.emit()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self._pan_last = e.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
            return
        elif e.button() == Qt.MouseButton.LeftButton:
            mods = e.modifiers()
            scene_pos = self.mapToScene(e.pos())
            if mods & Qt.KeyboardModifier.ControlModifier:
                self.sig_ctrl_press.emit(scene_pos)
            elif mods & Qt.KeyboardModifier.AltModifier:
                self.sig_alt_press.emit(scene_pos)
            else:
                self.sig_press.emit(scene_pos)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self._pan_last = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            e.accept()
            return
        elif e.button() == Qt.MouseButton.LeftButton:
            self.sig_release.emit(self.mapToScene(e.pos()))
        super().mouseReleaseEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan_last is not None:
            delta = e.pos() - self._pan_last
            self._pan_last = e.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            e.accept()
            return
        self.sig_move.emit(self.mapToScene(e.pos()))
        super().mouseMoveEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.sig_double.emit(self.mapToScene(e.pos()))
        super().mouseDoubleClickEvent(e)


# ── Apt Type Dialog ──────────────────────────────────────────────────────────
class AptTypeDialog(QDialog):
    def __init__(self, parent, building_ids: list[str], entrance_ids: list[str],
                 apt_types: list[str], prefill: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Apartment Type" if prefill else "Assign Apartment Type")
        self.setModal(True)
        self.setMinimumWidth(300)
        layout = QFormLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._type = QComboBox()
        self._type.setEditable(True)
        self._type.addItems(apt_types)
        if prefill and prefill.get("type_name"):
            self._type.setCurrentText(prefill["type_name"])
        layout.addRow("Type name:", self._type)

        self._bld = QComboBox()
        self._bld.setEditable(True)
        self._bld.addItems(building_ids)
        if prefill and prefill.get("building_id"):
            self._bld.setCurrentText(prefill["building_id"])
        layout.addRow("Building:", self._bld)

        self._ent = QComboBox()
        self._ent.setEditable(True)
        self._ent.addItems(entrance_ids)
        if prefill and prefill.get("entrance_id"):
            self._ent.setCurrentText(prefill["entrance_id"])
        layout.addRow("Entrance:", self._ent)

        self._height = QDoubleSpinBox()
        self._height.setRange(0.1, 99.0)
        self._height.setValue(prefill["extrusion_m"] if prefill else 3.0)
        self._height.setSuffix("  m")
        self._height.setDecimals(2)
        layout.addRow("Extrusion height:", self._height)

        btn = QPushButton("Confirm  ↵")
        btn.setDefault(True)
        btn.clicked.connect(self.accept)
        layout.addRow(btn)

    def result_values(self) -> tuple[str, str, str, float]:
        return (
            self._bld.currentText().strip(),
            self._ent.currentText().strip(),
            self._type.currentText().strip(),
            self._height.value(),
        )


# ── Bulk Height Dialog ───────────────────────────────────────────────────────
class _BulkHeightDialog(QDialog):
    """Dialog for bulk-setting the extrusion height of multiple apt polygons.

    Shows how many polygons are currently selected, offers a single height
    spinner, and an optional "Apply to every polygon of type '<name>'"
    checkbox that expands the target set to every polygon sharing the first
    selected polygon's type_name. Useful for re-heighting all apartments of
    a given type (e.g. every 'Type A' unit to 3.2 m) without Ctrl-clicking
    each one.
    """

    def __init__(self, parent, prefill: float, n_selected: int,
                 type_name: str, n_of_type: int):
        super().__init__(parent)
        self.setWindowTitle("Set Extrusion Height")
        self.setModal(True)
        self.setMinimumWidth(320)

        layout = QFormLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(f"Selected: {n_selected} apt polygon(s).")
        info.setStyleSheet("color:#888;")
        layout.addRow(info)

        self._height = QDoubleSpinBox()
        self._height.setRange(0.1, 99.0)
        self._height.setValue(float(prefill))
        self._height.setSuffix("  m")
        self._height.setDecimals(2)
        self._height.setSingleStep(0.1)
        layout.addRow("New height:", self._height)

        self._apply_to_type = QCheckBox(
            f"Apply to every polygon of type '{type_name}' ({n_of_type} total)"
            if type_name else "Apply to every polygon of the same type"
        )
        self._apply_to_type.setChecked(False)
        if not type_name or n_of_type <= n_selected:
            self._apply_to_type.setEnabled(False)
        layout.addRow(self._apply_to_type)

        btns = QHBoxLayout()
        ok = QPushButton("Apply  ↵")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addRow(btns)

    def result_values(self) -> tuple[float, bool]:
        return self._height.value(), self._apply_to_type.isChecked()
