"""
Download TeraBox files via teraboxstreamer.com using real Chrome.
Solves Cloudflare Turnstile by using the system's Chrome browser.
"""
from playwright.sync_api import sync_playwright
import time
import json
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

STREAMER_URL = "https://teraboxstreamer.com/"


def get_download_link(terabox_url: str) -> dict:
    """Get download link using real Chrome to bypass Turnstile."""
    with sync_playwright() as p:
        logger.info("Launching real Chrome (not headless)...")
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=['--disable-blink-features=AutomationControlled'],
        )
        
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
        )
        
        # Remove webdriver flag
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            delete navigator.__proto__.webdriver;
        """)
        
        page = context.new_page()
        
        # Capture API responses
        api_results = {}
        
        def handle_response(response):
            url = response.url
            if '/api/download/' in url or '/resolve/' in url:
                try:
                    data = response.json()
                    logger.info(f"API Response: {json.dumps(data, indent=2)}")
                    api_results.update(data)
                except Exception:
                    pass
        
        page.on("response", handle_response)
        
        logger.info(f"Navigating to {STREAMER_URL}")
        page.goto(STREAMER_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector('#url', timeout=30000)
        
        # Fill in the URL
        logger.info(f"Entering URL: {terabox_url}")
        page.fill('#url', terabox_url)
        
        # Wait for Turnstile to auto-solve
        logger.info("Waiting for Turnstile to solve...")
        token = None
        for i in range(90):
            token = page.evaluate(
                '() => { const i = document.querySelector("input[name=cf-turnstile-response]"); return i ? i.value : ""; }'
            )
            if token:
                logger.info(f"Turnstile solved! Token length: {len(token)}")
                break
            if i % 10 == 0:
                logger.info(f"  Waiting... ({i}s) - you may need to click the Turnstile checkbox in the browser")
            time.sleep(1)
        
        if not token:
            logger.error("Turnstile not solved after 90s")
            page.screenshot(path="turnstile_fail.png")
            browser.close()
            return {"error": "Turnstile not solved"}
        
        # Click Download
        logger.info("Clicking Download button...")
        page.click('button:has-text("Download")')
        
        # Wait for API response
        logger.info("Waiting for download link from API...")
        for i in range(90):
            if 'download_url' in api_results or 'error' in api_results:
                break
            # Also check if link appeared in the page
            dl = page.evaluate(
                '() => { const a = document.querySelector("#download-result a"); return a ? a.href : ""; }'
            )
            if dl:
                api_results['download_url'] = dl
                break
            if i % 10 == 0:
                logger.info(f"  Waiting for response... ({i}s)")
            time.sleep(1)
        
        logger.info(f"Final result: {json.dumps(api_results, indent=2)}")
        browser.close()
        return api_results


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://terasharefile.com/s/1FnWyXjwyJR5PPDYkWUjpOQ"
    
    result = get_download_link(url)
    
    if 'download_url' in result:
        dl_url = result['download_url']
        print(f"\n{'='*60}")
        print(f"DOWNLOAD URL:\n{dl_url}")
        print(f"{'='*60}\n")
        
        # Auto-download using the downloader module
        import requests
        import os
        import re
        from urllib.parse import urlparse, unquote
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        }
        
        print("Starting download...")
        resp = requests.get(dl_url, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()
        
        # Get filename
        cd = resp.headers.get('Content-Disposition', '')
        match = re.search(r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';]+)', cd)
        if match:
            filename = match.group(1)
        else:
            filename = unquote(urlparse(dl_url).path.split('/')[-1]) or 'download.mp4'
        
        for ch in '<>:"/\\|?*':
            filename = filename.replace(ch, '_')
        
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)
        
        total = int(resp.headers.get('Content-Length', 0))
        downloaded = 0
        
        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total / (1024 * 1024)
                        print(f"\r  Progress: {pct:.1f}% ({mb_done:.1f}MB / {mb_total:.1f}MB)", end='', flush=True)
        
        print(f"\n\nDownloaded: {filepath} ({downloaded / (1024*1024):.1f} MB)")
    
    elif 'm3u8_url' in result:
        print(f"\nSTREAM URL:\n{result['m3u8_url']}\n")
        print("Use VLC or: ffmpeg -i <url> -c copy output.mp4")
    else:
        print(f"\nFAILED: {json.dumps(result, indent=2)}")
        sys.exit(1)
