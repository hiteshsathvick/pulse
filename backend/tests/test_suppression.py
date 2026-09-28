"""Phase 25: pulse/suppression.py. Real Redis, no other store -- every test
uses its own random org/project UUIDs so it never collides with another
test's keys.

DoD: an event for a subject that was already in the Redis ingest stream when
a deletion ran must not land after the deletion. This file covers the
suppress/check primitives directly; tests/test_deletion.py covers the real
end-to-end path (delete_subject -> a racing event -> the real worker drops
it) through process_batch."""

import uuid

from pulse.repositories.redis import get_client as get_redis_client
from pulse.suppression import _key, is_suppressed, suppress


async def test_a_suppressed_user_id_is_reported_suppressed() -> None:
    client = get_redis_client()
    org, project = uuid.uuid4(), uuid.uuid4()

    await suppress(client, org, project, user_id="alice", anonymous_id=None, ttl_seconds=60)

    assert await is_suppressed(client, org, project, "alice", "") is True


async def test_an_unsuppressed_user_id_is_not_suppressed() -> None:
    client = get_redis_client()
    org, project = uuid.uuid4(), uuid.uuid4()

    assert await is_suppressed(client, org, project, "bob", "") is False


async def test_suppression_is_scoped_to_one_project() -> None:
    client = get_redis_client()
    org, project_a, project_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    await suppress(client, org, project_a, user_id="carol", anonymous_id=None, ttl_seconds=60)

    assert await is_suppressed(client, org, project_a, "carol", "") is True
    assert await is_suppressed(client, org, project_b, "carol", "") is False


async def test_suppressing_by_anonymous_id_works_independently_of_user_id() -> None:
    client = get_redis_client()
    org, project = uuid.uuid4(), uuid.uuid4()
    anon = str(uuid.uuid4())

    await suppress(client, org, project, user_id=None, anonymous_id=anon, ttl_seconds=60)

    assert await is_suppressed(client, org, project, "", anon) is True
    assert await is_suppressed(client, org, project, "someone-else", "") is False


async def test_suppressing_both_identifiers_suppresses_either_one_alone() -> None:
    client = get_redis_client()
    org, project = uuid.uuid4(), uuid.uuid4()
    anon = str(uuid.uuid4())

    await suppress(client, org, project, user_id="dana", anonymous_id=anon, ttl_seconds=60)

    assert await is_suppressed(client, org, project, "dana", "") is True
    assert await is_suppressed(client, org, project, "", anon) is True
    # An event carrying neither identifier can't be checked against anything.
    assert await is_suppressed(client, org, project, "", "") is False


async def test_suppressing_neither_identifier_is_a_no_op() -> None:
    client = get_redis_client()
    org, project = uuid.uuid4(), uuid.uuid4()

    await suppress(client, org, project, user_id=None, anonymous_id=None, ttl_seconds=60)

    assert await is_suppressed(client, org, project, "anyone", "anything") is False


async def test_suppression_carries_the_given_ttl() -> None:
    """Bounded, not permanent (pulse/suppression.py's own docstring) -- a key
    with no expiry at all would be the unbounded-growth bug Phase 23 already
    found and fixed for the ingest stream, happening again here."""
    client = get_redis_client()
    org, project = uuid.uuid4(), uuid.uuid4()

    await suppress(client, org, project, user_id="erin", anonymous_id=None, ttl_seconds=120)

    ttl = await client.ttl(_key(org, project, "user", "erin"))
    assert 0 < ttl <= 120
