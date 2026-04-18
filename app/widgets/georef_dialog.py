"""
Georef calibration — math + dialogs for the main placer canvas.

Usage from plan_canvas.py
-------------------------
    from app.widgets.georef_dialog import (
        GeorefPanel, UELocationDialog, parse_ue_location, fit_similarity,
    )

The canvas holds a list of `Correspondence` rows (plan pixel ↔ UE cm) and shows
the floating `GeorefPanel` while `_mode == "georef"`. Each left-click on the
plan in georef mode pops `UELocationDialog`; if the user pastes a valid UE
`(X=…,Y=…,Z=…)` string the canvas appends a pair and tells the panel to
refresh. The panel's `apply_requested` signal fires when the user clicks
"Apply to current calibration"; the canvas reacts by writing
`scale_px_per_m` / `north_angle_deg` and calling `_backfill_world_coords_all()`.

The math (parser + least-squares similarity fit) is pure Python with no Qt
imports so it's trivially unit-testable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Correspondence:
    """One landmark pair: plan-image pixel ↔ UE world cm."""
    px: float
    py: float
    ux_cm: float
    uy_cm: float
    uz_cm: float = 0.0
    label: str = ""


# ── UE Location parser ─────────────────────────────────────────────────────────

_UE_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_UE_PATTERNS = [
    # (X=123,Y=456,Z=789)  — UE Details panel copy format
    re.compile(
        rf"X\s*=\s*({_UE_NUM})\s*[,; ]+\s*Y\s*=\s*({_UE_NUM})"
        rf"(?:\s*[,; ]+\s*Z\s*=\s*({_UE_NUM}))?",
        re.IGNORECASE),
    # Bare "1240.19, 146.62, 7176.01"  or "1240.19 146.62 7176.01"
    re.compile(
        rf"({_UE_NUM})\s*[,; \t]+\s*({_UE_NUM})(?:\s*[,; \t]+\s*({_UE_NUM}))?"),
]


def parse_ue_location(text: str) -> Optional[tuple[float, float, float]]:
    """Parse X/Y/Z from a UE location clipboard string.

    Returns (x, y, z) in cm, or None if parsing fails. Z defaults to 0.0.
    Accepts UE's native ``(X=1.2,Y=3.4,Z=5.6)`` and bare ``1.2, 3.4, 5.6``.
    """
    if not text:
        return None
    s = text.strip().strip("()[]{}")
    for pat in _UE_PATTERNS:
        m = pat.search(s)
        if m:
            x = float(m.group(1))
            y = float(m.group(2))
            z = float(m.group(3)) if m.group(3) else 0.0
            return (x, y, z)
    return None


# ── Similarity least-squares fit ───────────────────────────────────────────────

@dataclass
class FitResult:
    s_cm_per_px: float
    theta_rad: float
    tx_cm: float
    ty_cm: float
    scale_px_per_m: float
    rotation_deg: float
    rms_cm: float
    per_point_cm: list[float] = field(default_factory=list)
    n_points: int = 0


def fit_similarity(pairs: list[Correspondence]) -> Optional[FitResult]:
    """Closed-form least-squares similarity fit.

    Model: (ux, uy) = s · R(θ) · (px, py) + (tx, ty).
    With a = s·cosθ, b = s·sinθ each pair contributes two linear equations:
        ux = a·px − b·py + tx
        uy = b·px + a·py + ty
    Requires ≥2 non-coincident pairs. Returns None on degenerate input.
    """
    n = len(pairs)
    if n < 2:
        return None

    ATA = [[0.0] * 4 for _ in range(4)]
    ATb = [0.0] * 4

    def _addrow(r: list[float], rhs: float) -> None:
        for i in range(4):
            for j in range(4):
                ATA[i][j] += r[i] * r[j]
            ATb[i] += r[i] * rhs

    for p in pairs:
        _addrow([p.px, -p.py, 1.0, 0.0], p.ux_cm)
        _addrow([p.py,  p.px, 0.0, 1.0], p.uy_cm)

    try:
        a, b, tx, ty = _solve4(ATA, ATb)
    except ValueError:
        return None

    s = math.hypot(a, b)
    if s <= 0.0:
        return None
    theta = math.atan2(b, a)

    per_pt = []
    sse = 0.0
    for p in pairs:
        pred_ux = a * p.px - b * p.py + tx
        pred_uy = b * p.px + a * p.py + ty
        d = math.hypot(p.ux_cm - pred_ux, p.uy_cm - pred_uy)
        per_pt.append(d)
        sse += d * d

    return FitResult(
        s_cm_per_px=s,
        theta_rad=theta,
        tx_cm=tx,
        ty_cm=ty,
        scale_px_per_m=100.0 / s,
        rotation_deg=math.degrees(theta),
        rms_cm=math.sqrt(sse / n),
        per_point_cm=per_pt,
        n_points=n,
    )


def _solve4(A: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan on a 4×5 augmented matrix. Raises ValueError if singular."""
    m = [row[:] + [b[i]] for i, row in enumerate(A)]
    n = 4
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("Singular (collinear points?)")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for k in range(col, n + 1):
            m[col][k] /= pv
        for r in range(n):
            if r == col:
                continue
            f = m[r][col]
            if f == 0.0:
                continue
            for k in range(col, n + 1):
                m[r][k] -= f * m[col][k]
    return [m[i][n] for i in range(n)]


