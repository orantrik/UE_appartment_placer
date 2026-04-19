from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QCheckBox, QLabel, QLineEdit, QTextEdit, QApplication, QFileDialog, QMessageBox,
)
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont


class OutputPanel(QWidget):
    generate_requested = pyqtSignal()
    generate_volumes_requested = pyqtSignal()
    floor_gaps_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._script = ""
        self._obj_files: dict[str, str] = {}
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Project name row — used as prefix for the unique mesh folder in UE
        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel("Project Name:"))
        self._proj_edit = QLineEdit()
        self._proj_edit.setPlaceholderText("e.g. Soho  →  /Game/ApartmentMeshes/Soho_<hash>/…")
        self._proj_edit.setToolTip(
            "Used as the prefix of the unique folder created in the UE Content "
            "Browser for this batch of meshes.\n"
            "Final path: /Game/ApartmentMeshes/<ProjectName>_<8-char hash>/<Type>/<Mesh>\n"
            "A fresh hash is generated every time you press 'Generate Volume Script', "
            "so re-imports never overwrite previous batches.")
        proj_row.addWidget(self._proj_edit)
        layout.addLayout(proj_row)

        # Blueprint path row
        bp_row = QHBoxLayout()
        bp_row.addWidget(QLabel("Blueprint Path:"))
        self.bp_edit = QLineEdit("/Game/BP_Apartment.BP_Apartment_C")
        self.bp_edit.setPlaceholderText("/Game/YourFolder/BP_Name.BP_Name_C")
        bp_row.addWidget(self.bp_edit)
        layout.addLayout(bp_row)

        # Generate button
        gen_btn = QPushButton("⚡  Generate Script")
        gen_btn.setStyleSheet(
            "QPushButton { background:#2d6a4f; color:white; font-weight:bold;"
            " padding:6px; border-radius:4px; }"
            "QPushButton:hover { background:#40916c; }"
        )
        gen_btn.clicked.connect(self.generate_requested)
        layout.addWidget(gen_btn)

        # Generate Volume Script button
        gen_vol_btn = QPushButton("⬛  Generate Volume Script")
        gen_vol_btn.setStyleSheet(
            "QPushButton { background:#5c3317; color:white; font-weight:bold;"
            " padding:6px; border-radius:4px; }"
            "QPushButton:hover { background:#8b4513; }"
        )
        gen_vol_btn.clicked.connect(self.generate_volumes_requested)
        layout.addWidget(gen_vol_btn)

        # Floor gaps configurator — only affects Z per floor, no mesh changes.
        gaps_btn = QPushButton("↕  Floor Gaps…")
        gaps_btn.setToolTip(
            "Configure custom Z gaps between specific consecutive floors.\n"
            "Default stacking is floor × 3 m. Override, for example, the "
            "floor 1 → 2 gap to 9 m and every floor above shifts up by 6 m."
        )
        gaps_btn.clicked.connect(self.floor_gaps_requested)
        layout.addWidget(gaps_btn)

        # Folder hierarchy option
        self._folders_cb = QCheckBox("🗂  World Outliner folder hierarchy  (Apartments / Building / Entrance / Type)")
        self._folders_cb.setChecked(True)
        self._folders_cb.setToolTip(
            "When checked, spawned actors are grouped into nested folders in the UE World Outliner.\n"
            "Uncheck to spawn all actors flat (no folders)."
        )
        layout.addWidget(self._folders_cb)

        # ── Spawn mode ─────────────────────────────────────────────────────
        self._poi_cb = QCheckBox("🏠  Spawn as BP_POI  (sets POI_Geometry mesh + Row Name = Number)")
        self._poi_cb.setChecked(False)
        self._poi_cb.setToolTip(
            "When checked, spawns instances of your BP_POI blueprint instead of plain StaticMeshActors.\n"
            "• POI_Geometry component receives the apartment-type mesh\n"
            "• Row Name variable is set to the apartment's Number value\n"
            "The blueprint itself is NOT modified — only per-instance values are changed."
        )
        layout.addWidget(self._poi_cb)

        poi_path_row = QHBoxLayout()
        poi_path_row.addWidget(QLabel("  BP_POI Path:"))
        self._poi_path_edit = QLineEdit("/Game/ArchVizExplorer/Blueprints/BP__Persistant_POI.BP__Persistant_POI_C")
        self._poi_path_edit.setPlaceholderText("/Game/YourFolder/BP__Persistant_POI.BP__Persistant_POI_C")
        self._poi_path_edit.setEnabled(False)
        poi_path_row.addWidget(self._poi_path_edit)
        layout.addLayout(poi_path_row)

        # Enable / disable path field together with the checkbox
        self._poi_cb.toggled.connect(self._poi_path_edit.setEnabled)

        # Script output
        self._editor = QTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setFont(QFont("Consolas", 9))
        self._editor.setPlaceholderText("Generated script will appear here…")
        layout.addWidget(self._editor)

        # Bottom buttons
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(self._copy)
        save_btn = QPushButton("Save As…")
        save_btn.clicked.connect(self._save)
        folder_btn = QPushButton("📁  Save Folder…")
        folder_btn.clicked.connect(self._save_folder)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(folder_btn)
        layout.addLayout(btn_row)

    @property
    def use_folders(self) -> bool:
        return self._folders_cb.isChecked()

    @property
    def use_poi(self) -> bool:
        return self._poi_cb.isChecked()

    @property
    def poi_bp_path(self) -> str:
        return self._poi_path_edit.text().strip()

    @property
    def project_name(self) -> str:
        return self._proj_edit.text().strip()

    def set_project_name(self, name: str):
        self._proj_edit.setText(name or "")

    def set_script(self, text: str):
        self._script = text
        self._editor.setPlainText(text)

    def set_volumes(self, script: str, obj_files: dict[str, str]):
        self._script = script
        self._obj_files = obj_files
        self._editor.setPlainText(script)

    def show_error(self, msg: str):
        QMessageBox.warning(self, "Cannot Generate", msg)

    def _copy(self):
        if self._script:
            QApplication.clipboard().setText(self._script)

    def _save(self):
        if not self._script:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Script", "spawn_apartments.py",
            "Python Files (*.py);;All Files (*)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._script)

    def _save_folder(self):
        if not self._script:
            return
        import os
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if not folder:
            return
        # Use volumes filename when OBJ meshes are bundled, otherwise use the generic name
        script_name = "spawn_volumes.py" if self._obj_files else "spawn_apartments.py"
        with open(os.path.join(folder, script_name), "w", encoding="utf-8") as f:
            f.write(self._script)
        for rel_path, content in self._obj_files.items():
            abs_path = os.path.join(folder, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
        obj_note = f" + {len(self._obj_files)} OBJ file(s)" if self._obj_files else ""
        QMessageBox.information(
            self, "Saved",
            f"Saved {script_name}{obj_note} to:\n{folder}")
