import email
import email.header
from email import policy
import io
import json
from pathlib import Path
import tempfile
import zipfile



def parse_rsmf(path: Path | str) -> dict:
    path = Path(path)
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)

    def decode_header_value(val):
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

def parse_rsmf_manifest(zip_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_file:
        manifest = json.loads(zip_file.read('rsmf_manifest.json'))
    return manifest

def extract_rsmf_files(zip_bytes: bytes) -> Path:
    tmp_dir = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_file:
        zip_file.extractall(tmp_dir)
    return tmp_dir
        