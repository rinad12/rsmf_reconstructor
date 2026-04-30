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
        print(entry["status"], entry["data"]["body"])
"""

import hashlib


def build_participants_map(participants: list[dict]) -> dict[str, str]:
    """Build a ``{user_id: display_name}`` lookup map from the manifest participants list.

    Entries without a ``"display"`` key, or whose ``"display"`` is empty, are
    stored with an empty string so that downstream code can still fall back to
    the raw ID.

    Args:
        participants: Participant list from the RSMF manifest, e.g.
            ``[{"id": "user_0", "display": "יואב גולן"}, ...]``.

    Returns:
        A dict mapping each participant's ``"id"`` to its ``"display"`` value
        (or ``""`` when ``"display"`` is absent or empty).

    Examples:
        >>> build_participants_map([{"id": "user_0", "display": "יואב גולן"}, {"id": "user_1"}])
        {'user_0': 'יואב גולן', 'user_1': ''}
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
        >>> pmap = {"user_0": "יואב גולן", "user_1": "יעל כהן"}
        >>> _get_sender({"participant": "user_0"}, pmap)
        'יואב גולן'
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


def normalize_message(msg: dict, participants_map: dict[str, str]) -> dict:
    """Extract and return the canonical fields from a raw RSMF message dict.

    Reduces an arbitrary RSMF message dict to the six fields used downstream
    by the reconciliation logic, discarding everything else.

    Args:
        msg: A raw RSMF message dict.  Recognised keys are ``"id"``,
            ``"timestamp"``, ``"body"``, ``"attachments"``, ``"edits"``, and
            any keys consumed by :func:`_get_sender`.
        participants_map: Lookup map produced by :func:`build_participants_map`,
            passed through to :func:`_get_sender` for display-name resolution.

    Returns:
        A dict with the following keys:

        - ``"id"`` (any): Native message identifier, or ``None`` if absent.
        - ``"sender"`` (str): Display name resolved by :func:`_get_sender`.
        - ``"timestamp"`` (any): ISO-8601 timestamp string, or ``None``.
        - ``"body"`` (str | None): Plain-text message body.
        - ``"attachments"`` (list): List of attachment dicts (empty by default).
        - ``"edits"`` (list): List of edit-history dicts (empty by default).
        - ``"custom"`` (list): Copy of ``msg["custom"]`` (empty by default).
          :func:`reconcile_conversations` appends a ``{"name": "Deleted by",
          "value": "<name>"}`` entry here for messages absent from one export.

    Examples:
        >>> normalize_message({"id": "1", "participant": "u0", "body": "Hi"}, {"u0": "Alice"})
        {'id': '1', 'sender': 'Alice', 'timestamp': None, 'body': 'Hi', 'attachments': [], 'edits': [], 'custom': []}
    """
    return {
        "id": msg.get("id"),
        "sender": _get_sender(msg, participants_map),
        "timestamp": msg.get("timestamp"),
        "body": msg.get("body"),
        "attachments": msg.get("attachments", []),
        "edits": msg.get("edits", []),
        "custom": list(msg.get("custom", [])),
    }


