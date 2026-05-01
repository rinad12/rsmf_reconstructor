"""Tests for rsmf_reconstruct.rsmf_parser."""

import io
import json
import zipfile
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytest

from rsmf_reconstruct.rsmf_parser import (
    extract_rsmf_files,
    parse_rsmf,
    _parse_rsmf_manifest as parse_rsmf_manifest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_rsmf_file(tmp_path: Path, from_: str, to: str, text: str, zip_bytes: bytes) -> Path:
    msg = MIMEMultipart()
    if from_:
        msg["From"] = from_
    if to:
        msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEApplication(zip_bytes, "zip"))

    path = tmp_path / "sample.rsmf"
    path.write_bytes(msg.as_bytes())
    return path


# ---------------------------------------------------------------------------
# parse_rsmf
# ---------------------------------------------------------------------------

class TestParseRsmf:
    def test_returns_head_text_zip(self, tmp_path):
        zip_bytes = _make_zip({"rsmf_manifest.json": b"{}"})
        path = _make_rsmf_file(tmp_path, "alice@example.com", "bob@example.com", "Hello", zip_bytes)
        result = parse_rsmf(path)
        assert result["head"]["from"] == "alice@example.com"
        assert result["head"]["to"] == "bob@example.com"
        assert "Hello" in result["text"]
        assert result["zip_bytes"] == zip_bytes

    def test_accepts_string_path(self, tmp_path):
        zip_bytes = _make_zip({"x": b"data"})
        path = _make_rsmf_file(tmp_path, "a@a.com", "b@b.com", "body", zip_bytes)
        result = parse_rsmf(str(path))
        assert result["head"]["from"] == "a@a.com"

    def test_missing_from_header_returns_empty(self, tmp_path):
        zip_bytes = _make_zip({"x": b"data"})
        path = _make_rsmf_file(tmp_path, "", "", "body", zip_bytes)
        result = parse_rsmf(path)
        assert result["head"]["from"] == ""

    def test_zip_bytes_is_bytes(self, tmp_path):
        zip_bytes = _make_zip({"f": b"content"})
        path = _make_rsmf_file(tmp_path, "a@a.com", "b@b.com", "t", zip_bytes)
        result = parse_rsmf(path)
        assert isinstance(result["zip_bytes"], bytes)


# ---------------------------------------------------------------------------
# parse_rsmf_manifest
# ---------------------------------------------------------------------------

class TestParseRsmfManifest:
    def test_parses_manifest_json(self):
        manifest = {"version": "1.0", "participants": [{"id": "u0", "display": "Alice"}]}
        zip_bytes = _make_zip({"rsmf_manifest.json": json.dumps(manifest).encode()})
        result = parse_rsmf_manifest(zip_bytes)
        assert result["version"] == "1.0"
        assert result["participants"][0]["display"] == "Alice"

    def test_raises_on_missing_manifest(self):
        zip_bytes = _make_zip({"other.txt": b"data"})
        with pytest.raises(KeyError):
            parse_rsmf_manifest(zip_bytes)

    def test_raises_on_invalid_json(self):
        zip_bytes = _make_zip({"rsmf_manifest.json": b"not-json"})
        with pytest.raises(Exception):
            parse_rsmf_manifest(zip_bytes)


# ---------------------------------------------------------------------------
# extract_rsmf_files
# ---------------------------------------------------------------------------

class TestExtractRsmfFiles:
    def test_extracts_only_attachments(self):
        zip_bytes = _make_zip({
            "attachments/file.txt": b"hello",
            "rsmf_manifest.json": b"{}",
        })
        attachments_dir = extract_rsmf_files(zip_bytes)
        assert (attachments_dir / "file.txt").read_bytes() == b"hello"
        assert not (attachments_dir.parent / "rsmf_manifest.json").exists()

    def test_returns_path_object(self):
        zip_bytes = _make_zip({"attachments/a.bin": b"\x00"})
        result = extract_rsmf_files(zip_bytes)
        assert isinstance(result, Path)

    def test_empty_attachments_folder_gives_empty_dir(self):
        zip_bytes = _make_zip({"rsmf_manifest.json": b"{}"})
        attachments_dir = extract_rsmf_files(zip_bytes)
        assert not attachments_dir.exists() or not any(attachments_dir.iterdir())

    def test_nested_attachment_preserved(self):
        zip_bytes = _make_zip({"attachments/sub/img.png": b"\x89PNG"})
        attachments_dir = extract_rsmf_files(zip_bytes)
        assert (attachments_dir / "sub" / "img.png").read_bytes() == b"\x89PNG"
