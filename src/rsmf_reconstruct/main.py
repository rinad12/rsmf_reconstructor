"""CLI entry point for RSMF reconciliation and export.

Usage:
    uv run -m rsmf_reconstruct file_a.rsmf file_b.rsmf
    rsmf-reconstruct file_a.rsmf file_b.rsmf [-o output.rsmf]  # after pip install
"""

import argparse
import os
import sys
from pathlib import Path

from rsmf_reconstruct.exporter_rsmf import export_to_rsmf
from rsmf_reconstruct.hash_join import build_participants_map, reconcile_conversations
from rsmf_reconstruct.rsmf_parser import extract_rsmf_files, rsmf_load
from rsmf_reconstruct.rsmf_parser import parse_rsmf
from rsmf_reconstruct.export_pdf import generate_pdf_report

def resource_path(relative_path):
    """ Get the absolute path to a resource, supporting both dev mode and PyInstaller. """
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller creates a temporary folder and stores the path in _MEIPASS
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def main() -> None:
    """Run the RSMF reconciliation pipeline and write the result as an .rsmf container.

    Reads two RSMF export files, merges their participant rosters, reconciles
    the message timelines, and packages the result into a valid RSMF 2.0.0
    container via :func:`export_to_rsmf`.

    Command-line interface::

        rsmf-reconstruct <file_a.rsmf> <file_b.rsmf> [-o output.rsmf]

    Side effects:
        * Writes the output .rsmf file (default: ``reconciled.rsmf``).
        * Exits with code 1 on argument errors.
    """
    # == Argument parsing ==
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

    base = Path(args.report_output)
    output_dir = str(base) if not base.exists() else next(
        str(base.parent / f"{base.name} ({i})")
        for i in range(1, 10000)
        if not (base.parent / f"{base.name} ({i})").exists()
    )

    path_a, path_b = args.file_a, args.file_b
    parsed_a = parse_rsmf(path_a)
    parsed_b = parse_rsmf(path_b)

    head_a, manifest_a = rsmf_load(path_a)
    head_b, manifest_b = rsmf_load(path_b)

    # file_b is loaded first so that file_a wins on duplicate participant IDs
    all_participants = {p["id"]: p for p in manifest_b.get("participants", [])}
    all_participants.update({p["id"]: p for p in manifest_a.get("participants", [])})
    participants_map = build_participants_map(list(all_participants.values()))

    # Fall back to the file path when the archive has no "from" header
    user_a_name = head_a.get("from") or path_a
    user_b_name = head_b.get("from") or path_b

    timeline = reconcile_conversations(
        user_a_msgs=manifest_a.get("events", []),
        user_b_msgs=manifest_b.get("events", []),
        user_a_name=user_a_name,
        user_b_name=user_b_name,
        participants_map=participants_map,
    )

    # Use file_a as the source of truth for version and conversation metadata
    reconciled_timeline = {
        "version": manifest_a.get("version", "2.0.0"),
        "participants": list(all_participants.values()),
        "conversations": manifest_a.get("conversations", []),
        "events": timeline,
    }

    # Extract attachments from both files so every referenced file is available,
    # including ones deleted by one participant that only appear in the other's export.
    att_dir_a = extract_rsmf_files(parse_rsmf(path_a)["zip_bytes"])
    att_dir_b = extract_rsmf_files(parse_rsmf(path_b)["zip_bytes"])

    # == Export reconciled timeline to .rsmf ==
    export_to_rsmf(
        reconciled_timeline=reconciled_timeline,
        original_manifest=manifest_a,
        original_attachments_dir=[att_dir_a, att_dir_b],
        output_path=output_dir + "/reconciled.rsmf",
    )

    # == Generate PDF report ==
    template_path = resource_path("generic_template.html")

    if os.path.exists(template_path):
        generate_pdf_report(
            parsed_a=parsed_a,
            parsed_b=parsed_b,
            manifest_a=manifest_a,
            manifest_b=manifest_b,
            participants_map=participants_map,
            timeline=timeline,
            template_path=template_path, # Pass the internal resource path
            output_dir=output_dir,
        )
        print(f"Report saved to: {output_dir}")
    else:
        # This triggers only if the template file was missing during the PyInstaller build process
        print(f"Error: Critical resource {template_path} not found inside the application.")


if __name__ == "__main__":
    main()
