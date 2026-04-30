"""Hash-join utilities for reconciling RSMF message exports.

This module provides functions to normalize, fingerprint, and merge message
lists exported independently by two conversation participants.  The core
operation is a hash-join: each message is keyed by its native ``id`` (or by
an MD5 fingerprint when ``id`` is absent), and the two keyed maps are merged
into a single timeline annotated with a deduplication status.

Typical workflow::

    from rsmf_reconstruct.hash_join import build_participants_map, reconcile_conversations

    participants_map = build_participants_map(manifest["participants"])
    timeline = reconcile_conversations(
        user_a_msgs=alice_export["events"],
        user_b_msgs=bob_export["events"],
        user_a_name="Alice",
        user_b_name="Bob",
        participants_map=participants_map,
    )
    for entry in timeline:
        print(entry["deleted"], entry["body"])
"""

import hashlib
import re
import unicodedata


def build_participants_map(participants: list[dict]) -> dict[str, str]:
    """Build a ``{user_id: display_name}`` lookup map from the manifest participants list.

    Entries without a ``"display"`` key, or whose ``"display"`` is empty, are
    stored with an empty string so that downstream code can still fall back to
    the raw ID.

    Args:
        participants: Participant list from the RSMF manifest, e.g.
            ``[{"id": "user_0", "display": "Alice Johnson"}, ...]``.

    Returns:
        A dict mapping each participant's ``"id"`` to its ``"display"`` value
        (or ``""`` when ``"display"`` is absent or empty).

    Examples:
        >>> build_participants_map([{"id": "user_0", "display": "Alice Johnson"}, {"id": "user_1"}])
        {'user_0': 'Alice Johnson', 'user_1': ''}
    """
    return {p["id"]: p.get("display", "") for p in participants}


def _get_sender(msg: dict, participants_map: dict[str, str]) -> str:
    """Resolve the sender display name for a raw RSMF message dict.

    Resolution order:

    1. **Custom field** — search ``msg["custom"]`` for an entry whose
       ``"name"`` equals ``"From"`` and return its ``"value"``.
    2. **Participants map** — look up ``msg["participant"]`` in
       ``participants_map`` and return the display name if it is non-empty.
    3. **Participant field** — return the raw ``msg["participant"]`` ID as a
       last resort (e.g. ``"user_0"``).
    4. **Empty string** — returned when none of the above sources are present.

    Args:
        msg: A raw RSMF message dict.  The ``"participant"`` key (str) is used
            as the lookup key; ``"custom"`` (list of ``{"name": str, "value":
            str}`` dicts) is the fallback source.
        participants_map: Lookup map produced by :func:`build_participants_map`,
            mapping participant IDs to display names.

    Returns:
        The resolved display name string, or ``""`` if nothing could be found.

    Examples:
        >>> pmap = {"user_0": "Alice Johnson", "user_1": "Bob Smith"}
        >>> _get_sender({"participant": "user_0"}, pmap)
        'Alice Johnson'
        >>> _get_sender({"participant": "user_0"}, {"user_0": ""})  # no display → fallback to id
        'user_0'
        >>> _get_sender({"custom": [{"name": "From", "value": "Alice"}]}, {})
        'Alice'
        >>> _get_sender({"participant": "Bob"}, {})
        'Bob'
        >>> _get_sender({}, {})
        ''
    """
    for field in msg.get("custom", []):
        if field.get("name") == "From":
            return field.get("value", "")

    participant_id = msg.get("participant")
    if participant_id and (display := participants_map.get(participant_id)):
        return display

    return participant_id or ""


def normalize_message(msg: dict) -> dict:
    """Return a shallow copy of a raw RSMF message dict, preserving all fields.

    The copy keeps the original structure intact so downstream code can rely on
    all RSMF fields being present.  :func:`reconcile_conversations` appends a
    ``{"name": "Deleted by", "value": "<name>"}`` entry to ``"custom"`` for
    messages that appear in only one export — mutating the copy, not the input.

    Args:
        msg: A raw RSMF message dict.

    Returns:
        A shallow copy of ``msg`` with ``"custom"`` replaced by a new list so
        that mutations to ``"custom"`` do not affect the original.

    Examples:
        >>> msg = {"id": "41", "type": "message", "body": "Hi", "custom": [{"name": "Source", "value": "WhatsApp"}]}
        >>> normalize_message(msg) == {**msg, "custom": [{"name": "Source", "value": "WhatsApp"}]}
        True
    """
    return {**msg, "custom": list(msg.get("custom", []))}


def _compute_fingerprint(msg: dict, participants_map: dict[str, str]) -> str:
    """Always compute the MD5 fingerprint, ignoring any native ``id``.

    Normalises three sources of instability before hashing:

    - **Sub-second clock drift** — milliseconds are stripped so that
      ``12:00:01.000Z`` and ``12:00:01.005Z`` produce the same key.
    - **Unicode form** — body and sender are NFC-normalised so that the same
      text in NFC and NFD form (e.g. ``"café"`` vs ``"cafe\\u0301"``) hashes
      identically regardless of which client generated the export.
    - **Null body** — ``None`` and absent ``"body"`` both collapse to ``""``.

    This function is also used to build a secondary fingerprint index for
    id-bearing messages, enabling :func:`reconcile_conversations` to resolve
    ID-ambiguity (one export has a native id, the other does not).
    """
    ts = re.sub(r"\.\d+", "", msg.get("timestamp", ""))
    sender = unicodedata.normalize("NFC", _get_sender(msg, participants_map))
    body = unicodedata.normalize("NFC", msg.get("body") or "")
    composite = f"{ts}|{sender}|{body}"
    return hashlib.md5(composite.encode("utf-8")).hexdigest()


