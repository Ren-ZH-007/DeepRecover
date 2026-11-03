"""Render the existing local brand mark as the standard macOS iconset sizes."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from app.theme import brand_icon

    app = QApplication.instance() or QApplication([])
    args.output.mkdir(parents=True, exist_ok=True)
    brand = brand_icon(1024)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = '@2x' if scale == 2 else ''
            pixels = size * scale
            image = brand.pixmap(1024, 1024).toImage().scaled(
                pixels, pixels, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            image.setDevicePixelRatio(1)
            if not image.save(str(args.output / f'icon_{size}x{size}{suffix}.png')):
                raise RuntimeError('Cannot write macOS application icon')
    app.quit()


if __name__ == '__main__':
    main()
