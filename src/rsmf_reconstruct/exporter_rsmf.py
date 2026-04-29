"""RSMF 2.0.0 exporter.

Packages reconciled chat data into a Relativity-compliant .rsmf MIME container,
preserving original metadata and all attachments (including deleted-message files).

Primary entry point:
    export_to_rsmf(reconciled_timeline, original_manifest, original_attachments_dir, output_path)
"""

import base64
import hashlib
import io
import json
import logging
import unicodedata
import zipfile
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rfc2047_b64(text: str) -> str:
    """Return an RFC 2047 Base64 encoded word for a UTF-8 display name."""
    normalized = unicodedata.normalize('NFC', text)
    encoded = base64.b64encode(normalized.encode('utf-8')).decode('ascii')
    return f"=?UTF-8?B?{encoded}?="


def _iso_to_display(ts: str) -> str:
    """Convert an ISO 8601 timestamp to the transcript display format YYYY-MM-DD HH:MM:SS."""
    ts = ts.rstrip('Z')
    if '.' in ts:
        ts = ts.split('.')[0]
    return ts.replace('T', ' ')


def _resolve_display_name(event: dict, participants_map: dict) -> str:
    """Return the best display name for an event's sender.

    Priority: custom "From" field → participants_map → raw participant id.
    """
    for custom in event.get("custom", []):
        if custom.get("name") == "From":
            name = custom.get("value", "")
            if name:
                return unicodedata.normalize('NFC', name)
    pid = event.get("participant", "")
    raw = participants_map.get(pid, pid)
    return unicodedata.normalize('NFC', raw)


def _build_transcript(events: list, participants_map: dict) -> str:
    """Build the plain-text transcript (Display Name / timestamp / body blocks).

    Each message block:
        Display Name
        YYYY-MM-DD HH:MM:SS
        Message Body
        <blank line>
    """
    blocks = []
    for event in events:
        if event.get("type") != "message":
            continue
        display = _resolve_display_name(event, participants_map)
        ts = _iso_to_display(event.get("timestamp", ""))
        body = unicodedata.normalize('NFC', event.get("body") or "")
        blocks.append(f"{display}\n{ts}\n{body}\n")
    return "\n".join(blocks)


def _collect_attachment_ids(events: list) -> set:
    """Return the set of attachment file IDs referenced across all events."""
    return {
        att["id"]
        for event in events
        for att in event.get("attachments", [])
        if att.get("id")
    }


def _resolve_dirs(attachments_dir) -> list:
    """Normalise attachments_dir to a list of valid Path objects."""
    if attachments_dir is None:
        return []
    if isinstance(attachments_dir, (str, Path)):
        candidates = [Path(attachments_dir)]
    else:
        candidates = [Path(d) for d in attachments_dir if d is not None]
    return [d for d in candidates if d.is_dir()]


