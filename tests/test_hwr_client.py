from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

if "PIL" not in sys.modules:
    pil_module = ModuleType("PIL")
    pil_module.Image = SimpleNamespace()
    pil_module.ImageDraw = SimpleNamespace()
    sys.modules["PIL"] = pil_module

from rm_sync import hwr_client
from rm_sync.config import Config


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.request = httpx.Request("POST", hwr_client._OPENROUTER_URL)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(
                self.status_code,
                request=self.request,
                json=self._payload,
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=response,
            )

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse], model_calls: list[str], timeout: int):
        self._responses = responses
        self._model_calls = model_calls
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        self._model_calls.append(json["model"])
        return self._responses.pop(0)


class HwrClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hwr_client._WARNED_DEAD_OPENROUTER_MODELS.clear()

    async def test_openrouter_falls_back_across_models_in_order(self) -> None:
        responses = [
            _FakeResponse(404),
            _FakeResponse(429),
            _FakeResponse(
                200,
                {
                    "choices": [
                        {"message": {"content": "Recovered text"}}
                    ]
                },
            ),
        ]
        model_calls: list[str] = []
        fake_config = SimpleNamespace(
            openrouter_api_key="test-key",
            llm_vision_models=("dead-model", "busy-model", "good-model"),
            hwr_language="English",
            myscript_app_key="",
            myscript_hmac_key="",
        )

        def client_factory(*, timeout: int) -> _FakeAsyncClient:
            return _FakeAsyncClient(responses, model_calls, timeout)

        with patch.object(hwr_client, "config", fake_config), \
             patch.object(hwr_client, "render_page_to_png", return_value=b"png-bytes"), \
             patch.object(hwr_client.httpx, "AsyncClient", side_effect=client_factory), \
             patch.object(hwr_client.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            result = await hwr_client.recognize_page_result([{"id": "s0"}])

        self.assertEqual(model_calls, ["dead-model", "busy-model", "good-model"])
        self.assertEqual(result.engine, "openrouter")
        self.assertEqual(result.model, "good-model")
        self.assertEqual(result.text, "Recovered text")
        sleep_mock.assert_awaited_once()

    async def test_dead_model_warning_is_emitted_once_per_run(self) -> None:
        responses = [
            _FakeResponse(404),
            _FakeResponse(200, {"choices": [{"message": {"content": "First"}}]}),
            _FakeResponse(404),
            _FakeResponse(200, {"choices": [{"message": {"content": "Second"}}]}),
        ]
        model_calls: list[str] = []
        fake_config = SimpleNamespace(
            openrouter_api_key="test-key",
            llm_vision_models=("dead-model", "good-model"),
            hwr_language="English",
            myscript_app_key="",
            myscript_hmac_key="",
        )

        def client_factory(*, timeout: int) -> _FakeAsyncClient:
            return _FakeAsyncClient(responses, model_calls, timeout)

        with patch.object(hwr_client, "config", fake_config), \
             patch.object(hwr_client, "render_page_to_png", return_value=b"png-bytes"), \
             patch.object(hwr_client.httpx, "AsyncClient", side_effect=client_factory), \
             self.assertLogs("rm_sync.hwr_client", level="WARNING") as logs:
            await hwr_client.recognize_page_result([{"id": "s0"}])
            await hwr_client.recognize_page_result([{"id": "s1"}])

        dead_model_logs = [
            line for line in logs.output
            if "Configured OpenRouter model is unavailable" in line
        ]
        self.assertEqual(len(dead_model_logs), 1)
        self.assertEqual(model_calls, ["dead-model", "good-model", "dead-model", "good-model"])

    def test_legacy_single_model_alias_populates_model_list(self) -> None:
        env = dict(os.environ)
        env.pop("LLM_VISION_MODELS", None)
        env["LLM_VISION_MODEL"] = "single-model"

        with patch.dict(os.environ, env, clear=True):
            cfg = Config()

        self.assertEqual(cfg.llm_vision_models, ("single-model",))


if __name__ == "__main__":
    unittest.main()
