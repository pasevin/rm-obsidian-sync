from __future__ import annotations

import unittest
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("dotenv", SimpleNamespace(load_dotenv=lambda: None))

from rm_sync.notebook_content import (
    NotebookPageContent,
    filter_empty_pages,
    notebook_has_real_content,
    page_has_real_content,
)
from rm_sync.vault_writer import delete_note, load_state, mark_note_skipped


class NotebookContentTests(unittest.TestCase):
    def test_body_text_deduplicates_identical_sources(self) -> None:
        page = NotebookPageContent(
            typed_text="Meeting notes",
            recognized_text="Meeting notes",
        )

        self.assertEqual(page.body_text(), "Meeting notes")

    def test_empty_page_detection_requires_strokes_or_text(self) -> None:
        blank_page = NotebookPageContent()
        inked_page = NotebookPageContent(strokes=[{"id": "s0"}])
        typed_page = NotebookPageContent(typed_text="Typed heading")

        self.assertFalse(page_has_real_content(blank_page))
        self.assertTrue(page_has_real_content(inked_page))
        self.assertTrue(page_has_real_content(typed_page))

    def test_filter_empty_pages_keeps_only_meaningful_pages(self) -> None:
        pages = [
            NotebookPageContent(),
            NotebookPageContent(strokes=[{"id": "s0"}], image_bytes=b"png"),
            NotebookPageContent(typed_text="Typed paragraph"),
        ]

        kept = filter_empty_pages(pages, skip_empty_pages=True)

        self.assertEqual(len(kept), 2)
        self.assertTrue(notebook_has_real_content(kept))
        self.assertEqual(kept[0].image_bytes, b"png")
        self.assertEqual(kept[1].body_text(), "Typed paragraph")


class SkippedNotebookStateTests(unittest.TestCase):
    def test_skipped_empty_notebook_persists_state_without_vault_path(self) -> None:
        with TemporaryDirectory() as tmpdir:
            sync_state_file = Path(tmpdir) / "state.json"
            fake_config = SimpleNamespace(sync_state_file=sync_state_file)

            with patch("rm_sync.vault_writer.config", fake_config):
                mark_note_skipped("doc-123", doc_size=42)

                state = load_state()
                self.assertEqual(state["doc-123"]["doc_size"], 42)
                self.assertEqual(state["doc-123"]["vault_path"], "")
                self.assertEqual(state["doc-123"]["synced_at"], "")
                self.assertTrue(state["doc-123"]["processed_at"])
                self.assertTrue(state["doc-123"]["skipped_empty"])

                self.assertTrue(delete_note("doc-123"))
                self.assertEqual(load_state(), {})


if __name__ == "__main__":
    unittest.main()
