from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QTabWidget, QStatusBar,
    QDialog, QFormLayout, QDoubleSpinBox, QDialogButtonBox, QMessageBox,
)
from app.core.data_model import AppData
from app.core import generator
from app.widgets.import_panel import ImportPanel
from app.widgets.mapping_panel import MappingPanel
from app.widgets.output_panel import OutputPanel
from app.widgets.plan_canvas import PlanCanvas


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UE Apartment Placer")
        self.resize(1280, 760)
        self._data = AppData()
        self._build_ui()
        self._wire_signals()

    # ── UI construction ────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        tabs = QTabWidget()

        # Data tab — file browse + full data table
        self._import = ImportPanel()
        tabs.addTab(self._import, "📊  Data")

        # Field mapping
        self._mapping = MappingPanel()
        tabs.addTab(self._mapping, "🗺  Field Mapping")

        # Floor plan canvas
        self._plan = PlanCanvas()
        tabs.addTab(self._plan, "🏢  Floor Plan")

        # Script output
        self._output = OutputPanel()
        tabs.addTab(self._output, "📄  Script")

        self._tabs = tabs
        root.addWidget(tabs)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Load a file to begin.")

    # ── Signal wiring ──────────────────────────────────────────────────
    def _wire_signals(self):
        self._import.file_loaded.connect(self._on_file_loaded)
        self._mapping.mappings_changed.connect(self._on_mappings_changed)
        self._plan.calibration_changed.connect(self._on_calibration_changed)
        self._output.generate_requested.connect(self._on_generate)
        self._output.generate_volumes_requested.connect(self._on_generate_volumes)
        self._plan.auto_place_requested.connect(self._on_auto_place)

    # ── Slots ──────────────────────────────────────────────────────────
    def _on_file_loaded(self, df):
        self._data.df = df
        self._mapping.refresh_columns(df.columns.tolist())
        # refresh_columns blocks signals — manually sync mappings now
        self._data.required_mappings = self._mapping.get_required()
        self._data.extra_mappings    = self._mapping.get_extra()
        self._status.showMessage(
            f"Loaded {len(df)} rows × {len(df.columns)} columns."
        )
        self._refresh_canvas_ids()
        self._refresh_coverage()

    def _on_calibration_changed(self, cal: dict):
        self._data.calibration = cal
        self._refresh_coverage()

    def _refresh_coverage(self):
        """Recolour import table rows based on which polygons have been drawn."""
        try:
            cal  = self._data.calibration or {}
            rm   = self._data.required_mappings or {}
            b_col = rm.get("building",  "")
            e_col = rm.get("entrance",  "")
            t_col = rm.get("type",      "")
            if not (b_col and e_col and t_col):
                return
            df = self._data.df
            if df is None:
                return
            # Verify all three columns actually exist in the dataframe
            if not all(c in df.columns for c in (b_col, e_col, t_col)):
                return
            drawn_keys = {
                (p["building_id"], p["entrance_id"], p["type_name"])
                for p in cal.get("apt_type_polygons", [])
            }
            self._import.refresh_coverage(drawn_keys, self._data, b_col, e_col, t_col)
        except Exception:
            pass  # never let coverage colouring crash the app

    def _on_mappings_changed(self):
        self._data.required_mappings = self._mapping.get_required()
        self._data.extra_mappings    = self._mapping.get_extra()
        self._refresh_canvas_ids()
        self._refresh_coverage()

    def _refresh_canvas_ids(self):
        df = self._data.df
        if df is None:
            return
        rm = self._data.required_mappings
        for method, key in [
            ("set_building_ids", "building"),
            ("set_apt_types",    "type"),
            ("set_entrance_ids", "entrance"),
        ]:
            col = rm.get(key)
            if col and col in df.columns:
                ids = df[col].dropna().unique().tolist()
                getattr(self._plan, method)([str(i) for i in ids])

    def _on_generate(self):
        self._data.blueprint_path = self._output.bp_edit.text().strip()
        try:
            script = generator.generate(self._data)
            self._output.set_script(script)
            n_cal = len(self._data.calibration.get("entrances", []))
            msg = "Script generated successfully."
            if n_cal:
                msg += f"  (Floor plan: {n_cal} entrance positions used)"
            self._status.showMessage(msg)
            self._tabs.setCurrentWidget(self._output)
        except Exception as e:
            import traceback
            detail = traceback.format_exc()
            self._output.show_error(f"{e}\n\n{detail}")
            self._status.showMessage(f"Error: {e}")

    def _on_generate_volumes(self):
        try:
            use_folders  = self._output.use_folders
            use_poi      = self._output.use_poi
            poi_bp_path  = self._output.poi_bp_path
            project_name = self._output.project_name
            self._data.project_name = project_name
            script, obj_files = generator.generate_volumes(
                self._data,
                use_folders=use_folders,
                use_poi=use_poi,
                poi_bp_path=poi_bp_path,
                project_name=project_name,
            )
            self._output.set_volumes(script, obj_files)
            self._status.showMessage(
                f"Volume script generated — {len(obj_files)} OBJ mesh file(s) ready."
            )
            self._tabs.setCurrentWidget(self._output)
        except Exception as e:
            import traceback
            detail = traceback.format_exc()
            self._output.show_error(f"{e}\n\n{detail}")
            self._status.showMessage(f"Error: {e}")

    def _on_auto_place(self):
        import pandas as _pd
        df = self._data.df
        rm = self._data.required_mappings
        if df is None:
            QMessageBox.warning(self, "No Data", "Load a data file first.")
            return
        b_col = rm.get("building")
        e_col = rm.get("entrance")
        t_col = rm.get("type")
        if not (b_col and e_col and t_col):
            QMessageBox.warning(self, "Missing Mappings",
                                "Map Building, Entrance, and Type columns first.")
            return
        d_col = rm.get("direction", "")
        req_cols = [c for c in [b_col, e_col, t_col] if c]
        extra_cols = [d_col] if (d_col and d_col in df.columns) else []
        sub = df[req_cols + extra_cols].dropna(subset=req_cols)
        combos = []
        seen = set()
        for _, r in sub.iterrows():
            direction = ""
            if extra_cols:
                val = r[d_col]
                if not (isinstance(val, float) and _pd.isna(val)):
                    direction = str(val).strip()
            key = (str(r[b_col]), str(r[e_col]), str(r[t_col]), direction)
            if key not in seen:
                seen.add(key)
                combos.append(key)
        if not combos:
            QMessageBox.warning(self, "No Data",
                                "No (building, entrance, type) combinations found.")
            return

        dlg = _AutoPlaceDialog(self, len(combos))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._plan.do_auto_place(combos, dlg.params())
        self._tabs.setCurrentWidget(self._plan)