def _build_zip(manifest: dict, attachments_dir) -> bytes:
    """Build the rsmf.zip archive bytes.

    Contents:
        rsmf_manifest.json   — the export manifest (original metadata + reconciled events)
        attachments/<id>     — one file per referenced attachment ID, searched across all
                               directories in *attachments_dir* (single path or list of paths)
    """
    dirs = _resolve_dirs(attachments_dir)
    att_ids = _collect_attachment_ids(manifest.get("events", []))

    if att_ids and not dirs:
        logger.warning(
            "No valid attachments directory provided; %d attachment(s) will be missing.",
            len(att_ids),
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        zf.writestr('rsmf_manifest.json', manifest_bytes)

        for att_id in sorted(att_ids):
            src = next((d / att_id for d in dirs if (d / att_id).is_file()), None)
            if src:
                zf.write(src, f'attachments/{att_id}')
                logger.debug("Packed attachment: %s", att_id)
            else:
                logger.warning("Attachment not found in any source directory, skipping: %s", att_id)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_to_rsmf(
    reconciled_timeline: dict,
    original_manifest: dict,
    original_attachments_dir,
    output_path,
) -> None:
    """Export reconciled chat data as a Relativity-compliant .rsmf container.

    The container is a MIME multipart/mixed message with:
      - Part 1: plain-text transcript (base64-encoded)
      - Part 2: rsmf.zip archive (base64-encoded) containing the manifest JSON
                and the attachments/ folder

    Metadata (version, participants, conversations) is taken verbatim from
    *original_manifest*; events come from *reconciled_timeline*.

    Args:
        reconciled_timeline: Output of reconcile_conversations() — dict with
            keys ``version``, ``participants``, ``conversations``, ``events``.
        original_manifest: Manifest dict parsed from one source .rsmf file.
            Supplies the authoritative ``version``, ``participants``, and
            ``conversations`` blocks written into the export.
        original_attachments_dir: Path (or list of paths) to the ``attachments/``
            directories extracted from the source .rsmf files.  Each referenced
            attachment ID is searched across all provided directories in order.
            Missing files are logged and skipped.
        output_path: Filesystem path for the generated ``.rsmf`` file.
            Parent directories are created if they do not exist.
    """
    participants = original_manifest.get("participants") or reconciled_timeline.get("participants", [])
    participants_map = {p["id"]: p.get("display", "") for p in participants}

    export_manifest = {
        "version": original_manifest.get("version", "2.0.0"),
        "participants": original_manifest.get("participants", []),
        "conversations": original_manifest.get("conversations", []),
        "events": reconciled_timeline.get("events", []),
    }

    transcript_bytes = _build_transcript(
        reconciled_timeline.get("events", []), participants_map
    ).encode('utf-8')

    zip_bytes = _build_zip(export_manifest, original_attachments_dir)

    # Stable unique boundary derived from the ZIP content digest
    boundary = "===============" + hashlib.md5(zip_bytes).hexdigest()[:16] + "=="

    # --- Root multipart envelope ---
    # charset="utf-8" is part of the Content-Type per the RSMF spec
    msg = MIMEMultipart('mixed', boundary=boundary, charset='utf-8')

    # Headers in spec order (MIME-Version and Content-Type are set by MIMEBase.__init__)
    msg['Content-Transfer-Encoding'] = 'base64'
    msg['X-RSMF-Generator'] = 'ISA Chat Flow RSMF Generator'

    # From = participants[0] as archive owner
    if participants:
        p0 = unicodedata.normalize('NFC', participants[0].get("display", ""))
        msg['From'] = f"{_rfc2047_b64(p0)} <owner@example.com>"

    msg['X-RSMF-Version'] = '2.0.0'

    # To = participants[1:] (user0, user1, …) then participants[0] appended last
    if len(participants) >= 2:
        to_parts = participants[1:] + [participants[0]]
        to_entries = []
        for i, p in enumerate(to_parts):
            name = unicodedata.normalize('NFC', p.get("display", ""))
            to_entries.append(f"{_rfc2047_b64(name)} <user{i}@example.com>")
        msg['To'] = ", ".join(to_entries)

    # --- Part 1: plain-text transcript ---
    text_part = MIMEBase('text', 'plain', charset='utf-8')
    text_part['Content-Transfer-Encoding'] = 'base64'
    # Replicate the original generator's Content-Transfer_Encoding quirk (underscore)
    text_part['Content-Transfer_Encoding'] = 'quoted-printable'
    text_part.set_payload(base64.encodebytes(transcript_bytes).decode('ascii'))
    msg.attach(text_part)

    # --- Part 2: rsmf.zip binary archive ---
    zip_part = MIMEBase('application', 'octet-stream')
    zip_part['Content-Transfer-Encoding'] = 'base64'
    zip_part['Content-Disposition'] = 'attachment; filename="rsmf.zip"'
    zip_part.set_payload(base64.encodebytes(zip_bytes).decode('ascii'))
    msg.attach(zip_part)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(msg.as_bytes())
    logger.info("RSMF export written: %s (%d bytes)", out, out.stat().st_size)
