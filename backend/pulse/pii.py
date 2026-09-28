"""Pure PII-value primitives (stringify, hash), used by both the worker's
live-ingest enforcement (pulse/worker/processing.py::apply_pii_rules, which
enforces rules against ClickHouse and the schema registry) and the one-time
archive rewrite a newly created rule triggers (pulse/archive.py). A leaf
module: it imports nothing from either of those two, so whichever one
imports it can never create a cycle.

`apply_to_archived_properties` is intentionally a SEPARATE function from the
worker's `apply_pii_rules`, not a shared one both call, even though the two
overlap heavily. The worker's `ParsedEvent` carries two properties
representations that could in principle diverge (`properties`, already
stringified for ClickHouse's Map(String,String) column, and
`raw_properties`, the pre-stringification values for the schema registry;
`apply_pii_rules` treats `properties` as the source of truth for "does this
key exist" and is exercised by that exact edge case in
tests/test_pii_enforcement.py). An archived event has only ONE properties
representation -- the customer's original raw JSON -- so there is nothing
to diverge from, and a function shaped for the two-dict case would be the
wrong shape here. Both functions still hash a value the identical way, so a
hash looked up in ClickHouse and in the archive for the same underlying
value match."""

import hashlib
import hmac

from pulse.models import PiiAction

PropertyValue = str | float | bool | None


def stringify_properties(properties: dict[str, PropertyValue]) -> dict[str, str]:
    """Matches Phase 6's fixture convention exactly (events/fixtures.py):
    plain str() for numbers, lowercase true/false for bool (Python's own
    str(True) == "True" would silently break a later `= 'true'`-style
    query), and a property with a null value is dropped -- Map(String,String)
    has no null representation, so an absent key is the natural encoding."""
    result: dict[str, str] = {}
    for key, value in properties.items():
        if value is None:
            continue
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        else:
            result[key] = str(value)
    return result


def hash_value(value: str, secret: str) -> str:
    """Keyed HMAC, not a plain hash: deterministic (the same input always
    hashes the same, so a hashed property still supports unique-user-style
    grouping) but not reversible or rainbow-table-able without the secret."""
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def apply_to_archived_properties(
    properties: dict[str, PropertyValue], rules: dict[str, PiiAction], secret: str
) -> tuple[dict[str, PropertyValue], bool]:
    """Applies `rules` to one archived event's raw properties dict. Returns a
    new dict and whether anything actually changed, so a caller rewriting
    many archive objects can skip writing back the ones that didn't. A value
    that stringifies to nothing (i.e. is itself None) is left untouched:
    Map(String,String) never held it either (`stringify_properties` drops
    such keys), so there is nothing there to hash, and a rule can't act on a
    property that was never really present."""
    changed = False
    result = dict(properties)
    for key, action in rules.items():
        if key not in result:
            continue
        if action == PiiAction.DROP:
            del result[key]
            changed = True
        elif action == PiiAction.HASH:
            stringified = stringify_properties({key: result[key]}).get(key)
            if stringified is not None:
                result[key] = hash_value(stringified, secret)
                changed = True
    return result, changed
