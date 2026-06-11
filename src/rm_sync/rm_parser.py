"""
rm_parser.py — Parse .rmdoc archives and extract full-fidelity stroke data.

Public functions
----------------
extract_metadata(rmdoc_bytes) -> dict
    Returns the notebook's file type, display name, and page-UUID list.

parse_rmdoc(rmdoc_bytes) -> list[list[dict]]
    Returns one entry per page; each entry is a list of stroke dicts
    preserving color, tool type, thickness, and per-point pressure/width::

        {
            "id":        "s0",
            "x":         [float, ...],
            "y":         [float, ...],
            "t":         [int, ...],      # synthesised timestamps (ms)
            "p":         [float, ...],    # pressure 0–1
            "width":     [float, ...],    # per-point width from tablet
            "color":     [int, int, int], # RGB 0–255
            "opacity":   float,           # 0–1  (< 1 for highlighters)
            "tool":      str,             # "fineliner" | "ballpoint" | …
            "thickness": float,           # stroke-level scale factor
        }
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# rmscene compat shim
# ──────────────────────────────────────────────────────────────────────────────

try:
    from rmscene import read_tree, SceneLineItemBlock  # type: ignore[import]
    from rmscene.scene_items import Line, Pen, PenColor  # type: ignore[import]

    _RMSCENE_AVAILABLE = True
except ImportError:
    logger.warning(
        "rmscene is not installed — stroke parsing will return empty pages. "
        "Install it with: uv pip install rmscene"
    )
    _RMSCENE_AVAILABLE = False
    Line = None  # type: ignore[assignment,misc]
    Pen = None   # type: ignore[assignment,misc]
    PenColor = None  # type: ignore[assignment,misc]


# ──────────────────────────────────────────────────────────────────────────────
# Color + tool mapping
# ──────────────────────────────────────────────────────────────────────────────

# Map PenColor enum values → (R, G, B, opacity)
# Opacity < 1.0 gives highlighter-style translucency in the renderer.
_COLOR_MAP: dict[int, tuple[int, int, int, float]] = {
    0:  (0,   0,   0,   1.0),   # BLACK
    1:  (144, 144, 144, 1.0),   # GRAY
    2:  (255, 255, 255, 1.0),   # WHITE
    3:  (255, 235, 0,   0.4),   # YELLOW  (highlight)
    4:  (0,   255, 100, 0.4),   # GREEN   (highlight)
    5:  (255, 105, 180, 0.4),   # PINK    (highlight)
    6:  (30,  100, 255, 1.0),   # BLUE
    7:  (210, 0,   50,  1.0),   # RED
    8:  (100, 100, 100, 0.5),   # GRAY_OVERLAP
    9:  (255, 235, 0,   0.4),   # HIGHLIGHT (fallback — overridden by color_rgba)
    10: (0,   200, 80,  1.0),   # GREEN_2
    11: (0,   200, 220, 1.0),   # CYAN
    12: (200, 0,   200, 1.0),   # MAGENTA
    13: (240, 220, 0,   0.4),   # YELLOW_2 (highlight)
}
_DEFAULT_COLOR: tuple[int, int, int, float] = (0, 0, 0, 1.0)

# Map Pen enum values → human-readable tool name
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
    21: "calligraphy",
    23: "shader",
}
_DEFAULT_TOOL = "fineliner"


def _resolve_color(line: Any) -> tuple[tuple[int, int, int], float]:
    """
    Return (R, G, B), opacity for a Line object.

    Prefers the explicit RGBA field (set for highlight colors on Paper Pro).
    Falls back to the PenColor enum lookup table.
    """
    # Explicit RGBA overrides the enum — used for custom / highlight colors
    rgba = getattr(line, "color_rgba", None)
    if rgba and len(rgba) == 4:
        r, g, b, a = rgba
        return (r, g, b), round(a / 255.0, 3)

    color_val = int(getattr(line, "color", 0))
    entry = _COLOR_MAP.get(color_val, _DEFAULT_COLOR)
    return (entry[0], entry[1], entry[2]), entry[3]


def _resolve_tool(line: Any) -> str:
    tool_val = int(getattr(line, "tool", 4))
    return _TOOL_MAP.get(tool_val, _DEFAULT_TOOL)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _open_rmdoc(rmdoc_bytes: bytes) -> zipfile.ZipFile:
    """Open *rmdoc_bytes* as a :class:`zipfile.ZipFile`."""
    return zipfile.ZipFile(io.BytesIO(rmdoc_bytes))


def _find_content_file(zf: zipfile.ZipFile) -> str | None:
    """
    Return the path of the ``.content`` metadata entry inside the ZIP,
    or *None* if not found.
    """
    for name in zf.namelist():
        if name.endswith(".content") and "/" not in name.lstrip("/"):
            return name
    for name in zf.namelist():
        if name.endswith(".content"):
            return name
    return None


def _line_to_stroke(line: Any, stroke_index: int) -> dict[str, Any] | None:
    """
    Convert a rmscene Line object to a full-fidelity stroke dict.

    Returns None if the line has no usable points.
    """
    points = getattr(line, "points", None)
    if not points:
        return None

    xs: list[float] = []
    ys: list[float] = []
    ts: list[int] = []
    ps: list[float] = []
    widths: list[float] = []

    for idx, pt in enumerate(points):
        try:
            xs.append(float(pt.x))
            ys.append(float(pt.y))
            ts.append(idx * 5)  # synthesised 5 ms intervals
            # pressure: rmscene stores it as an int 0–255 in newer formats
            raw_p = getattr(pt, "pressure", None)
            if raw_p is None:
                raw_p = getattr(pt, "p", 128)
            ps.append(float(raw_p) / 255.0 if raw_p > 1 else float(raw_p))
            # per-point width from tablet (int 0–255 or float)
            raw_w = getattr(pt, "width", 1)
            widths.append(float(raw_w))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.debug("Skipping bad point at index %d: %s", idx, exc)

    if not xs:
        return None

    color_rgb, opacity = _resolve_color(line)
    tool = _resolve_tool(line)
    thickness = float(getattr(line, "thickness_scale", 1.0))

    return {
        "id":        f"s{stroke_index}",
        "x":         xs,
        "y":         ys,
        "t":         ts,
        "p":         ps,
        "width":     widths,
        "color":     list(color_rgb),
        "opacity":   opacity,
        "tool":      tool,
        "thickness": thickness,
    }


def _parse_strokes_from_rm_bytes(rm_bytes: bytes, page_id: str) -> list[dict]:
    """
    Parse a single ``.rm`` (v6) binary blob and return a list of full-fidelity
    stroke dicts.

    On any parse error the exception is logged and an empty list is returned
    so the rest of the document can still be processed.
    """
    if not _RMSCENE_AVAILABLE:
        return []

    try:
        tree = read_tree(io.BytesIO(rm_bytes))
    except Exception as exc:
        logger.error("Failed to read .rm tree for page %s: %s", page_id, exc)
        return []

    strokes: list[dict] = []
    stroke_index = 0

    def _collect_lines(node: Any) -> list[Any]:
        """Recursively walk the CRDT scene tree collecting Line objects."""
        found: list[Any] = []
        if Line is not None and isinstance(node, Line):
            found.append(node)
            return found
        if isinstance(node, SceneLineItemBlock):
            found.append(node)
            return found
        if hasattr(node, "children"):
            for child in node.children.values():
                found.extend(_collect_lines(child))
        if hasattr(node, "value") and node.value is not None:
            v = node.value
            if Line is not None and isinstance(v, Line):
                found.append(v)
            elif hasattr(v, "children"):
                for child in v.children.values():
                    found.extend(_collect_lines(child))
        return found

    root = tree.root if hasattr(tree, "root") else tree
    line_nodes = _collect_lines(root)

    for node in line_nodes:
        if isinstance(node, SceneLineItemBlock):
            try:
                items = list(node.value)
            except Exception:
                continue
            for item in items:
                line = getattr(item, "value", None) or (
                    item if hasattr(item, "points") else None
                )
                if line is None:
                    continue
                stroke = _line_to_stroke(line, stroke_index)
                if stroke:
                    strokes.append(stroke)
                    stroke_index += 1
        else:
            # Direct Line node
            stroke = _line_to_stroke(node, stroke_index)
            if stroke:
                strokes.append(stroke)
                stroke_index += 1

    logger.debug(
        "Page %s → %d strokes, %d total points",
        page_id,
        len(strokes),
        sum(len(s["x"]) for s in strokes),
    )
    return strokes


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def extract_metadata(rmdoc_bytes: bytes) -> dict[str, Any]:
    """
    Extract document metadata from an .rmdoc archive.

    Args:
        rmdoc_bytes: Raw bytes of the .rmdoc (ZIP) file.

    Returns:
        A dict with keys:
        - ``fileType`` (str): ``"notebook"`` or ``"pdf"``
        - ``name`` (str): Display name of the document
        - ``pages`` (list[str]): Ordered list of page UUIDs
        - ``doc_uuid`` (str): The UUID used as the ZIP entry prefix
    """
    with _open_rmdoc(rmdoc_bytes) as zf:
        content_path = _find_content_file(zf)
        if content_path is None:
            logger.warning("No .content file found in .rmdoc archive")
            return {"fileType": "notebook", "name": "Unknown", "pages": [], "doc_uuid": ""}

        try:
            content = json.loads(zf.read(content_path))
        except Exception as exc:
            logger.error("Failed to parse .content JSON: %s", exc)
            content = {}

        doc_uuid = content_path.rsplit(".content", 1)[0].lstrip("/")
        file_type: str = content.get("fileType", "notebook")
        name: str = content.get(
            "VissibleName",
            content.get("visibleName", content.get("name", "Untitled")),
        )

        pages: list[str] = []
        c_pages = content.get("cPages", {})
        if isinstance(c_pages, dict):
            for p in c_pages.get("pages", []):
                if isinstance(p, dict):
                    pid = p.get("id", "")
                    if pid:
                        pages.append(pid)
                elif isinstance(p, str):
                    pages.append(p)
        elif isinstance(c_pages, list):
            pages = [str(p) for p in c_pages]

        return {"fileType": file_type, "name": name, "pages": pages, "doc_uuid": doc_uuid}


def parse_rmdoc(rmdoc_bytes: bytes) -> list[list[dict]]:
    """
    Parse all stroke data from an .rmdoc archive.

    Args:
        rmdoc_bytes: Raw bytes of the .rmdoc (ZIP) file.

    Returns:
        A list of pages.  Each page is a list of full-fidelity stroke dicts::

            [
                [  # page 0
                    {
                        "id":        "s0",
                        "x":         [float, ...],
                        "y":         [float, ...],
                        "t":         [int, ...],
                        "p":         [float, ...],   # pressure 0–1
                        "width":     [float, ...],   # per-point width
                        "color":     [R, G, B],      # 0–255 each
                        "opacity":   float,          # 0–1
                        "tool":      str,
                        "thickness": float,
                    },
                    ...
                ],
                ...
            ]

        Pages that fail to parse are represented as empty lists ``[]``.
    """
    metadata = extract_metadata(rmdoc_bytes)
    page_ids: list[str] = metadata["pages"]
    doc_uuid: str = metadata["doc_uuid"]

    if not page_ids:
        logger.warning("No pages found in document (doc_uuid=%s)", doc_uuid)
        return []

    all_pages: list[list[dict]] = []

    with _open_rmdoc(rmdoc_bytes) as zf:
        zip_names = set(zf.namelist())

        for page_id in page_ids:
            rm_path = f"{doc_uuid}/{page_id}.rm"
            if rm_path not in zip_names:
                alt = rm_path.lstrip("/")
                if alt in zip_names:
                    rm_path = alt
                else:
                    logger.debug("No .rm file for page %s", page_id)
                    all_pages.append([])
                    continue

            try:
                rm_bytes = zf.read(rm_path)
            except Exception as exc:
                logger.error("Failed to read %s from archive: %s", rm_path, exc)
                all_pages.append([])
                continue

            strokes = _parse_strokes_from_rm_bytes(rm_bytes, page_id)
            all_pages.append(strokes)

    logger.info("Parsed %d page(s) for doc %s", len(all_pages), doc_uuid or "(unknown)")
    return all_pages
