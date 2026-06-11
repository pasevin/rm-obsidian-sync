"""
stroke_renderer.py — Render reMarkable stroke data to a PNG image via Pillow.

Converts the list-of-strokes format produced by rm_parser into a greyscale
PNG suitable for OCR engines such as Tesseract.

Public functions
----------------
render_page_to_png(strokes, scale) -> bytes
    Render one page's strokes to a PNG byte string.
"""

from __future__ import annotations

import logging
from typing import Any

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# reMarkable coordinate system: origin is at the horizontal centre of the
# page, slightly above the top edge.  Raw X values can be negative.
# We normalise the bounding box to fit within a padded canvas so all ink
# is visible regardless of scroll position.

# 4× gives Tesseract enough pixels on cursive handwriting without blurring
_DEFAULT_SCALE: float = 4.0

# Padding added around the bounding box (source units, before scaling)
_PADDING: int = 60

# Thicker strokes improve OCR accuracy on sparse cursive handwriting
_BASE_STROKE_WIDTH: float = 6.0


def _collect_all_points(
    strokes: list[dict[str, Any]],
) -> tuple[list[float], list[float]]:
    """Return flat lists of all x and y coordinates across every stroke."""
    all_x: list[float] = []
    all_y: list[float] = []
    for s in strokes:
        all_x.extend(s.get("x", []))
        all_y.extend(s.get("y", []))
    return all_x, all_y


def render_page_to_png(
    strokes: list[dict[str, Any]],
    scale: float = _DEFAULT_SCALE,
) -> bytes:
    """
    Render a list of stroke dicts to a PNG byte string.

    Coordinates are normalised to a tight bounding box with padding so
    negative or large offsets (the reMarkable origin is centred) are
    compensated automatically.  Uses a white background with black strokes
    and an optional upscale factor for OCR legibility.

    Args:
        strokes: List of stroke dicts in MyScript / rm_parser format::

                     {"id": "s0", "x": [float, ...], "y": [float, ...], ...}

        scale:   Scaling factor applied to both dimensions.  Default 2.0.

    Returns:
        Raw PNG bytes ready to write to disk or pass to an OCR engine.
    """
    if not strokes:
        # Empty stroke list means a blank or v5-format page — no image to write.
        # vault_writer skips zero-length bytes and omits the embed entirely.
        return b""

    all_x, all_y = _collect_all_points(strokes)
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    # Canvas size in source units (bounding box + padding on all sides)
    src_w = (max_x - min_x) + 2 * _PADDING
    src_h = (max_y - min_y) + 2 * _PADDING

    w = max(1, int(src_w * scale))
    h = max(1, int(src_h * scale))
    stroke_width = max(1, int(_BASE_STROKE_WIDTH * scale))

    img = Image.new("L", (w, h), color=255)  # greyscale, white background
    draw = ImageDraw.Draw(img)

    total_points = 0
    for stroke in strokes:
        xs = stroke.get("x", [])
        ys = stroke.get("y", [])
        if len(xs) < 2 or len(ys) < 2:
            continue

        # Shift so min_x/min_y maps to the padding origin, then scale
        pts = [
            ((x - min_x + _PADDING) * scale, (y - min_y + _PADDING) * scale)
            for x, y in zip(xs, ys)
        ]
        draw.line(pts, fill=0, width=stroke_width)
        total_points += len(pts)

    logger.debug("Rendered %d strokes, %d total points → %dx%d px", len(strokes), total_points, w, h)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
