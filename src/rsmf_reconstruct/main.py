"""CLI entry point for RSMF reconciliation and export.

Usage:
    uv run -m rsmf_reconstruct file_a.rsmf file_b.rsmf
    rsmf-reconstruct file_a.rsmf file_b.rsmf [-o output.rsmf]  # after pip install
"""

import argparse
import email.utils
import logging
import os
import sys
from pathlib import Path

from rsmf_reconstruct.exporter_rsmf import export_to_rsmf
from rsmf_reconstruct.hash_join import build_participants_map, reconcile_conversations
from rsmf_reconstruct.rsmf_parser import RsmfParseError, extract_rsmf_files, rsmf_load
from rsmf_reconstruct.rsmf_parser import parse_rsmf
from rsmf_reconstruct.export_pdf import generate_pdf_report

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("rsmf_reconstruct.log", encoding="utf-8")],
)
logger = logging.getLogger(__name__)

_RUNNING_AS_EXE = getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")


def resource_path(relative_path: str) -> str:
    """Return absolute path to a bundled resource, supporting PyInstaller and dev mode."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


def _exit_with_error(message: str) -> None:
    """Print a user-friendly error message and exit, pausing if running as an EXE."""
    print(f"\nError: {message}\n")
    logger.error(message)
    if _RUNNING_AS_EXE:
        input("Press Enter to exit...")
    sys.exit(1)


def _validate_input_file(path: str) -> None:
    """Raise a user-friendly error if the file is missing or has the wrong extension."""
    p = Path(path)
    if not p.exists():
        _exit_with_error(f"The file '{p.name}' was not found. Please check the path and try again.")
    if p.suffix.lower() != ".rsmf":
        _exit_with_error(
            f"Unsupported file format: '{p.name}'. Please provide a valid .rsmf file."
        )


def _load_rsmf(path: str) -> tuple[dict, dict, dict]:
    """Load and validate a single RSMF file, returning (parsed, head, manifest)."""
    name = Path(path).name
    try:
        parsed = parse_rsmf(path)
    except FileNotFoundError:
        _exit_with_error(f"The file '{name}' was not found.")
    except RsmfParseError as exc:
        logger.debug("Parse error for '%s'", name, exc_info=True)
        _exit_with_error(str(exc))

    try:
        head, manifest = rsmf_load(path)
    except RsmfParseError as exc:
        logger.debug("Manifest error for '%s'", name, exc_info=True)
        _exit_with_error(str(exc))

    if not manifest.get("participants") and not manifest.get("events"):
        _exit_with_error(
            f"Required data (participants/events) is missing from '{name}'. "
            "The file may be empty or exported incorrectly."
        )

    return parsed, head, manifest  # type: ignore[return-value]


def main() -> None:
    """Run the RSMF reconciliation pipeline and write the result as an .rsmf container."""
    parser = argparse.ArgumentParser(
        prog="rsmf-reconstruct",
        description="Reconcile two RSMF exports into a .rsmf container and PDF report.",
    )
    parser.add_argument("file_a", help="First participant's .rsmf export")
    parser.add_argument("file_b", help="Second participant's .rsmf export")
    parser.add_argument(
        "--report-output",
        default="./report_output",
        help="Directory for the PDF report and attachments (default: ./report_output)",
    )
    args = parser.parse_args()

    # == Input validation ==
    _validate_input_file(args.file_a)
    _validate_input_file(args.file_b)

    # == Load & parse both archives ==
    parsed_a, head_a, manifest_a = _load_rsmf(args.file_a)
    parsed_b, head_b, manifest_b = _load_rsmf(args.file_b)

    # == Resolve output directory ==
    base = Path(args.report_output)
    output_dir = str(base) if not base.exists() else next(
        str(base.parent / f"{base.name} ({i})")
        for i in range(1, 10000)
        if not (base.parent / f"{base.name} ({i})").exists()
    )

    # file_b is loaded first so that file_a wins on duplicate participant IDs
    all_participants = {p["id"]: p for p in manifest_b.get("participants", [])}
    all_participants.update({p["id"]: p for p in manifest_a.get("participants", [])})
    participants_map = build_participants_map(list(all_participants.values()))

    # The MIME "From" header identifies the archive owner; fall back to filename.
    user_a_name = email.utils.parseaddr(head_a.get("from", ""))[0] or args.file_a
    user_b_name = email.utils.parseaddr(head_b.get("from", ""))[0] or args.file_b

    try:
        timeline = reconcile_conversations(
            user_a_msgs=manifest_a.get("events", []),
            user_b_msgs=manifest_b.get("events", []),
            user_a_name=user_a_name,
            user_b_name=user_b_name,
            participants_map=participants_map,
        )
    except Exception as exc:
        logger.exception("Unexpected error during reconciliation")
        _exit_with_error(
            f"An unexpected error occurred while reconciling the conversations: {exc}\n"
            "Please check 'rsmf_reconstruct.log' for technical details."
        )

    reconciled_timeline = {
        "version": manifest_a.get("version", "2.0.0"),
        "participants": list(all_participants.values()),
        "conversations": manifest_a.get("conversations", []),
        "events": timeline,
    }

    # Extract attachments from both archives for cross-source recovery.
    try:
        att_dir_a = extract_rsmf_files(parsed_a["zip_bytes"])
        att_dir_b = extract_rsmf_files(parsed_b["zip_bytes"])
    except Exception as exc:
        logger.exception("Failed to extract attachments")
        _exit_with_error(
            f"Could not extract attachments from the RSMF archives: {exc}\n"
            "Please check 'rsmf_reconstruct.log' for technical details."
        )

    # == Export reconciled timeline to .rsmf ==
    rsmf_name = "reconciled"
    try:
        export_to_rsmf(
            reconciled_timeline=reconciled_timeline,
            original_manifest=manifest_a,
            original_attachments_dir=[att_dir_a, att_dir_b],
            output_path=output_dir + f"/{rsmf_name}.rsmf",
        )
    except Exception as exc:
        logger.exception("Failed to export .rsmf")
        _exit_with_error(
            f"Could not write the output .rsmf file: {exc}\n"
            "Please check 'rsmf_reconstruct.log' for technical details."
        )

    # == Generate PDF report (non-fatal if it fails) ==
    template_path = resource_path("generic_template.html")

    if not os.path.exists(template_path):
        print(
            f"Warning: Report template not found at '{template_path}'. "
            "The .rsmf file was saved, but no PDF report was generated.\n"
            f"Output saved to: {output_dir}"
        )
        logger.warning("Template not found: %s", template_path)
        if _RUNNING_AS_EXE:
            input("Press Enter to exit...")
        return

    try:
        generate_pdf_report(
            parsed_a=parsed_a,
            parsed_b=parsed_b,
            manifest_a=manifest_a,
            manifest_b=manifest_b,
            participants_map=participants_map,
            timeline=timeline,
            template_path=template_path,
            output_dir=output_dir,
            output_name=rsmf_name,
        )
        print(f"Report saved to: {output_dir}")
    except Exception as exc:
        logger.exception("PDF generation failed")
        print(
            f"\nWarning: The PDF report could not be generated ({exc}).\n"
            "The .rsmf file was saved successfully.\n"
            f"Output saved to: {output_dir}\n"
            "Check 'rsmf_reconstruct.log' for technical details."
        )

    if _RUNNING_AS_EXE:
        input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
