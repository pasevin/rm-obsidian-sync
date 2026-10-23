from __future__ import annotations

import builtins
import importlib
import sys
import unittest
from unittest.mock import patch


class RmParserImportTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("rm_sync.rm_parser", None)

    def test_parser_imports_cleanly_with_real_rmscene(self) -> None:
        from rmscene.text import TextDocument as RealTextDocument

        sys.modules.pop("rm_sync.rm_parser", None)
        module = importlib.import_module("rm_sync.rm_parser")

        self.assertTrue(module._RMSCENE_AVAILABLE)
        self.assertIs(module.TextDocument, RealTextDocument)
        self.assertEqual(module.TextDocument.__module__, "rmscene.text")

    def test_typed_text_import_failure_does_not_disable_stroke_parsing(self) -> None:
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "rmscene.text":
                raise ImportError("simulated typed-text import failure")
            return real_import(name, globals, locals, fromlist, level)

        sys.modules.pop("rm_sync.rm_parser", None)
        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertLogs("rm_sync.rm_parser", level="WARNING") as logs:
                module = importlib.import_module("rm_sync.rm_parser")

        self.assertTrue(module._RMSCENE_AVAILABLE)
        self.assertIsNone(module.TextDocument)
        joined_logs = "\n".join(logs.output)
        self.assertIn("simulated typed-text import failure", joined_logs)
        self.assertNotIn("not installed", joined_logs.lower())


if __name__ == "__main__":
    unittest.main()
