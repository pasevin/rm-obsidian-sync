from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from rm_sync.rm_parser import _parse_page_from_rm_bytes

if "PIL" not in sys.modules:
    pil_module = ModuleType("PIL")
    pil_module.Image = SimpleNamespace()
    pil_module.ImageDraw = SimpleNamespace()
    sys.modules["PIL"] = pil_module

from rm_sync.webhook import _sync_document


def _build_v5_page_bytes() -> bytes:
    header = b"reMarkable .lines file, version=5".ljust(43, b" ")
    segments = [
        struct.pack("<ffffff", 10.0, 20.0, 0.0, 0.0, 1.5, 0.8),
        struct.pack("<ffffff", 30.0, 40.0, 0.0, 0.0, 1.25, 0.6),
    ]
    return b"".join(
        [
            header,
            struct.pack("<I", 1),             # one layer
            struct.pack("<I", 1),             # one stroke
            struct.pack("<IIIfII", 17, 0, 0, 2.0, 0, len(segments)),
            *segments,
        ]
    )


def _build_rmdoc_with_page(page_bytes: bytes, *, page_id: str = "page-1") -> bytes:
    doc_uuid = "doc-uuid"
    content = {
        "fileType": "notebook",
        "VissibleName": "Parser test",
        "cPages": {"pages": [page_id]},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{doc_uuid}.content", json.dumps(content))
        zf.writestr(f"{doc_uuid}/{page_id}.rm", page_bytes)
    return buffer.getvalue()


class LegacyLinesParserTests(unittest.TestCase):
    def test_v5_page_bytes_parse_into_renderer_strokes(self) -> None:
        parsed = _parse_page_from_rm_bytes(_build_v5_page_bytes(), "page-v5")

        self.assertIsNone(parsed.parse_error)
        self.assertEqual(parsed.file_format, "v5-lines")
        self.assertEqual(parsed.page_id, "page-v5")
        self.assertEqual(len(parsed.strokes), 1)

        stroke = parsed.strokes[0]
        self.assertEqual(stroke["tool"], "fineliner")
        self.assertEqual(stroke["color"], [0, 0, 0])
        self.assertEqual(stroke["x"], [10.0, 30.0])
        self.assertEqual(stroke["y"], [20.0, 40.0])
        self.assertEqual(stroke["width"], [1.5, 1.25])
        self.assertAlmostEqual(stroke["p"][0], 0.8)
        self.assertAlmostEqual(stroke["p"][1], 0.6)

    def test_unparseable_page_is_classified_as_unparseable(self) -> None:
        parsed = _parse_page_from_rm_bytes(b"definitely-not-a-valid-rm-page", "page-bad")

        self.assertEqual(parsed.page_id, "page-bad")
        self.assertEqual(parsed.strokes, [])
        self.assertIsNotNone(parsed.parse_error)
        self.assertTrue(parsed.parse_error)
        self.assertEqual(parsed.file_format, "v6")


class UnparseableNotebookSyncTests(unittest.TestCase):
    def test_unparseable_page_is_written_instead_of_skipped(self) -> None:
        rmdoc_bytes = _build_rmdoc_with_page(b"not-a-real-page")

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def download_document_zip(self, doc_id: str) -> bytes:
                return rmdoc_bytes

        async def run_test() -> None:
            with patch("rm_sync.webhook.RmClient", return_value=FakeClient()), \
                 patch("rm_sync.webhook.resolve_note_path", return_value=Path("/tmp/parser-test.md")), \
                 patch("rm_sync.webhook.write_note") as write_note, \
                 patch("rm_sync.webhook.mark_note_skipped") as mark_note_skipped, \
                 patch("rm_sync.webhook._record_sync_complete", new=AsyncMock()):
                await _sync_document("doc-123", display_name="Parser test")

            mark_note_skipped.assert_not_called()
            write_note.assert_called_once()
            page_texts = write_note.call_args.kwargs["page_texts"]
            page_images = write_note.call_args.kwargs["page_images"]
            self.assertEqual(len(page_texts), 1)
            self.assertEqual(page_images, [b""])
            self.assertIn("could not be parsed", page_texts[0].lower())
            self.assertIn("page-1", page_texts[0])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
