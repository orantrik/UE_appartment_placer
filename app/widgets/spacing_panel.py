from PyQt6.QtWidgets import (
    QWidget, QGridLayout, QLabel, QSlider, QDoubleSpinBox,
)
from PyQt6.QtCore import pyqtSignal, Qt


SPACING_FIELDS = [
    # (internal_key, label, default_cm, min_cm, max_cm)
    ("building_spacing_cm",  "Building Spacing",  10000,  0,  50000),
    ("floor_height_cm",      "Floor Height",        300,  100,  2000),
    ("direction_spacing_cm", "Direction Spacing",  1000,  100,  5000),
    ("entrance_offset_cm",   "Entrance Offset",     500,  0,   2000),
    ("stack_offset_cm",      "Stack Offset",        200,  0,   1000),
]


class _SpacingRow:
    def __init__(self, key, label, default_cm, min_cm, max_cm, grid, row):
        self.key = key
        self._syncing = False

        lbl = QLabel(label)
        lbl.setFixedWidth(150)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(min_cm, max_cm)
        self.slider.setSingleStep(100)
        self.slider.setValue(default_cm)

        self.spinbox = QDoubleSpinBox()
        self.spinbox.setRange(min_cm / 100, max_cm / 100)
        self.spinbox.setSingleStep(0.5)
        self.spinbox.setDecimals(1)
        self.spinbox.setSuffix(" m")
        self.spinbox.setFixedWidth(90)
        self.spinbox.setValue(default_cm / 100)

        grid.addWidget(lbl, row, 0)
        grid.addWidget(self.slider, row, 1)
        grid.addWidget(self.spinbox, row, 2)

        self.slider.valueChanged.connect(self._slider_changed)
        self.spinbox.valueChanged.connect(self._spin_changed)

    def _slider_changed(self, val_cm):
        if self._syncing:
            return
        self._syncing = True
        self.spinbox.setValue(val_cm / 100)
        self._syncing = False
        self._notify()

    def _spin_changed(self, val_m):
        if self._syncing:
            return
        self._syncing = True
        self.slider.setValue(int(val_m * 100))
        self._syncing = False
        self._notify()

    def _notify(self):
        if self._on_change:
            self._on_change(self.key, self.slider.value())

    def connect(self, callback):
        self._on_change = callback

    @property
    def value_cm(self):
        return self.slider.value()


class SpacingPanel(QWidget):
    spacing_changed = pyqtSignal(str, int)   # (field_key, value_cm)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[_SpacingRow] = []
        self._build_ui()

    def _build_ui(self):
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        header = QLabel("Spacing Controls")
        header.setStyleSheet("font-weight: bold; margin-bottom: 6px;")
        grid.addWidget(header, 0, 0, 1, 3)

        for i, (key, label, default, mn, mx) in enumerate(SPACING_FIELDS):
            row = _SpacingRow(key, label, default, mn, mx, grid, i + 1)
            row.connect(self._on_row_change)
            self._rows.append(row)

    def _on_row_change(self, key, value_cm):
        self.spacing_changed.emit(key, value_cm)

    def get_values(self) -> dict:
        return {r.key: r.value_cm for r in self._rows}
