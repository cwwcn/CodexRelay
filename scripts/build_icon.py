from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
ICONSET = ROOT / "artifacts" / "icon-work" / "CodexRelay.iconset"
OUTPUT = ROOT / "assets" / "CodexRelay.icns"


def render(size: int, destination: Path) -> None:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = size / 1024
    rounded = QPainterPath()
    rounded.addRoundedRect(
        QRectF(80 * scale, 80 * scale, 864 * scale, 864 * scale),
        216 * scale,
        216 * scale,
    )
    gradient = QLinearGradient(160 * scale, 120 * scale, 850 * scale, 900 * scale)
    gradient.setColorAt(0, QColor("#377DB7"))
    gradient.setColorAt(1, QColor("#174A78"))
    painter.fillPath(rounded, gradient)

    stroke = QPen(QColor("#F7FAFC"), max(2, 72 * scale))
    stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(stroke)
    painter.drawArc(
        QRectF(270 * scale, 230 * scale, 450 * scale, 390 * scale),
        25 * 16,
        285 * 16,
    )
    painter.drawLine(271 * scale, 650 * scale, 753 * scale, 650 * scale)
    painter.drawLine(512 * scale, 650 * scale, 512 * scale, 802 * scale)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#8FE0BD"))
    painter.drawEllipse(QRectF(647 * scale, 332 * scale, 116 * scale, 116 * scale))
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
