# QGIS Web Map Exporter

A QGIS 3 plugin that exports selected layers to a **standalone HTML web map** powered by [Leaflet.js](https://leafletjs.com). Symbology is preserved.

## Features

- Export **vector layers** (point, line, polygon) as embedded GeoJSON with Leaflet path styles
- Export **raster layers** as base64-encoded PNG image overlays
- Preserves QGIS symbology:
  - Single symbol renderers (fill colour, stroke, weight)
  - Categorised renderers (per-category colours)
  - Graduated renderers (per-range colours)
  - Rule-based renderers (first-rule fallback)
- Optional OpenStreetMap basemap
- Optional layer toggle control
- Click features to see a popup with all attribute values
- Output is a **single `.html` file** — no server required, works offline (basemap tiles require internet)

## Requirements

- QGIS 3.0 or later

## Installation

```bash
python3 install_plugin.py
```

Then in QGIS: **Plugins → Manage and Install Plugins → Installed → Enable "QGIS Web Map Exporter"**.

Alternatively, copy the `qgis_webmap/` folder to your QGIS plugins directory:

| Platform | Path |
|----------|------|
| Linux    | `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/` |
| macOS    | `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/` |
| Windows  | `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\` |

## Usage

1. Open a QGIS project with one or more layers.
2. Go to **Web → Web Map Exporter → Export to Web Map…** (or click the toolbar icon).
3. Check the layers you want to include.
4. Choose whether to include an OSM basemap and layer toggle control.
5. Click **Browse…** to choose the output `.html` file path.
6. Click **Export**.
7. Open the generated `.html` file in any modern web browser.

## Plugin structure

```
qgis_webmap/
├── __init__.py          # QGIS entry point
├── metadata.txt         # Plugin metadata
├── plugin.py            # Plugin class (menu/toolbar wiring)
├── dialog.py            # Export dialog (layer selection, options)
├── exporter.py          # Core export logic (GeoJSON, symbology, HTML)
├── icon.png             # Toolbar icon
└── test_exporter_logic.py  # Offline unit tests
```

## Running tests

The tests mock QGIS and run with plain Python:

```bash
python3 qgis_webmap/test_exporter_logic.py
```
