import io
import email
import email.header
import zipfile
from email import policy
from pathlib import Path


def parse_rsmf(path: Path) -> dict:
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

    result = {
        'file': path.name,
        'from': decode_header_value(msg.get('From', '')),
        'to': decode_header_value(msg.get('To', '')),
        'parts': [],
    }

    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue

        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)

        if payload is None:
            continue

        if ctype.startswith('text/'):
            content = payload.decode('utf-8', errors='replace')
            result['parts'].append({
                'content_type': ctype,
                'content': content,
                'filename': part.get_filename(),
            })
        elif part.get_filename() == 'rsmf.zip':
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for name in zf.namelist():
                    doc_bytes = zf.read(name)
                    result['parts'].append({
                        'content_type': 'application/octet-stream',
                        'content': doc_bytes,
                        'filename': name,
                    })
        else:
            result['parts'].append({
                'content_type': ctype,
                'content': payload,
                'filename': part.get_filename(),
            })

    return result

def parse_rsfm_manifest(manifest_bytes: bytes) -> dict:
    manifest_str = manifest_bytes.decode('utf-8', errors='replace')
    manifest = {}
    for line in manifest_str.splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            manifest[key.strip()] = value.strip()
    return manifest