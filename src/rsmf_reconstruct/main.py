"""CLI entry point for RSMF reconciliation.

Usage:
    python -m rsmf_reconstruct file_a.rsmf file_b.rsmf
    rsmf-reconstruct file_a.rsmf file_b.rsmf      # after pip install

Prints a JSON array of reconciled messages to stdout.
"""

import json
import sys

from rsmf_reconstruct.hash_join import build_participants_map, reconcile_conversations
from rsmf_reconstruct.rsfm_parser import parse_rsmf, parse_rsmf_manifest


def _load(path: str) -> tuple[dict, dict]:
    """Return (head, manifest) for one RSMF file."""
    parsed = parse_rsmf(path)
    manifest = parse_rsmf_manifest(parsed["zip_bytes"])
    return parsed["head"], manifest


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: rsmf-reconstruct <file_a.rsmf> <file_b.rsmf>", file=sys.stderr)
        sys.exit(1)

    path_a, path_b = sys.argv[1], sys.argv[2]

    head_a, manifest_a = _load(path_a)
    head_b, manifest_b = _load(path_b)

    # Merge participants from both manifests so no sender is unknown
    all_participants = {p["id"]: p for p in manifest_b.get("participants", [])}
    all_participants.update({p["id"]: p for p in manifest_a.get("participants", [])})
    participants_map = build_participants_map(list(all_participants.values()))

    user_a_name = head_a.get("from") or path_a
    user_b_name = head_b.get("from") or path_b

    timeline = reconcile_conversations(
        user_a_msgs=manifest_a.get("events", []),
        user_b_msgs=manifest_b.get("events", []),
        user_a_name=user_a_name,
        user_b_name=user_b_name,
        participants_map=participants_map,
    )

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
