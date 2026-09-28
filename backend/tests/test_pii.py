"""Phase 25: pulse/pii.py's pure primitives, used both by the worker's live
enforcement (pulse/worker/processing.py, already covered by
tests/test_pii_enforcement.py) and by the archive rewrite a new PII rule
triggers (pulse/archive.py, tests/test_pii_archive_rewrite.py). No DB, no
Docker -- these are the low-level functions both of those build on."""

from pulse.models import PiiAction
from pulse.pii import apply_to_archived_properties, hash_value, stringify_properties


def test_stringify_matches_the_map_string_string_convention() -> None:
    assert stringify_properties({"a": 1, "b": 2.5, "c": True, "d": False, "e": "x", "f": None}) == {
        "a": "1",
        "b": "2.5",
        "c": "true",
        "d": "false",
        "e": "x",
    }


def test_hash_is_deterministic_and_secret_scoped() -> None:
    first = hash_value("alice@example.com", "secret-a")
    second = hash_value("alice@example.com", "secret-a")
    third = hash_value("alice@example.com", "secret-b")
    assert first == second
    assert first != third


def test_no_rules_leaves_properties_unchanged() -> None:
    properties = {"email": "a@example.com", "plan": "pro"}
    result, changed = apply_to_archived_properties(properties, {}, "secret")
    assert result == properties and changed is False


def test_a_rule_for_an_absent_key_changes_nothing() -> None:
    properties = {"plan": "pro"}
    result, changed = apply_to_archived_properties(properties, {"ssn": PiiAction.DROP}, "secret")
    assert result == properties and changed is False


def test_drop_removes_the_key() -> None:
    result, changed = apply_to_archived_properties(
        {"email": "a@example.com", "plan": "pro"}, {"email": PiiAction.DROP}, "secret"
    )
    assert result == {"plan": "pro"}
    assert changed is True


def test_hash_replaces_the_value_deterministically() -> None:
    result, changed = apply_to_archived_properties(
        {"email": "a@example.com"}, {"email": PiiAction.HASH}, "secret"
    )
    assert changed is True
    assert result["email"] != "a@example.com"
    assert result["email"] == hash_value("a@example.com", "secret")


def test_hash_of_a_non_string_value_stringifies_first() -> None:
    """A raw archived property can be any JSON type (int, bool) -- the hash
    must be of the SAME stringified form the worker hashes at ingest, so a
    value looked up in ClickHouse and in the archive match."""
    result, changed = apply_to_archived_properties({"age": 42}, {"age": PiiAction.HASH}, "secret")
    assert changed is True
    assert result["age"] == hash_value("42", "secret")

    result, changed = apply_to_archived_properties(
        {"active": True}, {"active": PiiAction.HASH}, "secret"
    )
    assert result["active"] == hash_value("true", "secret")


def test_hashing_a_none_value_is_left_untouched() -> None:
    """A None property was never in ClickHouse's Map(String,String) either
    (stringify_properties drops it), so there is nothing there to hash."""
    result, changed = apply_to_archived_properties(
        {"email": None}, {"email": PiiAction.HASH}, "secret"
    )
    assert result == {"email": None}
    assert changed is False


def test_the_input_dict_is_never_mutated() -> None:
    original = {"email": "a@example.com"}
    snapshot = dict(original)
    apply_to_archived_properties(original, {"email": PiiAction.DROP}, "secret")
    assert original == snapshot


def test_unmarked_properties_are_left_alone() -> None:
    result, changed = apply_to_archived_properties(
        {"email": "a@example.com", "plan": "pro"}, {"email": PiiAction.DROP}, "secret"
    )
    assert result["plan"] == "pro"
    assert changed is True
