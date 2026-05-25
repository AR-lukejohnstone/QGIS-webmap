import os
import json
import base64
import tempfile

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
from qgis.PyQt.QtCore import QSize


_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


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
    Keys:
      'type': 'single' | 'categorized' | 'graduated' | 'rule'
      'style': {...}            (for single)
      'field': str              (for categorized/graduated)
      'categories': {val: style} (for categorized)
      'ranges': [(min,max,style)] (for graduated)
      'default': {...}
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
        cats = {}
        for cat in renderer.categories():
            cats[str(cat.value())] = _extract_symbol_style(cat.symbol())
        return {
            "type": "categorized",
            "field": renderer.classAttribute(),
            "categories": cats,
            "default": _extract_symbol_style(renderer.symbol()) if renderer.symbol() else {},
        }

    if isinstance(renderer, QgsGraduatedSymbolRenderer):
        ranges = []
        for r in renderer.ranges():
            ranges.append((r.lowerValue(), r.upperValue(), _extract_symbol_style(r.symbol())))
        return {
            "type": "graduated",
            "field": renderer.classAttribute(),
            "ranges": ranges,
            "default": {},
        }

    if isinstance(renderer, QgsRuleBasedRenderer):
        # Flatten rules: use first matching rule per feature
        rules = []
        for rule in renderer.rootRule().children():
            rules.append({
                "label": rule.label(),
                "style": _extract_symbol_style(rule.symbol()),
            })
        return {
            "type": "rule",
            "rules": rules,
            "default": rules[0]["style"] if rules else {},
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
                b64, bounds = _raster_to_base64(layer)
                layer_defs.append({
                    "kind": "raster",
                    "name": layer.name(),
                    "data": b64,
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
            if ld["kind"] == "raster":
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
        layers_json = json.dumps(layer_defs, separators=(",", ":"))
        bounds_json = json.dumps(bounds)

        basemap_js = ""
        if self.include_basemap:
            basemap_js = """
  var osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
  }).addTo(map);
  var baseLayers = { "OpenStreetMap": osm };"""
        else:
            basemap_js = "  var baseLayers = {};"

        layer_control_js = ""
        if self.include_layer_control:
            layer_control_js = "  L.control.layers(baseLayers, overlays, {collapsed: false}).addTo(map);"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QGIS Web Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV/XN/sp38=" crossorigin=""></script>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; }}
  #map {{ height: 100%; width: 100%; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
(function() {{
  "use strict";

  var map = L.map('map');
  var bounds = {bounds_json};
  var LAYERS = {layers_json};

{basemap_js}

  // ── Marker shape helper ──────────────────────────────────────────────────
  var SHAPES = {{
    0: 'circle', 1: 'square', 2: 'diamond', 4: 'triangle', 5: 'triangle',
    6: 'cross', 8: 'star'
  }};

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

  // ── Style resolver ───────────────────────────────────────────────────────
  function resolveStyle(styleMap, props) {{
    var t = styleMap.type;
    if (t === 'single') return styleMap.style;
    if (t === 'categorized') {{
      var val = String(props[styleMap.field]);
      return styleMap.categories[val] || styleMap.default || {{}};
    }}
    if (t === 'graduated') {{
      var v = parseFloat(props[styleMap.field]);
      for (var i = 0; i < styleMap.ranges.length; i++) {{
        var r = styleMap.ranges[i];
        if (v >= r[0] && v <= r[1]) return r[2];
      }}
      return styleMap.default || {{}};
    }}
    if (t === 'rule') {{
      return (styleMap.rules[0] && styleMap.rules[0].style) || styleMap.default || {{}};
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

  // ── Layer builder ────────────────────────────────────────────────────────
  var overlays = {{}};

  function addVectorLayer(ld) {{
    var leafletLayer;
    if (ld.geomType === 'point') {{
      leafletLayer = L.geoJSON(ld.geojson, {{
        pointToLayer: function(feature, latlng) {{
          var s = resolveStyle(ld.styleMap, feature.properties || {{}});
          return makeCircleMarker(latlng, s);
        }},
        onEachFeature: onEachFeature
      }});
    }} else {{
      leafletLayer = L.geoJSON(ld.geojson, {{
        style: function(feature) {{
          var s = resolveStyle(ld.styleMap, feature.properties || {{}});
          return leafletPathStyle(s);
        }},
        onEachFeature: onEachFeature
      }});
    }}
    leafletLayer.addTo(map);
    overlays[ld.name] = leafletLayer;
  }}

  function addRasterLayer(ld) {{
    var imgUrl = 'data:image/png;base64,' + ld.data;
    var leafletLayer = L.imageOverlay(imgUrl, ld.bounds, {{opacity: 1}}).addTo(map);
    overlays[ld.name] = leafletLayer;
  }}

  function onEachFeature(feature, layer) {{
    if (!feature.properties) return;
    var rows = Object.entries(feature.properties)
      .filter(function(e) {{ return e[1] != null; }})
      .map(function(e) {{
        return '<tr><th>' + escHtml(String(e[0])) + '</th><td>' + escHtml(String(e[1])) + '</td></tr>';
      }}).join('');
    if (rows) {{
      layer.bindPopup('<table style="font-size:13px;border-collapse:collapse">' + rows + '</table>');
    }}
  }}

  function escHtml(s) {{
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  // Add layers (bottom to top)
  for (var i = 0; i < LAYERS.length; i++) {{
    var ld = LAYERS[i];
    if (ld.kind === 'vector') addVectorLayer(ld);
    else if (ld.kind === 'raster') addRasterLayer(ld);
  }}

{layer_control_js}

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
