from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QLineEdit, QPushButton, QLabel,
    QScrollArea, QFrame, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal


REQUIRED_FIELDS = [
    ("building",  "Building (בנין)"),
    ("entrance",  "Entrance (כניסה)  — optional"),
    ("floor",     "Floor (קומה)"),
    ("apt_id",    "Apartment ID (מספר דירה)"),
    ("direction", "Direction (כיוון)"),
    ("type",      "Apartment Type  — optional"),
]

NONE_SENTINEL = "(not mapped)"

# Auto-detection: for each required key, list candidate column names (lowercase)
_AUTO_CANDIDATES: dict[str, list[str]] = {
    "building":  ["building", "בניין", "בנין"],
    "entrance":  ["entrance", "כניסה"],
    "floor":     ["floor", "קומה"],
    "apt_id":    ["number", "apt_id", "apartment id", "מספר דירה", "מס דירה", "id"],
    "direction": ["כיוונים", "כיוון", "orientation", "direction"],
    "type":      ["type", "סוג", "apartment type", "unit type"],
}


def auto_detect(columns: list[str]) -> dict[str, str]:
    """Return {field_key: column_name} for columns that match known names."""
    lower_map = {c.strip().lower(): c for c in columns}
    result = {}
    for key, candidates in _AUTO_CANDIDATES.items():
        for candidate in candidates:
            if candidate in lower_map:
                result[key] = lower_map[candidate]
                break
    return result


class _ExtraRow(QWidget):
    removed = pyqtSignal(object)   # self
    changed = pyqtSignal()

    def __init__(self, columns, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        self.col_combo = QComboBox()
        self.col_combo.addItems(columns)
        self.col_combo.currentIndexChanged.connect(self.changed)
        self.col_combo.setMinimumWidth(160)

        arrow = QLabel("→")
        arrow.setFixedWidth(16)

        self.var_edit = QLineEdit()
        self.var_edit.setPlaceholderText("UE variable name")
        self.var_edit.textChanged.connect(self.changed)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(28)
        remove_btn.clicked.connect(lambda: self.removed.emit(self))

        layout.addWidget(self.col_combo)
        layout.addWidget(arrow)
        layout.addWidget(self.var_edit)
        layout.addWidget(remove_btn)

    def get_mapping(self):
        return self.col_combo.currentText(), self.var_edit.text().strip()

    def set_columns(self, columns):
        current = self.col_combo.currentText()
        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        self.col_combo.addItems(columns)
        idx = self.col_combo.findText(current)
        if idx >= 0:
            self.col_combo.setCurrentIndex(idx)
        self.col_combo.blockSignals(False)


class MappingPanel(QWidget):
    mappings_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns = []
        self._extra_rows: list[_ExtraRow] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Required fields
        req_label = QLabel("Required Field Mapping")
        req_label.setStyleSheet("font-weight: bold; margin-bottom: 4px;")
        layout.addWidget(req_label)

        form = QFormLayout()
        form.setVerticalSpacing(6)
        self._req_combos: dict[str, QComboBox] = {}
        for key, label in REQUIRED_FIELDS:
            combo = QComboBox()
            combo.addItem(NONE_SENTINEL)
            combo.currentIndexChanged.connect(self.mappings_changed)
            self._req_combos[key] = combo
            form.addRow(label + ":", combo)
        layout.addLayout(form)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #555;")
        layout.addWidget(line)

        # Extra mappings
        extra_label = QLabel("Extra Blueprint Variables")
        extra_label.setStyleSheet("font-weight: bold; margin-top: 4px; margin-bottom: 4px;")
        layout.addWidget(extra_label)

        hint = QLabel("Excel column  →  UE variable name")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(hint)

        # Scroll area for extra rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._extra_container = QWidget()
        self._extra_layout = QVBoxLayout(self._extra_container)
        self._extra_layout.setContentsMargins(0, 0, 0, 0)
        self._extra_layout.setSpacing(2)
        self._extra_layout.addStretch()
        scroll.setWidget(self._extra_container)
        layout.addWidget(scroll)

        add_btn = QPushButton("+ Add Variable")
        add_btn.clicked.connect(self._add_row)
        layout.addWidget(add_btn)

    def refresh_columns(self, columns: list[str]):
        self._columns = columns
        detected = auto_detect(columns)
        for key, combo in self._req_combos.items():
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(NONE_SENTINEL)
            combo.addItems(columns)
            # Prefer previously selected value; fall back to auto-detected
            preferred = current if current != NONE_SENTINEL else detected.get(key, "")
            idx = combo.findText(preferred)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        for row in self._extra_rows:
            row.set_columns(columns)
        self.mappings_changed.emit()

    def _add_row(self):
        row = _ExtraRow(self._columns or [])
        row.removed.connect(self._remove_row)
        row.changed.connect(self.mappings_changed)
        self._extra_rows.append(row)
        # Insert before the stretch
        self._extra_layout.insertWidget(self._extra_layout.count() - 1, row)
        self.mappings_changed.emit()

    def _remove_row(self, row: _ExtraRow):
        self._extra_rows.remove(row)
        self._extra_layout.removeWidget(row)
        row.deleteLater()
        self.mappings_changed.emit()

    def get_required(self) -> dict:
        result = {}
        for key, combo in self._req_combos.items():
            val = combo.currentText()
            if val != NONE_SENTINEL:
                result[key] = val
        return result

    def get_extra(self) -> list:
        result = []
        for row in self._extra_rows:
            col, var = row.get_mapping()
            if col and var:
                result.append((col, var))
        return result
