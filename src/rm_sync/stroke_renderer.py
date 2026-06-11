"""
stroke_renderer.py — Render reMarkable stroke data to a high-fidelity colour PNG.

Converts the full-fidelity stroke dicts produced by rm_parser into a colour PNG
that closely matches what you see on the reMarkable tablet:

- Correct RGB colour per stroke (black, grey, blue, red, yellow, …)
- Semi-transparent highlight strokes (HIGHLIGHTER pen type)
- Shader / pencil-shading approximation (semi-transparent wide strokes)
- Pen-specific base widths (fineliner thin, marker wide, brush pressure-sensitive)
- Per-point pressure × width modulation from tablet data
- Full A4 canvas with correct origin translation (reMarkable coords are centred)

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
# Canvas — reMarkable coordinate space
# ──────────────────────────────────────────────────────────────────────────────
# The reMarkable Paper Pro native resolution is 1404 × 1872 px.
# Stroke coordinates are centred: (0, 0) is roughly the top-centre of the page.
# The full horizontal range is ±702 units; vertical is 0 → 1872 (mostly positive
# but can go slightly negative for strokes near the top edge).
#
# To map to pixel space we translate by half the canvas width on X, and by a
# small top-margin offset on Y.  We also add padding so strokes near the edges
# are never clipped.

_RM_WIDTH:   int = 1404   # native canvas width  (source units)
_RM_HEIGHT:  int = 1872   # native canvas height (source units)
_PADDING:    int = 50     # extra padding on all sides (source units)
_DEFAULT_SCALE: float = 2.0

# X origin: coordinates are centred, so 0 maps to the horizontal midpoint
_X_ORIGIN: float = _RM_WIDTH / 2.0       # 702.0
# Y origin: (0,0) is near the top of the page; a small positive offset covers
# strokes with slightly negative Y (headers, menu areas).
_Y_ORIGIN: float = 0.0   # Y is already top-relative in v6 format

# ──────────────────────────────────────────────────────────────────────────────
# Per-tool rendering parameters
# ──────────────────────────────────────────────────────────────────────────────

# (base_width_su, pressure_min, pressure_max, opacity_multiplier)
# base_width is in source units before scale is applied.
_TOOL_PARAMS: dict[str, tuple[float, float, float, float]] = {
    #                       base_w  pmin  pmax  opacity
    "fineliner":           (1.0,    0.8,  1.0,  1.0),
    "ballpoint":           (1.5,    0.5,  1.2,  1.0),
    "marker":              (4.0,    0.7,  1.1,  1.0),
    "paintbrush":          (2.0,    0.1,  2.0,  1.0),
    "pencil":              (1.2,    0.3,  1.0,  0.85),
    "mechanical_pencil":   (0.8,    0.4,  0.9,  0.9),
    "calligraphy":         (2.5,    0.2,  2.2,  1.0),
    # Highlighter: wide, uniform, semi-transparent (opacity set in color map)
    "highlighter":         (18.0,   1.0,  1.0,  1.0),
    # Shader: pencil-shading fill — wide, very transparent, pressure-driven
    "shader":              (6.0,    0.05, 0.5,  0.25),
    "eraser":              (0.0,    1.0,  1.0,  0.0),
    "eraser_area":         (0.0,    1.0,  1.0,  0.0),
}
_DEFAULT_TOOL_PARAMS = (1.5, 0.6, 1.0, 1.0)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _to_px(x: float, y: float, scale: float, pad: int) -> tuple[int, int]:
    """
    Convert reMarkable source coordinates to canvas pixel coordinates.

    reMarkable X is centred at 0 (range ≈ −702 … +702).
    reMarkable Y starts near 0 at the top and increases downward.
    We translate and add padding so all ink lands inside the canvas.
    """
    px = int((x + _X_ORIGIN + pad) * scale)
    py = int((y + _Y_ORIGIN + pad) * scale)
    return px, py


def _stroke_rgba(stroke: dict[str, Any], point_index: int, scale: float) -> tuple[int, int, int, int]:
    """
    Return RGBA for a single point, incorporating tool opacity and pressure.
    """
    tool   = stroke.get("tool", "fineliner")
    color  = stroke.get("color", [0, 0, 0])
    base_opacity: float = stroke.get("opacity", 1.0)

    params = _TOOL_PARAMS.get(tool, _DEFAULT_TOOL_PARAMS)
    _, pmin, pmax, opacity_mult = params

    pressures = stroke.get("p", [])
    pressure  = pressures[point_index] if point_index < len(pressures) else 0.5

    # Shader / pencil: opacity scales with pressure
    if tool in ("shader", "pencil", "mechanical_pencil"):
        p_opacity = pmin + (pressure * (pmax - pmin))
        alpha = int(base_opacity * opacity_mult * p_opacity * 255)
    else:
        alpha = int(base_opacity * opacity_mult * 255)

    alpha = max(0, min(255, alpha))
    return (color[0], color[1], color[2], alpha)


def _stroke_width_px(stroke: dict[str, Any], point_index: int, scale: float) -> int:
    """
    Compute rendered stroke width in pixels for a given point.

    Formula: base_width × thickness_scale × pressure_factor × width_factor × scale
    The tablet's raw width field (typically 8–131) is used as an additional
    multiplier normalised around a midpoint of ~30 (empirically observed median).
    """
    tool      = stroke.get("tool", "fineliner")
    thickness = stroke.get("thickness", 1.0)
    params    = _TOOL_PARAMS.get(tool, _DEFAULT_TOOL_PARAMS)
    base_w, pmin, pmax, _ = params

    pressures = stroke.get("p", [])
    pressure  = pressures[point_index] if point_index < len(pressures) else 0.5

    raw_widths = stroke.get("width", [])
    raw_w      = raw_widths[point_index] if point_index < len(raw_widths) else 30.0
    # Normalise around empirical median of ~30; clamp to [0.4, 3.0]
    w_factor   = max(0.4, min(3.0, raw_w / 30.0))

    # Pressure factor: range [pmin, pmax]
    p_factor = pmin + pressure * (pmax - pmin)

    # Highlighter and shader: ignore per-point pressure for width — stay uniform
    if tool in ("highlighter", "shader"):
        p_factor = (pmin + pmax) / 2.0
        w_factor = 1.0

    width_f = base_w * thickness * p_factor * w_factor * scale
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

    Uses the reMarkable Paper Pro native coordinate space with correct origin
    translation so ink is never clipped at the edges. Highlights and shader
    strokes are composited on a separate RGBA layer for transparency.

    Args:
        strokes: Full-fidelity stroke dicts from ``rm_parser.parse_rmdoc``.
        scale:   Render scale (default 2.0 → ~2808×3944 px output).

    Returns:
        Raw PNG bytes. Returns ``b""`` for a blank page.
    """
    if not strokes:
        return b""

    visible = [s for s in strokes if s.get("tool", "") not in ("eraser", "eraser_area")]
    if not visible:
        return b""

    pad = _PADDING
    w   = int((_RM_WIDTH  + 2 * pad) * scale)
    h   = int((_RM_HEIGHT + 2 * pad) * scale)

    # Three layers: base (white RGB), opaque ink (RGBA), transparent overlay (RGBA)
    # Overlay carries: highlights, shader strokes, pencil shading
    base          = Image.new("RGB",  (w, h), (255, 255, 255))
    ink_layer     = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    ink_draw      = ImageDraw.Draw(ink_layer)
    overlay_layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    overlay_draw  = ImageDraw.Draw(overlay_layer)

    _OVERLAY_TOOLS = {"highlighter", "shader", "pencil", "mechanical_pencil"}

    for stroke in visible:
        xs   = stroke.get("x", [])
        ys   = stroke.get("y", [])
        tool = stroke.get("tool", "fineliner")

        target_draw = overlay_draw if tool in _OVERLAY_TOOLS else ink_draw

        if len(xs) < 2:
            if xs and ys:
                px, py = _to_px(xs[0], ys[0], scale, pad)
                rgba   = _stroke_rgba(stroke, 0, scale)
                r      = max(1, _stroke_width_px(stroke, 0, scale) // 2)
                target_draw.ellipse([px - r, py - r, px + r, py + r], fill=rgba)
            continue

        pts = list(zip(xs, ys))
        for i in range(len(pts) - 1):
            px0, py0 = _to_px(pts[i][0],   pts[i][1],   scale, pad)
            px1, py1 = _to_px(pts[i+1][0], pts[i+1][1], scale, pad)
            rgba     = _stroke_rgba(stroke, i, scale)
            width    = _stroke_width_px(stroke, i, scale)
            target_draw.line([(px0, py0), (px1, py1)], fill=rgba, width=width)

    # Composite: white ← ink ← overlay (highlights / shading on top)
    composited = Image.alpha_composite(base.convert("RGBA"), ink_layer)
    composited = Image.alpha_composite(composited, overlay_layer)

    buf = io.BytesIO()
    composited.convert("RGB").save(buf, format="PNG", optimize=False)
    logger.debug("Rendered page: %dx%d px, %d strokes", w, h, len(visible))
    return buf.getvalue()
