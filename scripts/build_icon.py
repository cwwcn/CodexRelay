from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
ICONSET = ROOT / "artifacts" / "icon-work" / "CodexRelay.iconset"
OUTPUT = ROOT / "assets" / "CodexRelay.icns"
SOURCE = ROOT / "assets" / "CodexRelay.svg"


def render(size: int, destination: Path) -> None:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer = QSvgRenderer(str(SOURCE))
    if not renderer.isValid():
        raise RuntimeError(f"invalid icon source: {SOURCE}")
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    if not image.save(str(destination), "PNG"):
        raise RuntimeError(f"failed to save {destination}")


def main() -> None:
    QApplication([])
    ICONSET.mkdir(parents=True, exist_ok=True)
    variants = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in variants.items():
        render(size, ICONSET / name)
    subprocess.run(
        ["/usr/bin/iconutil", "-c", "icns", str(ICONSET), "-o", str(OUTPUT)],
        check=True,
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
