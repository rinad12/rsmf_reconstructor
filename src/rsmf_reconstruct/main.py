"""CLI entry point for RSMF reconciliation.

Usage:
    python -m rsmf_reconstruct file_a.rsmf file_b.rsmf
    rsmf-reconstruct file_a.rsmf file_b.rsmf [-o output.json]  # after pip install
"""

import argparse
import json

from rsmf_reconstruct.hash_join import build_participants_map, reconcile_conversations
from rsmf_reconstruct.rsfm_parser import rsmf_load


def main() -> None:
    """Run the RSMF reconciliation pipeline and write the result to disk.

    Reads two RSMF export files supplied as positional command-line
    arguments, merges their participant rosters, and produces a single
    chronologically-sorted timeline via :func:`reconcile_conversations`.
    The merged output is written to ``reconciled_messages.json`` in the
    current working directory.

    Command-line interface::

        rsmf-reconstruct <file_a.rsmf> <file_b.rsmf> [-o output.json]

    Args:
        None — arguments are read from :data:`sys.argv`.

    Returns:
        None

    Side effects:
        * Writes the output file (default: ``reconciled_messages.json``).
        * Exits with code 1 on argument errors.

    Output schema:

    .. code-block:: json

        {
            "version": "2.0.0",
            "participants": [...],
            "conversations": [...],
            "events": [...]
        }
    """
    parser = argparse.ArgumentParser(
        prog="rsmf-reconstruct",
        description="Reconcile two RSMF message exports into a single timeline.",
    )
    parser.add_argument("file_a", help="First participant's .rsmf export")
    parser.add_argument("file_b", help="Second participant's .rsmf export")
    parser.add_argument(
        "-o", "--output",
        default="reconciled_messages.json",
        help="Output file path (default: reconciled_messages.json)",
    )
    args = parser.parse_args()

    path_a, path_b = args.file_a, args.file_b

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
    output = {
        "version": manifest_a.get("version", "2.0.0"),
        "participants": list(all_participants.values()),
        "conversations": manifest_a.get("conversations", []),
        "events": timeline,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
