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


def _get_leaflet_assets() -> tuple[str, str] | tuple[None, None]:
    """
    Return (css, js) strings for inline embedding, downloading and caching
    from CDN on first export. Returns (None, None) if unavailable.
    """
    v        = _LEAFLET_VERSION
    css_path = os.path.join(_LIB_DIR, f"leaflet-{v}.min.css")
    js_path  = os.path.join(_LIB_DIR, f"leaflet-{v}.min.js")

    if os.path.exists(css_path) and os.path.exists(js_path):
        with open(css_path, encoding="utf-8") as f:
            css = f.read()
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        return css, js

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
            style["markerSize"] = max(4, round(sl.size() * 3))
            style["markerShape"] = sl.shape()  # enum int
            break

        elif isinstance(sl, QgsSvgMarkerSymbolLayer):
            color = sl.fillColor()
            style["markerColor"] = _color_to_hex(color)
            style["markerOpacity"] = round(color.alphaF(), 3)
            style["markerSize"] = max(4, round(sl.size() * 3))
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
</style>
</head>
<body>
<div id="map"></div>
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
  if (INCLUDE_BASEMAP) {{
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19
    }}).addTo(map);
  }}

  // ── Helpers ──────────────────────────────────────────────────────────────
  function escHtml(s) {{
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  function makeCircleMarker(latlng, style) {{
    return L.circleMarker(latlng, {{
      radius: (style.markerSize || 8) / 2,
      fillColor: style.markerColor || '#3388ff',
      fillOpacity: style.markerOpacity != null ? style.markerOpacity : 0.8,
      color: style.markerStrokeColor || '#ffffff',
      weight: 1,
      opacity: 1
    }});
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
      svg += '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '"'
          + ' fill="' + escHtml(style.markerColor || '#3388ff') + '"'
          + ' fill-opacity="' + (style.markerOpacity != null ? style.markerOpacity : 0.8) + '"'
          + ' stroke="' + escHtml(style.markerStrokeColor || '#fff') + '"'
          + ' stroke-width="1"/>';
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
  var leafletLayers = [];

  function addVectorLayer(ld) {{
    var lfl;
    if (ld.geomType === 'point') {{
      lfl = L.geoJSON(ld.geojson, {{
        pointToLayer: function(feature, latlng) {{
          return makeCircleMarker(latlng, resolveStyle(ld.styleMap, feature.properties || {{}}));
        }},
        onEachFeature: onEachFeature
      }});
    }} else {{
      lfl = L.geoJSON(ld.geojson, {{
        style: function(feature) {{
          return leafletPathStyle(resolveStyle(ld.styleMap, feature.properties || {{}}));
        }},
        onEachFeature: onEachFeature
      }});
    }}
    lfl.addTo(map);
    leafletLayers.push(lfl);
    return lfl;
  }}

  function addRasterLayer(ld) {{
    var lfl = L.imageOverlay('data:image/png;base64,' + ld.data, ld.bounds, {{opacity: 1}}).addTo(map);
    leafletLayers.push(lfl);
    return lfl;
  }}

  function addWmsLayer(ld) {{
    var lfl;
    if (ld.tileType === 'xyz') {{
      lfl = L.tileLayer(ld.wmsUrl).addTo(map);
    }} else {{
      lfl = L.tileLayer.wms(ld.wmsUrl, {{
        layers:      ld.wmsLayers,
        format:      ld.wmsFormat  || 'image/png',
        styles:      ld.wmsStyles  || '',
        version:     ld.wmsVersion || '1.1.1',
        transparent: true,
        opacity:     1
      }}).addTo(map);
    }}
    leafletLayers.push(lfl);
    return lfl;
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

  // Build Leaflet layers and collect metadata for legend
  var legendItems = [];
  for (var i = 0; i < LAYERS.length; i++) {{
    var ld = LAYERS[i];
    var lfl = ld.kind === 'vector'  ? addVectorLayer(ld)
            : ld.kind === 'wms'    ? addWmsLayer(ld)
            : addRasterLayer(ld);
    legendItems.push({{ ld: ld, lfl: lfl }});
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

    // Legend items are shown top-to-bottom (reverse of leafletLayers order)
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
      if (entriesDiv) layerDiv.appendChild(entriesDiv);
      body.appendChild(layerDiv);
      item.checkbox = cb;
      item.layerDiv = layerDiv;
    }});
  }}

  function setLayerVisible(item, visible) {{
    if (visible) item.lfl.addTo(map);
    else map.removeLayer(item.lfl);
    if (item.checkbox) item.checkbox.checked = visible;
    if (item.layerDiv) item.layerDiv.classList.toggle('hidden', !visible);
  }}

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
