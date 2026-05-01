
"""Forensic stress-test suite for hash_join.py.

Each test class maps to one risk category and asserts the correct,
production-grade behaviour expected of the reconciliation logic.
"""

import hashlib
import time
import unicodedata

from rsmf_reconstruct.hash_join import (
    _get_sender,
    build_participants_map,
    get_message_key,
    normalize_message,
    reconcile_conversations,
)

PMAP = {"u1": "Alice", "u2": "Bob"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _msg(id_=None, ts="2024-01-01T12:00:00.000Z", sender="u1", body="Hello", **kw):
    m = {"timestamp": ts, "participant": sender, "body": body}
    if id_ is not None:
        m["id"] = id_
    m.update(kw)
    return m


def _reconcile(a_msgs, b_msgs, pmap=None):
    return reconcile_conversations(a_msgs, b_msgs, "Alice", "Bob", pmap if pmap is not None else PMAP)


def _deleted_flags(result):
    return [e["deleted"] for e in result]


# ---------------------------------------------------------------------------
# 1. Clock Drift & Precision
# ---------------------------------------------------------------------------

class TestClockDrift:

    def test_exact_millisecond_match_is_verified(self):
        a = _msg(ts="2024-01-01T12:00:01.000Z", body="Same")
        b = _msg(ts="2024-01-01T12:00:01.000Z", body="Same")
        result = _reconcile([a], [b])
        assert len(result) == 1
        assert result[0]["deleted"] == False

    def test_5ms_clock_drift_resolves_to_single_verified_entry(self):
        a = _msg(ts="2024-01-01T12:00:01.000Z", body="Meeting at 3")
        b = _msg(ts="2024-01-01T12:00:01.005Z", body="Meeting at 3")
        result = _reconcile([a], [b])
        assert len(result) == 1, (
            f"Expected 1 entry for clock-drifted messages, got {len(result)}. "
            "Both entries are incorrectly marked 'Deleted by …'."
        )
        assert result[0]["deleted"] == False

    def test_one_second_difference_stays_distinct(self):
        """A genuine 1-second gap between identical bodies is correctly two messages."""
        a = _msg(ts="2024-01-01T12:00:01Z", body="Hi")
        b = _msg(ts="2024-01-01T12:00:02Z", body="Hi")
        result = _reconcile([a], [b])
        assert len(result) == 2
        assert all(_deleted_flags(result))


# ---------------------------------------------------------------------------
# 2. Unicode Normalization
# ---------------------------------------------------------------------------

class TestUnicodeNormalization:
    """NFC normalisation ensures the same text in different Unicode forms hashes identically.

    Hebrew and Arabic base characters have no precomposed NFC forms, so they are
    unaffected.  The real risk is Latin diacritics (e.g. "café", "Zürich") or
    Romanised display names where different client apps may produce NFC vs NFD.
    """

    # Latin text: precomposed NFC (é) vs decomposed NFD (e + combining acute)
    NFC_TEXT = "café"        # café  — one codepoint for é
    NFD_TEXT = "café"       # café  — e + U+0301 COMBINING ACUTE ACCENT

    # Hebrew base chars are already maximally decomposed; there are no precomposed
    # NFC forms, so NFC("שלום") == NFD("שלום").  We verify that assumption here
    # so the test suite stays honest about what the real risk vector is.
    def test_hebrew_base_chars_have_no_nfc_nfd_difference(self):
        """Confirm Hebrew letters alone are unaffected — so the suite is honest."""
        hebrew = "שלום"
        assert unicodedata.normalize("NFC", hebrew) == unicodedata.normalize("NFD", hebrew)

    def test_nfc_nfd_are_visually_identical(self):
        assert unicodedata.normalize("NFC", self.NFC_TEXT) == unicodedata.normalize("NFC", self.NFD_TEXT)
        assert self.NFC_TEXT != self.NFD_TEXT  # but byte-level they differ

    def test_nfc_nfd_body_produce_same_fingerprint(self):
        key_nfc = get_message_key(_msg(body=self.NFC_TEXT), PMAP)
        key_nfd = get_message_key(_msg(body=self.NFD_TEXT), PMAP)
        assert key_nfc == key_nfd

    def test_nfc_nfd_body_reconcile_as_one_verified_entry(self):
        a = _msg(body=self.NFC_TEXT)
        b = _msg(body=self.NFD_TEXT)
        result = _reconcile([a], [b])
        assert len(result) == 1
        assert result[0]["deleted"] == False

    def test_sender_display_name_nfc_nfd_fingerprint_stable(self):
        # Romanised Hebrew name containing a diacritic ("Yoav Gölan")
        name_nfc = "Yoav Gölan"    # ö as single codepoint
        name_nfd = "Yoav Gölan"   # o + combining diaeresis
        pmap_nfc = {"u1": name_nfc, "u2": "Bob"}
        pmap_nfd = {"u1": name_nfd, "u2": "Bob"}
        msg = _msg(body="Test")
        assert get_message_key(msg, pmap_nfc) == get_message_key(msg, pmap_nfd)


# ---------------------------------------------------------------------------
# 3. Identical Message Collisions (Duplicate Suppression)
# ---------------------------------------------------------------------------

class TestIdenticalMessageCollisions:

    def test_two_identical_messages_same_millisecond_both_survive(self):
        msg1 = _msg(ts="2024-01-01T12:00:00.000Z", body="👍")
        msg2 = _msg(ts="2024-01-01T12:00:00.000Z", body="👍")
        result = _reconcile([msg1, msg2], [msg1, msg2])
        assert len(result) == 2, (
            f"Expected 2 entries for two distinct identical messages; got {len(result)}. "
            "One message was silently dropped — DATA LOSS."
        )
        assert all(e["deleted"] == False for e in result)

    def test_different_bodies_same_timestamp_are_distinct(self):
        a = _msg(ts="2024-01-01T12:00:00.000Z", body="yes")
        b = _msg(ts="2024-01-01T12:00:00.000Z", body="no")
        result = _reconcile([a, b], [a, b])
        assert len(result) == 2
        assert all(e["deleted"] == False for e in result)

    def test_different_senders_same_body_timestamp_are_distinct(self):
        a = _msg(ts="2024-01-01T12:00:00.000Z", body="ok", sender="u1")
        b = _msg(ts="2024-01-01T12:00:00.000Z", body="ok", sender="u2")
        result = _reconcile([a, b], [a, b])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# 4. ID Ambiguity
# ---------------------------------------------------------------------------

class TestIDAmbiguity:

    def test_id_vs_null_same_physical_message_merges(self):
        msg_with_id = {
            "id": "42",
            "timestamp": "2024-01-01T12:00:00Z",
            "participant": "u1",
            "body": "Hello",
        }
        msg_without_id = {
            "timestamp": "2024-01-01T12:00:00Z",
            "participant": "u1",
            "body": "Hello",
        }
        result = _reconcile([msg_with_id], [msg_without_id])
        assert len(result) == 1
        assert result[0]["deleted"] == False

    def test_null_id_field_falls_back_to_md5(self):
        msg = {"id": None, "timestamp": "2024-01-01T12:00:00Z", "participant": "u1", "body": "x"}
        key = get_message_key(msg, PMAP)
        assert len(key) == 32

    def test_empty_string_id_falls_back_to_md5(self):
        msg = {"id": "", "timestamp": "2024-01-01T12:00:00Z", "participant": "u1", "body": "x"}
        key = get_message_key(msg, PMAP)
        assert len(key) == 32

    def test_matching_native_ids_verify(self):
        a = {"id": "99", "body": "Hi", "participant": "u1"}
        b = {"id": "99", "body": "Hi", "participant": "u1"}
        result = _reconcile([a], [b])
        assert len(result) == 1
        assert result[0]["deleted"] == False

    def test_native_id_takes_precedence_over_fingerprint(self):
        """Native id wins even when body would hash to the same MD5."""
        msg = {"id": "native", "timestamp": "2024-01-01T12:00:00Z", "participant": "u1", "body": "X"}
        assert get_message_key(msg, PMAP) == "native"


# ---------------------------------------------------------------------------
# 5. Missing or Corrupt Fields
# ---------------------------------------------------------------------------

class TestMissingOrCorruptFields:

    def test_body_null_and_body_absent_fingerprint_identical(self):
        ts = "2024-01-01T12:00:00Z"
        msg_null   = {"timestamp": ts, "participant": "u1", "body": None}
        msg_absent = {"timestamp": ts, "participant": "u1"}
        assert get_message_key(msg_null, PMAP) == get_message_key(msg_absent, PMAP)

    def test_body_null_does_not_crash_reconcile(self):
        msg = {"id": "a1", "timestamp": "2024-01-01T12:00:00Z", "participant": "u1", "body": None}
        result = _reconcile([msg], [msg])
        assert result[0]["deleted"] == False

    def test_missing_timestamp_does_not_crash(self):
        msg = {"id": "a2", "participant": "u1", "body": "No time"}
        result = _reconcile([msg], [msg])
        assert len(result) == 1

    def test_missing_timestamp_fingerprint_uses_empty_string(self):
        msg = {"participant": "u1", "body": "No ts"}
        key = get_message_key(msg, PMAP)
        expected = hashlib.md5("|Alice|No ts".encode("utf-8")).hexdigest()
        assert key == expected

    def test_unknown_participant_id_falls_back_to_raw(self):
        msg = {"timestamp": "2024-01-01T12:00:00Z", "participant": "ghost_user", "body": "Hi"}
        assert _get_sender(msg, {}) == "ghost_user"

    def test_unknown_participant_in_reconcile_does_not_crash(self):
        msg = {"id": "x", "participant": "ghost_user", "body": "Boo"}
        result = _reconcile([msg], [msg], pmap={})
        assert result[0]["deleted"] == False

    def test_no_participant_field_returns_empty_sender(self):
        assert _get_sender({"timestamp": "T", "body": "Orphan"}, PMAP) == ""

    def test_custom_from_field_overrides_participant(self):
        msg = {
            "participant": "u2",
            "body": "Hi",
            "custom": [{"name": "From", "value": "ExternalAlice"}],
        }
        assert _get_sender(msg, PMAP) == "ExternalAlice"

    def test_attachment_only_event_reconciles_by_id(self):
        """Attachment-only messages (body=null) keyed by native id still reconcile correctly."""
        att = {"id": "att1", "participant": "u1", "body": None,
               "type": "attachment", "timestamp": "2024-01-01T12:00:00Z"}
        result = _reconcile([att], [att])
        assert result[0]["deleted"] == False

    def test_participant_display_empty_falls_back_to_id(self):
        pmap = {"u1": ""}
        msg = {"participant": "u1", "body": "Hi"}
        assert _get_sender(msg, pmap) == "u1"


# ---------------------------------------------------------------------------
# 6. Sorting Stability
# ---------------------------------------------------------------------------

class TestSortingStability:

    def test_output_is_chronologically_sorted(self):
        early = _msg(ts="2024-01-01T09:00:00Z", body="Morning", id_="1")
        late  = _msg(ts="2024-01-01T17:00:00Z", body="Evening", id_="2")
        result = _reconcile([late, early], [early, late])
        timestamps = [e.get("timestamp", "") for e in result]
        assert timestamps == sorted(timestamps), (
            f"Output timestamps {timestamps} are not in ascending order."
        )

    def test_equal_timestamp_tie_breaker_is_deterministic(self):
        alice_msg = _msg(ts="2024-01-01T12:00:00Z", body="A says", id_="a1", sender="u1")
        bob_msg   = _msg(ts="2024-01-01T12:00:00Z", body="B says", id_="b1", sender="u2")
        order1 = _reconcile([alice_msg, bob_msg], [alice_msg, bob_msg])
        order2 = _reconcile([bob_msg, alice_msg], [bob_msg, alice_msg])
        assert [e["body"] for e in order1] == [e["body"] for e in order2]

    def test_five_same_timestamp_messages_all_present(self):
        msgs = [_msg(ts="2024-01-01T12:00:00Z", body=f"msg{i}", id_=str(i)) for i in range(5)]
        result = _reconcile(msgs, msgs)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# 7. Scale and Performance
# ---------------------------------------------------------------------------

class TestScaleAndPerformance:

    N = 10_000

    def _shared_msgs(self, n):
        return [
            {
                "id": f"shared-{i}",
                "timestamp": (
                    f"2024-01-{(i // 86400) % 28 + 1:02d}T"
                    f"{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"
                ),
                "participant": "u1" if i % 2 == 0 else "u2",
                "body": f"Message {i}",
            }
            for i in range(n)
        ]

    def test_10k_shared_messages_under_2_seconds(self):
        msgs = self._shared_msgs(self.N)
        start = time.perf_counter()
        result = _reconcile(msgs, msgs)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"10k messages took {elapsed:.3f}s — exceeds 2s budget"
        assert len(result) == self.N
        assert all(e["deleted"] == False for e in result)

    def test_10k_disjoint_messages_correct_statuses(self):
        a_msgs = [{"id": f"a-{i}", "participant": "u1", "body": f"a{i}"} for i in range(self.N)]
        b_msgs = [{"id": f"b-{i}", "participant": "u2", "body": f"b{i}"} for i in range(self.N)]
        result = _reconcile(a_msgs, b_msgs)
        assert len(result) == 2 * self.N
        assert all(e["deleted"] == True for e in result)

    def test_5k_fingerprint_only_messages_under_2_seconds(self):
        """Performance of the MD5 path (no native id) at scale."""
        msgs = [
            {
                "timestamp": f"2024-01-{(i % 28) + 1:02d}T12:{(i // 60) % 60:02d}:{i % 60:02d}Z",
                "participant": "u1",
                "body": f"body-{i}",
            }
            for i in range(5_000)
        ]
        start = time.perf_counter()
        result = _reconcile(msgs, msgs)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0
        assert len(result) == 5_000

    def test_no_cross_contamination_of_custom_fields(self):
        """Mutations to a result entry's custom list must not affect the original message."""
        original = {"id": "42", "body": "Shared", "participant": "u1", "custom": []}
        _reconcile([original], [original])
        assert original["custom"] == [], "Original message's custom list was mutated"

    def test_verified_entry_has_no_deleted_by_annotation(self):
        msg = {"id": "1", "body": "Both have this", "participant": "u1"}
        result = _reconcile([msg], [msg])
        assert not any(c.get("name") == "Deleted by" for c in result[0]["custom"])


# ---------------------------------------------------------------------------
# 8. Fingerprint Strategy Analysis
# ---------------------------------------------------------------------------

class TestFingerprintStrategySufficiency:
    """Probe whether timestamp|sender|body is sufficient for forensic standards.

    Tests in this class deliberately assert the *current broken behaviour* to
    document known fingerprint gaps.  They pass today precisely because the gap
    exists; they serve as regression sentinels and as a specification for
    enhancements.
    """

    def test_message_type_not_in_fingerprint(self):
        """A 'message' and a 'reaction' with the same body/ts/sender are indistinguishable.

        RECOMMENDATION: Include the ``type`` field in the composite:
        ``f"{ts}|{sender}|{body}|{msg.get('type', '')}"``
        """
        msg      = _msg(body="👍")
        reaction = {**_msg(body="👍"), "type": "reaction"}
        assert get_message_key(msg, PMAP) == get_message_key(reaction, PMAP), (
            "Fingerprint gap confirmed: message type is not part of the hash. "
            "A message and a reaction with the same body/ts/sender collide."
        )

    def test_attachment_filename_not_in_fingerprint(self):
        """Two different attachment-only events with null body share the same key.

        RECOMMENDATION: Include attachment filenames or content hashes in the composite:
        ``filenames = '|'.join(a.get('filename','') for a in msg.get('attachments',[]))``
        ``f"{ts}|{sender}|{body}|{filenames}"``
        """
        att1 = {"timestamp": "2024-01-01T12:00:00Z", "participant": "u1",
                "body": None, "attachments": [{"filename": "contract.pdf"}]}
        att2 = {"timestamp": "2024-01-01T12:00:00Z", "participant": "u1",
                "body": None, "attachments": [{"filename": "invoice.pdf"}]}
        assert get_message_key(att1, PMAP) == get_message_key(att2, PMAP), (
            "Fingerprint gap confirmed: attachment metadata is not included in the hash. "
            "Two different attachments sent at the same millisecond collide."
        )

    def test_deleted_by_annotation_removed_on_verify(self):
        msg = {"id": "10", "body": "In both", "participant": "u1"}
        result = _reconcile([msg], [msg])
        assert result[0]["deleted"] == False
        assert not any(c.get("name") == "Deleted by" for c in result[0]["custom"])

    def test_deleted_by_annotation_present_for_a_only_message(self):
        msg = {"id": "X", "body": "Only in A", "participant": "u1"}
        result = _reconcile([msg], [])
        entry = result[0]
        assert entry["deleted"] == True
        assert any(c["name"] == "Deleted by" for c in entry["custom"])

    def test_build_participants_map_missing_display_stores_empty(self):
        pmap = build_participants_map([{"id": "u1"}, {"id": "u2", "display": ""}])
        assert pmap == {"u1": "", "u2": ""}

    def test_build_participants_map_hebrew_display_preserved(self):
        pmap = build_participants_map([{"id": "u1", "display": "יואב גולן"}])
        assert pmap["u1"] == "יואב גולן"

    def test_normalize_message_isolates_custom_list(self):
        original = {"body": "Hi", "custom": [{"name": "Source", "value": "WhatsApp"}]}
        copy = normalize_message(original)
        copy["custom"].append({"name": "Injected", "value": "x"})
        assert len(original["custom"]) == 1

    def test_normalize_message_absent_custom_defaults_to_empty_list(self):
        copy = normalize_message({"body": "Hi"})
        assert copy["custom"] == []

    def test_empty_exports_return_empty_list(self):
        assert _reconcile([], []) == []

    def test_a_only_all_marked_deleted_by_bob(self):
        msgs = [{"id": str(i), "body": f"a{i}", "participant": "u1"} for i in range(5)]
        result = _reconcile(msgs, [])
        assert all(e["deleted"] == True for e in result)

    def test_b_only_all_marked_deleted_by_alice(self):
        msgs = [{"id": str(i), "body": f"b{i}", "participant": "u2"} for i in range(5)]
        result = _reconcile([], msgs)
        assert all(e["deleted"] == True for e in result)
