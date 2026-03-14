"""
File download module.
Handles downloading files from TeraBox dlink URLs with progress tracking,
chunked transfer, and resume support.
"""

import logging
import os
import sys
import time

import requests

import config

logger = logging.getLogger(__name__)


def download_file(
    dlink: str,
    filename: str,
    ndus: str,
    output_dir: str = None,
    on_progress: callable = None,
) -> str:
    """
    Download a file from a TeraBox dlink URL.

    Args:
        dlink: Direct download link from TeraBox API.
        filename: Name to save the file as.
        ndus: Session cookie for authentication.
        output_dir: Directory to save file to. Defaults to config.DEFAULT_DOWNLOAD_DIR.
        on_progress: Optional callback(downloaded_bytes, total_bytes, speed_bps).

    Returns:
        Full path to the downloaded file.
    """
    if output_dir is None:
        output_dir = config.DEFAULT_DOWNLOAD_DIR

    os.makedirs(output_dir, exist_ok=True)

    # Sanitize filename
    filename = _sanitize_filename(filename)
    filepath = os.path.join(output_dir, filename)

    host = config.get_host()
    headers = {
        "User-Agent": config.USER_AGENT,
        "Referer": f"{host}/",
        "Cookie": f"ndus={ndus}",
        "Accept": "*/*",
    }

    # Check for partial download (resume support)
    downloaded = 0
    if os.path.exists(filepath):
        downloaded = os.path.getsize(filepath)
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            logger.info(f"Resuming download from byte {downloaded}")

    logger.info(f"Downloading: {filename}")
    logger.info(f"  URL: {dlink[:80]}...")
    logger.info(f"  Destination: {filepath}")

    try:
        resp = requests.get(
            dlink,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=60,
        )

        # Handle response status
        if resp.status_code == 416:
            # Range not satisfiable — file is already complete
            logger.info("File already fully downloaded.")
            return filepath

        resp.raise_for_status()

        # Get total size
        total_size = 0
        content_length = resp.headers.get("Content-Length")
        if content_length:
            total_size = int(content_length) + downloaded

        # If server didn't accept Range, start from scratch
        if downloaded > 0 and resp.status_code != 206:
            logger.info("Server doesn't support resume, downloading from start.")
            downloaded = 0

        mode = "ab" if resp.status_code == 206 else "wb"

        start_time = time.time()
        last_report_time = start_time

        with open(filepath, mode) as f:
            for chunk in resp.iter_content(chunk_size=config.CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    elapsed = now - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0

                    # Report progress
                    if on_progress:
                        on_progress(downloaded, total_size, speed)
                    elif now - last_report_time >= 1.0:
                        # CLI progress
                        _print_progress(downloaded, total_size, speed)
                        last_report_time = now

        # Final progress update
        if not on_progress:
            _print_progress(downloaded, total_size, 0, final=True)

        elapsed_total = time.time() - start_time
        avg_speed = downloaded / elapsed_total if elapsed_total > 0 else 0
        logger.info(f"Download complete: {filepath} ({_format_speed(avg_speed)})")
        return filepath

    except requests.exceptions.RequestException as e:
        logger.error(f"Download failed: {e}")
        raise RuntimeError(f"Download failed: {e}")


def download_file_as_stream(dlink: str, ndus: str):
    """
    Stream a file download (for web proxy use).
    Yields chunks for Flask response streaming.

    Returns:
        Generator yielding (chunk, content_type, content_length, filename) on first yield,
        then raw chunks thereafter.
    """
    host = config.get_host()
    headers = {
        "User-Agent": config.USER_AGENT,
        "Referer": f"{host}/",
        "Cookie": f"ndus={ndus}",
        "Accept": "*/*",
    }

    resp = requests.get(
        dlink,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=60,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    content_length = resp.headers.get("Content-Length", "0")

    # Try to extract filename from Content-Disposition
    filename = "download"
    cd = resp.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        import re
        match = re.search(r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd)
        if match:
            from urllib.parse import unquote
            filename = unquote(match.group(1))

    yield {
        "content_type": content_type,
        "content_length": content_length,
        "filename": filename,
    }

    for chunk in resp.iter_content(chunk_size=config.CHUNK_SIZE):
        if chunk:
            yield chunk


# ─── CLI Progress Display ────────────────────────────────────────────────────


def _print_progress(downloaded: int, total: int, speed: float, final: bool = False):
    """Print a progress bar to terminal."""
    if total > 0:
        pct = (downloaded / total) * 100
        bar_len = 30
        filled = int(bar_len * downloaded / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        line = f"\r  [{bar}] {pct:5.1f}%  {_format_size(downloaded)}/{_format_size(total)}  {_format_speed(speed)}"
    else:
        line = f"\r  Downloaded: {_format_size(downloaded)}  {_format_speed(speed)}"

    sys.stdout.write(line)
    sys.stdout.flush()

    if final:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _format_size(size_bytes: int) -> str:
    """Format byte count."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"


def _format_speed(bps: float) -> str:
    """Format bytes per second."""
    if bps <= 0:
        return ""
    return f"{_format_size(int(bps))}/s"


def _sanitize_filename(name: str) -> str:
    """Remove invalid characters from filename."""
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "_")
    # Remove leading/trailing whitespace and dots
    name = name.strip().strip(".")
    return name or "download"
