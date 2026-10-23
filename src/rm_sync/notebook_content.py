"""
notebook_content.py — Helpers for deciding whether notebook pages are empty.

The sync pipeline treats a page as meaningful when it has at least one stroke
or any non-whitespace text (typed text from the notebook format, or recognised
text from HWR).  This module keeps that rule in one place so the writer,
webhook, and tests all agree on the definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NotebookPageContent:
    """Prepared content for one notebook page."""

    strokes: list[dict[str, Any]] = field(default_factory=list)
    typed_text: str = ""
    recognized_text: str = ""
    image_bytes: bytes = b""

    def body_text(self) -> str:
        """
        Return the Markdown body for this page.

        Typed text and recognised handwriting are combined, with duplicate text
        suppressed when both sources happen to produce the same content.
        """
        parts: list[str] = []
        seen: set[str] = set()
        for raw_text in (self.typed_text, self.recognized_text):
            text = raw_text.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            parts.append(text)
        return "\n\n".join(parts)


def page_has_real_content(page: NotebookPageContent) -> bool:
    """Return ``True`` when a page has strokes or any real text."""
    return bool(page.strokes) or bool(page.body_text())


def filter_empty_pages(
    pages: list[NotebookPageContent],
    *,
    skip_empty_pages: bool,
) -> list[NotebookPageContent]:
    """Drop empty pages when configured, preserving the original order."""
    if not skip_empty_pages:
        return list(pages)
    return [page for page in pages if page_has_real_content(page)]


def notebook_has_real_content(pages: list[NotebookPageContent]) -> bool:
    """Return ``True`` when at least one page has meaningful content."""
    return any(page_has_real_content(page) for page in pages)
