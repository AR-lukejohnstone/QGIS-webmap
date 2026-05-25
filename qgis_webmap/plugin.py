import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QCoreApplication


class WebMapExporterPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None

    def tr(self, message):
        return QCoreApplication.translate("WebMapExporter", message)

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, self.tr("Export to Web Map…"), self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToWebMenu(self.tr("Web Map Exporter"), self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginWebMenu(self.tr("Web Map Exporter"), self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        from .dialog import WebMapExportDialog
        dlg = WebMapExportDialog(self.iface)
        dlg.exec_()
