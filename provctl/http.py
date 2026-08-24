"""Minimal HTTP GET with a selectable backend."""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import urllib.request

DEFAULT_TIMEOUT = 60
_GZIP_MAGIC = b"\x1f\x8b"


class HttpError(RuntimeError):
    pass


def _via_urllib(url: str, accept: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Accept-Encoding": "gzip",
            "User-Agent": "provctl/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _via_curl(url: str, accept: str, timeout: int) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        raise HttpError("PROVCTL_HTTP_BACKEND=curl but curl is not on PATH")
    proc = subprocess.run(
        [
            curl, "-sS", "--fail", "--compressed",
            "--max-time", str(timeout),
            "-H", f"Accept: {accept}",
            "-H", "User-Agent: provctl/0.1",
            url,
        ],
        capture_output=True,
        timeout=timeout + 10,
    )
    if proc.returncode != 0:
        raise HttpError(proc.stderr.decode("utf-8", "replace").strip() or "curl failed")
    return proc.stdout


def get(url: str, *, accept: str = "*/*", timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Fetch `url`, transparently decompressing a gzipped body."""
    backend = os.environ.get("PROVCTL_HTTP_BACKEND", "urllib").lower()
    if backend == "curl":
        raw = _via_curl(url, accept, timeout)
    else:
        raw = _via_urllib(url, accept, timeout)

    if raw[:2] == _GZIP_MAGIC:
        raw = gzip.decompress(raw)
    return raw
