#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


ICON_SIZES = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]


def build_iconset(source_path: Path, output_dir: Path) -> None:
    source = Image.open(source_path).convert("RGBA")
    output_dir.mkdir(parents=True, exist_ok=True)

    for size, filename in ICON_SIZES:
        resized = source.resize((size, size), Image.Resampling.LANCZOS)
        rounded = apply_rounded_mask(resized)
        rounded.save(output_dir / filename)


def apply_rounded_mask(image: Image.Image) -> Image.Image:
    size = image.width
    radius = max(2, int(size * 0.225))

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)

    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    return rounded


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: build_app_icon.py <icon.png> <output.iconset>", file=sys.stderr)
        return 1

    source_path = Path(sys.argv[1]).expanduser().resolve()
    output_dir = Path(sys.argv[2]).expanduser().resolve()
    build_iconset(source_path, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
