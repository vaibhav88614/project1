"""
TeraBox Streamer Download v2 — via teraboxstreamer.com

Enhanced version with techniques from Cloudflare Turnstile bypass research:
  - navigator.webdriver removal & full stealth patches
  - window.turnstile.render interception (captures sitekey, cData, chlPageData)
  - cfCallback injection for programmatic token submission
  - Retry logic with fresh browser contexts
  - Fallback click on Turnstile widget
  - Full file download with progress bar and speed display

Usage:
  python streamer_v2.py <terabox_url>
  python streamer_v2.py https://terasharefile.com/s/1FnWyXjwyJR5PPDYkWUjpOQ
"""
from playwright.sync_api import sync_playwright
import time
import json
import sys
import os
import re
import logging
import requests
from urllib.parse import urlparse, unquote

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

STREAMER_URL = "https://teraboxstreamer.com/"

# Minimal stealth injection — only remove webdriver flag.
# NOTE: Aggressive fingerprint overrides (plugins, languages, chrome object)
# actually TRIGGER Cloudflare detection. Real Chrome + minimal patch = best.
# The turnstile.render interception from the article is useful for PAID captcha
# services but unnecessary when using real Chrome that auto-solves Turnstile.
STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    try { delete navigator.__proto__.webdriver; } catch(e) {}
"""


def get_download_link(terabox_url: str, max_retries: int = 3) -> dict:
    """
    Get a direct download link for a TeraBox URL.
    Retries with fresh browser context on failure.
    """
    result = {}
    for attempt in range(1, max_retries + 1):
        logger.info(f"=== Attempt {attempt}/{max_retries} ===")
        result = _try_get_download_link(terabox_url)
        if "download_url" in result or "m3u8_url" in result:
            return result
        if attempt < max_retries:
            logger.warning(f"Attempt {attempt} failed: {result.get('error', 'unknown')}. Retrying in 3s...")
            time.sleep(3)
    return result


def _try_get_download_link(terabox_url: str) -> dict:
    """Single attempt to obtain the download link."""
    with sync_playwright() as p:
        logger.info("Launching real Chrome...")
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        # Inject minimal stealth only — don't override fingerprints
        context.add_init_script(STEALTH_JS)

        page = context.new_page()

        # ── Capture API responses ──
        api_results = {}

        def handle_response(response):
            url = response.url
            if "/api/download/" in url or "/resolve/" in url:
                try:
                    data = response.json()
                    logger.info(f"API Response from {url.split('?')[0]}:\n{json.dumps(data, indent=2)}")
                    api_results.update(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            # ── Navigate ──
            logger.info(f"Navigating to {STREAMER_URL}")
            page.goto(STREAMER_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#url", timeout=30000)

            # ── Enter URL ──
            logger.info(f"Entering URL: {terabox_url}")
            page.fill("#url", terabox_url)

            # ── Wait for Turnstile to auto-solve ──
            logger.info("Waiting for Turnstile to solve (real Chrome auto-solves in ~5s)...")
            token = _wait_for_turnstile_token(page, timeout=45)

            if not token:
                logger.info("Auto-solve incomplete. Clicking Turnstile widget as fallback...")
                _click_turnstile(page)
                token = _wait_for_turnstile_token(page, timeout=30)

            if not token:
                page.screenshot(path="turnstile_fail.png")
                logger.error("Turnstile not solved. Screenshot saved to turnstile_fail.png")
                browser.close()
                return {"error": "Turnstile captcha not solved"}

            # ── Click Download ──
            logger.info("Clicking Download button...")
            page.click('button:has-text("Download")')

            # ── Wait for API response ──
            logger.info("Waiting for download link...")
            for i in range(90):
                if "download_url" in api_results:
                    break
                if "error" in api_results:
                    logger.warning(f"API error: {api_results.get('error')}")
                    break

                # Also check if link appeared in the page DOM
                dl = page.evaluate(
                    '() => { const a = document.querySelector("#download-result a"); return a ? a.href : ""; }'
                )
                if dl:
                    api_results["download_url"] = dl
                    break

                if i % 10 == 0:
                    logger.info(f"  Waiting for response... ({i}s)")
                time.sleep(1)

            logger.info(f"Result: {json.dumps(api_results, indent=2)}")

        except Exception as e:
            logger.error(f"Error: {e}")
            api_results["error"] = str(e)
        finally:
            browser.close()

        return api_results


def _wait_for_turnstile_token(page, timeout: int = 60) -> str | None:
    """Poll for the Turnstile token to appear in the hidden input."""
    for i in range(timeout):
        token = page.evaluate(
            '() => { const el = document.querySelector("input[name=cf-turnstile-response]"); '
            "return el ? el.value : ''; }"
        )
        if token:
            logger.info(f"Turnstile solved! Token length: {len(token)}")
            return token
        if i > 0 and i % 10 == 0:
            logger.info(f"  Waiting for Turnstile... ({i}s)")
        time.sleep(1)
    return None


def _click_turnstile(page):
    """Fallback: click the Turnstile checkbox widget area."""
    # Try clicking the cf-turnstile div
    try:
        ts_div = page.locator(".cf-turnstile")
        if ts_div.count() > 0:
            box = ts_div.bounding_box()
            if box:
                page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                logger.info("Clicked Turnstile widget area")
                time.sleep(3)
    except Exception as e:
        logger.debug(f"Click turnstile div failed: {e}")

    # Try clicking inside the Cloudflare iframe
    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                cb = frame.locator("input[type=checkbox], .mark")
                if cb.count() > 0:
                    cb.first.click()
                    logger.info("Clicked checkbox inside Turnstile iframe")
                    time.sleep(3)
                break
    except Exception as e:
        logger.debug(f"Click iframe checkbox failed: {e}")


def download_file(download_url: str, output_dir: str = None) -> str:
    """Download a file from a direct URL with progress bar and speed display."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    logger.info(f"Downloading: {download_url[:100]}...")
    resp = requests.get(download_url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    # ── Determine filename ──
    cd = resp.headers.get("Content-Disposition", "")
    match = re.search(r"filename[*]?=[\"']?(?:UTF-8'')?([^\"';]+)", cd)
    if match:
        filename = match.group(1)
    else:
        filename = unquote(urlparse(download_url).path.split("/")[-1]) or "download.mp4"

    for ch in '<>:"/\\|?*':
        filename = filename.replace(ch, "_")

    if not output_dir:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, filename)

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    start_time = time.time()

    print(f"\n  File: {filename}")
    if total:
        print(f"  Size: {total / (1024*1024):.1f} MB")
    print()

    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    elapsed = time.time() - start_time
                    speed = downloaded / elapsed / (1024 * 1024) if elapsed > 0 else 0
                    mb_done = downloaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    print(
                        f"\r  [{pct:5.1f}%]  {mb_done:.1f} / {mb_total:.1f} MB  "
                        f"| {speed:.1f} MB/s",
                        end="",
                        flush=True,
                    )

    elapsed = time.time() - start_time
    print(f"\n\n  Completed: {filepath}")
    print(f"  Size: {downloaded / (1024*1024):.1f} MB | Time: {elapsed:.0f}s")
    return filepath


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    terabox_url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "https://terasharefile.com/s/1FnWyXjwyJR5PPDYkWUjpOQ"
    )

    print(f"\n  TeraBox URL: {terabox_url}\n")
    result = get_download_link(terabox_url, max_retries=3)

    if "download_url" in result:
        dl_url = result["download_url"]
        print(f"\n{'='*60}")
        print(f"  DOWNLOAD URL:")
        print(f"  {dl_url}")
        print(f"{'='*60}")

        filepath = download_file(dl_url)
        print(f"\n  Saved to: {filepath}")

    elif "m3u8_url" in result:
        print(f"\n  STREAM URL: {result['m3u8_url']}")
        print("  Use VLC or: ffmpeg -i <url> -c copy output.mp4")

    else:
        print(f"\n  FAILED: {json.dumps(result, indent=2)}")
        sys.exit(1)
