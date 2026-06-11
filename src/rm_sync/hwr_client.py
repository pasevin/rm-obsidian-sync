"""
hwr_client.py — Handwriting recognition with a three-tier fallback chain.

Priority
--------
1. **LLM Vision** (OpenRouter) — send a rendered PNG to a vision-capable model.
   Excellent on cursive and mixed handwriting.  Free models available.
   Active when ``OPENROUTER_API_KEY`` is set in .env.

2. **MyScript iink Cloud** — stroke-level API, requires a valid application key
   and HMAC key from developer.myscript.com.
   Active when ``MYSCRIPT_APP_KEY`` + ``MYSCRIPT_HMAC_KEY`` are set.

3. **Tesseract** (Docker fallback) — offline, zero-cost, handles print/block text.
   Falls back automatically if both cloud options fail.

If all three fail the page still lands in the vault — body will be empty so
at least the frontmatter, title and sync timestamp are preserved.

Public functions
----------------
recognize_page(strokes, lang) -> str
recognize_document(pages_strokes, lang) -> str
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
import subprocess
import tempfile
from typing import Any

import httpx

from rm_sync.config import config
from rm_sync.stroke_renderer import render_page_to_png

logger = logging.getLogger(__name__)

_MYSCRIPT_URL = "https://cloud.myscript.com/api/v4.0/iink/batch"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_TESSERACT_IMAGE = "tesseractshadow/tesseract4re"

def _build_hwr_prompt(language: str) -> str:
    """Return the LLM vision HWR prompt with the target language injected."""
    return (
        f"This is a handwritten note rendered from a reMarkable e-ink tablet. "
        f"The text is written in {language}. "
        f"Transcribe the exact handwritten text you see. "
        f"Output ONLY the transcribed text in {language} — no commentary, no quotation marks, "
        f"no explanation, no translation."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Backend 1 — LLM Vision via OpenRouter
# ──────────────────────────────────────────────────────────────────────────────


async def _recognize_via_llm_vision(
    strokes: list[dict[str, Any]],
    model: str,
    api_key: str,
    language: str = "English",
) -> str:
    """
    Render strokes to PNG and send to an OpenRouter vision model for HWR.

    Args:
        strokes:   Stroke dicts from rm_parser.
        model:     OpenRouter model ID (e.g. ``nvidia/nemotron-nano-12b-v2-vl:free``).
        api_key:   OpenRouter API key.
        language:  Natural-language name of the writing language (e.g. ``English``).

    Returns:
        Recognised text.

    Raises:
        httpx.HTTPStatusError: On non-2xx responses.
    """
    png_bytes = await asyncio.get_event_loop().run_in_executor(
        None, render_page_to_png, strokes
    )
    img_b64 = base64.b64encode(png_bytes).decode()

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": _build_hwr_prompt(language)},
            ],
        }],
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/rm-obsidian-sync",
        "X-Title": "rm-obsidian-sync",
    }

    logger.info("Sending %d stroke(s) to LLM Vision HWR (model=%s) …", len(strokes), model)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(_OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()

    result = resp.json()
    text: str = result["choices"][0]["message"]["content"].strip()
    logger.info("LLM Vision recognised %d character(s)", len(text))
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Backend 2 — MyScript iink Cloud
# ──────────────────────────────────────────────────────────────────────────────


def _compute_hmac(app_key: str, hmac_key: str, body: str) -> str:
    """HMAC-SHA512( key=hmac_key, msg=(app_key+body) ) — required by MyScript."""
    msg = (app_key + body).encode("utf-8")
    h = _hmac.new(hmac_key.encode("utf-8"), msg, hashlib.sha512)
    return h.hexdigest()


async def _recognize_via_myscript(
    strokes: list[dict[str, Any]],
    lang: str,
    app_key: str,
    hmac_key: str,
) -> str:
    """Call MyScript iink batch API. Raises on any error."""
    payload = {
        "configuration": {"lang": lang},
        "contentType": "Text",
        "strokeGroups": [{"strokes": strokes}],
    }
    body = json.dumps(payload, separators=(",", ":"))
    sig = _compute_hmac(app_key, hmac_key, body)
    headers = {
        "applicationKey": app_key,
        "hmac": sig,
        "Content-Type": "application/json",
        "Accept": "application/vnd.myscript.jiix",
    }

    logger.info("Sending %d stroke(s) to MyScript HWR (lang=%s) …", len(strokes), lang)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(_MYSCRIPT_URL, content=body, headers=headers)
        resp.raise_for_status()

    text: str = resp.json().get("label", "")
    logger.info("MyScript recognised %d character(s)", len(text))
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Backend 3 — Tesseract via Docker (offline fallback)
# ──────────────────────────────────────────────────────────────────────────────


def _recognize_via_tesseract(strokes: list[dict[str, Any]]) -> str:
    """Render strokes to PNG, run Tesseract 4 via Docker, return text."""
    png_bytes = render_page_to_png(strokes)
    if not png_bytes:
        return ""

    with tempfile.TemporaryDirectory(prefix="rm-tess-") as tmpdir:
        img_path = os.path.join(tmpdir, "page.png")
        out_base = os.path.join(tmpdir, "out")

        with open(img_path, "wb") as f:
            f.write(png_bytes)

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{tmpdir}:/tmp/tess",
            _TESSERACT_IMAGE,
            "tesseract",
            "/tmp/tess/page.png",
            "/tmp/tess/out",
            "-l", "eng",
            "--oem", "1",   # LSTM engine — better on cursive
            "--psm", "6",   # uniform text block
        ]

        logger.info("Running Tesseract (Docker fallback) on %d strokes …", len(strokes))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            logger.error("Tesseract exited %d: %s", result.returncode, result.stderr.strip())
            return ""

        out_txt = out_base + ".txt"
        if not os.path.exists(out_txt):
            return ""

        with open(out_txt) as f:
            return f.read().strip()


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


async def recognize_page(
    strokes: list[dict[str, Any]],
    lang: str | None = None,
    *,
    app_key: str | None = None,
    hmac_key: str | None = None,
) -> str:
    """
    Recognise a single page's strokes using the best available backend.

    Priority: LLM Vision (OpenRouter) → MyScript → Tesseract

    Args:
        strokes:  Stroke dicts (rm_parser format).
        lang:     BCP-47 language tag (used by MyScript only).
        app_key:  MyScript application key override.
        hmac_key: MyScript HMAC key override.

    Returns:
        Recognised text, or ``""`` on failure.
    """
    if not strokes:
        logger.debug("No strokes on page — skipping HWR")
        return ""

    # HWR_LANGUAGE is a natural-language name (e.g. "English") — used in the
    # LLM Vision prompt.  MyScript requires a BCP-47 tag (e.g. "en_US"), so
    # fall back to the explicit lang parameter or a sensible default for it.
    _lang_natural = config.hwr_language or "English"
    _lang_bcp47   = lang or "en_US"     # caller passes BCP-47 if they know it

    # ── 1. LLM Vision ────────────────────────────────────────────────────
    _or_key = config.openrouter_api_key
    _model = config.llm_vision_model
    if _or_key and _model:
        try:
            return await _recognize_via_llm_vision(strokes, _model, _or_key, language=_lang_natural)
        except Exception as exc:
            logger.warning("LLM Vision HWR failed — trying MyScript: %s", exc)
    else:
        logger.debug("OpenRouter key not configured — skipping LLM Vision")

    # ── 2. MyScript ───────────────────────────────────────────────────────
    _app_key = app_key or config.myscript_app_key
    _hmac_key = hmac_key or config.myscript_hmac_key
    if _app_key and _hmac_key:
        try:
            return await _recognize_via_myscript(strokes, _lang_bcp47, _app_key, _hmac_key)
        except Exception as exc:
            logger.warning("MyScript HWR failed — falling back to Tesseract: %s", exc)
    else:
        logger.debug("MyScript keys not configured — skipping MyScript")

    # ── 3. Tesseract ──────────────────────────────────────────────────────
    try:
        return await asyncio.get_event_loop().run_in_executor(
            None, _recognize_via_tesseract, strokes
        )
    except Exception as exc:
        logger.error("Tesseract fallback also failed: %s", exc)
        return ""


async def recognize_document(
    pages_strokes: list[list[dict[str, Any]]],
    lang: str | None = None,
    *,
    app_key: str | None = None,
    hmac_key: str | None = None,
) -> str:
    """
    Recognise all pages of a document and join them with page separators.

    Returns:
        Full document text with pages separated by ``\\n\\n---\\n\\n``.
    """
    page_texts: list[str] = []

    for i, strokes in enumerate(pages_strokes):
        logger.info("Recognising page %d/%d …", i + 1, len(pages_strokes))
        try:
            text = await recognize_page(
                strokes,
                lang=lang,
                app_key=app_key,
                hmac_key=hmac_key,
            )
        except Exception as exc:
            logger.error("HWR failed for page %d: %s", i + 1, exc)
            text = ""

        page_texts.append(text)

    return "\n\n---\n\n".join(page_texts)


# Expose for external consumers that need the HMAC utility
compute_hmac = _compute_hmac
