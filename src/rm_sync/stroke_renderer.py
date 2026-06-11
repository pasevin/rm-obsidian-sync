"""
stroke_renderer.py — Render reMarkable stroke data to a high-fidelity colour PNG.

Converts the full-fidelity stroke dicts produced by rm_parser into a colour PNG
that closely matches what you see on the reMarkable tablet:

- Correct RGB colour per stroke (black, grey, blue, red, yellow, …)
- Semi-transparent highlight strokes (HIGHLIGHTER pen type)
- Pen-specific base widths (fineliner thin, marker wide, brush pressure-sensitive)
- Per-point width modulation from tablet pressure + width data
- Full A4 canvas (1404 × 1872 px at 2× — the reMarkable Paper Pro native resolution)

Public API
----------
render_page_to_png(strokes, scale) -> bytes
    Render one page's stroke list to a PNG byte string.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Canvas constants — reMarkable Paper Pro native coordinate space
# ──────────────────────────────────────────────────────────────────────────────

# The reMarkable Paper Pro (and earlier models) use a fixed coordinate space:
#   width  = 1404 units
#   height = 1872 units
# We render at 2× by default, giving 2808 × 3744 px — crisp on any screen.
_RM_WIDTH:  int = 1404
_RM_HEIGHT: int = 1872
_DEFAULT_SCALE: float = 2.0

# Padding inside the canvas (source units)
_PADDING: int = 0   # no extra padding — use the full page

# ──────────────────────────────────────────────────────────────────────────────
# Per-tool base stroke widths (in source units, scaled up by _DEFAULT_SCALE)
# ──────────────────────────────────────────────────────────────────────────────

_TOOL_BASE_WIDTH: dict[str, float] = {
    "fineliner":        1.5,
    "ballpoint":        2.5,
    "marker":           6.0,
    "paintbrush":       3.0,
    "pencil":           2.0,
    "mechanical_pencil":1.5,
    "calligraphy":      3.5,
    "highlighter":      24.0,  # highlighters are wide stripes
    "shader":           8.0,
    "eraser":           0.0,   # eraser strokes are not rendered
    "eraser_area":      0.0,
}
_DEFAULT_BASE_WIDTH: float = 2.0

# ──────────────────────────────────────────────────────────────────────────────
# Rendering helpers
# ──────────────────────────────────────────────────────────────────────────────


def _rgba_for_stroke(stroke: dict[str, Any]) -> tuple[int, int, int, int]:
    """
    Return an RGBA tuple for *stroke*, applying opacity as the alpha channel.
    Eraser strokes return fully transparent (will be skipped by the caller).
    """
    color: list[int] = stroke.get("color", [0, 0, 0])
    opacity: float   = stroke.get("opacity", 1.0)
    alpha = int(round(opacity * 255))
    return (color[0], color[1], color[2], alpha)


def _stroke_width_at(
    stroke: dict[str, Any],
    point_index: int,
    scale: float,
) -> int:
    """
    Compute the rendered stroke width at *point_index* (pixels).

    Width = base_tool_width × thickness_scale × pressure_factor × render_scale

    The tablet's per-point ``width`` field is normalised to a 0–255 range;
    we use it as an additional multiplier clamped to [0.3, 2.5] to avoid
    invisible hairlines or comically thick strokes.
    """
    tool      = stroke.get("tool", "fineliner")
    thickness = stroke.get("thickness", 1.0)
    base      = _TOOL_BASE_WIDTH.get(tool, _DEFAULT_BASE_WIDTH)

    # Per-point pressure (0–1)
    pressures = stroke.get("p", [])
    pressure  = pressures[point_index] if point_index < len(pressures) else 0.5

    # Per-point width hint from tablet (raw units, typically 10–200)
    widths    = stroke.get("width", [])
    raw_w     = widths[point_index] if point_index < len(widths) else 100.0
    w_factor  = max(0.3, min(2.5, raw_w / 100.0))

    # Pencil / mechanical pencil: pressure matters a lot
    if tool in ("pencil", "mechanical_pencil"):
        pressure_factor = 0.4 + pressure * 0.8
    # Brush / calligraphy: full pressure modulation
    elif tool in ("paintbrush", "calligraphy"):
        pressure_factor = 0.2 + pressure * 1.6
    # Highlighter: ignore pressure — uniform wide stripe
    elif tool == "highlighter":
        pressure_factor = 1.0
        w_factor        = 1.0
    # Everything else: mild pressure effect
    else:
        pressure_factor = 0.6 + pressure * 0.6

    width_f = base * thickness * pressure_factor * w_factor * scale
    return max(1, int(round(width_f)))


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def render_page_to_png(
    strokes: list[dict[str, Any]],
    scale: float = _DEFAULT_SCALE,
) -> bytes:
    """
    Render a list of stroke dicts to a full-colour PNG byte string.

    Uses the reMarkable Paper Pro's native 1404×1872 coordinate space as the
    canvas, so all ink is positioned correctly relative to the page regardless
    of where on the page it was written.  Highlights are rendered with
    transparency on a separate RGBA layer composited over the white background.

    Args:
        strokes: List of full-fidelity stroke dicts from ``rm_parser.parse_rmdoc``.
        scale:   Render scale factor (default 2.0 → 2808×3744 px output).

    Returns:
        Raw PNG bytes.  Returns ``b""`` for a blank page (no strokes → caller
        skips the embed entirely).
    """
    if not strokes:
        return b""

    # Filter eraser strokes — nothing to draw
    visible = [s for s in strokes if s.get("tool", "") not in ("eraser", "eraser_area")]
    if not visible:
        return b""

    w = int(_RM_WIDTH  * scale)
    h = int(_RM_HEIGHT * scale)

    # Base layer: white RGB (for the final output)
    base = Image.new("RGB", (w, h), color=(255, 255, 255))

    # Highlight layer: RGBA, composited last so highlights sit on top of ink
    highlight_layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    highlight_draw  = ImageDraw.Draw(highlight_layer)

    # Ink layer: RGBA (supports future semi-transparent tool additions)
    ink_layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    ink_draw  = ImageDraw.Draw(ink_layer)

    for stroke in visible:
        xs    = stroke.get("x", [])
        ys    = stroke.get("y", [])
        tool  = stroke.get("tool", "fineliner")
        rgba  = _rgba_for_stroke(stroke)

        if len(xs) < 2:
            # Single-point tap — draw a small dot
            if xs and ys:
                px = int(xs[0] * scale)
                py = int(ys[0] * scale)
                r  = _stroke_width_at(stroke, 0, scale) // 2
                target_draw = highlight_draw if tool == "highlighter" else ink_draw
                target_draw.ellipse([px - r, py - r, px + r, py + r], fill=rgba)
            continue

        is_highlight = (tool == "highlighter")
        target_draw  = highlight_draw if is_highlight else ink_draw

        # Draw segment-by-segment so width can vary per point
        pts = list(zip(xs, ys))
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            px0, py0 = int(x0 * scale), int(y0 * scale)
            px1, py1 = int(x1 * scale), int(y1 * scale)
            seg_width = _stroke_width_at(stroke, i, scale)
            target_draw.line([(px0, py0), (px1, py1)], fill=rgba, width=seg_width)

        logger.debug(
            "Rendered %s stroke: %d pts, color=%s, opacity=%.2f",
            tool, len(pts), rgba[:3], rgba[3] / 255.0,
        )

    # Composite: white base ← ink ← highlights
    base_rgba = base.convert("RGBA")
    composited = Image.alpha_composite(base_rgba, ink_layer)
    composited = Image.alpha_composite(composited, highlight_layer)
    final = composited.convert("RGB")

    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=False)
    logger.debug("Rendered page: %dx%d px, %d strokes", w, h, len(visible))
    return buf.getvalue()
