"""Render app-logo.svg to a PNG preview via PySide6 QSvgRenderer."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "adofai_publisher" / "assets" / "icons" / "app-logo.svg"
OUT = ROOT / "docs" / "logo-preview.png"


def main() -> None:
    app = QApplication.instance() or QApplication([])
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise SystemExit("SVG renderer invalid")
    size = renderer.defaultSize()
    img = QImage(size, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter, QRectF(img.rect()))
    painter.end()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(OUT))
    print("saved", OUT)


if __name__ == "__main__":
    main()
