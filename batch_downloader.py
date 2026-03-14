"""
Batch TeraBox Downloader — reads links from Excel, downloads all media files,
and writes status (Success/Failed) back into the Excel file.

Excel format:
  Column A: TeraBox Link
  Column B: Status (Success / Failed: reason)
  Column C: Filename
  Column D: Size (MB)
  Column E: Date (download timestamp)

Usage:
  python batch_downloader.py                          # default: links.xlsx
  python batch_downloader.py --input mylinks.xlsx     # custom Excel file
  python batch_downloader.py --input links.xlsx --output-dir D:\\Videos
"""

import argparse
import os
import sys
import time
import logging
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

# ── Import streamer_v2 functions ─────────────────────────────────────────────
# Add project root to path so we can import sibling modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from streamer_v2 import get_download_link, download_file
from config import SUPPORTED_DOMAINS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Styling ──────────────────────────────────────────────────────────────────
HEADER_FONT = Font(bold=True, size=12, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
SUCCESS_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FAIL_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
SKIP_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
HEADERS = ["Link", "Status", "Filename", "Size (MB)", "Date"]


def is_terabox_url(url: str) -> bool:
    """Check if a URL belongs to a known TeraBox domain."""
    if not url or not url.startswith("http"):
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url.strip()).hostname
        return host in SUPPORTED_DOMAINS if host else False
    except Exception:
        return False


def create_sample_excel(filepath: str):
    """Create a sample Excel file with headers and example link."""
    wb = Workbook()
    ws = wb.active
    ws.title = "TeraBox Links"

    # Write headers
    for col, header in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    # Column widths
    ws.column_dimensions["A"].width = 65
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 22

    # Example row
    ws.cell(row=2, column=1, value="https://terasharefile.com/s/EXAMPLE_LINK_HERE")

    wb.save(filepath)
    logger.info(f"Created sample Excel: {filepath}")
    logger.info("Add your TeraBox links in column A, then run again.")


def load_excel(filepath: str):
    """Load workbook and return (workbook, worksheet)."""
    wb = load_workbook(filepath)
    ws = wb.active

    # Ensure headers exist in row 1
    if ws.cell(row=1, column=1).value != "Link":
        # Insert headers if missing
        ws.insert_rows(1)
        for col, header in enumerate(HEADERS, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center")

    return wb, ws


def process_downloads(input_file: str, output_dir: str, max_retries: int = 3):
    """Main batch download loop: read Excel, download each link, write status."""

    if not os.path.exists(input_file):
        logger.warning(f"Excel file not found: {input_file}")
        create_sample_excel(input_file)
        return

    wb, ws = load_excel(input_file)
    total_rows = ws.max_row - 1  # exclude header
    if total_rows <= 0:
        logger.warning("No links found in Excel file.")
        wb.close()
        return

    # Ensure column widths are set
    ws.column_dimensions["A"].width = 65
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 22

    succeeded = 0
    failed = 0
    skipped = 0

    print(f"\n{'='*60}")
    print(f"  Batch TeraBox Downloader")
    print(f"  Input:  {input_file}")
    print(f"  Output: {output_dir}")
    print(f"  Links:  {total_rows}")
    print(f"{'='*60}\n")

    for row_idx in range(2, ws.max_row + 1):
        link = ws.cell(row=row_idx, column=1).value
        status = ws.cell(row=row_idx, column=2).value

        # Skip empty rows
        if not link or not str(link).strip():
            continue

        link = str(link).strip()

        # Skip already successful downloads
        if status and str(status).strip().lower() == "success":
            logger.info(f"[{row_idx-1}/{total_rows}] Skipping (already downloaded): {link[:60]}...")
            for col in range(1, 6):
                ws.cell(row=row_idx, column=col).fill = SKIP_FILL
            skipped += 1
            continue

        # Validate URL
        if not is_terabox_url(link):
            logger.warning(f"[{row_idx-1}/{total_rows}] Invalid URL: {link[:60]}...")
            ws.cell(row=row_idx, column=2).value = "Failed: Invalid TeraBox URL"
            for col in range(1, 6):
                ws.cell(row=row_idx, column=col).fill = FAIL_FILL
            failed += 1
            wb.save(input_file)
            continue

        # ── Download ─────────────────────────────────────────────────
        print(f"\n--- [{row_idx-1}/{total_rows}] {link} ---")

        try:
            # Step 1: Get download URL via streamer
            logger.info("Getting download link via teraboxstreamer.com...")
            result = get_download_link(link, max_retries=max_retries)

            if "download_url" not in result:
                error_msg = result.get("error", "No download URL returned")
                raise RuntimeError(error_msg)

            dl_url = result["download_url"]
            video_name = result.get("video_name", "")
            file_size_mb = result.get("file_size_mb", 0)

            # Step 2: Download the file
            logger.info("Downloading file...")
            filepath = download_file(dl_url, output_dir=output_dir)
            filename = os.path.basename(filepath)
            actual_size = os.path.getsize(filepath) / (1024 * 1024)

            # Step 3: Write success to Excel
            ws.cell(row=row_idx, column=2).value = "Success"
            ws.cell(row=row_idx, column=3).value = filename
            ws.cell(row=row_idx, column=4).value = round(actual_size, 2)
            ws.cell(row=row_idx, column=5).value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for col in range(1, 6):
                ws.cell(row=row_idx, column=col).fill = SUCCESS_FILL
            succeeded += 1
            logger.info(f"SUCCESS: {filename} ({actual_size:.1f} MB)")

        except Exception as e:
            error_msg = str(e)[:100]
            logger.error(f"FAILED: {error_msg}")
            ws.cell(row=row_idx, column=2).value = f"Failed: {error_msg}"
            ws.cell(row=row_idx, column=5).value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for col in range(1, 6):
                ws.cell(row=row_idx, column=col).fill = FAIL_FILL
            failed += 1

        # Save after each row (so progress is preserved if script is interrupted)
        wb.save(input_file)
        logger.info(f"Excel saved. Progress: {succeeded} ok / {failed} fail / {skipped} skip")

        # Brief pause between downloads to avoid rate limiting
        if row_idx < ws.max_row:
            time.sleep(2)

    wb.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  BATCH DOWNLOAD COMPLETE")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed:    {failed}")
    print(f"  Skipped:   {skipped}")
    print(f"  Total:     {total_rows}")
    print(f"  Excel:     {os.path.abspath(input_file)}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Batch download TeraBox files from an Excel spreadsheet."
    )
    parser.add_argument(
        "--input", "-i",
        default="links.xlsx",
        help="Path to the Excel file with TeraBox links (default: links.xlsx)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"),
        help="Directory to save downloaded files (default: ./downloads/)",
    )
    parser.add_argument(
        "--retries", "-r",
        type=int,
        default=3,
        help="Max retries per link (default: 3)",
    )

    args = parser.parse_args()
    process_downloads(args.input, args.output_dir, max_retries=args.retries)


if __name__ == "__main__":
    main()
