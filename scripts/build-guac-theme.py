#!/usr/bin/env python3
"""Build the CRANGE Guacamole theme extension without requiring Java tooling."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "guacamole" / "extension"
TARGET = ROOT / "guacamole" / "crange-theme.jar"

with zipfile.ZipFile(TARGET, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name in ("guac-manifest.json", "theme.css"):
        archive.write(SOURCE / name, name)
print(TARGET)
