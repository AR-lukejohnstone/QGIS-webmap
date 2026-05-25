#!/usr/bin/env python3
"""
Install the QGIS Web Map Exporter plugin into the active QGIS profile.

Usage:
    python3 install_plugin.py

Or run from within QGIS Python console:
    exec(open('/path/to/install_plugin.py').read())
"""
import os
import shutil
import subprocess
import sys


def get_qgis_plugin_dir():
    # Try common locations
    candidates = []

    # Linux
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    candidates.append(os.path.join(xdg, "QGIS", "QGIS3", "profiles", "default", "python", "plugins"))
    candidates.append(os.path.expanduser("~/.local/share/QGIS/QGIS3/profiles/default/python/plugins"))

    # macOS
    candidates.append(os.path.expanduser(
        "~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins"
    ))

    # Windows
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(os.path.join(appdata, "QGIS", "QGIS3", "profiles", "default", "python", "plugins"))

    for c in candidates:
        if os.path.isdir(os.path.dirname(c)):
            return c
    return candidates[0]  # return Linux default and let user fix


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(script_dir, "qgis_webmap")

    if not os.path.isdir(src):
        print(f"ERROR: plugin source not found at {src}")
        sys.exit(1)

    plugin_dir = get_qgis_plugin_dir()
    dst = os.path.join(plugin_dir, "qgis_webmap")

    print(f"Installing to: {dst}")
    os.makedirs(plugin_dir, exist_ok=True)

    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    print("Done. Restart QGIS and enable 'QGIS Web Map Exporter' in Plugins > Manage Plugins.")


if __name__ == "__main__":
    main()
