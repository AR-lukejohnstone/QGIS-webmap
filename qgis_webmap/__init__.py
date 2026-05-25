def classFactory(iface):
    from .plugin import WebMapExporterPlugin
    return WebMapExporterPlugin(iface)