# ── UE Location popup ──────────────────────────────────────────────────────────

class UELocationDialog(QDialog):
    """Modal popup fired after each map click in georef mode."""

    def __init__(self, parent: QWidget, px: float, py: float,
                 existing_count: int):
        super().__init__(parent)
        self.setWindowTitle(f"UE Location for landmark #{existing_count + 1}")
        self.resize(520, 260)

        self._parsed: Optional[tuple[float, float, float]] = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"<b>Map pixel:</b> ({px:.1f}, {py:.1f})<br>"
            f"Paste the UE Location for this landmark. Accepts UE's native "
            f"format, e.g.<br><code>(X=1240.192491,Y=146.628964,Z=7176.018800)</code>"
            f"<br>or a bare triple like <code>1240.19, 146.62, 7176.01</code>."
        ))

        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText(
            "(X=1240.192491,Y=146.628964,Z=7176.018800)")
        self._edit.textChanged.connect(self._on_text_changed)
        lay.addWidget(self._edit)

        self._status = QLabel("Awaiting input…")
        self._status.setStyleSheet("color: #888;")
        lay.addWidget(self._status)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self._btns = btns
        btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        lay.addWidget(btns)

    def _on_text_changed(self) -> None:
        parsed = parse_ue_location(self._edit.toPlainText())
        ok_btn = self._btns.button(QDialogButtonBox.StandardButton.Ok)
        if parsed is None:
            self._parsed = None
            self._status.setText("❌  Could not parse — need X/Y (and optional Z).")
            self._status.setStyleSheet("color: #c44;")
            ok_btn.setEnabled(False)
        else:
            self._parsed = parsed
            x, y, z = parsed
            self._status.setText(
                f"✔ Parsed: X = {x:.3f}, Y = {y:.3f}, Z = {z:.3f} (cm)")
            self._status.setStyleSheet("color: #2a7;")
            ok_btn.setEnabled(True)

    def result_value(self) -> Optional[tuple[float, float, float]]:
        return self._parsed


# ── Floating georef panel ──────────────────────────────────────────────────────

