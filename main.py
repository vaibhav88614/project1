"""
TeraBox Video Downloader — Main Entry Point.

Modes:
  - Web:   python main.py                 → Launch Flask web UI on localhost:5000
  - CLI:   python main.py --url <URL>     → Download a single TeraBox link
  - Batch: python main.py --file urls.txt → Download all links from a text file

First-time setup:
  Create config.json with your TeraBox credentials:
  {"email": "you@example.com", "password": "yourpassword"}
"""

import argparse
import json
import logging
import os
import sys
import uuid

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

import auth
import config
import downloader
from terabox_api import TeraBoxAPI, is_valid_terabox_url

# ─── Logging Setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ─── Flask App ───────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = os.urandom(24)

# In-memory cache: token → file info (for download proxy)
_download_tokens: dict[str, dict] = {}


@app.route("/")
def index():
    """Serve the main web UI."""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Check if the app is ready (has valid session)."""
    try:
        ndus = config.get_cached_session()
        if ndus and auth.validate_session(ndus):
            return jsonify({"status": "ready", "logged_in": True})
        return jsonify({"status": "no_session", "logged_in": False})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/login", methods=["POST"])
def api_login():
    """Trigger login with credentials from config.json or request body."""
    try:
        data = request.get_json(silent=True) or {}
        email = data.get("email", "")
        password = data.get("password", "")

        if email and password:
            # Save if provided in request
            cfg = config.load_config()
            cfg["email"] = email
            cfg["password"] = password
            config.save_config(cfg)

        ndus = auth.ensure_session()
        return jsonify({"status": "ok", "message": "Login successful"})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    """
    Resolve a TeraBox share URL and return file info.
    Request: {"url": "https://terabox.com/s/..."}
    Response: {"files": [{filename, size, size_str, thumbnail, token}, ...]}
    """
    try:
        data = request.get_json()
        if not data or "url" not in data:
            return jsonify({"error": "Missing 'url' in request body"}), 400

        share_url = data["url"].strip()

        if not is_valid_terabox_url(share_url):
            return jsonify({"error": "Invalid TeraBox URL. Please provide a valid TeraBox share link."}), 400

        # Ensure session
        ndus = auth.ensure_session()
        api = TeraBoxAPI(ndus)

        # Resolve and get files
        files = api.get_download_link(share_url)

        # Generate download tokens
        result_files = []
        for f in files:
            token = str(uuid.uuid4())
            _download_tokens[token] = {
                "dlink": f["dlink"],
                "filename": f["filename"],
                "ndus": ndus,
            }
            result_files.append({
                "filename": f["filename"],
                "size": f["size"],
                "size_str": f["size_str"],
                "thumbnail": f["thumbnail"],
                "is_dir": f["is_dir"],
                "token": token,
            })

        return jsonify({"files": result_files})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.exception("Error resolving URL")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/api/download")
def api_download():
    """
    Proxy download through the server.
    The dlink requires cookies/headers that browsers can't send cross-origin.
    Query params: ?token=<uuid>
    """
    token = request.args.get("token", "")
    if not token or token not in _download_tokens:
        return jsonify({"error": "Invalid or expired download token"}), 400

    info = _download_tokens[token]
    dlink = info["dlink"]
    ndus = info["ndus"]
    expected_filename = info["filename"]

    try:
        stream = downloader.download_file_as_stream(dlink, ndus)

        # First yield is metadata
        meta = next(stream)
        content_type = meta.get("content_type", "application/octet-stream")
        content_length = meta.get("content_length", "0")
        filename = expected_filename or meta.get("filename", "download")

        # Sanitize filename for Content-Disposition
        safe_filename = filename.encode("ascii", "ignore").decode()
        if not safe_filename:
            safe_filename = "download"

        headers = {
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{safe_filename}"; filename*=UTF-8\'\'{requests_quote(filename)}',
            "Content-Length": content_length,
            "Cache-Control": "no-cache",
        }

        return Response(
            stream_with_context(stream),
            headers=headers,
            status=200,
        )

    except Exception as e:
        logger.exception("Download proxy error")
        return jsonify({"error": f"Download failed: {str(e)}"}), 500


def requests_quote(s: str) -> str:
    """URL-encode a string for Content-Disposition filename*."""
    from urllib.parse import quote
    return quote(s, safe="")


# ─── CLI Mode ────────────────────────────────────────────────────────────────


def cli_download(url: str, output_dir: str = None):
    """Download a single TeraBox share URL from CLI."""
    print(f"\n{'='*60}")
    print(f"  TeraBox Video Downloader")
    print(f"{'='*60}\n")

    if not is_valid_terabox_url(url):
        print(f"[ERROR] Invalid TeraBox URL: {url}")
        print("  Supported domains: terabox.com, 1024tera.com, freeterabox.com, etc.")
        sys.exit(1)

    # Login
    print("[*] Authenticating...")
    try:
        ndus = auth.ensure_session()
        print("[+] Authentication successful.\n")
    except Exception as e:
        print(f"[ERROR] Authentication failed: {e}")
        sys.exit(1)

    # Resolve URL
    print(f"[*] Resolving: {url}")
    api = TeraBoxAPI(ndus)

    try:
        files = api.get_download_link(url)
    except Exception as e:
        print(f"[ERROR] Failed to resolve URL: {e}")
        sys.exit(1)

    if not files:
        print("[!] No files found in this share link.")
        sys.exit(0)

    # Display files
    print(f"\n[+] Found {len(files)} file(s):\n")
    for i, f in enumerate(files, 1):
        icon = "📁" if f["is_dir"] else "📄"
        print(f"  {i}. {icon} {f['filename']}  ({f['size_str']})")

    print()

    # Download each file
    for f in files:
        if f["is_dir"]:
            print(f"  [!] Skipping directory: {f['filename']}")
            continue
        if not f["dlink"]:
            print(f"  [!] No download link for: {f['filename']}")
            continue

        print(f"  [*] Downloading: {f['filename']} ({f['size_str']})")
        try:
            path = downloader.download_file(
                dlink=f["dlink"],
                filename=f["filename"],
                ndus=ndus,
                output_dir=output_dir,
            )
            print(f"  [+] Saved: {path}\n")
        except Exception as e:
            print(f"  [ERROR] Failed: {e}\n")

    print(f"{'='*60}")
    print("  Done!")
    print(f"{'='*60}\n")


def cli_batch_download(filepath: str, output_dir: str = None):
    """Download multiple URLs from a text file."""
    if not os.path.exists(filepath):
        print(f"[ERROR] File not found: {filepath}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not urls:
        print("[!] No URLs found in file.")
        sys.exit(0)

    print(f"[*] Found {len(urls)} URL(s) to download.\n")

    for i, url in enumerate(urls, 1):
        print(f"\n--- [{i}/{len(urls)}] ---")
        try:
            cli_download(url, output_dir)
        except SystemExit:
            pass  # Don't exit on individual failures in batch mode


def setup_wizard():
    """Interactive setup for first-time users."""
    print(f"\n{'='*60}")
    print("  TeraBox Downloader — First-Time Setup")
    print(f"{'='*60}\n")

    cfg = config.load_config()
    if cfg.get("email") and cfg.get("password"):
        print("[*] Credentials already configured.")
        resp = input("  Overwrite? (y/N): ").strip().lower()
        if resp != "y":
            return

    email = input("  TeraBox Email: ").strip()
    password = input("  TeraBox Password: ").strip()

    if not email or not password:
        print("[ERROR] Email and password are required.")
        return

    cfg["email"] = email
    cfg["password"] = password
    config.save_config(cfg)

    print("\n[+] Credentials saved to config.json")
    print("[*] Testing login...")

    try:
        ndus = auth.ensure_session()
        print("[+] Login successful! You're all set.\n")
    except Exception as e:
        print(f"[!] Login failed: {e}")
        print("  Credentials are saved — you can try again later.\n")


# ─── Entry Point ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="TeraBox Video Downloader — Download TeraBox videos and files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                        Start web UI (http://localhost:5000)
  python main.py --url <TERABOX_URL>    Download a single link
  python main.py --file urls.txt        Download all links from file
  python main.py --setup                Configure credentials
        """,
    )

    parser.add_argument("--url", "-u", help="TeraBox share URL to download")
    parser.add_argument("--file", "-f", help="Text file with TeraBox URLs (one per line)")
    parser.add_argument("--output", "-o", help="Output directory for downloads")
    parser.add_argument("--web", "-w", action="store_true", help="Launch web UI (default if no URL/file given)")
    parser.add_argument("--port", "-p", type=int, default=5000, help="Web UI port (default: 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="Web UI host (default: 127.0.0.1)")
    parser.add_argument("--setup", "-s", action="store_true", help="Run first-time setup wizard")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Setup wizard
    if args.setup:
        setup_wizard()
        return

    # CLI: single URL
    if args.url:
        cli_download(args.url, args.output)
        return

    # CLI: batch file
    if args.file:
        cli_batch_download(args.file, args.output)
        return

    # Default: Web UI
    # Check if credentials exist; if not, prompt
    cfg = config.load_config()
    if not cfg.get("email") or not cfg.get("password"):
        print("[!] No credentials found. Running setup wizard...")
        setup_wizard()

    print(f"\n{'='*60}")
    print(f"  TeraBox Video Downloader — Web UI")
    print(f"  http://{args.host}:{args.port}")
    print(f"{'='*60}\n")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
