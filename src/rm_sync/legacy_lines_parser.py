"""
legacy_lines_parser.py — Parse legacy reMarkable v3/v5 `.lines` page files.

This module implements a small pure-Python parser for the pre-v6 page format
used by older reMarkable notebooks. The binary layout is based on public format
documentation and prior open-source parsers such as rmtool, Remy, and rM2svg,
but the implementation here is intentionally minimal and tailored to the stroke
dict structure already consumed by this project.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

_HEADER_LEN = 43
_HEADER_TEMPLATE = "reMarkable .lines file, version={version}"
_STROKE_V3 = struct.Struct("<IIIfI")
_STROKE_V5 = struct.Struct("<IIIfII")
_SEGMENT = struct.Struct("<ffffff")
_UINT32 = struct.Struct("<I")

_COLOR_MAP: dict[int, tuple[int, int, int, float]] = {
    0: (0, 0, 0, 1.0),
    1: (144, 144, 144, 1.0),
    2: (255, 255, 255, 1.0),
    3: (255, 235, 0, 0.4),
    4: (0, 255, 100, 0.4),
    5: (255, 105, 180, 0.4),
    6: (30, 100, 255, 1.0),
    7: (210, 0, 50, 1.0),
    8: (100, 100, 100, 0.5),
    9: (255, 235, 0, 0.4),
    10: (0, 200, 80, 1.0),
    11: (0, 200, 220, 1.0),
    12: (200, 0, 200, 1.0),
    13: (240, 220, 0, 0.4),
}
_DEFAULT_COLOR = (0, 0, 0, 1.0)

_TOOL_MAP: dict[int, str] = {
    0: "paintbrush",
    1: "pencil",
    2: "ballpoint",
    3: "marker",
    4: "fineliner",
    5: "highlighter",
    6: "eraser",
    7: "mechanical_pencil",
    8: "eraser_area",
    12: "paintbrush",
    13: "mechanical_pencil",
    14: "pencil",
    15: "ballpoint",
    16: "marker",
    17: "fineliner",
    18: "highlighter",
}
_DEFAULT_TOOL = "fineliner"


class LegacyLinesFormatError(ValueError):
    """Raised when a legacy `.lines` page cannot be parsed safely."""


@dataclass
class ParsedLegacyLinesPage:
    """Parsed content for one legacy v3/v5 page."""

    version: int
    strokes: list[dict[str, Any]]


def detect_legacy_lines_version(rm_bytes: bytes) -> int | None:
    """Return 3 or 5 for legacy `.lines` pages, otherwise ``None``."""
    if len(rm_bytes) < _HEADER_LEN:
        return None

    header = rm_bytes[:_HEADER_LEN].decode("ascii", errors="ignore").rstrip()
    for version in (3, 5):
        if header == _HEADER_TEMPLATE.format(version=version):
            return version
    return None


def parse_legacy_lines_page(rm_bytes: bytes) -> ParsedLegacyLinesPage:
    """Parse a legacy reMarkable v3/v5 page into renderer-compatible strokes."""
    version = detect_legacy_lines_version(rm_bytes)
    if version is None:
        raise LegacyLinesFormatError("Not a supported legacy .lines page")

    offset = _HEADER_LEN
    n_layers = _unpack_scalar(_UINT32, rm_bytes, offset, "layer count")
    offset += _UINT32.size
    strokes: list[dict[str, Any]] = []
    stroke_index = 0

    for layer_index in range(n_layers):
        n_strokes = _unpack_scalar(
            _UINT32,
            rm_bytes,
            offset,
            f"stroke count for layer {layer_index}",
        )
        offset += _UINT32.size
        for stroke_number in range(n_strokes):
            if version == 3:
                pen, color, _unk1, base_width, n_segments, offset = _read_v3_stroke(
                    rm_bytes,
                    offset,
                    layer_index,
                    stroke_number,
                )
            else:
                pen, color, _unk1, base_width, _unk2, n_segments, offset = _read_v5_stroke(
                    rm_bytes,
                    offset,
                    layer_index,
                    stroke_number,
                )

            xs: list[float] = []
            ys: list[float] = []
            ts: list[int] = []
            ps: list[float] = []
            widths: list[float] = []

            for point_index in range(n_segments):
                (
                    x,
                    y,
                    _speed,
                    _direction,
                    point_width,
                    pressure,
                    offset,
                ) = _read_segment(
                    rm_bytes,
                    offset,
                    layer_index,
                    stroke_number,
                    point_index,
                )
                xs.append(x)
                ys.append(y)
                ts.append(point_index * 5)
                ps.append(float(pressure))
                widths.append(float(point_width))

            if not xs:
                continue

            color_rgb, opacity = _resolve_legacy_color(color)
            strokes.append(
                {
                    "id": f"s{stroke_index}",
                    "x": xs,
                    "y": ys,
                    "t": ts,
                    "p": ps,
                    "width": widths,
                    "color": list(color_rgb),
                    "opacity": opacity,
                    "tool": _resolve_legacy_tool(pen),
                    "thickness": float(base_width),
                }
            )
            stroke_index += 1

    return ParsedLegacyLinesPage(version=version, strokes=strokes)


def _unpack(
    fmt: struct.Struct,
    rm_bytes: bytes,
    offset: int,
    label: str,
) -> tuple[Any, ...]:
    """Unpack *fmt* from *rm_bytes* at *offset*, raising a descriptive error."""
    end = offset + fmt.size
    if end > len(rm_bytes):
        raise LegacyLinesFormatError(
            f"Unexpected EOF while reading {label} at byte offset {offset}"
        )
    return fmt.unpack_from(rm_bytes, offset)


def _unpack_scalar(
    fmt: struct.Struct,
    rm_bytes: bytes,
    offset: int,
    label: str,
) -> int:
    """Unpack and return a single integer scalar."""
    values = _unpack(fmt, rm_bytes, offset, label)
    return int(values[0])


def _read_v3_stroke(
    rm_bytes: bytes,
    offset: int,
    layer_index: int,
    stroke_number: int,
) -> tuple[int, int, int, float, int, int]:
    pen, color, unk1, base_width, n_segments = _unpack(
        _STROKE_V3,
        rm_bytes,
        offset,
        f"v3 stroke {stroke_number} in layer {layer_index}",
    )
    return pen, color, unk1, base_width, n_segments, offset + _STROKE_V3.size


def _read_v5_stroke(
    rm_bytes: bytes,
    offset: int,
    layer_index: int,
    stroke_number: int,
) -> tuple[int, int, int, float, int, int, int]:
    pen, color, unk1, base_width, unk2, n_segments = _unpack(
        _STROKE_V5,
        rm_bytes,
        offset,
        f"v5 stroke {stroke_number} in layer {layer_index}",
    )
    return pen, color, unk1, base_width, unk2, n_segments, offset + _STROKE_V5.size


def _read_segment(
    rm_bytes: bytes,
    offset: int,
    layer_index: int,
    stroke_number: int,
    point_index: int,
) -> tuple[float, float, float, float, float, float, int]:
    x, y, speed, direction, point_width, pressure = _unpack(
        _SEGMENT,
        rm_bytes,
        offset,
        (
            f"segment {point_index} of stroke {stroke_number} "
            f"in layer {layer_index}"
        ),
    )
    return x, y, speed, direction, point_width, pressure, offset + _SEGMENT.size


def _resolve_legacy_color(color_value: int) -> tuple[tuple[int, int, int], float]:
    entry = _COLOR_MAP.get(int(color_value), _DEFAULT_COLOR)
    return (entry[0], entry[1], entry[2]), entry[3]


def _resolve_legacy_tool(tool_value: int) -> str:
    return _TOOL_MAP.get(int(tool_value), _DEFAULT_TOOL)