class GeorefPanel(QDialog):
    """Non-modal floating panel listing correspondences + Compute + Apply.

    Signals
    -------
    request_remove_row(int)    → canvas removes that row + its pin.
    request_undo_last()        → canvas removes most-recent pair + pin.
    request_clear_all()        → canvas removes every pair + pin.
    apply_requested(FitResult) → canvas writes scale_px_per_m / north_angle_deg
                                 and runs _backfill_world_coords_all().
    closed()                   → canvas exits georef mode (if still in it).
    """

    request_remove_row = pyqtSignal(int)
    request_undo_last  = pyqtSignal()
    request_clear_all  = pyqtSignal()
    apply_requested    = pyqtSignal(object)   # FitResult
    closed             = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Georef Calibration")
        self.setWindowFlag(Qt.WindowType.Tool, True)   # floats above, no taskbar
        self.resize(520, 640)

        self._pairs: list[Correspondence] = []
        self._last_fit: Optional[FitResult] = None
        self._old_scale: Optional[float] = None
        self._old_rot_deg: float = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        lay.addWidget(QLabel(
            "<b>Georef mode active.</b><br>"
            "<small>Left-click a real-world landmark on the plan image; "
            "paste its UE Location in the popup. Add 2+ landmarks (ideally 3–4) "
            "then Compute + Apply to fix the calibration's scale &amp; rotation."
            "</small>"))

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["#", "Pixel (px, py)", "UE X cm", "UE Y cm"])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._table, stretch=2)

        row = QHBoxLayout()
        self._btn_remove = QPushButton("Remove selected")
        self._btn_remove.clicked.connect(self._on_remove_selected)
        self._btn_undo = QPushButton("Undo last")
        self._btn_undo.clicked.connect(self.request_undo_last.emit)
        self._btn_clear = QPushButton("Clear all")
        self._btn_clear.clicked.connect(self._on_clear_all)
        row.addWidget(self._btn_remove)
        row.addWidget(self._btn_undo)
        row.addWidget(self._btn_clear)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addSpacing(6)

        self._btn_compute = QPushButton("Compute similarity fit  (needs ≥ 2 pairs)")
        self._btn_compute.clicked.connect(self._on_compute)
        self._btn_compute.setEnabled(False)
        lay.addWidget(self._btn_compute)

        self._results = QPlainTextEdit()
        self._results.setReadOnly(True)
        self._results.setPlaceholderText(
            "Add ≥2 correspondences, then click Compute…")
        mono = QFont("Consolas, Courier New, monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._results.setFont(mono)
        lay.addWidget(self._results, stretch=3)

        self._btn_apply = QPushButton("Apply to current calibration")
        self._btn_apply.setStyleSheet(
            "QPushButton { background-color: #2a7; color: white; "
            "padding: 8px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #555; color: #888; }")
        self._btn_apply.clicked.connect(self._on_apply)
        self._btn_apply.setEnabled(False)
        lay.addWidget(self._btn_apply)

    # ── Public API (called by the canvas) ──────────────────────────────────

    def set_old_calibration(self, scale_px_per_m: Optional[float],
                            rot_deg: float) -> None:
        """Record current calibration so Compute can show a before/after delta."""
        self._old_scale = scale_px_per_m
        self._old_rot_deg = rot_deg

    def refresh(self, pairs: list[Correspondence]) -> None:
        """Canvas owns the pair list; panel re-renders whenever it changes."""
        self._pairs = pairs
        self._table.setRowCount(len(pairs))
        for i, p in enumerate(pairs):
            cells = [
                p.label or f"#{i + 1}",
                f"{p.px:.1f}, {p.py:.1f}",
                f"{p.ux_cm:.2f}",
                f"{p.uy_cm:.2f}",
            ]
            for c, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, c, item)
        self._btn_compute.setEnabled(len(pairs) >= 2)
        self._btn_compute.setText(
            f"Compute similarity fit  ({len(pairs)} pair"
            f"{'s' if len(pairs) != 1 else ''})")
        # Invalidate the fit until Compute is clicked again
        self._last_fit = None
        self._btn_apply.setEnabled(False)

    # ── Internal slots ─────────────────────────────────────────────────────

    def _on_remove_selected(self) -> None:
        sel = self._table.selectedIndexes()
        if not sel:
            return
        rows = sorted({idx.row() for idx in sel}, reverse=True)
        for r in rows:
            self.request_remove_row.emit(r)

    def _on_clear_all(self) -> None:
        if not self._pairs:
            return
        if QMessageBox.question(
            self, "Clear all",
            f"Remove all {len(self._pairs)} correspondence(s)?"
        ) == QMessageBox.StandardButton.Yes:
            self.request_clear_all.emit()

    def _on_compute(self) -> None:
        fit = fit_similarity(self._pairs)
        if fit is None:
            QMessageBox.warning(
                self, "Fit failed",
                "Need at least 2 non-coincident correspondences "
                "(and ideally not collinear).")
            return
        self._last_fit = fit
        self._btn_apply.setEnabled(True)
        self._results.setPlainText(self._format_fit(fit))

    def _on_apply(self) -> None:
        if self._last_fit is None:
            return
        if QMessageBox.question(
            self, "Apply calibration?",
            f"Overwrite the current calibration with:\n\n"
            f"  scale_px_per_m = {self._last_fit.scale_px_per_m:.4f}\n"
            f"  north_angle_deg = {self._last_fit.rotation_deg:.4f}\n\n"
            f"World coordinates for all committed apt-type polygons will be "
            f"re-stamped from the corrected scale. Entrance world coords will "
            f"also update. This doesn't regenerate the UE script — do that "
            f"from the Output tab afterwards."
        ) != QMessageBox.StandardButton.Yes:
            return
        self.apply_requested.emit(self._last_fit)

    def _format_fit(self, fit: FitResult) -> str:
        lines = [
            f"  Points fit       : {fit.n_points}",
            f"  Scale (cm / px)  : {fit.s_cm_per_px:.6f}",
            f"  scale_px_per_m   : {fit.scale_px_per_m:.4f}",
            f"  Rotation (deg)   : {fit.rotation_deg:+.4f}",
            f"  Translation (cm) : tx = {fit.tx_cm:+.2f},  ty = {fit.ty_cm:+.2f}",
            f"  RMS residual     : {fit.rms_cm:.2f} cm  ({fit.rms_cm/100:.3f} m)",
            "",
        ]
        if self._old_scale:
            ratio = fit.scale_px_per_m / float(self._old_scale)
            lines.append(
                f"  OLD scale_px_per_m : {float(self._old_scale):.4f}")
            lines.append(
                f"  NEW scale_px_per_m : {fit.scale_px_per_m:.4f}")
            lines.append(
                f"  Correction factor  : {ratio:.4f}  "
                f"(meshes were {ratio:.3f}x "
                f"{'too small' if ratio > 1.0 else 'too large'})")
            lines.append(
                f"  OLD north_angle_deg: {float(self._old_rot_deg):.4f}")
            lines.append(
                f"  NEW north_angle_deg: {fit.rotation_deg:.4f}")
            lines.append("")

        if fit.per_point_cm:
            lines.append("  Per-point residual (cm):")
            for i, (p, d) in enumerate(zip(self._pairs, fit.per_point_cm)):
                lines.append(
                    f"    {p.label or f'#{i+1}':>4}  "
                    f"px=({p.px:>7.1f},{p.py:>7.1f})  "
                    f"ue=({p.ux_cm:>10.1f},{p.uy_cm:>10.1f})  "
                    f"residual = {d:7.2f} cm")
        if fit.n_points == 2:
            lines.append("")
            lines.append("  NOTE: with 2 points the fit is mathematically exact "
                         "(residual = 0).")
            lines.append("        Add ≥3 points to verify calibration "
                         "consistency.")
        return "\n".join(lines)

    # ── Qt plumbing ────────────────────────────────────────────────────────

    def closeEvent(self, ev) -> None:
        self.closed.emit()
        super().closeEvent(ev)