class _AutoPlaceDialog(QDialog):
    """Parameter dialog for the Auto-Place polygon generator."""

    def __init__(self, parent, n_combos: int):
        super().__init__(parent)
        self.setWindowTitle("Auto-Place Polygons")
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        def _spin(lo, hi, val, suffix="  m", dec=1):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            s.setSuffix(suffix)
            s.setDecimals(dec)
            return s

        self._area    = _spin(1,  5000, 80,  "  m²", 0)
        self._type_g  = _spin(0,   100,  2)
        self._ent_g   = _spin(0,   500,  5)
        self._bld_g   = _spin(0,  1000, 15)
        self._dir_spr = _spin(0,  5000, 50)
        self._extrude = _spin(0.1,  99,  3)

        layout.addRow(f"Unit area  ({n_combos} unique types):", self._area)
        layout.addRow("Gap between types (same entrance):",     self._type_g)
        layout.addRow("Gap between entrances:",                 self._ent_g)
        layout.addRow("Gap between buildings:",                 self._bld_g)
        layout.addRow("Direction zone spread:",                 self._dir_spr)
        layout.addRow("Extrusion height:",                      self._extrude)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def params(self) -> dict:
        return {
            "area_m2":        self._area.value(),
            "type_gap_m":     self._type_g.value(),
            "entrance_gap_m": self._ent_g.value(),
            "building_gap_m": self._bld_g.value(),
            "dir_spread_m":   self._dir_spr.value(),
            "extrusion_m":    self._extrude.value(),
        }
