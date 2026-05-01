"""Forensic PDF Report Generator & Media Consolidator for RSMF archives.

Pipeline: ZIP Access -> Manifest Extraction -> Attachment Merging -> HTML Rendering -> PDF Generation

Standalone usage:
    python -m rsmf_reconstruct.export_pdf \\
        --source_a path/to/a.rsmf \\
        --source_b path/to/b.rsmf \\
        --template generic_template.html \\
        --output ./report_output
"""

import argparse
import io
import os
import subprocess
import sys
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from rsmf_reconstruct.hash_join import build_participants_map, reconcile_conversations
from rsmf_reconstruct.rsmf_parser import parse_rsmf, parse_rsmf_manifest


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nfc(text: str | None) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFC", str(text))


def _format_timestamp(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts


def _resolve_sender(msg: dict, participants_map: dict) -> str:
    # Priority 1: custom field "From"
    for field in msg.get("custom", []):
        if field.get("name") == "From":
            return _nfc(field.get("value", ""))
    # Priority 2: participants_map lookup
    pid = msg.get("participant", "")
    display = participants_map.get(pid, "")
    return _nfc(display or pid)


def _resolve_deleted_label(msg: dict) -> str:
    for field in msg.get("custom", []):
        if field.get("name") == "Deleted by":
            name = _nfc(field.get("value", "")) or "Unknown"
            return f"Deleted by {name}"
    return "Deleted by Unknown"


def _attachment_filename(att: dict) -> str:
    for key in ("file_id", "fileId", "name", "filename", "id"):
        val = att.get(key)
        if val:
            return str(val)
    return ""


# ---------------------------------------------------------------------------
# Attachment consolidation
# ---------------------------------------------------------------------------

def _consolidate_attachments(
    zip_bytes_a: bytes | None,
    zip_bytes_b: bytes | None,
    att_out: Path,
) -> set[str]:
    """Extract all attachments/ entries from both ZIPs into att_out.

    Files from source_b fill gaps not present in source_a, achieving
    cross-source recovery for deleted-message attachments.
    Returns the set of filenames successfully written to att_out.
    """
    att_out.mkdir(parents=True, exist_ok=True)
    present: set[str] = set()

    for zip_bytes in (zip_bytes_a, zip_bytes_b):
        if not zip_bytes:
            continue
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for info in zf.infolist():
                if not info.filename.startswith("attachments/"):
                    continue
                name = Path(info.filename).name
                if not name:
                    continue
                dest = att_out / name
                if not dest.exists():
                    dest.write_bytes(zf.read(info.filename))
                present.add(name)

    return present


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------

def _map_attachment(att: dict, present_files: set[str]) -> dict | None:
    filename = _attachment_filename(att)
    if not filename:
        return None
    ext = Path(filename).suffix.lower()
    display_name = _nfc(att.get("name") or att.get("filename") or filename)
    return {
        "url": f"attachments/{filename}",
        "display_name": display_name,
        "is_image": ext in _IMAGE_EXTENSIONS,
        "available": filename in present_files,
    }


def _map_message(msg: dict, participants_map: dict, present_files: set[str]) -> dict:
    edits = []
    for edit in msg.get("edits", []):
        pid = edit.get("participant", "")
        author = _nfc(participants_map.get(pid, "") or pid)
        edits.append({"body": _nfc(edit.get("body", "")), "author": author})

    reactions = []
    for rxn in msg.get("reactions", []):
        pid = rxn.get("participant", "")
        participant = _nfc(participants_map.get(pid, "") or pid)
        reactions.append({"value": _nfc(rxn.get("value", "")), "participant": participant})

    attachments = [
        mapped
        for att in msg.get("attachments", [])
        if (mapped := _map_attachment(att, present_files)) is not None
    ]

    deleted = bool(msg.get("deleted"))
    return {
        "id": _nfc(str(msg.get("id", ""))),
        "sender_display": _resolve_sender(msg, participants_map),
        "timestamp": _format_timestamp(msg.get("timestamp")),
        "body": _nfc(msg.get("body") or ""),
        "deleted": deleted,
        "deleted_label": _resolve_deleted_label(msg) if deleted else "",
        "importance": _nfc(msg.get("importance", "")),
        "edits": edits,
        "reactions": reactions,
        "attachments": attachments,
    }


# ---------------------------------------------------------------------------
# Report metadata extraction
# ---------------------------------------------------------------------------

def _resolve_meta(manifest_a: dict, manifest_b: dict) -> tuple[str, str]:
    """Return (source_platform, chat_name) from manifest metadata."""
    conversations = manifest_a.get("conversations") or manifest_b.get("conversations") or [{}]
    conv = conversations[0] if conversations else {}

    source_platform = _nfc(
        manifest_a.get("platform")
        or manifest_a.get("source")
        or conv.get("platform")
        or conv.get("source")
        or "RSMF"
    )
    chat_name = _nfc(
        conv.get("name")
        or conv.get("title")
        or conv.get("display")
        or "Reconstructed Conversation"
    )
    return source_platform, chat_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_pdf_report(
    parsed_a: dict,
    parsed_b: dict,
    manifest_a: dict,
    manifest_b: dict,
    participants_map: dict,
    timeline: list,
    template_path: str,
    output_dir: str,
) -> Path:
    """Render the reconciled timeline into a forensic PDF report using Playwright.

    Accepts pre-parsed data so it can be called directly from main.py
    without re-parsing the RSMF archives a second time.

    Returns the path to the generated ``report.pdf``.
    """
    output = Path(output_dir).resolve()
    att_out = output / "attachments"

    # Attachment consolidation & cross-source recovery
    present_files = _consolidate_attachments(
        parsed_a["zip_bytes"],
        parsed_b["zip_bytes"],
        att_out,
    )

    messages = [_map_message(msg, participants_map, present_files) for msg in timeline]
    source_platform, chat_name = _resolve_meta(manifest_a, manifest_b)

    context = {
        "source_platform": source_platform,
        "chat_name": chat_name,
        "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_messages": len(messages),
        "messages": messages,
    }

    # Setup Jinja2 environment
    template_file = Path(template_path).resolve()
    env = Environment(
        loader=FileSystemLoader(str(template_file.parent)),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )
    html_content = env.get_template(template_file.name).render(**context)

    output.mkdir(parents=True, exist_ok=True)
    
    # Save temporary HTML to the output directory
    # This allows Playwright to resolve relative file links correctly
    html_out = output / "report.html"
    html_out.write_text(html_content, encoding="utf-8")

    pdf_out = output / "report.pdf"

    # Use Playwright for PDF generation
    with sync_playwright() as p:
        try:
            # Launch browser
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            if "playwright install" in str(e).lower():
                print("Playwright browsers not found. Attempting to install Chromium...")
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
                browser = p.chromium.launch(headless=True)
            else:
                raise e

        context_pw = browser.new_context()
        page = context_pw.new_page()

        # Navigation to the actual file on disk is better for resolving local assets/links
        page.goto(html_out.as_uri(), wait_until="networkidle")
        
        # Generate the PDF with links enabled (default behavior in Playwright)
        page.pdf(
            path=str(pdf_out),
            format="A4",
            print_background=True,
            display_header_footer=False,
            margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"}
        )

        browser.close()

    return pdf_out
