"""Download a document from a blob URL to a local temp file."""

import tempfile
from contextlib import nullcontext as _nullcontext
from pathlib import Path

try:
    import logfire
except ImportError:
    logfire = None

import httpx


def download(url: str, timeout: int = 60) -> Path:
    """Download a URL to a temp file with the correct extension.

    Args:
        url: The blob URL to download.
        timeout: HTTP timeout in seconds.

    Returns:
        Path to the downloaded temp file. Caller must delete when done.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx responses.
        httpx.TimeoutException: On timeout.
    """
    with logfire.span("download_document", url=url[:200]) if logfire else _nullcontext():
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()

        # Guess extension from URL
        url_lower = url.lower()
        suffix = ".pdf"  # default
        for ext in [".docx", ".xlsx", ".pptx", ".html", ".jpg", ".png"]:
            if url_lower.endswith(ext):
                suffix = ext
                break

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(response.content)
        tmp.close()
        return Path(tmp.name)
