import os
import json
import base64
import tempfile
import urllib.request
from urllib.parse import parse_qs

from qgis.core import (
    QgsMapLayer, QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsProject, QgsRenderContext,
    QgsFeatureRequest, QgsSingleSymbolRenderer, QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer, QgsRuleBasedRenderer,
    QgsSymbol, QgsSimpleMarkerSymbolLayer, QgsSimpleLineSymbolLayer,
    QgsSimpleFillSymbolLayer, QgsSvgMarkerSymbolLayer,
    QgsSimpleMarkerSymbolLayerBase, QgsUnitTypes,
    QgsMapSettings, QgsRectangle
)
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import QSize, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest


_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

_PLUGIN_DIR      = os.path.dirname(__file__)
_LIB_DIR         = os.path.join(_PLUGIN_DIR, "lib")
_LEAFLET_VERSION = "1.9.4"
_LEAFLET_URLS = [
    "https://unpkg.com/leaflet@{v}/dist/leaflet.min.{ext}",
    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/{v}/leaflet.min.{ext}",
]


def _qgis_fetch(url_str: str) -> str | None:
    """
    Download text from url_str using QGIS's network stack (respects proxy /
    auth settings configured in QGIS options) with a fallback to urllib.
    Returns the decoded text or None on failure.
    """
    # QgsBlockingNetworkRequest available since QGIS 3.6
    try:
        from qgis.core import QgsBlockingNetworkRequest
        req = QNetworkRequest(QUrl(url_str))
        blocker = QgsBlockingNetworkRequest()
        err = blocker.get(req)
        if err == QgsBlockingNetworkRequest.NoError:
            return bytes(blocker.reply().content()).decode("utf-8")
    except Exception:
        pass

    # Plain urllib fallback
    try:
        with urllib.request.urlopen(url_str, timeout=20) as r:
            return r.read().decode("utf-8")
    except Exception:
        pass

    return None


_VENDOR_CSS = os.path.join(_PLUGIN_DIR, "vendor", "leaflet.css")
_VENDOR_JS  = os.path.join(_PLUGIN_DIR, "vendor", "leaflet.js")


def _get_leaflet_assets() -> tuple[str, str] | tuple[None, None]:
    """
    Return (css, js) strings for inline embedding.

    Priority:
      1. Bundled vendor/ files shipped with the plugin (always available).
      2. Previously downloaded + cached copy in lib/.
      3. Download from CDN and cache in lib/.
      4. Return (None, None) — caller falls back to CDN <link>/<script> tags.
    """
    # 1. Bundled files — committed to the repo, always present
    if os.path.exists(_VENDOR_CSS) and os.path.exists(_VENDOR_JS):
        with open(_VENDOR_CSS, encoding="utf-8") as f:
            css = f.read()
        with open(_VENDOR_JS, encoding="utf-8") as f:
            js = f.read()
        return css, js

    # 2. Previously cached download
    v        = _LEAFLET_VERSION
    css_path = os.path.join(_LIB_DIR, f"leaflet-{v}.min.css")
    js_path  = os.path.join(_LIB_DIR, f"leaflet-{v}.min.js")
    if os.path.exists(css_path) and os.path.exists(js_path):
        with open(css_path, encoding="utf-8") as f:
            css = f.read()
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        return css, js

    # 3. Download and cache
    os.makedirs(_LIB_DIR, exist_ok=True)
    for tpl in _LEAFLET_URLS:
        css = _qgis_fetch(tpl.format(v=v, ext="css"))
        js  = _qgis_fetch(tpl.format(v=v, ext="js"))
        if css and js:
            with open(css_path, "w", encoding="utf-8") as f:
                f.write(css)
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js)
            return css, js

    return None, None


def _parse_wms_source(layer) -> dict | None:
    """
    If layer is a WMS/WMTS raster layer, return a dict describing how to
    add it in Leaflet. Returns None for plain file-based rasters.
    """
    provider = layer.dataProvider()
    if provider is None or provider.name() != "wms":
        return None

    uri_str = provider.dataSourceUri()
    params  = parse_qs(uri_str, keep_blank_values=True)

    url = (params.get("url") or params.get("URL") or [None])[0]
    if not url:
        return None

    layers  = (params.get("layers")  or [""])[0]
    format_ = (params.get("format")  or ["image/png"])[0]
    styles  = (params.get("styles")  or [""])[0]
    crs     = (params.get("crs") or params.get("CRS") or
               params.get("srs") or params.get("SRS") or ["EPSG:3857"])[0]
    version = (params.get("version") or ["1.1.1"])[0]

    # WMTS / XYZ tile layers embed the tile URL template directly
    ttype = (params.get("type") or ["wms"])[0].lower()

    return {
        "wmsUrl":     url,
        "wmsLayers":  layers,
        "wmsFormat":  format_,
        "wmsStyles":  styles,
        "wmsCrs":     crs,
        "wmsVersion": version,
        "tileType":   ttype,
    }


def _color_to_hex(color: QColor) -> str:
    return "#{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue())


def _color_to_rgba(color: QColor) -> str:
    return "rgba({},{},{},{:.3f})".format(
        color.red(), color.green(), color.blue(), color.alphaF()
    )


def _size_to_px(size: float, unit) -> float:
    """Convert a QGIS symbol size in its render unit to approximate pixels (96 DPI)."""
    try:
        if unit == QgsUnitTypes.RenderPixels:
            return size
        if unit == QgsUnitTypes.RenderPoints:
            return size * 96.0 / 72.0
        if unit == QgsUnitTypes.RenderInches:
            return size * 96.0
        # Millimeters (QGIS default) and everything else
        return size * 96.0 / 25.4
    except Exception:
        # Fallback assuming millimeters
        return size * 96.0 / 25.4


# Map QGIS marker shape names to a small set the web map can draw.
_SHAPE_ALIASES = {
    "square": "square",
    "rectangle": "square",
    "square_with_corners": "square",
    "rounded_square": "square",
    "diamond": "diamond",
    "triangle": "triangle",
    "equilateral_triangle": "triangle",
    "star": "star",
    "regular_star": "star",
    "pentagon": "pentagon",
    "hexagon": "hexagon",
    "octagon": "octagon",
    "cross": "cross",
    "cross2": "x",
    "x": "x",
    "cross_fill": "square",
    "circle": "circle",
}


