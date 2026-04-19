"""AI Import panel — paste fal.ai key, load a PDF/image floor-plan, run
Florence-2 object detection, review detected regions, and import them into
the Floor Plan canvas as draggable apt-type polygons.
"""
from __future__ import annotations

import os
import uuid as _uuid_mod

from PyQt6.QtCore import (
    QObject, QSettings, QSize, Qt, QThread, pyqtSignal,
)
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from app.core import ai_analyzer, sam_refiner


_SETTINGS_ORG = "UEPlacer"
_SETTINGS_APP = "ai_import"
_SETTINGS_KEY_API = "fal_api_key"

# SAM refinement settings (all optional — panel still works without them).
_SETTINGS_KEY_SAM_ENABLED = "sam_enabled"
_SETTINGS_KEY_SAM_PYTHON = "sam_python_path"
_SETTINGS_KEY_SAM_MODEL = "sam_model_path"
_SETTINGS_KEY_SAM_DEVICE = "sam_device"   # "cuda" or "cpu"


# ── QThread worker so the UI stays responsive during the fal.ai round-trip ──

class _AnalyzeWorker(QObject):
    finished = pyqtSignal(object)      # ai_analyzer.AnalyzeResult
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, source_path: str, page_index: int, api_key: str,
                 sam_config: "sam_refiner.SamConfig | None" = None):
        super().__init__()
        self._source_path = source_path
        self._page_index = page_index
        self._api_key = api_key
        self._sam_config = sam_config

    def run(self):
        try:
            res = ai_analyzer.analyze_floor_plan(
                self._source_path,
                self._page_index,
                self._api_key,
                progress_cb=lambda m: self.progress.emit(m),
                sam_config=self._sam_config,
            )
            self.finished.emit(res)
        except Exception as exc:  # pragma: no cover - network-dependent
            import traceback
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class AiImportPanel(QWidget):
    """Tab widget for AI-assisted floor-plan region detection."""

    polygons_ready = pyqtSignal(list)   # list[dict] — apt-type polygon entries

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        self._source_path: str = ""
        self._page_png: bytes = b""
        self._page_w: int = 0
        self._page_h: int = 0
        self._regions: list[ai_analyzer.DetectedRegion] = []
        self._thread: QThread | None = None
        self._worker: _AnalyzeWorker | None = None

        self._build_ui()
        self._restore_api_key()
        self._restore_sam_settings()

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        layout.addWidget(self._make_api_group())
        layout.addWidget(self._make_sam_group())
        layout.addWidget(self._make_source_group())
        layout.addWidget(self._make_results_group(), stretch=1)

        status_row = QHBoxLayout()
        self._status = QLabel(
            "Paste your Google AI Studio key and load a PDF to begin.")
        self._status.setStyleSheet("color: #888;")
        status_row.addWidget(self._status, stretch=1)

        self._open_log_btn = QPushButton("Open Log")
        self._open_log_btn.setFixedWidth(90)
        self._open_log_btn.setToolTip(
            f"Open the AI-analysis diagnostic log:\n{ai_analyzer.LOG_PATH}")
        self._open_log_btn.clicked.connect(self._open_log)
        status_row.addWidget(self._open_log_btn)
        layout.addLayout(status_row)

    def _make_api_group(self) -> QGroupBox:
        box = QGroupBox("Google AI Studio API Key (Gemini / Nano Banana)")
        lay = QHBoxLayout(box)

        self._api_edit = QLineEdit()
        self._api_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_edit.setPlaceholderText(
            "Paste your Google AI Studio key (starts with AIza…)")
        self._api_edit.setToolTip(
            "Your Google AI Studio API key. Get one at "
            "https://aistudio.google.com/apikey\n"
            "This is the same key that unlocks Gemini and Nano Banana models.\n"
            "Stored locally via QSettings (Windows registry / per-user)."
        )
        lay.addWidget(self._api_edit, stretch=1)

        self._show_btn = QPushButton("Show")
        self._show_btn.setCheckable(True)
        self._show_btn.setFixedWidth(70)
        self._show_btn.toggled.connect(self._toggle_api_visibility)
        lay.addWidget(self._show_btn)

        save_btn = QPushButton("Save Key")
        save_btn.setFixedWidth(100)
        save_btn.clicked.connect(self._save_api_key)
        lay.addWidget(save_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self._clear_api_key)
        lay.addWidget(clear_btn)
        return box

    # ── SAM refinement group ─────────────────────────────────────────────
    #
    # The placer's frozen exe can't itself run SAM (torch is 2.5 GB, which
    # would triple the installer). Instead we shell out to the user's
    # existing ComfyUI python.exe which already has torch + a SAM checkpoint
    # on disk. Two path fields + an "Auto-detect" button are all the user
    # needs to configure this once; settings persist via QSettings.

    def _make_sam_group(self) -> QGroupBox:
        box = QGroupBox("Polygon Refinement (optional — Segment Anything)")
        box.setToolTip(
            "Gemini locates apartments, but its polygon edges are coarse.\n"
            "If you have ComfyUI installed with a SAM checkpoint (vit_h / "
            "vit_l / vit_b), enable this to let Meta's Segment Anything "
            "Model refine every bounding box into a pixel-exact outline.\n\n"
            "Runs locally via your ComfyUI python.exe — no cloud calls, "
            "no extra cost, and your floor-plan never leaves this machine.\n\n"
            "If unchecked, polygons come straight from Gemini as before."
        )
        outer = QVBoxLayout(box)

        row1 = QHBoxLayout()
        self._sam_enable_cb = QCheckBox("Refine polygons with SAM")
        self._sam_enable_cb.setToolTip(
            "Turn on once you have both paths below pointing at real files.\n"
            "Expect ~5–15 s per analysis on GPU, 30–90 s on CPU.")
        self._sam_enable_cb.toggled.connect(self._on_sam_toggled)
        row1.addWidget(self._sam_enable_cb)

        self._sam_status = QLabel("")
        self._sam_status.setStyleSheet("color: #888;")
        row1.addWidget(self._sam_status, stretch=1)

        self._sam_autodetect_btn = QPushButton("Auto-detect")
        self._sam_autodetect_btn.setFixedWidth(100)
        self._sam_autodetect_btn.setToolTip(
            "Search common ComfyUI install paths on this machine for a "
            "python.exe and a SAM .pth checkpoint.")
        self._sam_autodetect_btn.clicked.connect(self._auto_detect_sam)
        row1.addWidget(self._sam_autodetect_btn)
        outer.addLayout(row1)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)

        py_row = QHBoxLayout()
        self._sam_python_edit = QLineEdit()
        self._sam_python_edit.setPlaceholderText(
            r"e.g. C:\ComfyUI_windows_portable\python_embeded\python.exe")
        self._sam_python_edit.editingFinished.connect(self._save_sam_settings)
        py_row.addWidget(self._sam_python_edit, stretch=1)
        py_browse = QPushButton("Browse…")
        py_browse.setFixedWidth(80)
        py_browse.clicked.connect(self._browse_sam_python)
        py_row.addWidget(py_browse)
        py_wrap = QWidget()
        py_wrap.setLayout(py_row)
        form.addRow("ComfyUI python.exe:", py_wrap)

        model_row = QHBoxLayout()
        self._sam_model_edit = QLineEdit()
        self._sam_model_edit.setPlaceholderText(
            r"e.g. C:\ComfyUI\models\sams\sam_vit_h_4b8939.pth")
        self._sam_model_edit.editingFinished.connect(self._save_sam_settings)
        model_row.addWidget(self._sam_model_edit, stretch=1)
        model_browse = QPushButton("Browse…")
        model_browse.setFixedWidth(80)
        model_browse.clicked.connect(self._browse_sam_model)
        model_row.addWidget(model_browse)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow("SAM weights (.pth):", model_wrap)

        outer.addLayout(form)

        hint = QLabel(
            "Need to install? Open a terminal in your ComfyUI folder and run ONE of:<br>"
            "&nbsp;• <b>SAM 2</b> (matches <code>sam2_*.safetensors / .pt</code> "
            "weights — recommended):<br>"
            "&nbsp;&nbsp;&nbsp;<code>&lt;your python.exe&gt; -m pip install "
            "git+https://github.com/facebookresearch/segment-anything-2.git</code><br>"
            "&nbsp;• <b>SAM v1</b> (matches <code>sam_vit_*.pth</code>):<br>"
            "&nbsp;&nbsp;&nbsp;<code>&lt;your python.exe&gt; -m pip install "
            "git+https://github.com/facebookresearch/segment-anything.git</code>")
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet("color: #777; font-size: 9pt;")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return box

    def _browse_sam_python(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ComfyUI python.exe",
            self._sam_python_edit.text() or os.path.expanduser("~"),
            "Python executable (python.exe);;All Files (*)")
        if path:
            self._sam_python_edit.setText(path)
            self._save_sam_settings()

    def _browse_sam_model(self) -> None:
        start = self._sam_model_edit.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SAM checkpoint",
            start,
            "SAM weights (*.pth *.pt *.safetensors);;All Files (*)")
        if path:
            self._sam_model_edit.setText(path)
            self._save_sam_settings()

    def _auto_detect_sam(self) -> None:
        py = sam_refiner.find_comfyui_python()
        mdl = sam_refiner.find_sam_model()
        found_any = False
        if py and not self._sam_python_edit.text().strip():
            self._sam_python_edit.setText(py)
            found_any = True
        if mdl and not self._sam_model_edit.text().strip():
            self._sam_model_edit.setText(mdl)
            found_any = True
        # If fields were already populated, still offer to overwrite.
        if (py or mdl) and not found_any:
            resp = QMessageBox.question(
                self, "Overwrite SAM paths?",
                "Auto-detection found candidate paths — overwrite the "
                "current values?\n\n"
                f"python.exe: {py or '(not found)'}\n"
                f"SAM weights: {mdl or '(not found)'}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if resp == QMessageBox.StandardButton.Yes:
                if py:
                    self._sam_python_edit.setText(py)
                if mdl:
                    self._sam_model_edit.setText(mdl)
                found_any = True
        if not py and not mdl:
            QMessageBox.information(
                self, "SAM auto-detect",
                "Could not find a ComfyUI install on this machine.\n\n"
                "Point the two fields below at:\n"
                "  • the python.exe inside your ComfyUI folder\n"
                "  • a sam_vit_h / vit_l / vit_b .pth checkpoint\n\n"
                "If ComfyUI doesn't yet have SAM installed, open a terminal "
                "in its folder and run:\n"
                "  python_embeded\\python.exe -m pip install "
                "git+https://github.com/facebookresearch/segment-anything.git")
            return
        self._save_sam_settings()
        self._refresh_sam_status()

    def _restore_sam_settings(self) -> None:
        self._sam_enable_cb.blockSignals(True)
        self._sam_enable_cb.setChecked(self._settings.value(
            _SETTINGS_KEY_SAM_ENABLED, False, type=bool))
        self._sam_enable_cb.blockSignals(False)
        self._sam_python_edit.setText(self._settings.value(
            _SETTINGS_KEY_SAM_PYTHON, "", type=str))
        self._sam_model_edit.setText(self._settings.value(
            _SETTINGS_KEY_SAM_MODEL, "", type=str))
        self._refresh_sam_status()

    def _save_sam_settings(self) -> None:
        self._settings.setValue(
            _SETTINGS_KEY_SAM_ENABLED, self._sam_enable_cb.isChecked())
        self._settings.setValue(
            _SETTINGS_KEY_SAM_PYTHON, self._sam_python_edit.text().strip())
        self._settings.setValue(
            _SETTINGS_KEY_SAM_MODEL, self._sam_model_edit.text().strip())
        self._settings.sync()
        self._refresh_sam_status()

    def _on_sam_toggled(self, _checked: bool) -> None:
        self._save_sam_settings()

    def _refresh_sam_status(self) -> None:
        cfg = self._current_sam_config()
        if not self._sam_enable_cb.isChecked():
            self._sam_status.setText("disabled")
            self._sam_status.setStyleSheet("color: #888;")
            return
        if cfg and cfg.ok:
            self._sam_status.setText("✓ configured")
            self._sam_status.setStyleSheet("color: #4a4;")
        else:
            missing = []
            py = self._sam_python_edit.text().strip()
            mdl = self._sam_model_edit.text().strip()
            if not py or not os.path.isfile(py):
                missing.append("python.exe")
            if not mdl or not os.path.isfile(mdl):
                missing.append("weights")
            self._sam_status.setText(
                "missing " + " + ".join(missing) if missing else "not ready")
            self._sam_status.setStyleSheet("color: #c66;")

    def _current_sam_config(self) -> "sam_refiner.SamConfig | None":
        """Build a SamConfig from UI state, or None if disabled / invalid."""
        if not self._sam_enable_cb.isChecked():
            return None
        cfg = sam_refiner.SamConfig(
            python_path=self._sam_python_edit.text().strip(),
            model_path=self._sam_model_edit.text().strip(),
            device="cuda",
        )
        return cfg if cfg.ok else None

    def _make_source_group(self) -> QGroupBox:
        box = QGroupBox("Floor Plan Source")
        outer = QVBoxLayout(box)

        row1 = QHBoxLayout()
        browse = QPushButton("Browse PDF / Image…")
        browse.setFixedWidth(180)
        browse.clicked.connect(self._browse)
        row1.addWidget(browse)

        self._path_label = QLabel("No file loaded")
        self._path_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._path_label.setStyleSheet("color: #999;")
        row1.addWidget(self._path_label, stretch=1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Page:"))
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 1)
        self._page_spin.setFixedWidth(70)
        self._page_spin.valueChanged.connect(self._reload_thumbnail)
        row2.addWidget(self._page_spin)
        row2.addSpacing(16)

        self._analyze_btn = QPushButton("Analyze Floor Plan")
        self._analyze_btn.setFixedHeight(30)
        self._analyze_btn.clicked.connect(self._on_analyze)
        self._analyze_btn.setEnabled(False)
        row2.addWidget(self._analyze_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setVisible(False)
        self._progress.setFixedHeight(20)
        row2.addWidget(self._progress, stretch=1)
        outer.addLayout(row2)

        self._thumb = QLabel()
        self._thumb.setFixedHeight(220)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "border: 1px dashed #555; color: #777; background: #222;")
        self._thumb.setText("(PDF page thumbnail will appear here)")
        outer.addWidget(self._thumb)
        return box

    def _make_results_group(self) -> QGroupBox:
        box = QGroupBox("Detected Regions")
        lay = QVBoxLayout(box)

        hint = QLabel(
            "Tick the regions you want to import, edit their Building / Entrance "
            "/ Type, then click 'Add Selected to Floor Plan'. "
            "The polygons will appear on the canvas — drag them into position "
            "with the Move tool, then Commit All."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        lay.addWidget(hint)

        # Columns: 0=Use, 1=Detected Label, 2=Source (SAM/Gemini toggle),
        #          3=Building, 4=Entrance, 5=Type Name
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Use", "Detected Label", "Source", "Building", "Entrance",
             "Type Name"])
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setFont(QFont("Segoe UI", 9))
        lay.addWidget(self._table, stretch=1)

        btn_row = QHBoxLayout()
        check_all = QPushButton("Check All")
        check_all.clicked.connect(lambda: self._set_all_checked(True))
        uncheck_all = QPushButton("Uncheck All")
        uncheck_all.clicked.connect(lambda: self._set_all_checked(False))
        btn_row.addWidget(check_all)
        btn_row.addWidget(uncheck_all)

        all_gemini_btn = QPushButton("All → Gemini")
        all_gemini_btn.setToolTip(
            "Flip every row's Source back to Gemini's original polygon.")
        all_gemini_btn.clicked.connect(
            lambda: self._set_all_source("gemini"))
        btn_row.addWidget(all_gemini_btn)

        all_sam_btn = QPushButton("All → SAM")
        all_sam_btn.setToolTip(
            "Flip every row's Source to the SAM-refined polygon where "
            "available (rows without a SAM polygon stay on Gemini).")
        all_sam_btn.clicked.connect(lambda: self._set_all_source("sam"))
        btn_row.addWidget(all_sam_btn)

        btn_row.addStretch(1)

        self._import_btn = QPushButton("Add Selected to Floor Plan")
        self._import_btn.setFixedHeight(32)
        self._import_btn.setStyleSheet("font-weight: bold;")
        self._import_btn.clicked.connect(self._on_import)
        self._import_btn.setEnabled(False)
        btn_row.addWidget(self._import_btn)
        lay.addLayout(btn_row)
        return box

    # ── API key persistence ──────────────────────────────────────────────
    def _restore_api_key(self):
        key = self._settings.value(_SETTINGS_KEY_API, "", type=str)
        if key:
            self._api_edit.setText(key)
            self._status.setText("API key loaded from settings.")

    def _save_api_key(self):
        key = self._api_edit.text().strip()
        self._settings.setValue(_SETTINGS_KEY_API, key)
        self._settings.sync()
        self._status.setText(
            "API key saved." if key else "API key cleared.")

    def _clear_api_key(self):
        self._api_edit.clear()
        self._settings.remove(_SETTINGS_KEY_API)
        self._settings.sync()
        self._status.setText("API key cleared.")

    def _toggle_api_visibility(self, shown: bool):
        self._api_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self._show_btn.setText("Hide" if shown else "Show")

    def _open_log(self):
        """Open the AI-analysis log in the default editor / reveal in Explorer."""
        path = ai_analyzer.LOG_PATH
        try:
            if os.path.exists(path):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                folder = os.path.dirname(path) or path
                os.makedirs(folder, exist_ok=True)
                os.startfile(folder)  # type: ignore[attr-defined]
                QMessageBox.information(
                    self, "Log not created yet",
                    "The log file will be created the next time you run "
                    f"an analysis.\n\nLog folder:\n{folder}")
        except Exception as exc:
            QMessageBox.warning(
                self, "Cannot open log",
                f"Log path:\n{path}\n\nError: {exc}")

    # ── Source loading ───────────────────────────────────────────────────
    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Floor Plan", "",
            "Floor plans (*.pdf *.png *.jpg *.jpeg *.webp);;All Files (*)")
        if not path:
            return
        self._source_path = path
        self._path_label.setText(path)

        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            try:
                n = ai_analyzer.pdf_page_count(path)
            except Exception as e:
                QMessageBox.warning(self, "PDF error", str(e))
                return
            self._page_spin.blockSignals(True)
            self._page_spin.setRange(1, max(1, n))
            self._page_spin.setValue(1)
            self._page_spin.blockSignals(False)
        else:
            self._page_spin.setRange(1, 1)
            self._page_spin.setValue(1)

        self._reload_thumbnail()
        self._analyze_btn.setEnabled(True)

    def _reload_thumbnail(self):
        if not self._source_path:
            return
        try:
            page_idx = self._page_spin.value() - 1
            ext = os.path.splitext(self._source_path)[1].lower()
            if ext == ".pdf":
                png, w, h = ai_analyzer.render_pdf_page(
                    self._source_path, page_idx, dpi=96)
            else:
                png, w, h = ai_analyzer.load_image_as_png(self._source_path)
            self._page_png, self._page_w, self._page_h = png, w, h
            pix = QPixmap()
            pix.loadFromData(png)
            self._thumb.setPixmap(pix.scaled(
                QSize(self._thumb.width(), self._thumb.height()),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self._status.setText(
                f"Loaded page {page_idx + 1} ({w}x{h}px). Click 'Analyze'.")
        except Exception as e:
            QMessageBox.warning(self, "Load error", str(e))
            self._thumb.clear()
            self._thumb.setText("(failed to render page)")

    # ── Analyze ──────────────────────────────────────────────────────────
    def _on_analyze(self):
        key = self._api_edit.text().strip()
        if not key:
            QMessageBox.warning(
                self, "No API key",
                "Paste your Google AI Studio API key first, then click "
                "Save Key. Get one at https://aistudio.google.com/apikey")
            return
        if not self._source_path:
            return
        page_idx = self._page_spin.value() - 1

        self._analyze_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._status.setText("Starting analysis…")

        sam_cfg = self._current_sam_config()
        if self._sam_enable_cb.isChecked() and sam_cfg is None:
            # User asked for refinement but paths aren't valid — warn once,
            # then continue with Gemini-only so the run doesn't fail.
            QMessageBox.warning(
                self, "SAM refinement disabled",
                "SAM is enabled but the python.exe or weights path is "
                "missing / not a real file.\n\n"
                "Running Gemini only for this analysis. Fix the paths and "
                "re-run to enable refinement.")

        self._thread = QThread(self)
        self._worker = _AnalyzeWorker(
            self._source_path, page_idx, key, sam_config=sam_cfg)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._status.setText)
        self._worker.finished.connect(self._on_analysis_done)
        self._worker.failed.connect(self._on_analysis_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_analysis_done(self, result):
        self._progress.setVisible(False)
        self._analyze_btn.setEnabled(True)
        self._page_png = result.page_image_png
        self._page_w = result.page_w
        self._page_h = result.page_h
        self._regions = list(result.regions)
        self._populate_results_table()
        if self._regions:
            self._status.setText(
                f"Detected {len(self._regions)} region(s). "
                "Review and click 'Add Selected to Floor Plan'.")
            self._import_btn.setEnabled(True)
        else:
            self._status.setText(
                "No regions detected. Try a higher DPI page or a clearer plan.")
            self._import_btn.setEnabled(False)

    def _on_analysis_failed(self, msg: str):
        self._progress.setVisible(False)
        self._analyze_btn.setEnabled(True)
        self._status.setText(
            f"Analysis failed — see log: {ai_analyzer.LOG_PATH}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Analysis failed")
        box.setText("Gemini analysis failed.")
        box.setInformativeText(
            f"A full diagnostic log is at:\n{ai_analyzer.LOG_PATH}\n\n"
            "Click 'Show Details' below for the immediate error.")
        box.setDetailedText(msg)
        open_btn = box.addButton(
            "Open Log", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_log()

    # ── Results table ────────────────────────────────────────────────────
    def _populate_results_table(self):
        self._table.setRowCount(0)
        for idx, r in enumerate(self._regions):
            row = self._table.rowCount()
            self._table.insertRow(row)

            chk = QCheckBox()
            chk.setChecked(True)
            cell = QWidget()
            _lay = QHBoxLayout(cell)
            _lay.setContentsMargins(6, 0, 0, 0)
            _lay.addWidget(chk)
            _lay.addStretch(1)
            self._table.setCellWidget(row, 0, cell)
            cell.setProperty("_chk_ref", chk)

            lbl_item = QTableWidgetItem(r.label)
            lbl_item.setFlags(lbl_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, lbl_item)

            # Source toggle — col 2. Three visual states:
            #   "SAM"       green  — SAM polygon promoted + currently active
            #   "Gemini"    grey   — Gemini polygon active but SAM available
            #                        (e.g. quality gate rejected SAM, or user
            #                        flipped back manually)
            #   "Gemini only" disabled grey — no SAM polygon stored for this
            #                        region (either SAM wasn't run or it
            #                        returned nothing)
            src_btn = QPushButton()
            src_btn.setFixedWidth(96)
            src_btn.setProperty("_region_idx", idx)
            src_btn.clicked.connect(self._on_source_button_clicked)
            self._refresh_source_button(src_btn, r)
            self._table.setCellWidget(row, 2, src_btn)

            self._table.setItem(row, 3, QTableWidgetItem("1"))
            self._table.setItem(row, 4, QTableWidgetItem("1"))

            type_guess = _guess_type_name(r.label, row)
            self._table.setItem(row, 5, QTableWidgetItem(type_guess))

    def _refresh_source_button(self, btn: QPushButton, region) -> None:
        """Update the Source button's label/color/enabled state to match
        whichever polygon is currently active on `region`.
        """
        raw = getattr(region, "raw", {}) or {}
        has_sam = bool(raw.get("_polygon_pct_sam"))
        source = raw.get("_source") or ("gemini" if not has_sam else "gemini")
        area = raw.get("_sam_area_frac")
        tip_parts = []
        if has_sam and area is not None:
            tip_parts.append(f"SAM mask area = {area * 100:.0f}% of bbox")
        if not has_sam:
            btn.setText("Gemini only")
            btn.setEnabled(False)
            btn.setStyleSheet(
                "QPushButton{color:#888;background:#333;border:1px solid #444;}")
            tip_parts.append(
                "No SAM polygon for this region.\n"
                "Either SAM wasn't enabled, or it returned no usable mask.")
        elif source == "sam":
            btn.setText("SAM ✓")
            btn.setEnabled(True)
            btn.setStyleSheet(
                "QPushButton{color:#fff;background:#2a7a2a;border:1px solid #3b9;}"
                "QPushButton:hover{background:#359935;}")
            tip_parts.append("Click to switch to Gemini's polygon.")
        else:
            btn.setText("Gemini")
            btn.setEnabled(True)
            btn.setStyleSheet(
                "QPushButton{color:#ddd;background:#4a4a4a;border:1px solid #666;}"
                "QPushButton:hover{background:#555;}")
            tip_parts.append("Click to switch to the SAM-refined polygon.")
        btn.setToolTip("\n".join(tip_parts))

    def _on_source_button_clicked(self) -> None:
        btn = self.sender()
        if btn is None:
            return
        idx = btn.property("_region_idx")
        if idx is None or idx < 0 or idx >= len(self._regions):
            return
        r = self._regions[idx]
        raw = getattr(r, "raw", None)
        if raw is None:
            return
        gemini = raw.get("_polygon_pct_gemini")
        sam = raw.get("_polygon_pct_sam")
        cur_source = raw.get("_source") or "gemini"
        if cur_source == "sam" and gemini:
            r.polygon_pct = list(gemini)
            raw["_source"] = "gemini"
        elif cur_source == "gemini" and sam:
            r.polygon_pct = list(sam)
            raw["_source"] = "sam"
        else:
            return
        self._refresh_source_button(btn, r)

    def _set_all_source(self, target: str) -> None:
        """Flip every row's Source to `target` ('sam' or 'gemini') where
        the corresponding polygon is available.
        """
        if target not in ("sam", "gemini"):
            return
        for row in range(self._table.rowCount()):
            if row >= len(self._regions):
                break
            r = self._regions[row]
            raw = getattr(r, "raw", None) or {}
            gemini = raw.get("_polygon_pct_gemini")
            sam = raw.get("_polygon_pct_sam")
            if target == "sam" and sam:
                r.polygon_pct = list(sam)
                raw["_source"] = "sam"
            elif target == "gemini" and gemini:
                r.polygon_pct = list(gemini)
                raw["_source"] = "gemini"
            btn = self._table.cellWidget(row, 2)
            if isinstance(btn, QPushButton):
                self._refresh_source_button(btn, r)

    def _set_all_checked(self, checked: bool):
        for row in range(self._table.rowCount()):
            cell = self._table.cellWidget(row, 0)
            if cell is None:
                continue
            chk = cell.property("_chk_ref")
            if chk is not None:
                chk.setChecked(checked)

    # ── Import to canvas ─────────────────────────────────────────────────
    def _on_import(self):
        if not self._regions:
            return
        canvas = self.window().findChild(QWidget, "PlanCanvas")  # fallback
        # Primary lookup: via parent MainWindow attribute
        main = self.window()
        cw = getattr(main, "_plan", None)
        if cw is not None:
            canvas = cw
        img_w, img_h = (1000, 1000)
        if canvas is not None and hasattr(canvas, "canvas_image_size"):
            try:
                w, h = canvas.canvas_image_size
                if w > 0 and h > 0:
                    img_w, img_h = int(w), int(h)
            except Exception:
                pass

        polys: list[dict] = []
        for row, r in enumerate(self._regions):
            cell = self._table.cellWidget(row, 0)
            chk = cell.property("_chk_ref") if cell is not None else None
            if chk is None or not chk.isChecked():
                continue
            # Columns shifted after adding the "Source" toggle at col 2:
            #   0=Use, 1=Detected Label, 2=Source, 3=Building, 4=Entrance,
            #   5=Type Name.
            bld = (self._table.item(row, 3).text() or "1").strip() or "1"
            ent = (self._table.item(row, 4).text() or "1").strip() or "1"
            typ = (self._table.item(row, 5).text() or r.label).strip() or r.label

            poly_img = [(px * img_w, py * img_h) for (px, py) in r.polygon_pct]
            cx = sum(p[0] for p in poly_img) / len(poly_img)
            cy = sum(p[1] for p in poly_img) / len(poly_img)

            polys.append({
                "building_id": bld,
                "entrance_id": ent,
                "type_name":   typ,
                "uid":         _uuid_mod.uuid4().hex[:10],
                "extrusion_m": 3.0,
                "polygon_img": poly_img,
                "center_img":  (cx, cy),
                "committed":   False,
                "source":      "ai_import",
                "ai_label":    r.label,
            })

        if not polys:
            QMessageBox.information(self, "Nothing to import",
                                    "Tick at least one row first.")
            return
        self.polygons_ready.emit(polys)
        self._status.setText(
            f"Sent {len(polys)} polygon(s) to Floor Plan. "
            "Switch to Move mode and drag them into position.")


def _guess_type_name(label: str, idx: int) -> str:
    """Sanitize Florence-2 label into something usable as an apt-type key."""
    lbl = (label or "").strip()
    if not lbl:
        return f"APT_{idx + 1}"
    safe = "".join(ch if ch.isalnum() or ch in "_- " else "_" for ch in lbl)
    safe = safe.strip().replace(" ", "_")
    return safe or f"APT_{idx + 1}"
