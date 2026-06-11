"""
rm_parser.py — Parse .rmdoc archives and extract stroke data.

Public functions
----------------
extract_metadata(rmdoc_bytes) -> dict
    Returns the notebook's file type, display name, and page-UUID list.

parse_rmdoc(rmdoc_bytes) -> list[list[dict]]
    Returns one entry per page; each entry is a list of stroke dicts in
    the format expected by the MyScript iink API::

        {"id": "s0", "x": [...], "y": [...], "t": [...], "p": [...]}

The rmscene library is imported defensively — if an individual page fails
to parse, the error is logged and an empty stroke list is returned for
that page so the rest of the document can still be processed.
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

    _RMSCENE_AVAILABLE = True
except ImportError:
    logger.warning(
        "rmscene is not installed — stroke parsing will return empty pages. "
        "Install it with: uv pip install rmscene"
    )
    _RMSCENE_AVAILABLE = False


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
    # Fallback: any .content
    for name in zf.namelist():
        if name.endswith(".content"):
            return name
    return None


def _points_to_stroke(
    points: list,
    stroke_index: int,
    strokes: list[dict],
    page_id: str,
) -> tuple[int, list[dict]]:
    """
    Convert a list of reMarkable Point objects to a MyScript stroke dict
    and append it to *strokes*.

    Returns:
        Updated (stroke_index, strokes) tuple.
    """
    xs: list[float] = []
    ys: list[float] = []
    ts: list[int] = []
    ps: list[float] = []

    for idx, pt in enumerate(points):
        try:
            xs.append(float(pt.x))
            ys.append(float(pt.y))
            ts.append(idx * 5)  # Synthesised 5 ms intervals
            pressure = getattr(pt, "pressure", None)
            if pressure is None:
                pressure = getattr(pt, "p", 0.5)
            ps.append(float(pressure))
        except (AttributeError, TypeError, ValueError) as exc:
            logger.debug(
                "Skipping bad point in page %s stroke %d: %s",
                page_id,
                stroke_index,
                exc,
            )

    if xs:
        strokes.append(
            {
                "id": f"s{stroke_index}",
                "x": xs,
                "y": ys,
                "t": ts,
                "p": ps,
            }
        )
        stroke_index += 1

    return stroke_index, strokes


def _parse_strokes_from_rm_bytes(rm_bytes: bytes, page_id: str) -> list[dict]:
    """
    Parse a single ``.rm`` (v6) binary blob and return a list of stroke dicts.

    Each stroke dict is MyScript-compatible::

        {"id": "sN", "x": [float, ...], "y": [float, ...],
         "t": [int, ...], "p": [float, ...]}

    *t* (timestamps) is synthesised as sequential 5 ms intervals because
    the reMarkable v6 format does not store absolute timestamps per point.

    On any parse error the exception is logged and an empty list returned.
    """
    if not _RMSCENE_AVAILABLE:
        return []

    strokes: list[dict] = []
    try:
        tree = read_tree(io.BytesIO(rm_bytes))
    except Exception as exc:
        logger.error("Failed to read .rm tree for page %s: %s", page_id, exc)
        return []

    stroke_index = 0

    # Deep-walk the CRDT scene tree to collect all Line objects.
    # The v6 format nests strokes inside Group → Group → Line, not
    # directly under the root — so a flat iteration misses everything.
    def _collect_lines(node: Any) -> list[Any]:
        found: list[Any] = []
        if isinstance(node, SceneLineItemBlock):
            found.append(node)
            return found
        # Line objects from scene_items live directly in children dicts
        if hasattr(node, "__class__") and node.__class__.__name__ == "Line":
            found.append(node)
            return found
        if hasattr(node, "children"):
            for child in node.children.values():
                found.extend(_collect_lines(child))
        if hasattr(node, "value") and node.value is not None:
            v = node.value
            if v.__class__.__name__ == "Line":
                found.append(v)
            elif hasattr(v, "children"):
                for child in v.children.values():
                    found.extend(_collect_lines(child))
        return found

    line_nodes = _collect_lines(tree.root if hasattr(tree, "root") else tree)

    for node in line_nodes:
        # Handle both SceneLineItemBlock (old path) and Line (new path)
        if isinstance(node, SceneLineItemBlock):
            try:
                items = list(node.value)
            except Exception:
                continue
            for item in items:
                line = getattr(item, "value", None) or (item if hasattr(item, "points") else None)
                if line is None:
                    continue
                points = getattr(line, "points", None)
                if not points:
                    continue
                stroke_index, strokes = _points_to_stroke(points, stroke_index, strokes, page_id)
        else:
            # Direct Line node
            points = getattr(node, "points", None)
            if not points:
                continue
            stroke_index, strokes = _points_to_stroke(points, stroke_index, strokes, page_id)

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
            return {
                "fileType": "notebook",
                "name": "Unknown",
                "pages": [],
                "doc_uuid": "",
            }

        try:
            content = json.loads(zf.read(content_path))
        except Exception as exc:
            logger.error("Failed to parse .content JSON: %s", exc)
            content = {}

        # Derive the UUID prefix (the .content file is named <uuid>.content)
        doc_uuid = content_path.rsplit(".content", 1)[0].lstrip("/")

        file_type: str = content.get("fileType", "notebook")
        name: str = content.get(
            "VissibleName",
            content.get("visibleName", content.get("name", "Untitled")),
        )

        # Extract page UUIDs
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
            # Older format: cPages is a list of page UUIDs
            pages = [str(p) for p in c_pages]

        return {
            "fileType": file_type,
            "name": name,
            "pages": pages,
            "doc_uuid": doc_uuid,
        }


def parse_rmdoc(rmdoc_bytes: bytes) -> list[list[dict]]:
    """
    Parse all stroke data from an .rmdoc archive.

    Args:
        rmdoc_bytes: Raw bytes of the .rmdoc (ZIP) file.

    Returns:
        A list of pages.  Each page is a list of stroke dicts in
        MyScript iink format::

            [
                [  # page 0
                    {"id": "s0", "x": [...], "y": [...], "t": [...], "p": [...]},
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
            # .rm files live at <doc_uuid>/<page_uuid>.rm
            rm_path = f"{doc_uuid}/{page_id}.rm"

            # Try with/without leading slash
            if rm_path not in zip_names:
                alt = rm_path.lstrip("/")
                if alt in zip_names:
                    rm_path = alt
                else:
                    logger.debug(
                        "No .rm file for page %s (tried %s)", page_id, rm_path
                    )
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

    logger.info(
        "Parsed %d page(s) for doc %s", len(all_pages), doc_uuid or "(unknown)"
    )
    return all_pages