def _encode_marker_shape(sl) -> str:
    """Return a normalized shape name string for a simple marker symbol layer."""
    try:
        raw = QgsSimpleMarkerSymbolLayerBase.encodeShape(sl.shape())
        return _SHAPE_ALIASES.get(str(raw).lower(), "circle")
    except Exception:
        return "circle"


def _extract_symbol_style(symbol) -> dict:
    """Extract Leaflet path/marker style from a QGIS symbol."""
    style = {}
    if symbol is None:
        return style

    geom_type = symbol.type()  # 0=marker, 1=line, 2=fill

    # Walk symbol layers to find the primary paint layer
    for i in range(symbol.symbolLayerCount()):
        sl = symbol.symbolLayer(i)

        if isinstance(sl, QgsSimpleFillSymbolLayer):
            fill_color = sl.fillColor()
            stroke_color = sl.strokeColor()
            style["fillColor"] = _color_to_hex(fill_color)
            style["fillOpacity"] = round(fill_color.alphaF(), 3)
            style["color"] = _color_to_hex(stroke_color)
            style["opacity"] = round(stroke_color.alphaF(), 3)
            style["weight"] = round(sl.strokeWidth() * 2, 1) or 1
            break

        elif isinstance(sl, QgsSimpleLineSymbolLayer):
            color = sl.color()
            style["color"] = _color_to_hex(color)
            style["opacity"] = round(color.alphaF(), 3)
            style["weight"] = round(sl.width() * 2, 1) or 2
            style["fillOpacity"] = 0
            break

        elif isinstance(sl, QgsSimpleMarkerSymbolLayer):
            color = sl.color()
            stroke_color = sl.strokeColor()
            style["markerColor"] = _color_to_hex(color)
            style["markerOpacity"] = round(color.alphaF(), 3)
            style["markerStrokeColor"] = _color_to_hex(stroke_color)
            try:
                sw_px = _size_to_px(sl.strokeWidth(), sl.strokeWidthUnit())
            except Exception:
                sw_px = 1.0
            style["markerStrokeWidth"] = round(max(0.0, sw_px), 1)
            style["markerSize"] = max(4, round(_size_to_px(sl.size(), sl.sizeUnit())))
            style["markerShape"] = _encode_marker_shape(sl)
            try:
                style["markerAngle"] = round(sl.angle(), 1)
            except Exception:
                style["markerAngle"] = 0
            break

        elif isinstance(sl, QgsSvgMarkerSymbolLayer):
            color = sl.fillColor()
            style["markerColor"] = _color_to_hex(color)
            style["markerOpacity"] = round(color.alphaF(), 3)
            try:
                style["markerStrokeColor"] = _color_to_hex(sl.strokeColor())
            except Exception:
                pass
            style["markerSize"] = max(4, round(_size_to_px(sl.size(), sl.sizeUnit())))
            # SVG markers can't be reproduced exactly; approximate with a square
            style["markerShape"] = "square"
            break

    # Defaults for fill polygons if nothing matched
    if geom_type == QgsSymbol.Fill and "fillColor" not in style:
        c = symbol.color()
        style["fillColor"] = _color_to_hex(c)
        style["fillOpacity"] = round(c.alphaF(), 3)
        style["color"] = "#000000"
        style["weight"] = 1
        style["opacity"] = 1

    elif geom_type == QgsSymbol.Line and "color" not in style:
        c = symbol.color()
        style["color"] = _color_to_hex(c)
        style["opacity"] = round(c.alphaF(), 3)
        style["weight"] = 2
        style["fillOpacity"] = 0

    elif geom_type == QgsSymbol.Marker and "markerColor" not in style:
        c = symbol.color()
        style["markerColor"] = _color_to_hex(c)
        style["markerOpacity"] = round(c.alphaF(), 3)
        style["markerSize"] = 8

    return style


def _build_style_map(layer) -> dict:
    """
    Returns a dict describing how to style the layer in JS.

    Common shape:
      type: 'single' | 'categorized' | 'graduated' | 'rule'
      entries: list of legend items (for multi-symbol renderers)
      style: {...}   (for single)
      field: str     (for categorized / graduated)
      default: {...}
    """
    renderer = layer.renderer()
    if renderer is None:
        return {"type": "single", "style": {}}

    if isinstance(renderer, QgsSingleSymbolRenderer):
        return {
            "type": "single",
            "style": _extract_symbol_style(renderer.symbol()),
        }

    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        entries = []
        for cat in renderer.categories():
            entries.append({
                "value": str(cat.value()),
                "label": cat.label() or str(cat.value()),
                "style": _extract_symbol_style(cat.symbol()),
            })
        return {
            "type": "categorized",
            "field": renderer.classAttribute(),
            "entries": entries,
            "default": {},
        }

    if isinstance(renderer, QgsGraduatedSymbolRenderer):
        entries = []
        for r in renderer.ranges():
            entries.append({
                "min": r.lowerValue(),
                "max": r.upperValue(),
                "label": r.label() or f"{r.lowerValue()} – {r.upperValue()}",
                "style": _extract_symbol_style(r.symbol()),
            })
        return {
            "type": "graduated",
            "field": renderer.classAttribute(),
            "entries": entries,
            "default": {},
        }

    if isinstance(renderer, QgsRuleBasedRenderer):
        entries = []
        for rule in renderer.rootRule().children():
            entries.append({
                "label": rule.label() or "Rule",
                "style": _extract_symbol_style(rule.symbol()),
            })
        return {
            "type": "rule",
            "entries": entries,
            "default": entries[0]["style"] if entries else {},
        }

    # Fallback
    return {"type": "single", "style": {}}


