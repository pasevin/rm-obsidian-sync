"""
cli.py — Click CLI commands for rm-obsidian-sync.

Commands
--------
rm-register --code <one-time-code>
    Register the daemon as an rmfakecloud device.

rm-upload <file> [--folder <name>]
    Upload a PDF or Markdown file to the reMarkable via rmfakecloud.
    Markdown files are converted to PDF using pandoc (if available),
    falling back to the ``markdown`` + weasyprint libraries.

rm-setup-webhook [--url <webhook-url>]
    Configure the rmfakecloud webhook to point at this daemon.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Logging is set up when the CLI is invoked directly
# ──────────────────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stderr,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Markdown → PDF conversion helpers
# ──────────────────────────────────────────────────────────────────────────────


def _convert_md_to_pdf_pandoc(md_path: Path, out_pdf: Path) -> None:
    """
    Convert *md_path* to *out_pdf* using ``pandoc``.

    Raises:
        RuntimeError: If pandoc exits with a non-zero status.
    """
    cmd = [
        "pandoc",
        str(md_path),
        "-o",
        str(out_pdf),
        "--pdf-engine=xelatex",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"pandoc failed (exit {result.returncode}):\n{result.stderr}"
        )


def _convert_md_to_pdf_weasyprint(md_path: Path, out_pdf: Path) -> None:
    """
    Convert *md_path* to *out_pdf* using the ``markdown`` + ``weasyprint``
    Python libraries.

    Raises:
        ImportError:  If ``markdown`` or ``weasyprint`` are not installed.
        RuntimeError: On any conversion error.
    """
    import markdown as md_lib  # type: ignore[import]
    from weasyprint import HTML  # type: ignore[import]

    md_text = md_path.read_text(encoding="utf-8")
    html_body = md_lib.markdown(md_text, extensions=["extra", "tables"])
    html_full = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; line-height: 1.6; }}
  pre, code {{ background: #f4f4f4; padding: 2px 4px; }}
  pre {{ padding: 1em; overflow-x: auto; }}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""
    HTML(string=html_full, base_url=str(md_path.parent)).write_pdf(str(out_pdf))


def _markdown_to_pdf(md_path: Path) -> Path:
    """
    Convert *md_path* (Markdown) to a temporary PDF file.

    Strategy:
    1. If ``pandoc`` is on PATH, use it.
    2. Else if ``weasyprint`` is importable, use markdown + weasyprint.
    3. Else raise a clear error.

    Returns:
        Path to the generated PDF (in a temporary directory).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="rm-upload-"))
    out_pdf = tmpdir / (md_path.stem + ".pdf")

    if shutil.which("pandoc"):
        click.echo(f"Converting via pandoc: {md_path.name} → {out_pdf.name}")
        try:
            _convert_md_to_pdf_pandoc(md_path, out_pdf)
            return out_pdf
        except RuntimeError as exc:
            click.echo(f"pandoc conversion failed: {exc}", err=True)
            click.echo("Falling back to weasyprint …", err=True)

    try:
        import markdown  # noqa: F401
        import weasyprint  # noqa: F401

        click.echo(
            f"Converting via weasyprint: {md_path.name} → {out_pdf.name}"
        )
        _convert_md_to_pdf_weasyprint(md_path, out_pdf)
        return out_pdf
    except ImportError:
        pass

    raise click.ClickException(
        "Cannot convert Markdown to PDF.\n\n"
        "Please install one of:\n"
        "  • pandoc (recommended):\n"
        "      sudo apt install pandoc texlive-xetex\n"
        "  • weasyprint + markdown:\n"
        "      uv pip install weasyprint markdown\n"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Async runner helper
# ──────────────────────────────────────────────────────────────────────────────


def _run(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine from a synchronous Click command."""
    return asyncio.run(coro)


# ──────────────────────────────────────────────────────────────────────────────
# CLI commands
# ──────────────────────────────────────────────────────────────────────────────


@click.command("rm-register")
@click.option(
    "--code",
    required=True,
    prompt="One-time registration code",
    help="The short one-time code shown by rmfakecloud's device pairing UI.",
)
def register_cmd(code: str) -> None:
    """
    Register this daemon as an rmfakecloud device.

    Exchanges the one-time *code* for a device JWT, then immediately
    acquires a user JWT and stores both in the auth state file.

    Example::

        rm-register --code abc123
    """
    _setup_logging()
    from rm_sync.auth import register_device, get_user_token

    click.echo(f"Registering with rmfakecloud using code: {code!r} …")
    try:
        device_token = register_device(code)
        click.echo(
            f"Device registered. Token starts with: {device_token[:12]}…"
        )
    except Exception as exc:
        raise click.ClickException(f"Device registration failed: {exc}") from exc

    click.echo("Acquiring user token …")
    try:
        user_token = get_user_token()
        click.echo(f"User token acquired. Starts with: {user_token[:12]}…")
    except Exception as exc:
        raise click.ClickException(f"User token acquisition failed: {exc}") from exc

    from rm_sync.config import config

    click.echo(f"\n✓ Registration complete. Tokens stored in {config.auth_state_file}")


@click.command("rm-upload")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--folder",
    default=None,
    help="Name (or UUID) of the destination folder on the reMarkable.",
)
@click.option(
    "--name",
    default=None,
    help="Display name for the document (defaults to the filename stem).",
)
def upload_cmd(file: Path, folder: str | None, name: str | None) -> None:
    """
    Upload FILE to the reMarkable via rmfakecloud.

    FILE may be a PDF (uploaded directly) or a Markdown file (converted to
    PDF first using pandoc or weasyprint).

    Examples::

        rm-upload notes.pdf
        rm-upload my-article.md --folder "Reading List"
        rm-upload sketch.pdf --name "Project Sketch" --folder uuid-of-folder
    """
    _setup_logging()

    display_name = name or file.stem

    # ------------------------------------------------------------------ #
    # Resolve to a PDF
    # ------------------------------------------------------------------ #
    suffix = file.suffix.lower()
    pdf_path: Path
    _tmpdir_to_clean: Path | None = None

    if suffix == ".pdf":
        pdf_path = file
    elif suffix in (".md", ".markdown", ".txt"):
        click.echo(f"Input is {suffix!r} — converting to PDF …")
        pdf_path = _markdown_to_pdf(file)
        _tmpdir_to_clean = pdf_path.parent
    else:
        raise click.ClickException(
            f"Unsupported file type: {suffix!r}. "
            "Only .pdf, .md, .markdown, and .txt are accepted."
        )

    # ------------------------------------------------------------------ #
    # Upload
    # ------------------------------------------------------------------ #
    async def _upload() -> None:
        from rm_sync.rm_client import RmClient

        async with RmClient() as client:
            result = await client.upload_document(pdf_path, display_name, folder)
            click.echo(f"Upload response: {result}")

    try:
        _run(_upload())
        click.echo(f"\n✓ '{display_name}' uploaded successfully.")
    except Exception as exc:
        raise click.ClickException(f"Upload failed: {exc}") from exc
    finally:
        # Clean up temp PDF if we created one
        if _tmpdir_to_clean and _tmpdir_to_clean.exists():
            import shutil as _shutil

            _shutil.rmtree(_tmpdir_to_clean, ignore_errors=True)


@click.command("rm-setup-webhook")
@click.option(
    "--url",
    default="http://localhost:9090/webhook",
    show_default=True,
    help="Public URL of the rm-obsidian-sync webhook endpoint.",
)
def setup_webhook_cmd(url: str) -> None:
    """
    Configure rmfakecloud to send document events to this daemon.

    After running this command, rmfakecloud will POST a multipart/form-data
    notification to *url* every time a document is modified on the
    reMarkable.

    The default URL assumes the daemon is running on the same host as
    rmfakecloud.  For remote setups, expose the daemon with a reverse
    proxy or ngrok and pass the public URL via ``--url``.

    Examples::

        rm-setup-webhook
        rm-setup-webhook --url https://my-server.example.com/webhook
    """
    _setup_logging()

    async def _configure() -> None:
        from rm_sync.rm_client import RmClient

        async with RmClient() as client:
            current = await client.get_integrations()
            click.echo(f"Current integrations: {current}")

            result = await client.set_webhook(url)
            click.echo(f"Update response: {result}")

    click.echo(f"Configuring webhook → {url} …")
    try:
        _run(_configure())
        click.echo(f"\n✓ Webhook configured: {url}")
    except Exception as exc:
        raise click.ClickException(f"Webhook setup failed: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Make the module directly executable (python -m rm_sync.cli)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Expose all commands under a unified group when run directly
    @click.group()
    def _cli() -> None:
        pass

    _cli.add_command(register_cmd)
    _cli.add_command(upload_cmd)
    _cli.add_command(setup_webhook_cmd)
    _cli()
