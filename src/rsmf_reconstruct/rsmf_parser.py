"""RSMF file parser.

Provides utilities for reading and unpacking RSMF (Rich Secure Message Format) files:

- :func:`parse_rsmf` — parse an RSMF file into its header, text body, and ZIP bytes.
- :func:`parse_rsmf_manifest` — extract and parse the manifest JSON from the ZIP.
- :func:`extract_rsmf_files` — unpack the ``attachments/`` folder from the ZIP to disk.
"""

import email
import email.header
from email import policy
import io
import json
from pathlib import Path
import tempfile
import zipfile


def _parse_rsmf(path: Path | str) -> dict:
    """Parse an RSMF file and extract its components.

    Reads the file as a MIME message and extracts the sender/recipient headers,
    the plain-text body, and the raw ZIP attachment bytes.

    Args:
        path: Path to the .rsmf file on disk.

    Returns:
        A dict with keys:
            - ``head``: dict with ``from`` and ``to`` decoded header strings.
            - ``text``: decoded text body of the first text part, or ``None``.
            - ``zip_bytes``: raw bytes of the first non-text attachment, or ``None``.
    """
    path = Path(path)
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)

    def decode_header_value(val):
        """Decode a potentially RFC-2047-encoded header value to a plain string."""
        if not val:
            return ""
        parts = email.header.decode_header(val)
        return ''.join(
            p.decode(enc or 'utf-8') if isinstance(p, bytes) else p
            for p, enc in parts
        )

    head = {
        'from': decode_header_value(msg.get('From', '')),
        'to': decode_header_value(msg.get('To', '')),
    }
    text = None
    zip_bytes = None

    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        if part.get_content_maintype() == 'text' and text is None:
            text = payload.decode('utf-8', errors='replace')
        elif zip_bytes is None:
            zip_bytes = payload

    return {
        'head': head,
        'text': text,
        'zip_bytes': zip_bytes
    }


def _parse_rsmf_manifest(zip_bytes: bytes) -> dict:
    """Read and parse the RSMF manifest from the ZIP attachment.

    Args:
        zip_bytes: Raw bytes of the ZIP archive embedded in an RSMF file.

    Returns:
        Parsed contents of ``rsmf_manifest.json`` as a dict.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_file:
        manifest = json.loads(zip_file.read('rsmf_manifest.json'))
    return manifest


def extract_rsmf_files(zip_bytes: bytes) -> Path:
    """Extract the ``attachments/`` folder from the RSMF ZIP to a temporary directory.

    Only entries whose path starts with ``attachments/`` are extracted;
    all other ZIP members are ignored.

    Args:
        zip_bytes: Raw bytes of the ZIP archive embedded in an RSMF file.

    Returns:
        Path to the extracted ``attachments/`` directory inside a temporary folder.
        The caller is responsible for cleaning up the temporary directory when done.
    """
    tmp_dir = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_file:
        attachment_members = [m for m in zip_file.infolist() if m.filename.startswith('attachments/')]
        for member in attachment_members:
            zip_file.extract(member, tmp_dir)
    return tmp_dir / 'attachments'

def rsmf_load(path: str) -> tuple[dict, dict]:
    """Parse a single RSMF archive and return its head and manifest.

    Args:
        path: Filesystem path to a ``.rsmf`` file.

    Returns:
        A ``(head, manifest)`` tuple where *head* contains archive-level
        metadata (e.g. ``from``, ``date``) and *manifest* holds the
        structured payload (``participants``, ``events``, ``conversations``).

    Raises:
        FileNotFoundError: If *path* does not exist.
        KeyError: If the archive is missing required internal entries.
    """
    parsed = _parse_rsmf(path)
    manifest = _parse_rsmf_manifest(parsed["zip_bytes"])
    return parsed["head"], manifest
