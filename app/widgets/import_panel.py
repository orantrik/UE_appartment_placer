from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QTableWidget, QTableWidgetItem, QFileDialog, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont, QColor, QBrush
import pandas as pd
from app.core.reader import read_file

_GREEN = QColor("#1e4d2b")   # dark green bg — has polygon
_RED   = QColor("#4d1e1e")   # dark red bg  — missing polygon
_TEXT  = QColor("#ffffff")


class ImportPanel(QWidget):
    file_loaded = pyqtSignal(object)   # emits pd.DataFrame

    def __init__(self, parent=None):
        super().__init__(parent)
        self._df = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Top bar
        bar = QHBoxLayout()
        btn = QPushButton("Browse…")
        btn.setFixedWidth(90)
        btn.clicked.connect(self._browse)
        self._path_label = QLabel("No file loaded")
        self._path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.addWidget(btn)
        bar.addWidget(self._path_label)
        layout.addLayout(bar)

        # Table preview
        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setFont(QFont("Consolas", 9))
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Table", "",
            "Spreadsheets (*.xlsx *.xls *.csv);;All Files (*)"
        )
        if not path:
            return
        try:
            df = read_file(path)
            self._df = df
            self._path_label.setText(path)
            self._populate_table(df)
            self.file_loaded.emit(df)
        except Exception as e:
            self._path_label.setText(f"Error: {e}")

    def _populate_table(self, df: pd.DataFrame):
        preview = df
        self._table.setRowCount(len(preview))
        self._table.setColumnCount(len(df.columns))
        self._table.setHorizontalHeaderLabels(list(df.columns))
        for r, (_, row) in enumerate(preview.iterrows()):
            for c, val in enumerate(row):
                item = QTableWidgetItem("" if pd.isna(val) else str(val))
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        self._drawn_keys: set = set()   # reset coverage on new file

    def refresh_coverage(self, drawn_keys: set, data,
                         building_col: str, entrance_col: str, type_col: str):
        """Colour rows: green = polygon drawn, red = missing."""
        if self._df is None:
            return
        preview = self._df
        for r, (_, row) in enumerate(preview.iterrows()):
            b  = str(row.get(building_col,  "")).strip()
            e  = str(row.get(entrance_col,  "")).strip()
            t  = str(row.get(type_col,      "")).strip()
            bg = _GREEN if (b, e, t) in drawn_keys else _RED
            for c in range(self._table.columnCount()):
                item = self._table.item(r, c)
                if item:
                    item.setBackground(QBrush(bg))
                    item.setForeground(QBrush(_TEXT))