def get_message_key(msg: dict, participants_map: dict[str, str]) -> str:
    """Return a stable unique key for a raw RSMF message dict.

    Prefers the native ``"id"`` field.  When ``"id"`` is absent or falsy,
    delegates to :func:`_compute_fingerprint` which produces a normalised MD5
    digest that is stable across sub-second clock drift, Unicode forms, and
    null body values.

    The MD5 hash is used solely for deduplication keying, not for
    cryptographic purposes.

    Args:
        msg: A raw RSMF message dict.
        participants_map: Lookup map produced by :func:`build_participants_map`,
            passed through to :func:`_get_sender` so that the fingerprint uses
            the resolved display name rather than a raw participant ID.

    Returns:
        The native ``id`` string if it is truthy, otherwise a 32-character
        lowercase hex MD5 digest string.

    Examples:
        >>> get_message_key({"id": "abc123"}, {})
        'abc123'
        >>> key = get_message_key({"timestamp": "2024-01-01T00:00:00Z", "participant": "u0", "body": "Hi"}, {"u0": "Alice"})
        >>> len(key)
        32
    """
    if msg.get("id"):
        return msg["id"]
    return _compute_fingerprint(msg, participants_map)


def reconcile_conversations(
    user_a_msgs: list,
    user_b_msgs: list,
    user_a_name: str,
    user_b_name: str,
    participants_map: dict[str, str],
) -> list:
    """
    Reconcile and deduplicate messages from two separate RSMF exports.
    
    This function performs a full outer join. It accurately identifies which participant's
    export is missing a record and marks it as 'Deleted by' that specific participant.
    """
    
    def _make_entry(msg: dict, deleted_by_name: str) -> dict:
        """Helper to create a timeline entry marked as deleted by a specific user export."""
        data = normalize_message(msg)
        data["deleted"] = True
        # Clear any existing 'Deleted by' fields before adding the correct one
        data["custom"] = [f for f in data["custom"] if f.get("name") != "Deleted by"]
        data["custom"].append({"name": "Deleted by", "value": deleted_by_name})
        return data

    def _verify(data: dict) -> None:
        """Mark a message as verified (present in both exports)."""
        data["deleted"] = False
        data["custom"] = [f for f in data["custom"] if f.get("name") != "Deleted by"]

    global_timeline: dict[str, dict] = {}
    
    # Indices for cross-referencing IDs and Fingerprints (FPs)
    fp_to_keys_a: dict[str, list[str]] = {}
    fp_to_noid_a_keys: dict[str, list[str]] = {}
    seen_a: dict[str, int] = {}

    # 1. Map all messages from User A's export
    for msg in user_a_msgs:
        base_key = get_message_key(msg, participants_map)
        n = seen_a.get(base_key, 0)
        seen_a[base_key] = n + 1
        key = base_key if n == 0 else f"{base_key}#{n}"
        
        # Initial state: found in A, assume missing in B until proven otherwise
        global_timeline[key] = _make_entry(msg, user_b_name)
        
        # Secondary indexing for ID-bridging
        fp = _compute_fingerprint(msg, participants_map)
        if msg.get("id"):
            fp_to_keys_a.setdefault(fp, []).append(key)
        else:
            fp_to_noid_a_keys.setdefault(base_key, []).append(key)

    # 2. Reconcile with messages from User B's export
    seen_b: dict[str, int] = {}
    for msg in user_b_msgs:
        base_key = get_message_key(msg, participants_map)
        n = seen_b.get(base_key, 0)
        seen_b[base_key] = n + 1
        key = base_key if n == 0 else f"{base_key}#{n}"

        # Bridge logic: Match message even if ID presence differs between exports
        matched_key = None
        if key in global_timeline:
            matched_key = key
        else:
            # Check if B's message matches an A message via Fingerprint
            if not msg.get("id"):
                candidates = fp_to_keys_a.get(base_key)
                if candidates: matched_key = candidates.pop(0)
            else:
                fp = _compute_fingerprint(msg, participants_map)
                candidates = fp_to_noid_a_keys.get(fp)
                if candidates: matched_key = candidates.pop(0)

        if matched_key:
            # Present in both files
            _verify(global_timeline[matched_key])
        else:
            # Present ONLY in B's file, so it was "Deleted by" User A
            global_timeline[key] = _make_entry(msg, user_a_name)

    # 3. Finalize and sort
    result = list(global_timeline.values())
    result.sort(key=lambda e: (e.get("timestamp", ""), e.get("participant", "")))
    return result 
 
 
 
