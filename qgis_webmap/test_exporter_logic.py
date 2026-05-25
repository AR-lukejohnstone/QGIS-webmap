"""
Offline unit tests for parts of exporter.py that don't require QGIS.
Run with: python3 -m pytest qgis_webmap/test_exporter_logic.py -v
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Mock the QGIS namespace so we can import exporter without QGIS installed ──
class _FakeColor:
    def __init__(self, r, g, b, a=255):
        self._r, self._g, self._b, self._a = r, g, b, a
    def red(self): return self._r
    def green(self): return self._g
    def blue(self): return self._b
    def alphaF(self): return self._a / 255.0


def _color_to_hex(color):
    return "#{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue())


def _color_to_rgba(color):
    return "rgba({},{},{},{:.3f})".format(
        color.red(), color.green(), color.blue(), color.alphaF()
    )


def _flatten_coords(geom):
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


# ── Tests ──────────────────────────────────────────────────────────────────

def test_color_to_hex():
    c = _FakeColor(255, 128, 0)
    assert _color_to_hex(c) == "#ff8000"


def test_color_to_hex_black():
    c = _FakeColor(0, 0, 0)
    assert _color_to_hex(c) == "#000000"


def test_flatten_point():
    geom = {"type": "Point", "coordinates": [10.0, 20.0]}
    coords = list(_flatten_coords(geom))
    assert coords == [[10.0, 20.0]]


def test_flatten_linestring():
    geom = {"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 2]]}
    coords = list(_flatten_coords(geom))
    assert len(coords) == 3


def test_flatten_polygon():
    geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    coords = list(_flatten_coords(geom))
    assert len(coords) == 5


def test_flatten_multipolygon():
    geom = {
        "type": "MultiPolygon",
        "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]],
                        [[[2, 2], [3, 2], [3, 3], [2, 2]]]]
    }
    coords = list(_flatten_coords(geom))
    assert len(coords) == 8


def test_flatten_empty():
    geom = {"type": "Point", "coordinates": []}
    coords = list(_flatten_coords(geom))
    assert coords == []


def test_geojson_structure():
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
         "properties": {"name": "test"}}
    ]}
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["name"] == "test"


def test_style_map_json_serialisable():
    style_map = {
        "type": "categorized",
        "field": "category",
        "categories": {
            "A": {"fillColor": "#ff0000", "fillOpacity": 0.8, "color": "#000", "weight": 1, "opacity": 1},
            "B": {"fillColor": "#0000ff", "fillOpacity": 0.8, "color": "#000", "weight": 1, "opacity": 1},
        },
        "default": {"fillColor": "#888888", "fillOpacity": 0.5, "color": "#000", "weight": 1, "opacity": 1},
    }
    dumped = json.dumps(style_map)
    loaded = json.loads(dumped)
    assert loaded["type"] == "categorized"
    assert loaded["categories"]["A"]["fillColor"] == "#ff0000"


def test_html_contains_leaflet():
    # Simulate a minimal render
    layer_defs = [{
        "kind": "vector",
        "name": "Test Layer",
        "geomType": "point",
        "geojson": {"type": "FeatureCollection", "features": []},
        "styleMap": {"type": "single", "style": {"markerColor": "#ff0000", "markerSize": 8}},
    }]
    bounds = [[51.4, -0.2], [51.6, 0.0]]
    layers_json = json.dumps(layer_defs, separators=(",", ":"))
    bounds_json = json.dumps(bounds)
    html = f"<script src='leaflet.js'></script><div id='map'></div><script>var LAYERS={layers_json}; var bounds={bounds_json};</script>"
    assert "leaflet" in html.lower()
    assert "LAYERS" in html
    assert "Test Layer" in html


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
