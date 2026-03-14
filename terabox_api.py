"""
TeraBox API module.
Handles share URL resolution, file listing, and download link extraction.
"""

import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import requests

import config

logger = logging.getLogger(__name__)

# ─── URL Validation ──────────────────────────────────────────────────────────


def is_valid_terabox_url(url: str) -> bool:
    """Check if a URL is from a supported TeraBox domain."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return domain in config.SUPPORTED_DOMAINS
    except Exception:
        return False


def extract_surl(url: str) -> str | None:
    """
    Extract the surl parameter from a TeraBox share URL.
    Handles various URL formats:
      - https://terabox.com/s/1XXXXX  →  surl = 1XXXXX
      - https://terabox.com/sharing/link?surl=XXXXX  →  surl = XXXXX
    """
    try:
        parsed = urlparse(url)

        # Format: /s/XXXXX
        path_match = re.search(r'/s/([a-zA-Z0-9_-]+)', parsed.path)
        if path_match:
            return path_match.group(1)

        # Format: ?surl=XXXXX
        qs = parse_qs(parsed.query)
        if "surl" in qs:
            return qs["surl"][0]

        return None
    except Exception:
        return None


# ─── API Calls ───────────────────────────────────────────────────────────────


class TeraBoxAPI:
    """Interface to the TeraBox share/list API."""

    def __init__(self, ndus: str):
        self.ndus = ndus
        self.host = config.get_host()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.host}/",
        })
        self.session.cookies.set("ndus", ndus, domain=".terabox.com")

    def _get_headers(self) -> dict:
        """Get common request headers."""
        return {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.host}/",
            "Cookie": f"ndus={self.ndus}",
        }

    def resolve_share_url(self, share_url: str) -> dict:
        """
        Resolve a TeraBox share URL.
        Follows redirects, extracts surl, jsToken, logid, bdstoken from the page.

        Returns dict:
          {
            "surl": "...",
            "js_token": "...",
            "logid": "...",
            "bdstoken": "...",
            "final_url": "..."
          }
        """
        logger.info(f"Resolving share URL: {share_url}")

        # Follow redirects to get final URL
        resp = self.session.get(
            share_url,
            headers=self._get_headers(),
            allow_redirects=True,
            timeout=30,
        )

        final_url = resp.url
        text = resp.text

        # Update host if redirected
        parsed = urlparse(final_url)
        new_host = f"{parsed.scheme}://{parsed.netloc}"
        if new_host != self.host:
            self.host = new_host
            config.save_host(new_host)
            self.session.headers["Referer"] = f"{self.host}/"

        # Extract surl from URL
        surl = extract_surl(final_url)
        if not surl:
            surl = extract_surl(share_url)
        if not surl:
            raise ValueError(f"Could not extract surl from URL: {share_url}")

        # Extract jsToken
        js_token = ""
        match = re.search(r'fn%28%22(.+?)%22%29', text)
        if not match:
            match = re.search(r'jsToken\s*[=:]\s*["\']([^"\']+)["\']', text)
        if match:
            js_token = match.group(1)

        # Extract logid (dp-logid)
        logid = ""
        match = re.search(r'dp-logid=([^&"]+)', text)
        if match:
            logid = match.group(1)

        # Extract bdstoken
        bdstoken = ""
        match = re.search(r'bdstoken\s*["\']?\s*[:=]\s*["\']([^"\']+)["\']', text)
        if match:
            bdstoken = match.group(1)

        logger.info(f"Resolved: surl={surl}, jsToken={'yes' if js_token else 'no'}, "
                     f"logid={'yes' if logid else 'no'}")

        return {
            "surl": surl,
            "js_token": js_token,
            "logid": logid,
            "bdstoken": bdstoken,
            "final_url": final_url,
        }

    def get_file_list(self, surl: str, js_token: str = "", logid: str = "",
                      bdstoken: str = "", page: int = 1) -> list[dict]:
        """
        Call /share/list to get files in the shared link.

        Returns list of dicts:
          [{
            "filename": "video.mp4",
            "size": 123456789,
            "size_str": "117.7 MB",
            "dlink": "https://...",
            "thumbnail": "https://...",
            "is_dir": False,
            "fs_id": "...",
            "path": "/video.mp4"
          }, ...]
        """
        url = f"{self.host}/share/list"
        params = {
            "app_id": config.APP_ID,
            "web": config.WEB,
            "channel": config.CHANNEL,
            "clienttype": config.CLIENTTYPE,
            "jsToken": js_token,
            "dp-logid": logid,
            "page": str(page),
            "num": "20",
            "by": "name",
            "order": "asc",
            "shorturl": surl,
            "root": "1",
        }

        logger.info(f"Fetching file list for surl={surl}, page={page}...")
        resp = self.session.get(url, params=params, headers=self._get_headers(), timeout=30)
        data = resp.json()

        errno = data.get("errno", -1)
        if errno != 0:
            error_msg = data.get("errmsg", data.get("msg", f"errno={errno}"))
            raise RuntimeError(f"share/list failed: {error_msg} (errno={errno})")

        file_list = data.get("list", [])
        results = []

        for item in file_list:
            is_dir = item.get("isdir", 0) == 1
            size = item.get("size", 0)
            results.append({
                "filename": item.get("server_filename", "unknown"),
                "size": size,
                "size_str": _format_size(size),
                "dlink": item.get("dlink", ""),
                "thumbnail": item.get("thumbs", {}).get("url3", ""),
                "is_dir": is_dir,
                "fs_id": str(item.get("fs_id", "")),
                "path": item.get("path", ""),
            })

        logger.info(f"Found {len(results)} file(s).")
        return results

    def get_download_link(self, share_url: str) -> list[dict]:
        """
        High-level: resolve a share URL and return download info for all files.
        Combines resolve_share_url + get_file_list.
        """
        info = self.resolve_share_url(share_url)
        files = self.get_file_list(
            surl=info["surl"],
            js_token=info["js_token"],
            logid=info["logid"],
            bdstoken=info["bdstoken"],
        )
        return files

    def get_streaming_url(self, dlink: str) -> str:
        """
        Follow the dlink redirect to get the actual streaming/download URL.
        The dlink often redirects to a CDN URL (d.pcs.baidu.com or similar).
        """
        try:
            resp = self.session.head(
                dlink,
                headers=self._get_headers(),
                allow_redirects=True,
                timeout=30,
            )
            return resp.url
        except Exception as e:
            logger.warning(f"Could not resolve streaming URL: {e}")
            return dlink


# ─── Utilities ───────────────────────────────────────────────────────────────


def _format_size(size_bytes: int) -> str:
    """Format byte count to human-readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"