def get_message_key(msg: dict, participants_map: dict[str, str]) -> str:
    """Return a stable unique key for a raw RSMF message dict.

    Prefers the native ``"id"`` field.  When ``"id"`` is absent or falsy,
    computes an MD5 fingerprint from the concatenation of ``timestamp``,
    sender (resolved via :func:`_get_sender`), and ``body`` to produce a
    deterministic surrogate key.

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

    sender = _get_sender(msg, participants_map)
    composite = f"{msg.get('timestamp', '')}|{sender}|{msg.get('body', '')}"
    return hashlib.md5(composite.encode("utf-8")).hexdigest()


def reconcile_conversations(
    user_a_msgs: list,
    user_b_msgs: list,
    user_a_name: str,
    user_b_name: str,
    participants_map: dict[str, str],
) -> list:
    """Merge two RSMF message lists into a deduplicated, annotated timeline.

    Performs a full outer hash-join on the two message lists keyed by
    :func:`get_message_key`, then annotates each merged entry with a
    deduplication status:

    - ``"Verified in Both"`` — the message key appears in *both* exports,
      meaning neither participant deleted it.
    - ``"Deleted by <name>"`` — the message key appears in *only one* export,
      indicating the other participant deleted it from their side.  The name
      in the status is the person whose export is *missing* the message.

    The result preserves insertion order (Python 3.7+ dict ordering): messages
    from ``user_a_msgs`` appear first, followed by messages exclusive to
    ``user_b_msgs``.

    Args:
        user_a_msgs: Raw message dicts from User A's RSMF export.
        user_b_msgs: Raw message dicts from User B's RSMF export.
        user_a_name: Display name for User A, used in ``"Deleted by"`` labels.
        user_b_name: Display name for User B, used in ``"Deleted by"`` labels.
        participants_map: Lookup map produced by :func:`build_participants_map`,
            forwarded to :func:`get_message_key` and :func:`normalize_message`.

    Returns:
        A list of dicts, each with two keys:

        - ``"data"`` (dict): Normalised message produced by
          :func:`normalize_message`.
        - ``"status"`` (str): One of ``"Verified in Both"``,
          ``"Deleted by <user_a_name>"``, or ``"Deleted by <user_b_name>"``.

    Examples:
        >>> pmap = {"u1": "Alice", "u2": "Bob"}
        >>> shared = {"id": "1", "body": "Hello", "participant": "u1"}
        >>> only_a  = {"id": "2", "body": "Secret", "participant": "u1"}
        >>> only_b  = {"id": "3", "body": "Reply",  "participant": "u2"}
        >>> result = reconcile_conversations([shared, only_a], [shared, only_b], "Alice", "Bob", pmap)
        >>> {e["data"]["id"]: e["status"] for e in result}
        {'1': 'Verified in Both', '2': 'Deleted by Bob', '3': 'Deleted by Alice'}
    """
    def _make_entry(msg: dict, deleted_by: str) -> dict:
        data = normalize_message(msg, participants_map)
        data["custom"].append({"name": "Deleted by", "value": deleted_by})
        return {"data": data, "status": f"Deleted by {deleted_by}"}

    global_timeline = {}

    for msg in user_a_msgs:
        key = get_message_key(msg, participants_map)
        global_timeline[key] = _make_entry(msg, user_b_name)

    for msg in user_b_msgs:
        key = get_message_key(msg, participants_map)
        if key in global_timeline:
            entry = global_timeline[key]
            entry["status"] = "Verified in Both"
            entry["data"]["custom"] = [
                f for f in entry["data"]["custom"] if f.get("name") != "Deleted by"
            ]
        else:
            global_timeline[key] = _make_entry(msg, user_a_name)

    return list(global_timeline.values())


def create_evidence_hashmap(messages: list[dict]) -> dict:
    """Index a list of normalised message dicts by their ``"id"`` field.

    Produces a dictionary for O(1) lookup of a message by its identifier.
    Intended for use after :func:`reconcile_conversations` when callers need
    to cross-reference individual messages by ID.

    Args:
        messages: A list of message dicts.  Each dict **must** contain an
            ``"id"`` key; if two messages share an ID, the last one wins.

    Returns:
        A ``dict`` mapping each message's ``"id"`` value to the full message
        dict.

    Raises:
        KeyError: If any dict in ``messages`` does not contain an ``"id"``
            key.

    Examples:
        >>> msgs = [{"id": "1", "body": "Hi"}, {"id": "2", "body": "Bye"}]
        >>> create_evidence_hashmap(msgs)
        {'1': {'id': '1', 'body': 'Hi'}, '2': {'id': '2', 'body': 'Bye'}}
    """
    return {msg["id"]: msg for msg in messages}