def _layer_to_geojson(layer) -> dict:
    """Reproject and convert vector layer to GeoJSON dict."""
    transform = QgsCoordinateTransform(
        layer.crs(), _WGS84, QgsProject.instance()
    )

    features = []
    for feat in layer.getFeatures(QgsFeatureRequest()):
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            props = {k: (str(v) if v is not None else None) for k, v in feat.attributeMap().items()}
            features.append({"type": "Feature", "geometry": None, "properties": props})
            continue

        geom.transform(transform)
        geom_json = json.loads(geom.asJson())

        props = {}
        fields = layer.fields()
        for i, attr in enumerate(feat.attributes()):
            fname = fields[i].name()
            if attr is None:
                props[fname] = None
            elif isinstance(attr, (int, float, bool)):
                props[fname] = attr
            else:
                props[fname] = str(attr)

        features.append({
            "type": "Feature",
            "geometry": geom_json,
            "properties": props,
        })

    return {"type": "FeatureCollection", "features": features}


def _raster_to_base64(layer) -> tuple:
    """Render raster layer to PNG, return (base64_str, bounds_list [[s,w],[n,e]])."""
    extent = layer.extent()
    transform = QgsCoordinateTransform(layer.crs(), _WGS84, QgsProject.instance())
    wgs_extent = transform.transformBoundingBox(extent)

    width = 1024
    ratio = extent.height() / extent.width() if extent.width() > 0 else 1
    height = max(1, int(width * ratio))

    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setOutputSize(QSize(width, height))
    settings.setExtent(extent)
    settings.setDestinationCrs(layer.crs())
    settings.setBackgroundColor(QColor(0, 0, 0, 0))

    from qgis.core import QgsMapRendererParallelJob
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        img.save(tmp_path, "PNG")
        with open(tmp_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        os.unlink(tmp_path)

    bounds = [
        [wgs_extent.yMinimum(), wgs_extent.xMinimum()],
        [wgs_extent.yMaximum(), wgs_extent.xMaximum()],
    ]
    return b64, bounds


def _geom_type_str(layer) -> str:
    wkb = layer.wkbType()
    flat = QgsWkbTypes.flatType(wkb)
    if flat in (QgsWkbTypes.Point, QgsWkbTypes.MultiPoint):
        return "point"
    if flat in (QgsWkbTypes.LineString, QgsWkbTypes.MultiLineString):
        return "line"
    return "polygon"


class WebMapExporter:
    def __init__(self, layers, output_path, include_basemap=True,
                 include_layer_control=True, progress_callback=None):
        self.layers = layers
        self.output_path = output_path
        self.include_basemap = include_basemap
        self.include_layer_control = include_layer_control
        self.progress = progress_callback or (lambda v: None)

    def export(self):
        layer_defs = []
        step = 0

        for layer in self.layers:
            step += 1
            self.progress(step)

            if layer.type() == QgsMapLayer.VectorLayer:
                geojson = _layer_to_geojson(layer)
                style_map = _build_style_map(layer)
                geom_type = _geom_type_str(layer)
                layer_defs.append({
                    "kind": "vector",
                    "name": layer.name(),
                    "geomType": geom_type,
                    "geojson": geojson,
                    "styleMap": style_map,
                })

            elif layer.type() == QgsMapLayer.RasterLayer:
                wms = _parse_wms_source(layer)
                if wms:
                    # Reproject layer extent to WGS-84 for fitBounds
                    ext = layer.extent()
                    tr  = QgsCoordinateTransform(layer.crs(), _WGS84, QgsProject.instance())
                    wgs = tr.transformBoundingBox(ext)
                    layer_defs.append({
                        "kind":   "wms",
                        "name":   layer.name(),
                        "bounds": [
                            [wgs.yMinimum(), wgs.xMinimum()],
                            [wgs.yMaximum(), wgs.xMaximum()],
                        ],
                        **wms,
                    })
                else:
                    b64, bounds = _raster_to_base64(layer)
                    layer_defs.append({
                        "kind":   "raster",
                        "name":   layer.name(),
                        "data":   b64,
                        "bounds": bounds,
                    })

        self.progress(step + 1)

        # Compute overall bounds for map fitBounds
        all_bounds = self._overall_bounds(layer_defs)

        html = self._render_html(layer_defs, all_bounds)
        with open(self.output_path, "w", encoding="utf-8") as f:
            f.write(html)

    def _overall_bounds(self, layer_defs):
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for ld in layer_defs:
            if ld["kind"] in ("raster", "wms"):
                b = ld["bounds"]
                min_y = min(min_y, b[0][0])
                min_x = min(min_x, b[0][1])
                max_y = max(max_y, b[1][0])
                max_x = max(max_x, b[1][1])
            elif ld["kind"] == "vector":
                for feat in ld["geojson"]["features"]:
                    geom = feat.get("geometry")
                    if geom is None:
                        continue
                    for coord in _flatten_coords(geom):
                        min_x = min(min_x, coord[0])
                        min_y = min(min_y, coord[1])
                        max_x = max(max_x, coord[0])
                        max_y = max(max_y, coord[1])
        if min_x == float("inf"):
            return [[51.5, -0.1], [51.5, -0.1]]  # fallback: London
        return [[min_y, min_x], [max_y, max_x]]

    def _render_html(self, layer_defs, bounds) -> str:
        # Escape </script> in embedded JSON so it can't break the <script> block
        layers_json = json.dumps(layer_defs, separators=(",", ":")).replace(
            "</", "<\\/"
        )
        bounds_json = json.dumps(bounds)
        include_basemap = "true" if self.include_basemap else "false"
        include_legend = "true" if self.include_layer_control else "false"

        leaflet_css, leaflet_js = _get_leaflet_assets()
        if leaflet_css and leaflet_js:
            leaflet_head = (
                f"<style>\n{leaflet_css}\n</style>\n"
                f"<script>\n{leaflet_js}\n</script>"
            )
        else:
            # CDN fallback — requires internet access when the HTML is opened
            leaflet_head = (
                '<link rel="stylesheet"'
                ' href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"'
                ' crossorigin=""/>\n'
                '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"'
                ' crossorigin=""></script>'
            )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QGIS Web Map</title>
{leaflet_head}
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: sans-serif; }}
  #map {{ height: 100%; width: 100%; }}

  /* ── Legend panel ─────────────────────────────────────────────── */
  #legend {{
    position: absolute;
    top: 10px; right: 10px;
    z-index: 1000;
    background: rgba(255,255,255,0.96);
    border: 1px solid #bbb;
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    min-width: 180px;
    max-width: 260px;
    max-height: calc(100vh - 60px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  #legend-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 10px 6px;
    background: #f0f0f0;
    border-bottom: 1px solid #ddd;
    cursor: default;
    user-select: none;
    border-radius: 6px 6px 0 0;
  }}
  #legend-header span {{ font-weight: bold; font-size: 13px; color: #333; }}
  #legend-toggle-all {{
    font-size: 11px;
    color: #555;
    cursor: pointer;
    padding: 2px 5px;
    border-radius: 3px;
    border: 1px solid #ccc;
    background: #fff;
    line-height: 1.4;
  }}
  #legend-toggle-all:hover {{ background: #e8e8e8; }}
  #legend-body {{
    overflow-y: auto;
    padding: 4px 0;
  }}
  .legend-layer {{
    padding: 0;
  }}
  .legend-layer-row {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    cursor: pointer;
    user-select: none;
    transition: background 0.1s;
  }}
  .legend-layer-row:hover {{ background: #f5f5f5; }}
  .legend-layer-row input[type=checkbox] {{
    margin: 0;
    cursor: pointer;
    flex-shrink: 0;
  }}
  .legend-layer-name {{
    font-size: 12px;
    color: #222;
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .legend-expand {{
    font-size: 10px;
    color: #888;
    flex-shrink: 0;
    transition: transform 0.2s;
  }}
  .legend-entries {{
    display: none;
    padding: 0 0 3px 26px;
  }}
  .legend-entries.open {{ display: block; }}
  .legend-entry {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 10px 2px 0;
  }}
  .legend-entry-label {{
    font-size: 11px;
    color: #444;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .legend-swatch svg {{ display: block; }}
  .legend-layer.hidden .legend-layer-name {{ opacity: 0.45; }}
  .legend-opacity {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 10px 5px 26px;
  }}
  .legend-opacity-label {{ font-size: 10px; color: #888; flex-shrink: 0; }}
  .legend-opacity input[type=range] {{ flex: 1; height: 14px; cursor: pointer; }}
  .qgis-marker {{ background: none; border: none; }}

  /* ── Filter toolbar ───────────────────────────────────────────── */
  #filterbar {{
    position: absolute;
    top: 10px; left: 50px;
    z-index: 1000;
    background: rgba(255,255,255,0.96);
    border: 1px solid #bbb;
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    padding: 6px 8px;
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    max-width: calc(100vw - 320px);
    font-size: 12px;
  }}
  #filterbar label {{ font-weight: bold; color: #444; }}
  #filterbar select {{
    font-size: 12px;
    padding: 3px 4px;
    border: 1px solid #ccc;
    border-radius: 3px;
    background: #fff;
    max-width: 160px;
  }}
  #filterbar button {{
    font-size: 12px;
    padding: 3px 8px;
    border: 1px solid #ccc;
    border-radius: 3px;
    background: #fff;
    cursor: pointer;
  }}
  #filterbar button:hover {{ background: #eee; }}
  #filter-values-wrap {{ position: relative; }}
  #filter-values-btn {{
    min-width: 140px;
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  #filter-values-panel {{
    display: none;
    position: absolute;
    top: 100%;
    left: 0;
    margin-top: 2px;
    background: #fff;
    border: 1px solid #bbb;
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    min-width: 200px;
    max-width: 280px;
    z-index: 1100;
  }}
  #filter-values-panel.open {{ display: block; }}
  #filter-values-search {{
    width: 100%;
    box-sizing: border-box;
    border: none;
    border-bottom: 1px solid #ddd;
    padding: 6px 8px;
    font-size: 12px;
    outline: none;
  }}
  #filter-values-list {{
    max-height: 240px;
    overflow-y: auto;
    padding: 4px 0;
  }}
  .filter-value-item {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    cursor: pointer;
  }}
  .filter-value-item:hover {{ background: #f3f3f3; }}
  .filter-value-item span {{
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .filter-count {{ color: #888; font-size: 11px; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="filterbar" style="display:none">
  <label>Filter</label>
  <select id="filter-layer" title="Layer"></select>
  <select id="filter-attr" title="Attribute"></select>
  <span id="filter-values-wrap">
    <button id="filter-values-btn" type="button">All values</button>
    <div id="filter-values-panel">
      <input id="filter-values-search" type="text" placeholder="Type to search / filter…" autocomplete="off">
      <div id="filter-values-list"></div>
    </div>
  </span>
  <button id="filter-clear" type="button">Clear</button>
  <span id="filter-count" class="filter-count"></span>
</div>
<div id="legend" style="display:none"></div>
<script>
(function() {{
  "use strict";

  var map = L.map('map');
  var bounds = {bounds_json};
  var LAYERS = {layers_json};
  var INCLUDE_BASEMAP = {include_basemap};
  var INCLUDE_LEGEND = {include_legend};

  // ── Basemap ──────────────────────────────────────────────────────────────
  var basemap = null;
  if (INCLUDE_BASEMAP) {{
    basemap = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19
    }}).addTo(map);
  }}

  // ── Helpers ──────────────────────────────────────────────────────────────
  function escHtml(s) {{
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  // Return the inner SVG element(s) for a marker shape centred at (cx, cy)
  // with circumradius r. Used by both map markers and legend swatches.
  function shapeSvgInner(shape, cx, cy, r, fill, fillOp, stroke, strokeW) {{
    var attrs = ' fill="' + escHtml(fill) + '" fill-opacity="' + fillOp + '"'
              + ' stroke="' + escHtml(stroke) + '" stroke-width="' + strokeW + '"';
    function poly(pts) {{
      return '<polygon points="' + pts.map(function(p) {{ return p[0] + ',' + p[1]; }}).join(' ') + '"' + attrs + '/>';
    }}
    function regular(n, rot) {{
      var pts = [];
      for (var i = 0; i < n; i++) {{
        var a = rot + i * 2 * Math.PI / n;
        pts.push([(cx + r * Math.sin(a)).toFixed(2), (cy - r * Math.cos(a)).toFixed(2)]);
      }}
      return poly(pts);
    }}
    function starPts(points, outer, inner, rot) {{
      var pts = [];
      for (var i = 0; i < points * 2; i++) {{
        var rad = (i % 2 === 0) ? outer : inner;
        var a = rot + i * Math.PI / points;
        pts.push([(cx + rad * Math.sin(a)).toFixed(2), (cy - rad * Math.cos(a)).toFixed(2)]);
      }}
      return poly(pts);
    }}
    switch (shape) {{
      case 'square':
        return '<rect x="' + (cx - r) + '" y="' + (cy - r) + '" width="' + (2 * r) + '" height="' + (2 * r) + '"' + attrs + '/>';
      case 'diamond':
        return poly([[cx, cy - r], [cx + r, cy], [cx, cy + r], [cx - r, cy]]);
      case 'triangle':
        return regular(3, 0);
      case 'pentagon':
        return regular(5, 0);
      case 'hexagon':
        return regular(6, 0);
      case 'octagon':
        return regular(8, Math.PI / 8);
      case 'star':
        return starPts(5, r, r * 0.5, 0);
      case 'cross':
        return '<path d="M' + cx + ' ' + (cy - r) + ' V' + (cy + r) + ' M' + (cx - r) + ' ' + cy + ' H' + (cx + r) + '"'
             + ' stroke="' + escHtml(stroke !== 'none' ? stroke : fill) + '" stroke-width="' + Math.max(1.5, strokeW * 2) + '" fill="none"/>';
      case 'x':
        return '<path d="M' + (cx - r) + ' ' + (cy - r) + ' L' + (cx + r) + ' ' + (cy + r)
             + ' M' + (cx + r) + ' ' + (cy - r) + ' L' + (cx - r) + ' ' + (cy + r) + '"'
             + ' stroke="' + escHtml(stroke !== 'none' ? stroke : fill) + '" stroke-width="' + Math.max(1.5, strokeW * 2) + '" fill="none"/>';
      default: // circle
        return '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '"' + attrs + '/>';
    }}
  }}

  function makeMarker(latlng, style, paneName) {{
    var size = style.markerSize || 8;
    var shape = style.markerShape || 'circle';
    var fill = style.markerColor || '#3388ff';
    var fillOp = style.markerOpacity != null ? style.markerOpacity : 0.9;
    var stroke = style.markerStrokeColor || '#ffffff';
    var strokeW = style.markerStrokeWidth != null ? style.markerStrokeWidth : 1;

    // Circles scale crisply with L.circleMarker
    if (shape === 'circle') {{
      var copts = {{
        radius: size / 2,
        fillColor: fill,
        fillOpacity: fillOp,
        color: stroke,
        weight: strokeW,
        opacity: 1
      }};
      if (paneName) copts.pane = paneName;
      return L.circleMarker(latlng, copts);
    }}

    // All other shapes drawn as an inline-SVG divIcon
    var pad = strokeW + 2;
    var box = size + pad * 2;
    var c = box / 2;
    var r = size / 2;
    var inner = shapeSvgInner(shape, c, c, r, fill, fillOp, stroke, strokeW);
    var angle = style.markerAngle || 0;
    var rot = angle ? ' transform="rotate(' + angle + ' ' + c + ' ' + c + ')"' : '';
    var html = '<svg width="' + box + '" height="' + box + '" xmlns="http://www.w3.org/2000/svg">'
             + '<g' + rot + '>' + inner + '</g></svg>';
    var icon = L.divIcon({{
      html: html,
      className: 'qgis-marker',
      iconSize: [box, box],
      iconAnchor: [c, c]
    }});
    var mopts = {{ icon: icon }};
    if (paneName) mopts.pane = paneName;
    return L.marker(latlng, mopts);
  }}

  function resolveStyle(styleMap, props) {{
    var t = styleMap.type;
    if (t === 'single') return styleMap.style;
    if (t === 'categorized') {{
      var val = String(props[styleMap.field]);
      for (var i = 0; i < styleMap.entries.length; i++) {{
        if (styleMap.entries[i].value === val) return styleMap.entries[i].style;
      }}
      return styleMap.default || {{}};
    }}
    if (t === 'graduated') {{
      var v = parseFloat(props[styleMap.field]);
      for (var i = 0; i < styleMap.entries.length; i++) {{
        var e = styleMap.entries[i];
        if (v >= e.min && v <= e.max) return e.style;
      }}
      return styleMap.default || {{}};
    }}
    if (t === 'rule') {{
      return (styleMap.entries[0] && styleMap.entries[0].style) || styleMap.default || {{}};
    }}
    return {{}};
  }}

  function leafletPathStyle(s) {{
    return {{
      color: s.color || '#3388ff',
      weight: s.weight != null ? s.weight : 2,
      opacity: s.opacity != null ? s.opacity : 1,
      fillColor: s.fillColor || s.color || '#3388ff',
      fillOpacity: s.fillOpacity != null ? s.fillOpacity : 0.4
    }};
  }}

  // ── Swatch SVG ───────────────────────────────────────────────────────────
  function swatchSvg(geomType, style) {{
    var W = 20, H = 16;
    var svg = '<svg width="' + W + '" height="' + H + '" xmlns="http://www.w3.org/2000/svg">';
    if (geomType === 'point') {{
      var r = Math.min(6, Math.max(3, (style.markerSize || 8) / 2));
      var cx = W / 2, cy = H / 2;
      svg += shapeSvgInner(
        style.markerShape || 'circle', cx, cy, r,
        style.markerColor || '#3388ff',
        style.markerOpacity != null ? style.markerOpacity : 0.9,
        style.markerStrokeColor || '#666',
        Math.min(1.5, style.markerStrokeWidth != null ? style.markerStrokeWidth : 1)
      );
    }} else if (geomType === 'line') {{
      var w = Math.min(5, Math.max(1, style.weight || 2));
      svg += '<line x1="1" y1="' + (H/2) + '" x2="' + (W-1) + '" y2="' + (H/2) + '"'
          + ' stroke="' + escHtml(style.color || '#3388ff') + '"'
          + ' stroke-opacity="' + (style.opacity != null ? style.opacity : 1) + '"'
          + ' stroke-width="' + w + '"/>';
    }} else if (geomType === 'raster') {{
      svg += '<defs><pattern id="hatch" patternUnits="userSpaceOnUse" width="4" height="4">'
          + '<path d="M0,4 L4,0" stroke="#777" stroke-width="1"/></pattern></defs>'
          + '<rect x="1" y="1" width="' + (W-2) + '" height="' + (H-2) + '"'
          + ' fill="url(#hatch)" stroke="#999" stroke-width="1"/>';
    }} else {{
      svg += '<rect x="1" y="1" width="' + (W-2) + '" height="' + (H-2) + '"'
          + ' fill="' + escHtml(style.fillColor || '#3388ff') + '"'
          + ' fill-opacity="' + (style.fillOpacity != null ? style.fillOpacity : 0.4) + '"'
          + ' stroke="' + escHtml(style.color || '#333') + '"'
          + ' stroke-opacity="' + (style.opacity != null ? style.opacity : 1) + '"'
          + ' stroke-width="' + Math.min(3, style.weight || 1) + '"/>';
    }}
    return svg + '</svg>';
  }}

  // ── Layer builder ────────────────────────────────────────────────────────
  function buildVectorLayer(item) {{
    var ld = item.ld;
    var opts = {{
      pane: item.paneName,
      onEachFeature: onEachFeature,
      filter: function(feature) {{
        return item.filterFn ? item.filterFn(feature) : true;
      }}
    }};
    if (ld.geomType === 'point') {{
      opts.pointToLayer = function(feature, latlng) {{
        return makeMarker(latlng, resolveStyle(ld.styleMap, feature.properties || {{}}), item.paneName);
      }};
    }} else {{
      opts.style = function(feature) {{
        return leafletPathStyle(resolveStyle(ld.styleMap, feature.properties || {{}}));
      }};
    }}
    return L.geoJSON(ld.geojson, opts);
  }}

  function buildRasterLayer(item) {{
    return L.imageOverlay('data:image/png;base64,' + item.ld.data, item.ld.bounds, {{
      opacity: 1, pane: item.paneName
    }});
  }}

  function buildWmsLayer(item) {{
    var ld = item.ld;
    if (ld.tileType === 'xyz') {{
      return L.tileLayer(ld.wmsUrl, {{ pane: item.paneName }});
    }}
    return L.tileLayer.wms(ld.wmsUrl, {{
      layers:      ld.wmsLayers,
      format:      ld.wmsFormat  || 'image/png',
      styles:      ld.wmsStyles  || '',
      version:     ld.wmsVersion || '1.1.1',
      transparent: true,
      opacity:     1,
      pane:        item.paneName
    }});
  }}

  function buildLayer(item) {{
    if (item.ld.kind === 'vector') return buildVectorLayer(item);
    if (item.ld.kind === 'wms')    return buildWmsLayer(item);
    return buildRasterLayer(item);
  }}

  // Rebuild a layer in place (used after a filter change), preserving visibility.
  function rebuildLayer(item) {{
    var wasVisible = item.visible;
    if (item.lfl) map.removeLayer(item.lfl);
    item.lfl = buildLayer(item);
    if (wasVisible) item.lfl.addTo(map);
  }}

  function onEachFeature(feature, layer) {{
    if (!feature.properties) return;
    var rows = Object.entries(feature.properties)
      .filter(function(e) {{ return e[1] != null; }})
      .map(function(e) {{
        return '<tr><th style="text-align:left;padding:2px 8px 2px 0;white-space:nowrap">'
          + escHtml(e[0]) + '</th><td style="padding:2px 0">' + escHtml(e[1]) + '</td></tr>';
      }}).join('');
    if (rows) layer.bindPopup('<table style="font-size:13px;border-collapse:collapse">' + rows + '</table>');
  }}

  // Build Leaflet layers and collect metadata for legend.
  // Each layer gets a dedicated map pane so its opacity can be controlled
  // uniformly (works for vector markers, paths, rasters and WMS alike).
  var legendItems = [];
  for (var i = 0; i < LAYERS.length; i++) {{
    var paneName = 'layerPane' + i;
    map.createPane(paneName);
    map.getPane(paneName).style.zIndex = 400 + i;
    var item = {{
      ld: LAYERS[i], paneName: paneName, visible: true,
      filterFn: null, lfl: null, index: i
    }};
    item.lfl = buildLayer(item);
    item.lfl.addTo(map);
    legendItems.push(item);
  }}

  // ── Legend panel ─────────────────────────────────────────────────────────
  if (INCLUDE_LEGEND && legendItems.length > 0) {{
    var panel = document.getElementById('legend');
    panel.style.display = 'flex';

    // Header
    var hdr = document.getElementById('legend-header') || document.createElement('div');
    hdr.id = 'legend-header';
    hdr.innerHTML = '<span>Layers</span><button id="legend-toggle-all">Hide all</button>';
    panel.appendChild(hdr);

    var body = document.createElement('div');
    body.id = 'legend-body';
    panel.appendChild(body);

    var allVisible = true;
    document.getElementById('legend-toggle-all').addEventListener('click', function() {{
      allVisible = !allVisible;
      this.textContent = allVisible ? 'Hide all' : 'Show all';
      legendItems.forEach(function(item) {{
        setLayerVisible(item, allVisible);
      }});
    }});

    // Legend items are shown top-to-bottom (reverse of draw order)
    var displayItems = legendItems.slice().reverse();

    displayItems.forEach(function(item) {{
      var ld = item.ld;
      var sm = ld.styleMap || {{}};
      var geomType = (ld.kind === 'raster' || ld.kind === 'wms') ? 'raster' : ld.geomType;

      // Primary swatch: use first entry or the single style
      var primaryStyle = {{}};
      if (sm.type === 'single') primaryStyle = sm.style || {{}};
      else if (sm.entries && sm.entries.length) primaryStyle = sm.entries[0].style || {{}};

      var hasEntries = sm.entries && sm.entries.length > 1;

      var layerDiv = document.createElement('div');
      layerDiv.className = 'legend-layer';

      // Main row
      var row = document.createElement('div');
      row.className = 'legend-layer-row';

      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      cb.title = 'Toggle layer visibility';

      var swatch = document.createElement('span');
      swatch.className = 'legend-swatch';
      swatch.innerHTML = swatchSvg(geomType, primaryStyle);

      var nameEl = document.createElement('span');
      nameEl.className = 'legend-layer-name';
      nameEl.title = ld.name;
      nameEl.textContent = ld.name;

      row.appendChild(cb);
      row.appendChild(swatch);
      row.appendChild(nameEl);

      var entriesDiv = null;
      if (hasEntries) {{
        var expBtn = document.createElement('span');
        expBtn.className = 'legend-expand';
        expBtn.textContent = '▶';
        row.appendChild(expBtn);

        entriesDiv = document.createElement('div');
        entriesDiv.className = 'legend-entries';

        sm.entries.forEach(function(entry) {{
          var eRow = document.createElement('div');
          eRow.className = 'legend-entry';
          var eSwatch = document.createElement('span');
          eSwatch.className = 'legend-swatch';
          eSwatch.innerHTML = swatchSvg(geomType, entry.style || {{}});
          var eLabel = document.createElement('span');
          eLabel.className = 'legend-entry-label';
          eLabel.title = entry.label || '';
          eLabel.textContent = entry.label || '';
          eRow.appendChild(eSwatch);
          eRow.appendChild(eLabel);
          entriesDiv.appendChild(eRow);
        }});

        row.addEventListener('click', function(e) {{
          if (e.target === cb) return;
          var open = entriesDiv.classList.toggle('open');
          expBtn.style.transform = open ? 'rotate(90deg)' : '';
        }});
      }}

      // Checkbox toggles layer visibility
      cb.addEventListener('change', function() {{
        setLayerVisible(item, cb.checked);
        layerDiv.classList.toggle('hidden', !cb.checked);
      }});

      layerDiv.appendChild(row);

      // Opacity / transparency slider
      var opRow = document.createElement('div');
      opRow.className = 'legend-opacity';
      var opLabel = document.createElement('span');
      opLabel.className = 'legend-opacity-label';
      opLabel.textContent = 'Opacity';
      var slider = document.createElement('input');
      slider.type = 'range';
      slider.min = '0'; slider.max = '100'; slider.value = '100';
      slider.title = 'Layer transparency';
      slider.addEventListener('input', function() {{
        setLayerOpacity(item, parseInt(slider.value, 10) / 100);
      }});
      opRow.appendChild(opLabel);
      opRow.appendChild(slider);
      layerDiv.appendChild(opRow);

      if (entriesDiv) layerDiv.appendChild(entriesDiv);
      body.appendChild(layerDiv);
      item.checkbox = cb;
      item.layerDiv = layerDiv;
    }});

    // ── Basemap entry (OpenStreetMap) with transparency slider ───────────────
    if (basemap) {{
      var bDiv = document.createElement('div');
      bDiv.className = 'legend-layer';

      var bRow = document.createElement('div');
      bRow.className = 'legend-layer-row';

      var bCb = document.createElement('input');
      bCb.type = 'checkbox';
      bCb.checked = true;
      bCb.title = 'Toggle basemap';
      bCb.addEventListener('change', function() {{
        if (bCb.checked) basemap.addTo(map);
        else map.removeLayer(basemap);
        bDiv.classList.toggle('hidden', !bCb.checked);
      }});

      var bSwatch = document.createElement('span');
      bSwatch.className = 'legend-swatch';
      bSwatch.innerHTML = '<svg width="20" height="16" xmlns="http://www.w3.org/2000/svg">'
        + '<rect x="1" y="1" width="18" height="14" fill="#e8e4dc" stroke="#bbb"/>'
        + '<path d="M1 11 L7 6 L11 9 L19 3" stroke="#8bbf8b" stroke-width="1.5" fill="none"/>'
        + '<circle cx="14" cy="11" r="1.5" fill="#7a9fd0"/></svg>';

      var bName = document.createElement('span');
      bName.className = 'legend-layer-name';
      bName.textContent = 'OpenStreetMap';
      bName.title = 'OpenStreetMap basemap';

      bRow.appendChild(bCb);
      bRow.appendChild(bSwatch);
      bRow.appendChild(bName);
      bDiv.appendChild(bRow);

      var bOpRow = document.createElement('div');
      bOpRow.className = 'legend-opacity';
      var bOpLabel = document.createElement('span');
      bOpLabel.className = 'legend-opacity-label';
      bOpLabel.textContent = 'Opacity';
      var bSlider = document.createElement('input');
      bSlider.type = 'range';
      bSlider.min = '0'; bSlider.max = '100'; bSlider.value = '100';
      bSlider.title = 'Basemap transparency';
      bSlider.addEventListener('input', function() {{
        basemap.setOpacity(parseInt(bSlider.value, 10) / 100);
      }});
      bOpRow.appendChild(bOpLabel);
      bOpRow.appendChild(bSlider);
      bDiv.appendChild(bOpRow);

      body.appendChild(bDiv);
    }}
  }}

  function setLayerVisible(item, visible) {{
    item.visible = visible;
    if (visible) item.lfl.addTo(map);
    else map.removeLayer(item.lfl);
    if (item.checkbox) item.checkbox.checked = visible;
    if (item.layerDiv) item.layerDiv.classList.toggle('hidden', !visible);
  }}

  function setLayerOpacity(item, factor) {{
    var pane = map.getPane(item.paneName);
    if (pane) pane.style.opacity = factor;
  }}

  // ── Filter toolbar ─────────────────────────────────────────────────────────
  (function initFilter() {{
    var vectorItems = legendItems.filter(function(it) {{ return it.ld.kind === 'vector'; }});
    if (vectorItems.length === 0) return;

    var bar          = document.getElementById('filterbar');
    var layerSel     = document.getElementById('filter-layer');
    var attrSel      = document.getElementById('filter-attr');
    var valuesBtn    = document.getElementById('filter-values-btn');
    var valuesPanel  = document.getElementById('filter-values-panel');
    var valuesSearch = document.getElementById('filter-values-search');
    var valuesList   = document.getElementById('filter-values-list');
    var clearBtn     = document.getElementById('filter-clear');
    var countEl      = document.getElementById('filter-count');
    bar.style.display = 'flex';

    // Populate layer dropdown (value = index into legendItems)
    vectorItems.forEach(function(it) {{
      var o = document.createElement('option');
      o.value = it.index;
      o.textContent = it.ld.name;
      layerSel.appendChild(o);
    }});

    function currentItem() {{
      return legendItems[parseInt(layerSel.value, 10)];
    }}

    function checkedValues() {{
      var out = [];
      Array.prototype.forEach.call(valuesList.querySelectorAll('input:checked'), function(c) {{
        out.push(c.value);
      }});
      return out;
    }}

    function updateValuesBtn() {{
      var sel = checkedValues();
      if (sel.length) valuesBtn.textContent = sel.length + ' selected';
      else if (valuesSearch.value.trim()) valuesBtn.textContent = 'contains: ' + valuesSearch.value.trim();
      else valuesBtn.textContent = 'All values';
    }}

    function updateCount(item) {{
      var total = item.ld.geojson.features.length;
      var shown = item.filterFn ? item.ld.geojson.features.filter(item.filterFn).length : total;
      countEl.textContent = shown + ' / ' + total;
    }}

    function clearOtherFilters(keep) {{
      legendItems.forEach(function(it) {{
        if (it !== keep && it.filterFn) {{ it.filterFn = null; rebuildLayer(it); }}
      }});
    }}

    function applyFilter() {{
      var item = currentItem();
      if (!item) return;
      var attr = attrSel.value;
      var search = valuesSearch.value.trim().toLowerCase();
      var selected = checkedValues();
      if (!attr || (selected.length === 0 && !search)) {{
        item.filterFn = null;
      }} else {{
        item.filterFn = function(feature) {{
          var v = (feature.properties || {{}})[attr];
          var sv = (v == null ? '' : String(v));
          if (selected.length) return selected.indexOf(sv) !== -1;
          return sv.toLowerCase().indexOf(search) !== -1;
        }};
      }}
      rebuildLayer(item);
      updateCount(item);
    }}

    function populateAttrs() {{
      var item = currentItem();
      attrSel.innerHTML = '';
      if (!item) return;
      var feats = item.ld.geojson.features;
      var keys = [], seen = {{}};
      for (var i = 0; i < Math.min(feats.length, 50); i++) {{
        var p = feats[i].properties || {{}};
        for (var k in p) {{ if (!(k in seen)) {{ seen[k] = 1; keys.push(k); }} }}
      }}
      keys.forEach(function(k) {{
        var o = document.createElement('option');
        o.value = k; o.textContent = k;
        attrSel.appendChild(o);
      }});
    }}

    function populateValues() {{
      var item = currentItem();
      var attr = attrSel.value;
      valuesList.innerHTML = '';
      valuesSearch.value = '';
      if (!item || !attr) {{ updateValuesBtn(); return; }}
      var feats = item.ld.geojson.features;
      var seen = {{}}, vals = [];
      for (var i = 0; i < feats.length; i++) {{
        var v = (feats[i].properties || {{}})[attr];
        var sv = (v == null ? '' : String(v));
        if (!(sv in seen)) {{ seen[sv] = 1; vals.push(sv); }}
        if (vals.length > 2000) break;
      }}
      vals.sort(function(a, b) {{
        var na = parseFloat(a), nb = parseFloat(b);
        if (!isNaN(na) && !isNaN(nb)) return na - nb;
        return a < b ? -1 : (a > b ? 1 : 0);
      }});
      vals.forEach(function(val) {{
        var lab = document.createElement('label');
        lab.className = 'filter-value-item';
        var c = document.createElement('input');
        c.type = 'checkbox'; c.value = val;
        c.addEventListener('change', function() {{ applyFilter(); updateValuesBtn(); }});
        var s = document.createElement('span');
        s.textContent = (val === '' ? '(empty)' : val);
        s.title = val;
        lab.appendChild(c); lab.appendChild(s);
        valuesList.appendChild(lab);
      }});
      updateValuesBtn();
    }}

    // Events
    layerSel.addEventListener('change', function() {{
      var item = currentItem();
      clearOtherFilters(item);
      populateAttrs();
      populateValues();
      applyFilter();
    }});
    attrSel.addEventListener('change', function() {{
      populateValues();
      applyFilter();
    }});
    valuesSearch.addEventListener('input', function() {{
      var q = valuesSearch.value.trim().toLowerCase();
      Array.prototype.forEach.call(valuesList.children, function(el) {{
        el.style.display = el.textContent.toLowerCase().indexOf(q) !== -1 ? '' : 'none';
      }});
      applyFilter();
      updateValuesBtn();
    }});
    valuesBtn.addEventListener('click', function() {{
      valuesPanel.classList.toggle('open');
    }});
    document.addEventListener('click', function(e) {{
      if (!document.getElementById('filter-values-wrap').contains(e.target)) {{
        valuesPanel.classList.remove('open');
      }}
    }});
    clearBtn.addEventListener('click', function() {{
      valuesSearch.value = '';
      Array.prototype.forEach.call(valuesList.querySelectorAll('input:checked'), function(c) {{
        c.checked = false;
      }});
      Array.prototype.forEach.call(valuesList.children, function(el) {{ el.style.display = ''; }});
      applyFilter();
      updateValuesBtn();
    }});

    // Initialise with the first vector layer
    populateAttrs();
    populateValues();
    var first = currentItem();
    if (first) updateCount(first);
  }})();

  // Fit map to data
  try {{ map.fitBounds(bounds, {{padding: [20, 20]}}); }}
  catch(e) {{ map.setView([0, 0], 2); }}
}})();
</script>
</body>
</html>"""


def _flatten_coords(geom):
    """Yield all [x, y] coordinate pairs from a GeoJSON geometry dict."""
    gtype = geom.get("type", "")
    coords = geom.get("coordinates", [])
    if gtype == "Point":
        if coords:
            yield coords
    elif gtype in ("MultiPoint", "LineString"):
        for c in coords:
            yield c
    elif gtype in ("MultiLineString", "Polygon"):
        for ring in coords:
            for c in ring:
                yield c
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    yield c
    elif gtype == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from _flatten_coords(g)
