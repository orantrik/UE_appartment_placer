"""Floor Gaps dialog — configure custom Z gaps between consecutive floors.

Default stacking is Z(floor N) = N * floor_height_cm. A floor-gap override
replaces the gap for a single transition (N -> N+1); every floor above the
override cascades upward by (override - default).

Example with default 300 cm:
    floor 0 -> Z = 0
    floor 1 -> Z = 300   (1 * 300)
    floor 2 -> Z = 600   (2 * 300)
    floor 3 -> Z = 900   ...

Override {1: 900} (9 m gap from floor 1 to floor 2):
    floor 0 -> Z = 0
    floor 1 -> Z = 300
    floor 2 -> Z = 1200  (was 600, shifted up by +600)
    floor 3 -> Z = 1500  (was 900, shifted up by +600)
    ...
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)


def compute_z_by_floor_cm(floors: list[int], default_cm: int,
                          overrides: dict[int, int]) -> dict[int, int]:
    """Cumulative Z(cm) per floor. Same semantics as generator._build_z_by_floor_cm.

    Used by the dialog's live-preview; generator does the real thing server-side.
    """
    def _gap(n: int) -> int:
        if n in overrides:
            return int(overrides[n])
        return int(default_cm)

    touched = set(floors) | {0}
    for k in overrides.keys():
        try:
            ki = int(k)
        except (TypeError, ValueError):
            continue
        touched.add(ki)
        touched.add(ki + 1)
    if not touched:
        return {0: 0}
    f_min = min(touched)
    f_max = max(touched)

    out: dict[int, int] = {0: 0}
    z = 0
    for n in range(0, f_max):
        z += _gap(n)
        out[n + 1] = z
    z = 0
    for n in range(0, f_min, -1):
        z -= _gap(n - 1)
        out[n - 1] = z
    return out


class FloorGapsDialog(QDialog):
    """Dialog for editing per-transition floor gap overrides.

    Inputs:
      - floors_present: sorted list of int floor indices from the loaded data
      - default_cm:     the global floor_height_cm (fallback gap)
      - overrides:      current override dict {from_floor_int: gap_cm_int}

    The user can add/update/remove overrides. The right-hand preview
    recomputes the Z-by-floor table live. On accept, .result_overrides()
    returns the new dict.
    """

    def __init__(self, parent, floors_present: list[int],
                 default_cm: int, overrides: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Floor Gaps")
        self.setModal(True)
        self.resize(720, 520)

        self._default_cm = int(default_cm)
        self._floors = sorted({int(f) for f in floors_present})
        if not self._floors:
            self._floors = [0, 1]
        self._overrides: dict[int, int] = {}
        for k, v in (overrides or {}).items():
            try:
                self._overrides[int(k)] = int(v)
            except (TypeError, ValueError):
                continue

        self._build_ui()
        self._refresh_table()
        self._refresh_preview()

    # ── UI ─────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Editable default floor height — this is the single biggest knob.
        # Overrides apply on top of this; every un-overridden transition
        # uses this value. Match it to your polygon extrusion height if you
        # want flush stacking.
        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default floor height:"))
        self._default_spin = QDoubleSpinBox()
        self._default_spin.setRange(0.1, 100.0)
        self._default_spin.setDecimals(2)
        self._default_spin.setSingleStep(0.1)
        self._default_spin.setSuffix("  m")
        self._default_spin.setValue(self._default_cm / 100)
        self._default_spin.setToolTip(
            "Applied to every floor-to-floor transition that is NOT listed "
            "as an override below. Match this to your apartment extrusion "
            "height (e.g. 3.30 m) for flush stacking."
        )
        self._default_spin.valueChanged.connect(self._on_default_changed)
        default_row.addWidget(self._default_spin)
        default_row.addStretch(1)
        root.addLayout(default_row)

        hint = QLabel(
            "Overrides below replace this default for a single transition. "
            "Every floor above an override shifts up by (override − default)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888;")
        root.addWidget(hint)

        content = QHBoxLayout()
        content.setSpacing(12)

        left = QVBoxLayout()

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._from_spin = QSpinBox()
        lo = min(self._floors) - 5
        hi = max(self._floors) + 20
        self._from_spin.setRange(lo, hi)
        self._from_spin.setValue(self._floors[0] if self._floors else 0)
        self._to_lbl = QLabel(f"→ Floor {self._from_spin.value() + 1}")
        self._from_spin.valueChanged.connect(
            lambda v: self._to_lbl.setText(f"→ Floor {int(v) + 1}"))

        from_row = QHBoxLayout()
        from_row.addWidget(self._from_spin)
        from_row.addWidget(self._to_lbl)
        from_row.addStretch(1)
        form.addRow("From floor:", from_row)

        self._gap_spin = QDoubleSpinBox()
        self._gap_spin.setRange(0.01, 500.0)
        self._gap_spin.setDecimals(2)
        self._gap_spin.setSingleStep(0.1)
        self._gap_spin.setSuffix("  m")
        self._gap_spin.setValue(self._default_cm / 100)
        form.addRow("Gap:", self._gap_spin)

        left.addLayout(form)

        add_row = QHBoxLayout()
        add_btn = QPushButton("Add / update override")
        add_btn.clicked.connect(self._on_add_clicked)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        left.addLayout(add_row)

        tbl_lbl = QLabel("Current overrides:")
        tbl_lbl.setStyleSheet("font-weight:bold; margin-top:6px;")
        left.addWidget(tbl_lbl)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(
            ["Transition", "Gap (m)", ""])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        left.addWidget(self._table, stretch=1)

        clear_all = QPushButton("Reset all to default")
        clear_all.clicked.connect(self._on_clear_all)
        left.addWidget(clear_all)

        content.addLayout(left, stretch=3)

        right = QVBoxLayout()
        right_lbl = QLabel("Live preview (Z per floor):")
        right_lbl.setStyleSheet("font-weight:bold;")
        right.addWidget(right_lbl)

        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(
            "QTextEdit { font-family: Consolas, 'Courier New', monospace; }")
        right.addWidget(self._preview, stretch=1)

        content.addLayout(right, stretch=2)
        root.addLayout(content)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ── Slots ──────────────────────────────────────────────────────────────
    def _on_default_changed(self, value_m: float) -> None:
        """User edited the default floor height. Rebuild the preview and
        drop any override rows that exactly equal the new default — those
        are no-ops now and would just clutter the list."""
        self._default_cm = int(round(value_m * 100))
        stale = [k for k, v in self._overrides.items() if v == self._default_cm]
        for k in stale:
            self._overrides.pop(k, None)
        self._refresh_table()
        self._refresh_preview()

    def _on_add_clicked(self) -> None:
        n_from = int(self._from_spin.value())
        gap_cm = int(round(self._gap_spin.value() * 100))
        if gap_cm == self._default_cm:
            self._overrides.pop(n_from, None)
        else:
            self._overrides[n_from] = gap_cm
        self._refresh_table()
        self._refresh_preview()

    def _on_clear_all(self) -> None:
        if not self._overrides:
            return
        if (QMessageBox.question(
                self, "Reset overrides",
                "Remove every floor-gap override and use the default "
                f"{self._default_cm / 100:g} m everywhere?")
                != QMessageBox.StandardButton.Yes):
            return
        self._overrides.clear()
        self._refresh_table()
        self._refresh_preview()

    def _remove_override(self, n_from: int) -> None:
        self._overrides.pop(n_from, None)
        self._refresh_table()
        self._refresh_preview()

    # ── Rendering ──────────────────────────────────────────────────────────
    def _refresh_table(self) -> None:
        self._table.setRowCount(0)
        for n_from in sorted(self._overrides.keys()):
            gap_cm = self._overrides[n_from]
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(
                r, 0, QTableWidgetItem(f"Floor {n_from}  →  Floor {n_from + 1}"))
            self._table.setItem(
                r, 1, QTableWidgetItem(f"{gap_cm / 100:.2f}"))
            btn = QPushButton("Remove")
            btn.clicked.connect(
                lambda _checked=False, nf=n_from: self._remove_override(nf))
            self._table.setCellWidget(r, 2, btn)

    def _refresh_preview(self) -> None:
        z_map = compute_z_by_floor_cm(
            self._floors, self._default_cm, self._overrides)
        lines: list[str] = []
        for f in sorted(z_map.keys()):
            z_cm = z_map[f]
            prefix = ""
            if (f - 1) in self._overrides:
                prefix = "  (custom gap below)"
            lines.append(f"Floor {f:>3} → Z = {z_cm / 100:>7.2f} m"
                         f" ({z_cm} cm){prefix}")
        self._preview.setPlainText("\n".join(lines))

    # ── Public ─────────────────────────────────────────────────────────────
    def result_overrides(self) -> dict[int, int]:
        return dict(self._overrides)

    def result_default_cm(self) -> int:
        """The (possibly user-edited) default floor height in cm."""
        return int(self._default_cm)
