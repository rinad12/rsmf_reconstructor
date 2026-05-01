"""CLI entry point for RSMF reconciliation.

Usage:
    python -m rsmf_reconstruct file_a.rsmf file_b.rsmf
    rsmf-reconstruct file_a.rsmf file_b.rsmf      # after pip install

Prints a JSON array of reconciled messages to stdout.
"""

import json
import sys

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

        rsmf-reconstruct <file_a.rsmf> <file_b.rsmf>

    Args:
        None — arguments are read from :data:`sys.argv`.

    Returns:
        None

    Side effects:
        * Writes ``reconciled_messages.json`` to the working directory.
        * Prints a usage message to *stderr* and exits with code 1 if the
          wrong number of arguments is supplied.

    Output schema:

    .. code-block:: json

        {
            "version": "2.0.0",
            "participants": [...],
            "conversations": [...],
            "events": [...]
        }
    """
    if len(sys.argv) != 3:
        print("Usage: rsmf-reconstruct <file_a.rsmf> <file_b.rsmf>", file=sys.stderr)
        sys.exit(1)

    path_a, path_b = sys.argv[1], sys.argv[2]

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

    with open("reconciled_messages.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
