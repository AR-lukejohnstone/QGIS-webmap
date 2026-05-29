import os
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QLineEdit,
    QMessageBox, QProgressBar, QCheckBox, QGroupBox
)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsMapLayer


class WebMapExportDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Export to Web Map")
        self.setMinimumWidth(480)
        self._build_ui()
        self._populate_layers()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Layer selection
        layer_group = QGroupBox("Layers to export")
        layer_layout = QVBoxLayout(layer_group)

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self._select_all)
        deselect_btn = QPushButton("Deselect All")
        deselect_btn.clicked.connect(self._deselect_all)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(deselect_btn)
        btn_row.addStretch()
        layer_layout.addLayout(btn_row)

        self.layer_list = QListWidget()
        self.layer_list.setMinimumHeight(200)
        layer_layout.addWidget(self.layer_list)
        layout.addWidget(layer_group)

        # Options
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout(options_group)
        self.include_basemap_cb = QCheckBox("Include OpenStreetMap basemap")
        self.include_basemap_cb.setChecked(True)
        self.layer_control_cb = QCheckBox("Include legend / layer control (toggles + transparency)")
        self.layer_control_cb.setChecked(True)
        options_layout.addWidget(self.include_basemap_cb)
        options_layout.addWidget(self.layer_control_cb)
        layout.addWidget(options_group)

        # Output path
        path_group = QGroupBox("Output file")
        path_layout = QHBoxLayout(path_group)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Select output HTML file…")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(self.path_edit)
        path_layout.addWidget(browse_btn)
        layout.addWidget(path_group)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # Buttons
        bottom = QHBoxLayout()
        self.export_btn = QPushButton("Export")
        self.export_btn.setDefault(True)
        self.export_btn.clicked.connect(self._export)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        bottom.addStretch()
        bottom.addWidget(self.export_btn)
        bottom.addWidget(cancel_btn)
        layout.addLayout(bottom)

    def _populate_layers(self):
        self.layer_list.clear()
        root = QgsProject.instance().layerTreeRoot()
        # Preserve layer order from legend (top to bottom)
        for tree_layer in root.findLayers():
            layer = tree_layer.layer()
            if layer is None:
                continue
            if layer.type() not in (QgsMapLayer.VectorLayer, QgsMapLayer.RasterLayer):
                continue
            item = QListWidgetItem(layer.name())
            item.setData(Qt.UserRole, layer.id())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if tree_layer.isVisible() else Qt.Unchecked)
            self.layer_list.addItem(item)

    def _select_all(self):
        for i in range(self.layer_list.count()):
            self.layer_list.item(i).setCheckState(Qt.Checked)

    def _deselect_all(self):
        for i in range(self.layer_list.count()):
            self.layer_list.item(i).setCheckState(Qt.Unchecked)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Web Map", "", "HTML Files (*.html);;All Files (*)"
        )
        if path:
            if not path.lower().endswith(".html"):
                path += ".html"
            self.path_edit.setText(path)

    def _export(self):
        output_path = self.path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "No output file", "Please select an output file path.")
            return

        selected_ids = []
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if item.checkState() == Qt.Checked:
                selected_ids.append(item.data(Qt.UserRole))

        if not selected_ids:
            QMessageBox.warning(self, "No layers", "Please select at least one layer to export.")
            return

        layers = []
        for layer_id in selected_ids:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer:
                layers.append(layer)

        # Reverse so bottom layers render first in Leaflet
        layers = list(reversed(layers))

        self.export_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(layers) + 1)
        self.progress.setValue(0)

        try:
            from .exporter import WebMapExporter
            exporter = WebMapExporter(
                layers=layers,
                output_path=output_path,
                include_basemap=self.include_basemap_cb.isChecked(),
                include_layer_control=self.layer_control_cb.isChecked(),
                progress_callback=lambda v: self.progress.setValue(v),
            )
            exporter.export()
            QMessageBox.information(
                self, "Export complete",
                f"Web map exported successfully to:\n{output_path}"
            )
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
        finally:
            self.export_btn.setEnabled(True)
            self.progress.setVisible(False)
